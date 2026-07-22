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
discover_s3_dirs() {
  local prefix="$1"
  local s3_path="s3://${S3_BUCKET_NAME}/${prefix}/"

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

# ── Helper: discover all subdirectories locally inside SYNC_ROOT ───
discover_local_dirs() {
  local prefix="$1"
  local local_path="${SYNC_ROOT}/${prefix}"
  if [ -d "$local_path" ]; then
    find "$local_path" -mindepth 1 -type d 2>/dev/null | while IFS= read -r d; do
      rel="${d#${SYNC_ROOT}/}"
      [ -n "$rel" ] && echo "$rel"
    done
  fi
}

# ── Build full folder list (top-level + all discovered subfolders) ──
echo "🔍 Discovering folder structure (S3 + local)..."
ALL_FOLDERS=()
for top in "${TOP_FOLDERS[@]}"; do
  ALL_FOLDERS+=("$top")
  while IFS= read -r sub; do
    [ -n "$sub" ] || continue
    ALL_FOLDERS+=("$sub")
  done < <(discover_s3_dirs "$top")

  while IFS= read -r sub; do
    [ -n "$sub" ] || continue
    ALL_FOLDERS+=("$sub")
  done < <(discover_local_dirs "$top")
done

# De-duplicate while preserving order
SYNC_FOLDERS=()
for f in "${ALL_FOLDERS[@]}"; do
  already=false
  if [ ${#SYNC_FOLDERS[@]} -gt 0 ]; then
    for existing in "${SYNC_FOLDERS[@]}"; do
      if [[ "$existing" == "$f" ]]; then
        already=true
        break
      fi
    done
  fi
  if ! $already; then
    SYNC_FOLDERS+=("$f")
  fi
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

  if [ -n "$remote_names" ]; then
    remote_count=$(echo "$remote_names" | wc -l)
    remote_count=${remote_count//[[:space:]]/}
  else
    remote_count=0
  fi

  # ── Get local file list ───────────────────────────────────────────
  local_files=()
  if [ -d "$local_folder" ]; then
    while IFS= read -r lf; do
      [ -f "$lf" ] && local_files+=("$lf")
    done < <(find "$local_folder" -maxdepth 1 -type f 2>/dev/null)
  fi
  local_count=${#local_files[@]}

  # Skip if both remote and local are empty
  if [ "$remote_count" -eq 0 ] && [ "$local_count" -eq 0 ]; then
    echo "⏭️  ${folder}/  — empty on S3 and local, skipping"
    SKIPPED=$(( SKIPPED + 1 ))
    continue
  fi

  # ── PHASE 1: Download ──────────────────────────────────────────
  if [[ "$MODE" != "upload" ]]; then
    mkdir -p "$local_folder"
    downloaded=0

    if [ "$remote_count" -gt 0 ]; then
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
    fi

    echo "📁 ${folder}/  — 📥 ${downloaded} new / ${remote_count} S3 files (${local_count} local)"
    TOTAL_DOWNLOADED=$(( TOTAL_DOWNLOADED + downloaded ))
  else
    echo "📁 ${folder}/  — (${remote_count} S3 files, ${local_count} local files)"
  fi

  # ── PHASE 2: Upload files missing on S3 ────────────────────────
  if [[ "$MODE" != "download" ]]; then
    if [ "$local_count" -gt 0 ]; then
      for local_file in "${local_files[@]}"; do
        [ -f "$local_file" ] || continue
        fname=$(basename "$local_file")
        if [ "$remote_count" -eq 0 ] || ! echo "$remote_names" | grep -qxF "$fname"; then
          if $VERBOSE || [ "$remote_count" -eq 0 ]; then
            echo "   📤 uploading: ${folder}/${fname}"
          fi
          aws s3 cp "$local_file" "s3://${S3_BUCKET_NAME}/${folder}/${fname}" \
            --region "$AWS_REGION" --quiet 2>/dev/null || \
            echo "   ⚠️  failed to upload ${fname}"
          TOTAL_UPLOADED=$(( TOTAL_UPLOADED + 1 ))
        fi
      done
    fi
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
