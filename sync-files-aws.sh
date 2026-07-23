#!/bin/bash
# Sync runtime documents and guarded LangGraph state with AWS S3.
#
# Generic folders use `aws s3 sync` without deletion. LangGraph state always
# uses the guarded immutable-generation CLI.

set -euo pipefail

# Safe default: download generic files and restore committed snapshot state.
# Publishing requires explicit --upload.
MODE="download"
VERBOSE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --download) MODE="download"; shift ;;
    --upload) MODE="upload"; shift ;;
    --verbose) VERBOSE=true; shift ;;
    --help|-h)
      echo "Usage: ./sync-files-aws.sh [--download|--upload] [--verbose]"
      echo "Sync project docs, output, input, and guarded LangGraph state with S3."
      echo "Default: download and restore only. Use --upload to publish."
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${AWS_ENV_FILE:-$PROJECT_ROOT/env-aws.sh}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -f "$ENV_FILE" ]; then
  echo "AWS environment file not found: $ENV_FILE" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${S3_BUCKET_NAME:?S3_BUCKET_NAME must be set}"
: "${AWS_REGION:?AWS_REGION must be set}"

echo "Verifying AWS authentication..."
if ! aws sts get-caller-identity --query Account --output text >/dev/null; then
  echo "AWS authentication failed" >&2
  exit 1
fi
if ! aws s3api head-bucket \
  --bucket "$S3_BUCKET_NAME" \
  --region "$AWS_REGION" >/dev/null; then
  echo "S3 bucket is unavailable: $S3_BUCKET_NAME" >&2
  exit 1
fi

GENERIC_FOLDERS=(
  "docs"
  "output"
  "input"
)

aws_sync() {
  if $VERBOSE; then
    aws s3 sync "$@"
  else
    aws s3 sync "$@" --only-show-errors
  fi
}

download_generic_folders() {
  local folder
  local local_folder
  for folder in "${GENERIC_FOLDERS[@]}"; do
    local_folder="$PROJECT_ROOT/${folder}"
    mkdir -p "$local_folder"
    echo "Downloading s3://${S3_BUCKET_NAME}/${folder}/ → $local_folder"
    aws_sync \
      "s3://${S3_BUCKET_NAME}/${folder}/" \
      "$local_folder" \
      --region "$AWS_REGION"
  done
}

upload_generic_folders() {
  local folder
  local local_folder
  for folder in "${GENERIC_FOLDERS[@]}"; do
    local_folder="$PROJECT_ROOT/${folder}"
    if [ ! -d "$local_folder" ]; then
      echo "Skipping missing local folder: $local_folder"
      continue
    fi
    echo "Uploading $local_folder → s3://${S3_BUCKET_NAME}/${folder}/"
    aws_sync \
      "$local_folder" \
      "s3://${S3_BUCKET_NAME}/${folder}/" \
      --region "$AWS_REGION" \
      --no-follow-symlinks
  done
}

cd "$PROJECT_ROOT"

if [[ "$MODE" != "upload" ]]; then
  download_generic_folders
  "$PYTHON_BIN" -m langgraph_snapshot restore \
    --target "$PROJECT_ROOT/.langgraph_api"
fi

if [[ "$MODE" != "download" ]]; then
  upload_generic_folders
  "$PYTHON_BIN" -m langgraph_snapshot publish \
    --source "$PROJECT_ROOT/.langgraph_api"
fi

echo "AWS S3 sync complete."
