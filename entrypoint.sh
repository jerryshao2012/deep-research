#!/bin/bash
set -e

# ── Storage backend detection ────────────────────────────────────────────────
# This entrypoint works identically for Azure and AWS:
#   Azure: Azure File Share is mounted at MOUNT_PATH by the platform
#   AWS:   S3 bucket is mounted at MOUNT_PATH via s3fs-fuse
# After mounting, both backends use the same symlink logic below.

MOUNT_PATH="/deps/deep_research/mnt"

echo "═══════════════════════════════════════════════════════"
echo "🔧 Entrypoint: Configuring persistent storage..."

# ── Detect and mount storage backend ───────────────────────────────────────

if [ -n "$S3_BUCKET_NAME" ] && command -v s3fs &>/dev/null; then
    # ── AWS mode: mount S3 bucket via s3fs ───────────────────────────────────
    echo "☁️  AWS mode detected (S3_BUCKET_NAME=$S3_BUCKET_NAME)"
    echo "   Region: ${AWS_REGION:-us-east-1}"

    mkdir -p "$MOUNT_PATH"

    # s3fs requires a passwd file (can be empty when using IAM role)
    touch /etc/passwd-s3fs
    chmod 640 /etc/passwd-s3fs

    echo "   Mounting s3://${S3_BUCKET_NAME} → $MOUNT_PATH ..."
    s3fs "$S3_BUCKET_NAME" "$MOUNT_PATH" \
        -o iam_role=auto \
        -o url="https://s3.${AWS_REGION:-us-east-1}.amazonaws.com" \
        -o use_path_request_style \
        -o allow_other \
        -o nonempty \
        -o umask=022 \
        2>&1 || {
        echo "⚠️  s3fs mount failed. Directories will remain ephemeral."
        echo "   Check: IAM role has s3:ListBucket + s3:GetObject + s3:PutObject permissions"
        rm -rf "$MOUNT_PATH"
    }

    if mountpoint -q "$MOUNT_PATH" 2>/dev/null; then
        echo "✅ S3 bucket mounted at $MOUNT_PATH"
    else
        echo "⚠️  $MOUNT_PATH is not a mount point — falling back to ephemeral storage"
    fi

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
    local local_path="/deps/deep_research/$dir_name"
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

# Setup persistence for all required directories
setup_persistent_dir "docs"
setup_persistent_dir "output"
setup_persistent_dir "input"
setup_persistent_dir ".langgraph_api"

echo ""
echo "📋 Final state:"
ls -la /deps/deep_research/ | grep -E "^[dl]" || true
echo "═══════════════════════════════════════════════════════"

# Execute the passed command (e.g., langgraph dev)
echo "🚀 Starting application: $@"
exec "$@"
