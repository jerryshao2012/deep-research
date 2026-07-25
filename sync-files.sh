#!/bin/bash
# Sync local sync/ directory with Azure Blob Storage.
#
# Usage:
#   ./sync-files.sh                # full bi-directional sync (download then upload)
#   ./sync-files.sh --download     # only download (Azure → local)
#   ./sync-files.sh --upload       # only upload   (local → Azure)
#   ./sync-files.sh --help

set -euo pipefail

# Parse args
MODE="sync" # sync | download | upload

while [[ $# -gt 0 ]]; do
  case $1 in
    --download) MODE="download"; shift ;;
    --upload)   MODE="upload";   shift ;;
    --help|-h)
      echo "Usage: ./sync-files.sh [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --download   Only download from Azure Blob Storage to local sync/ folder"
      echo "  --upload     Only upload from local sync/ folder to Azure Blob Storage"
      echo "  --help, -h   Show this help message"
      exit 0
      ;;
    *)
      echo "❌ Unknown option: $1"
      exit 1
      ;;
  esac
done

source ./env.sh

if [ -f "./.env" ]; then
  set -a; source "./.env"; set +a
fi

echo "🔐 Retrieving storage credentials from Key Vault..."
STORAGE_ACCOUNT_NAME=$(az keyvault secret show --vault-name "$KV_NAME" --name STORAGE-ACCOUNT-NAME --query value -o tsv)
STORAGE_KEY=$(az keyvault secret show --vault-name "$KV_NAME" --name STORAGE-ACCOUNT-KEY --query value -o tsv)
BLOB_CONTAINER_NAME=$(az keyvault secret show --vault-name "$KV_NAME" --name AZURE-STORAGE-CONTAINER-NAME --query value -o tsv 2>/dev/null || echo "deep-research-blobs")

echo "✅ Storage Account: $STORAGE_ACCOUNT_NAME"
echo "✅ Blob Container:  $BLOB_CONTAINER_NAME"
echo ""

SYNC_ROOT="./sync"
mkdir -p "$SYNC_ROOT"

# Folders to sync
FOLDERS=("docs" "output" "input" ".langgraph_api")

if [ "$MODE" != "upload" ]; then
  echo "📥 Downloading files from Azure Blob Storage..."
  for folder in "${FOLDERS[@]}"; do
    echo "  + Downloading $folder/..."
    az storage blob download-batch \
      --source "$BLOB_CONTAINER_NAME" \
      --destination "$SYNC_ROOT" \
      --pattern "$folder/*" \
      --account-name "$STORAGE_ACCOUNT_NAME" \
      --account-key "$STORAGE_KEY" \
      --no-progress > /dev/null 2>&1 || echo "  ~ No files found under $folder/ on Azure"
  done
fi

if [ "$MODE" != "download" ]; then
  echo "📤 Uploading files to Azure Blob Storage..."
  for folder in "${FOLDERS[@]}"; do
    if [ -d "$SYNC_ROOT/$folder" ]; then
      echo "  + Uploading $folder/..."
      az storage blob upload-batch \
        --destination "$BLOB_CONTAINER_NAME" \
        --source "$SYNC_ROOT/$folder" \
        --destination-path "$folder" \
        --account-name "$STORAGE_ACCOUNT_NAME" \
        --account-key "$STORAGE_KEY" \
        --overwrite true \
        --no-progress > /dev/null
    else
      echo "  ~ Skipping local directory $folder/ (does not exist)"
    fi
  done
fi

# Sync database file (deep_research.db)
if [ "$MODE" != "upload" ]; then
  echo "📥 Downloading database file: deep_research.db..."
  if az storage blob exists --container-name "$BLOB_CONTAINER_NAME" --name "deep_research.db" --account-name "$STORAGE_ACCOUNT_NAME" --account-key "$STORAGE_KEY" --query "exists" -o tsv 2>/dev/null | grep -q "true"; then
    az storage blob download \
      --container-name "$BLOB_CONTAINER_NAME" \
      --name "deep_research.db" \
      --file "./deep_research.db" \
      --account-name "$STORAGE_ACCOUNT_NAME" \
      --account-key "$STORAGE_KEY" --no-progress >/dev/null 2>&1 || true
    cp "./deep_research.db" "$SYNC_ROOT/deep_research.db" 2>/dev/null || true
    echo "  + Downloaded deep_research.db successfully"
  else
    echo "  ~ No remote database file found"
  fi
fi

if [ "$MODE" != "download" ]; then
  if [ -f "./deep_research.db" ]; then
    echo "📤 Uploading database file: deep_research.db..."
    az storage blob upload \
      --container-name "$BLOB_CONTAINER_NAME" \
      --name "deep_research.db" \
      --file "./deep_research.db" \
      --account-name "$STORAGE_ACCOUNT_NAME" \
      --account-key "$STORAGE_KEY" \
      --overwrite true --no-progress >/dev/null
    echo "  + Uploaded deep_research.db successfully"
  fi
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ Sync complete!"
echo "═══════════════════════════════════════════════════════"
