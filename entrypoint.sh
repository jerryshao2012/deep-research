#!/bin/bash
set -e

# ── Storage backend detection ────────────────────────────────────────────────
# This entrypoint supports Azure and AWS:
#   Azure: Azure File Share is mounted at MOUNT_PATH by the platform
#   AWS:   boto3 downloads generic files and restores guarded LangGraph state

PROJECT_ROOT="${PROJECT_ROOT:-/deps/deep_research}"
MOUNT_PATH="${MOUNT_PATH:-$PROJECT_ROOT/mnt}"
AWS_MODE=false

echo "═══════════════════════════════════════════════════════"
echo "🔧 Entrypoint: Configuring persistent storage..."

# ── Detect storage backend ──────────────────────────────────────────────────

if [ -n "${S3_BUCKET_NAME:-}" ]; then
    # App Runner does not provide the FUSE device required by s3fs.
    AWS_MODE=true
    echo "☁️  AWS mode detected (S3_BUCKET_NAME=$S3_BUCKET_NAME)"
    echo "   Region: ${AWS_REGION:-us-east-1}"
    mkdir -p "$PROJECT_ROOT/docs" "$PROJECT_ROOT/output" "$PROJECT_ROOT/input"

elif mountpoint -q "$MOUNT_PATH" 2>/dev/null || [ -d "$MOUNT_PATH" ]; then
    # ── Azure mode: File Share already mounted by the platform ───────────────
    if mountpoint -q "$MOUNT_PATH" 2>/dev/null; then
        echo "🔵 Azure mode detected (File Share mounted at $MOUNT_PATH)"
    else
        echo "🔵 Mount path exists at $MOUNT_PATH (local dev or pre-mounted)"
    fi
else
    echo "⚠️  No persistent storage backend detected."
    echo "   Set S3_BUCKET_NAME (AWS) or mount Azure File Share at $MOUNT_PATH."
    echo "   Directories will be ephemeral."
fi

echo ""
echo "   Mount path: $MOUNT_PATH"
echo "   Mount exists: $([ -d "$MOUNT_PATH" ] && echo 'YES' || echo 'NO')"
echo "   Mount is mountpoint: $(mountpoint -q "$MOUNT_PATH" 2>/dev/null && echo 'YES' || echo 'NO')"
echo "═══════════════════════════════════════════════════════"

# ── Function to setup persistent directory using symlinks ────────────────────

setup_persistent_dir() {
    local dir_name=$1
    local local_path="$PROJECT_ROOT/$dir_name"
    local mount_path="$MOUNT_PATH/$dir_name"

    echo "🔍 Checking persistence for $dir_name..."
    echo "   local_path=$local_path (exists=$([ -e "$local_path" ] && echo 'yes' || echo 'no'), symlink=$([ -L "$local_path" ] && echo 'yes' || echo 'no'))"
    echo "   mount_path=$mount_path (exists=$([ -d "$mount_path" ] && echo 'yes' || echo 'no'))"

    if [ -d "$MOUNT_PATH" ]; then
        # Create directory on mount if it doesn't exist
        mkdir -p "$mount_path"
        
        # If local directory exists and is not a symlink, sync its content to mount then remove it
        if [ -d "$local_path" ] && [ ! -L "$local_path" ]; then
            echo "📦 Syncing existing $dir_name content to persistent storage..."
            cp -r "$local_path/." "$mount_path/" 2>/dev/null || true
            rm -rf "$local_path"
        fi
        
        # Remove stale symlink if it points to wrong target
        if [ -L "$local_path" ]; then
            current_target=$(readlink "$local_path")
            if [ "$current_target" != "$mount_path" ]; then
                echo "🔄 Removing stale symlink ($current_target → $mount_path)"
                rm -f "$local_path"
            fi
        fi
        
        # Create symlink from local path to mount path
        if [ ! -L "$local_path" ]; then
            ln -sfn "$mount_path" "$local_path"
        fi
        echo "✅ $dir_name → $mount_path (symlinked)"
    else
        echo "⚠️  Mount path $MOUNT_PATH not found. $dir_name will remain ephemeral."
        mkdir -p "$local_path"
    fi
}

if ! $AWS_MODE; then
    setup_persistent_dir "docs"
    setup_persistent_dir "output"
    setup_persistent_dir "input"
fi

echo ""
echo "📋 Final state:"
ls -la "$PROJECT_ROOT/" | grep -E "^[dl]" || true
echo "═══════════════════════════════════════════════════════"

cd "$PROJECT_ROOT"
if $AWS_MODE; then
    echo "📥 Downloading generic application files..."
    python3 -m s3_storage startup
    echo "📥 Restoring guarded LangGraph snapshot..."
    python3 -m langgraph_snapshot restore --write-receipt
fi

# Execute the passed command (e.g., langgraph dev)
echo "🚀 Starting application: $@"
exec "$@"
