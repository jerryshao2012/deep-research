#!/bin/bash
# Bi-directional sync between AWS S3 bucket and local ./sync-aws/ folder.
#
# S3 bucket: configured via S3_BUCKET_NAME in env-aws.sh
#
# Local mirror:  ./sync-aws/<folder>/
#
# Usage:
#   ./sync-files-aws.sh                # full bi-directional sync
#   ./sync-files-aws.sh --download     # only download (S3 → local)
#   ./sync-files-aws.sh --upload       # only upload   (local → S3)
#   ./sync-files-aws.sh --verbose      # show detailed output
#   ./sync-files-aws.sh --help

set -uo pipefail

# ── Parse arguments ─────────────────────────────────────────────────
MODE="sync"   # sync | download | upload
VERBOSE=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --download)  MODE="download";  shift ;;
    --upload)    MODE="upload";    shift ;;
    --verbose)   VERBOSE=true;     shift ;;
    --help|-h)
      echo "Usage: ./sync-files-aws.sh [OPTIONS]"
      echo ""
      echo "Bi-directional sync between AWS S3 bucket and local ./sync-aws/ folder."
      echo ""
      echo "Options:"
      echo "  --download   Only download from S3 to local"
      echo "  --upload     Only upload from local to S3"
      echo "  --verbose    Show detailed sync output"
      echo "  --help, -h   Show this help message"
      exit 0
      ;;
    *)
      echo "❌ Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# ── Source environment ──────────────────────────────────────────────
source ./env-aws.sh

SYNC_START_TIME=$(date +%s)

echo "☁️  AWS S3 ↔ Local Bi-directional Sync"
echo "   Mode:   ${MODE}"
echo "   Bucket: s3://${S3_BUCKET_NAME}"
echo ""

# ── Verify AWS CLI authentication ─────────────────────────────────
echo "🔐 Verifying AWS authentication..."
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
if [ -z "$AWS_ACCOUNT_ID" ]; then
  echo "❌ Not authenticated with AWS. Run 'aws configure' first."
  exit 1
fi
echo "✅ AWS Account: $AWS_ACCOUNT_ID"
echo ""

# ── Verify S3 bucket exists ─────────────────────────────────────────
echo "🪣 Checking S3 bucket: $S3_BUCKET_NAME..."
if ! aws s3api head-bucket --bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" 2>/dev/null; then
  echo "❌ S3 bucket '$S3_BUCKET_NAME' not found or not accessible."
  echo "   Run ./deploy-aws.sh first to create it, or create it manually."
  exit 1
fi
echo "✅ Bucket accessible"
echo ""

# ── Local sync root ─────────────────────────────────────────────────
SYNC_ROOT="./sync-aws"
mkdir -p "$SYNC_ROOT"

# ── Top-level folders to scan (subfolders discovered automatically) ──
TOP_FOLDERS=(
  "docs"
  "output"
  "input"
  ".langgraph_api"
)

# ── Counters ────────────────────────────────────────────────────────
TOTAL_DOWNLOADED=0
TOTAL_UPLOADED=0
SKIPPED=0

# ── Helper: discover all subdirectories on S3 for a given prefix ────
# Uses aws s3 ls to find common prefixes (directories).
discover_s3_dirs() {
  local prefix="$1"
  local s3_path="s3://${S3_BUCKET_NAME}/${prefix}/"

  # List common prefixes (subdirectories) at this level
  local subdirs
  subdirs=$(aws s3 ls "$s3_path" --region "$AWS_REGION" 2>/dev/null | \
    grep "PRE " | awk '{print $2}' | sed 's/\/$//') || subdirs=""

  while IFS= read -r d; do
    [ -n "$d" ] || continue
    local child
    if [ -z "$prefix" ]; then
      child="$d"
    else
      child="${prefix}/${d}"
    fi
    echo "$child"
    discover_s3_dirs "$child"
  done <<< "$subdirs"
}

# ── Build full folder list (top-level + all discovered subfolders) ──
echo "🔍 Discovering S3 folder structure..."
SYNC_FOLDERS=()
for top in "${TOP_FOLDERS[@]}"; do
  SYNC_FOLDERS+=("$top")
  while IFS= read -r sub; do
    [ -n "$sub" ] || continue
    SYNC_FOLDERS+=("$sub")
  done < <(discover_s3_dirs "$top")
done
echo "   Found ${#SYNC_FOLDERS[@]} folder(s): ${SYNC_FOLDERS[*]}"
echo ""

# ── Helper: list files in an S3 prefix (one name per line) ─────────
list_s3_files() {
  local folder="$1"
  local s3_path="s3://${S3_BUCKET_NAME}/${folder}/"

  aws s3 ls "$s3_path" --region "$AWS_REGION" 2>/dev/null | \
    grep -v "PRE " | awk '{print $NF}' || true
}

# ═════════════════════════════════════════════════════════════════════
# Main sync loop
# ═════════════════════════════════════════════════════════════════════
echo "📦 Starting bi-directional sync..."
echo ""

for folder in "${SYNC_FOLDERS[@]}"; do
  local_folder="${SYNC_ROOT}/${folder}"

  # ── Get remote file list ──────────────────────────────────────────
  remote_names=$(list_s3_files "$folder")

  if [ -z "$remote_names" ]; then
    echo "⏭️  ${folder}/  — empty or not on S3, skipping"
    SKIPPED=$(( SKIPPED + 1 ))
    continue
  fi

  # Count remote files
  remote_count=$(echo "$remote_names" | wc -l)
  remote_count=${remote_count//[[:space:]]/}

  # ── PHASE 1: Download ──────────────────────────────────────────
  if [[ "$MODE" != "upload" ]]; then
    mkdir -p "$local_folder"
    downloaded=0

    while IFS= read -r fname; do
      [ -n "$fname" ] || continue
      dest="${local_folder}/${fname}"
      if [ ! -f "$dest" ]; then
        if $VERBOSE; then
          echo "   📥 downloading: ${folder}/${fname}"
        fi
        aws s3 cp "s3://${S3_BUCKET_NAME}/${folder}/${fname}" "$dest" \
          --region "$AWS_REGION" --quiet 2>/dev/null || \
          echo "   ⚠️  failed to download ${fname}"
        downloaded=$(( downloaded + 1 ))
      fi
    done <<< "$remote_names"

    echo "📁 ${folder}/  — 📥 ${downloaded} new / ${remote_count} total"
    TOTAL_DOWNLOADED=$(( TOTAL_DOWNLOADED + downloaded ))
  else
    echo "📁 ${folder}/  — (${remote_count} files on S3)"
  fi

  # ── PHASE 2: Upload files missing on S3 ────────────────────────
  if [[ "$MODE" != "download" ]]; then
    if [ ! -d "$local_folder" ]; then
      continue
    fi

    # Use find to list ALL files (including dotfiles) — glob * misses dotfiles
    while IFS= read -r local_file; do
      [ -f "$local_file" ] || continue
      fname=$(basename "$local_file")
      if ! echo "$remote_names" | grep -qxF "$fname"; then
        if $VERBOSE; then
          echo "   📤 uploading: ${folder}/${fname}"
        fi
        aws s3 cp "$local_file" "s3://${S3_BUCKET_NAME}/${folder}/${fname}" \
          --region "$AWS_REGION" --quiet 2>/dev/null || \
          echo "   ⚠️  failed to upload ${fname}"
        TOTAL_UPLOADED=$(( TOTAL_UPLOADED + 1 ))
      fi
    done < <(find "$local_folder" -maxdepth 1 -type f 2>/dev/null)
  fi
done

# ── Count total files in sync root ──────────────────────────────────
TOTAL_LOCAL=$(find "$SYNC_ROOT" -type f 2>/dev/null | wc -l)
TOTAL_LOCAL=${TOTAL_LOCAL//[[:space:]]/}

# ── Summary ─────────────────────────────────────────────────────────
SYNC_END_TIME=$(date +%s)
SYNC_DURATION=$(( SYNC_END_TIME - SYNC_START_TIME ))

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ Sync complete! (${SYNC_DURATION}s)"
echo "═══════════════════════════════════════════════════════"
echo "   📥 Files downloaded:       ${TOTAL_DOWNLOADED}"
echo "   📤 Files uploaded to S3:   ${TOTAL_UPLOADED}"
echo "   ⏭️  Folders skipped:        ${SKIPPED}"
echo "   📂 Total files in ./sync-aws/: ${TOTAL_LOCAL}"
echo ""
echo "   S3 bucket:  s3://${S3_BUCKET_NAME}"
echo "   Local root: $(pwd)/${SYNC_ROOT}"
echo ""
echo "📂 Per-folder breakdown:"
for f in "${SYNC_FOLDERS[@]}"; do
  if [ -d "${SYNC_ROOT}/${f}" ]; then
    cnt=$(find "${SYNC_ROOT}/${f}" -maxdepth 1 -type f 2>/dev/null | wc -l)
    cnt=${cnt//[[:space:]]/}
    echo "   ${f}/  (${cnt} files)"
  else
    echo "   ${f}/  (skipped)"
  fi
done
echo "═══════════════════════════════════════════════════════"
