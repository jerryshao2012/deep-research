#!/bin/bash
# Bi-directional sync between Azure File Share and local ./sync/ folder.
#
# Remote (Azure File Share) top-level folders synced:
#   docs, output, input, .langgraph_api
#   (all subdirectories are discovered and synced automatically)
#
# Local mirror:  ./sync/<folder>/
#
# Usage:
#   ./sync-files.sh                # full bi-directional sync
#   ./sync-files.sh --download     # only download (Azure → local)
#   ./sync-files.sh --upload       # only upload   (local → Azure)
#   ./sync-files.sh --verbose      # show raw az CLI output for debugging
#   ./sync-files.sh --help

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
      echo "Usage: ./sync-files.sh [OPTIONS]"
      echo ""
      echo "Bi-directional sync between Azure File Share and local ./sync/ folder."
      echo ""
      echo "Options:"
      echo "  --download   Only download from Azure File Share to local"
      echo "  --upload     Only upload from local to Azure File Share"
      echo "  --verbose    Show raw Azure CLI output for debugging"
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
source ./env.sh

SYNC_START_TIME=$(date +%s)

echo "🔄 Azure File Share ↔ Local Bi-directional Sync"
echo "   Mode: ${MODE}"
echo ""

# ── Retrieve storage credentials from Key Vault ─────────────────────
echo "🔐 Retrieving storage credentials from Key Vault..."
STORAGE_ACCOUNT_NAME=$(az keyvault secret show \
  --vault-name "$KV_NAME" \
  --name STORAGE-ACCOUNT-NAME \
  --query value -o tsv)

STORAGE_KEY=$(az keyvault secret show \
  --vault-name "$KV_NAME" \
  --name STORAGE-ACCOUNT-KEY \
  --query value -o tsv)

FILE_SHARE_NAME=$(az keyvault secret show \
  --vault-name "$KV_NAME" \
  --name FILE-SHARE-NAME \
  --query value -o tsv)

echo "✅ Storage Account: $STORAGE_ACCOUNT_NAME"
echo "✅ File Share:      $FILE_SHARE_NAME"
echo ""

# ── Local sync root ─────────────────────────────────────────────────
SYNC_ROOT="./sync"
mkdir -p "$SYNC_ROOT"

# ── Top-level folders to scan (subfolders discovered automatically) ──
TOP_FOLDERS=(
  "docs"
  "output"
  "input"
)

# ── Counters ────────────────────────────────────────────────────────
TOTAL_DOWNLOADED=0
TOTAL_UPLOADED=0
SKIPPED=0

# ── Helper: recursively discover all subdirectories on Azure ────────
# Outputs one folder path per line (parent before children, DFS order).
discover_remote_dirs() {
  local dir="$1"
  local json
  json=$(az storage file list \
    --path "$dir" \
    --account-name "$STORAGE_ACCOUNT_NAME" \
    --account-key "$STORAGE_KEY" \
    --share-name "$FILE_SHARE_NAME" \
    -o json 2>/dev/null) || json="[]"

  # Debug: show raw JSON structure for the first folder only
  if $VERBOSE; then
    echo "   [debug] az storage file list --path '$dir' returned:" >&2
    echo "$json" | python3 -c "
import sys, json
try:
    items = json.load(sys.stdin)
    for item in items:
        print(f\"   [debug]   name={item.get('name','?')}  type={item.get('type','?')}  keys={list(item.keys())}\")
except Exception as e:
    print(f'   [debug]   parse error: {e}')
" >&2
  fi

  local dirs
  dirs=$(echo "$json" | python3 -c "
import sys, json
try:
    items = json.load(sys.stdin)
    for item in items:
        t = item.get('type', '')
        # Handle both 'type' at top level and nested in 'properties'
        if not t:
            t = item.get('properties', {}).get('type', '')
        if t.lower() in ('directory', 'dir'):
            print(item['name'])
except:
    pass
" 2>/dev/null || true)

  while IFS= read -r d; do
    [ -n "$d" ] || continue
    local child
    if [ -z "$dir" ]; then
      child="$d"
    else
      child="${dir}/${d}"
    fi
    echo "$child"
    discover_remote_dirs "$child"
  done <<< "$dirs"
}

# ── Build full folder list (top-level + all discovered subfolders) ──
echo "🔍 Discovering remote folder structure..."
SYNC_FOLDERS=()
for top in "${TOP_FOLDERS[@]}"; do
  SYNC_FOLDERS+=("$top")
  while IFS= read -r sub; do
    [ -n "$sub" ] || continue
    SYNC_FOLDERS+=("$sub")
  done < <(discover_remote_dirs "$top")
done
echo "   Found ${#SYNC_FOLDERS[@]} folder(s): ${SYNC_FOLDERS[*]}"
echo ""

# ── Helper: list remote files in a folder, one name per line ────────
# Uses az storage file list + python3 JSON parsing (reliable, bash 3.x safe).
# Returns empty string if folder doesn't exist or is empty.
list_remote_files() {
  local folder="$1"
  local json
  json=$(az storage file list \
    --path "$folder" \
    --account-name "$STORAGE_ACCOUNT_NAME" \
    --account-key "$STORAGE_KEY" \
    --share-name "$FILE_SHARE_NAME" \
    -o json 2>/dev/null) || json="[]"

  echo "$json" | python3 -c "
import sys, json
try:
    items = json.load(sys.stdin)
    for item in items:
        t = item.get('type', '')
        if not t:
            t = item.get('properties', {}).get('type', '')
        if t.lower() == 'file':
            print(item['name'])
except:
    pass
" 2>/dev/null || true
}

# ── Helper: download a single file from Azure File Share ────────────
download_file() {
  local folder="$1"
  local filename="$2"
  local dest_dir="$3"

  if $VERBOSE; then
    echo "   [debug] downloading ${folder}/${filename}"
  fi

  az storage file download \
    --share-name "$FILE_SHARE_NAME" \
    --path "${folder}/${filename}" \
    --dest "${dest_dir}/${filename}" \
    --account-name "$STORAGE_ACCOUNT_NAME" \
    --account-key "$STORAGE_KEY" \
    --no-overwrite >/dev/null 2>&1 || {
      if $VERBOSE; then
        echo "   ⚠️  failed to download ${filename} (retrying without --no-overwrite)"
      fi
      # Retry without --no-overwrite in case the flag is the issue
      az storage file download \
        --share-name "$FILE_SHARE_NAME" \
        --path "${folder}/${filename}" \
        --dest "${dest_dir}/${filename}" \
        --account-name "$STORAGE_ACCOUNT_NAME" \
        --account-key "$STORAGE_KEY" \
        >/dev/null 2>&1 || echo "   ⚠️  failed to download ${filename}"
    }
}

# ── Helper: upload a single file to Azure File Share ────────────────
upload_file() {
  local folder="$1"
  local local_path="$2"
  local filename="$3"

  if $VERBOSE; then
    echo "   [debug] uploading ${filename} to ${folder}/"
  fi

  az storage file upload \
    --source "$local_path" \
    --path "$folder" \
    --account-name "$STORAGE_ACCOUNT_NAME" \
    --account-key "$STORAGE_KEY" \
    --share-name "$FILE_SHARE_NAME" >/dev/null 2>&1 || \
    echo "   ⚠️  failed to upload ${filename}"
}

# ═════════════════════════════════════════════════════════════════════
# Main sync loop
# ═════════════════════════════════════════════════════════════════════
echo "📦 Starting bi-directional sync..."
echo ""

for folder in "${SYNC_FOLDERS[@]}"; do
  local_folder="${SYNC_ROOT}/${folder}"

  # ── Get remote file list (works for all folder types) ────────────
  remote_names=$(list_remote_files "$folder")

  if [ -z "$remote_names" ]; then
    echo "⏭️  ${folder}/  — empty or not on server, skipping"
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
        download_file "$folder" "$fname" "$local_folder"
        downloaded=$(( downloaded + 1 ))
      fi
    done <<< "$remote_names"

    echo "📁 ${folder}/  — 📥 ${downloaded} new / ${remote_count} total"
    TOTAL_DOWNLOADED=$(( TOTAL_DOWNLOADED + downloaded ))
  else
    echo "📁 ${folder}/  — (${remote_count} files on server)"
  fi

  # ── PHASE 2: Upload files missing on Azure ─────────────────────
  if [[ "$MODE" != "download" ]]; then
    # Ensure local folder exists for scanning
    if [ ! -d "$local_folder" ]; then
      continue
    fi

    # Use find to list ALL files (including dotfiles) — glob * misses dotfiles
    while IFS= read -r local_file; do
      [ -f "$local_file" ] || continue
      fname=$(basename "$local_file")
      if ! echo "$remote_names" | grep -qxF "$fname"; then
        echo "   ↑ uploading: ${folder}/${fname}"
        upload_file "$folder" "$local_file" "$fname"
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
echo "   📤 Files uploaded to Azure: ${TOTAL_UPLOADED}"
echo "   ⏭️  Folders skipped:        ${SKIPPED}"
echo "   📂 Total files in ./sync/:  ${TOTAL_LOCAL}"
echo ""
echo "   Local sync root: $(pwd)/${SYNC_ROOT}"
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
