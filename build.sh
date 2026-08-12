#!/bin/bash
set -e
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/scripts/container_runtime.sh"

# Timer tracking
TOTAL_START_TIME=$(date +%s)
STEP_TIMES=()
BUILD_CONTEXT_DIR=""
DOCKER_CREDENTIAL_DIR=""
DOCKER_PAT_FILE=""
BUILD_TRANSACTION_ARMED=false
CONFIG_BACKUP=""
EXPECTED_CONFIG=""
MARKER_BACKUP=""
MARKER_TEMP=""
MARKER_EXISTED=false

# Function to track step timing
start_step() {
  STEP_NAME="$1"
  STEP_START=$(date +%s)
  echo "⏱️  Starting: $STEP_NAME"
}

end_step() {
  STEP_END=$(date +%s)
  DURATION=$((STEP_END - STEP_START))
  STEP_TIMES+=("$STEP_NAME: ${DURATION}s")
  echo "✅ Completed: $STEP_NAME (${DURATION}s)"
  echo ""
}

print_timing_summary() {
  TOTAL_END=$(date +%s)
  TOTAL_DURATION=$((TOTAL_END - TOTAL_START_TIME))
  echo ""
  echo "═══════════════════════════════════════════════════════"
  echo "⏱️  Build Timing Summary"
  echo "═══════════════════════════════════════════════════════"
  for timing in "${STEP_TIMES[@]}"; do
    echo "   • $timing"
  done
  echo "───────────────────────────────────────────────────────"
  echo "   Total build time: ${TOTAL_DURATION}s"
  echo "═══════════════════════════════════════════════════════"
}

cleanup_build_context() {
  if [[ "$BUILD_CONTEXT_DIR" == "$SCRIPT_DIR"/.container-build-context.* ]] && [ -d "$BUILD_CONTEXT_DIR" ]; then
    rm -rf -- "$BUILD_CONTEXT_DIR"
  fi
  if [[ "$DOCKER_CREDENTIAL_DIR" == /tmp/deep-research-docker-credentials.* ]] && [ -d "$DOCKER_CREDENTIAL_DIR" ]; then
    rm -rf -- "$DOCKER_CREDENTIAL_DIR"
  fi
}

file_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"
}

same_file_state() {
  [ -f "$1" ] && [ -f "$2" ] && cmp -s "$1" "$2" && [ "$(file_mode "$1")" = "$(file_mode "$2")" ]
}

cleanup_transaction_files() {
  [ -z "$CONFIG_BACKUP" ] || rm -f -- "$CONFIG_BACKUP"
  [ -z "$EXPECTED_CONFIG" ] || rm -f -- "$EXPECTED_CONFIG"
  [ -z "$MARKER_BACKUP" ] || rm -f -- "$MARKER_BACKUP"
  [ -z "$MARKER_TEMP" ] || rm -f -- "$MARKER_TEMP"
}

rollback_build_owned_files() {
  if ! same_file_state "$SCRIPT_DIR/webapp/config.py" "$EXPECTED_CONFIG"; then
    echo "Error: concurrent change detected in webapp/config.py; refusing to overwrite it during rollback." >&2
    return 70
  fi
  if [ "$MARKER_EXISTED" = true ]; then
    if ! same_file_state "$SCRIPT_DIR/.build_version" "$MARKER_BACKUP"; then
      echo "Error: concurrent change detected in .build_version; refusing to overwrite it during rollback." >&2
      return 70
    fi
  elif [ -e "$SCRIPT_DIR/.build_version" ]; then
    echo "Error: concurrent change detected in .build_version; refusing to overwrite it during rollback." >&2
    return 70
  fi
  cp -p "$CONFIG_BACKUP" "$SCRIPT_DIR/webapp/config.py" || return $?
  if [ "$MARKER_EXISTED" = true ]; then
    cp -p "$MARKER_BACKUP" "$SCRIPT_DIR/.build_version" || return $?
  else
    rm -f -- "$SCRIPT_DIR/.build_version" || return $?
  fi
}

finish_build() {
  status=$?
  set +e
  cleanup_build_context
  cleanup_status=$?
  if [ "$status" -eq 0 ] && [ "$cleanup_status" -ne 0 ]; then
    status=$cleanup_status
  fi
  if [ "$BUILD_TRANSACTION_ARMED" = true ] && [ "$status" -ne 0 ]; then
    rollback_build_owned_files
    rollback_status=$?
    if [ "$rollback_status" -ne 0 ]; then
      status=$rollback_status
    fi
  fi
  cleanup_transaction_files
  trap - EXIT
  exit "$status"
}

begin_build_transaction() {
  if ! git diff --quiet HEAD -- webapp/config.py; then
    echo "Error: webapp/config.py is dirty; commit or restore it before building." >&2
    return 64
  fi
  git ls-files --error-unmatch webapp/config.py >/dev/null 2>&1 || {
    echo "Error: webapp/config.py must be tracked by Git before building." >&2
    return 64
  }
  CONFIG_BACKUP=$(mktemp "$SCRIPT_DIR/webapp/.config.py.build-backup.XXXXXX")
  cp -p "$SCRIPT_DIR/webapp/config.py" "$CONFIG_BACKUP"
  EXPECTED_CONFIG=$(mktemp "$SCRIPT_DIR/webapp/.config.py.build-expected.XXXXXX")
  if [ -e "$SCRIPT_DIR/.build_version" ]; then
    MARKER_EXISTED=true
    MARKER_BACKUP=$(mktemp "$SCRIPT_DIR/.build_version.build-backup.XXXXXX")
    cp -p "$SCRIPT_DIR/.build_version" "$MARKER_BACKUP"
  fi
  BUILD_TRANSACTION_ARMED=true
}

verify_build_owned_files_unchanged() {
  if ! same_file_state "$SCRIPT_DIR/webapp/config.py" "$EXPECTED_CONFIG"; then
    echo "Error: concurrent change detected in webapp/config.py before build completion." >&2
    return 70
  fi
  if [ "$MARKER_EXISTED" = true ]; then
    if ! same_file_state "$SCRIPT_DIR/.build_version" "$MARKER_BACKUP"; then
      echo "Error: concurrent change detected in .build_version before build completion." >&2
      return 70
    fi
  elif [ -e "$SCRIPT_DIR/.build_version" ]; then
    echo "Error: concurrent change detected in .build_version before build completion." >&2
    return 70
  fi
}

# Configuration
source "$SCRIPT_DIR/env.sh"
: "${BACKEND_APP_NAME:?Set BACKEND_APP_NAME in env.sh}"
: "${UI_APP_NAME:?Set UI_APP_NAME in env.sh}"

trap finish_build EXIT
OLD_UMASK=$(umask)
umask 077
DOCKER_CREDENTIAL_DIR="$(mktemp -d "/tmp/deep-research-docker-credentials.XXXXXX")"
umask "$OLD_UMASK"
DOCKER_PAT_FILE="$DOCKER_CREDENTIAL_DIR/pat"
XTRACE_WAS_ENABLED=false
case "$-" in
  *x*) XTRACE_WAS_ENABLED=true; set +x ;;
esac
OLD_UMASK=$(umask)
umask 077
if [ -n "${DOCKER_HUB_PAT:-}" ]; then
  printf '%s' "$DOCKER_HUB_PAT" >"$DOCKER_PAT_FILE"
fi
unset DOCKER_HUB_PAT
umask "$OLD_UMASK"
if [ -f "$SCRIPT_DIR/.env" ]; then
  CREDENTIAL_ARGS=()
  if [ -z "${DOCKER_HUB_USERNAME:-}" ]; then
    CREDENTIAL_ARGS+=(--username)
  fi
  if [ ! -e "$DOCKER_PAT_FILE" ]; then
    CREDENTIAL_ARGS+=(--pat-file "$DOCKER_PAT_FILE")
  fi
  if [ "${#CREDENTIAL_ARGS[@]}" -gt 0 ]; then
    FALLBACK_USERNAME=$(python3 "$SCRIPT_DIR/scripts/load_docker_credentials.py" --input "$SCRIPT_DIR/.env" "${CREDENTIAL_ARGS[@]}")
    if [ -z "${DOCKER_HUB_USERNAME:-}" ] && [ -n "$FALLBACK_USERNAME" ]; then
      DOCKER_HUB_USERNAME="$FALLBACK_USERNAME"
    fi
  fi
fi
if [ "$XTRACE_WAS_ENABLED" = true ]; then
  set -x
fi

python3 "$SCRIPT_DIR/scripts/sanitize_passkey_dotenv.py" --input "$SCRIPT_DIR/.env.docker" --check
RESOLVER_STDOUT="$DOCKER_CREDENTIAL_DIR/resolver.stdout"
RESOLVER_STDERR="$DOCKER_CREDENTIAL_DIR/resolver.stderr"
set +e
BACKEND_APP_NAME="$BACKEND_APP_NAME" UI_APP_NAME="$UI_APP_NAME" \
  "$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh" >"$RESOLVER_STDOUT" 2>"$RESOLVER_STDERR"
RESOLVER_STATUS=$?
set -e
cat "$RESOLVER_STDERR" >&2
if [ "$RESOLVER_STATUS" -ne 0 ]; then
  cat "$RESOLVER_STDOUT"
  exit "$RESOLVER_STATUS"
fi
SEEN_RESOLVER_KEYS="|"
RESOLVER_ASSIGNMENT_PATTERN="^([A-Z][A-Z0-9_]*)='([^']*)'$"
while IFS= read -r line; do
  if [[ ! "$line" =~ $RESOLVER_ASSIGNMENT_PATTERN ]]; then
    echo "Error: malformed resolver output assignment" >&2
    exit 65
  fi
  key="${BASH_REMATCH[1]}"
  value="${BASH_REMATCH[2]}"
  if [[ "$SEEN_RESOLVER_KEYS" == *"|$key|"* ]]; then
    echo "Error: duplicate resolver output key: $key" >&2
    exit 65
  fi
  case "$key" in
    AZURE_ENVIRONMENT_ID|AZURE_ENVIRONMENT_DEFAULT_DOMAIN|BACKEND_URL|AZURE_UI_URL|FRONTEND_URLS|GOOGLE_CALLBACK_URL|GITHUB_CALLBACK_URL|GITHUB_HOMEPAGE_URL) ;;
    BACKEND_APP_NAME) RESOLVED_BACKEND_APP_NAME="$value" ;;
    UI_APP_NAME) RESOLVED_UI_APP_NAME="$value" ;;
    CHANGED) RESOLVED_CHANGED="$value" ;;
    *)
      echo "Error: unexpected resolver output key: $key" >&2
      exit 65
      ;;
  esac
  SEEN_RESOLVER_KEYS="${SEEN_RESOLVER_KEYS}${key}|"
done <"$RESOLVER_STDOUT"
for key in AZURE_ENVIRONMENT_ID AZURE_ENVIRONMENT_DEFAULT_DOMAIN BACKEND_APP_NAME UI_APP_NAME BACKEND_URL AZURE_UI_URL FRONTEND_URLS GOOGLE_CALLBACK_URL GITHUB_CALLBACK_URL GITHUB_HOMEPAGE_URL CHANGED; do
  if [[ "$SEEN_RESOLVER_KEYS" != *"|$key|"* ]]; then
    echo "Error: missing resolver output key: $key" >&2
    exit 65
  fi
done
if [[ "$RESOLVED_BACKEND_APP_NAME" != "$BACKEND_APP_NAME" || "$RESOLVED_UI_APP_NAME" != "$UI_APP_NAME" ]]; then
  echo "Error: resolver app names do not match canonical env.sh names" >&2
  exit 65
fi
if [[ "$RESOLVED_CHANGED" != true && "$RESOLVED_CHANGED" != false ]]; then
  echo "Error: invalid resolver CHANGED value" >&2
  exit 65
fi

select_container_runtime
ensure_container_runtime_ready
echo "Using container runtime: $CONTAINER_RUNTIME"

echo "🚀 Starting Deep Research Agent build..."

# 1. Set Azure Subscription
start_step "Set Azure Subscription"
: "${AZURE_SUBSCRIPTION_ID:?Set AZURE_SUBSCRIPTION_ID in env.sh}"
if az account set --subscription "$AZURE_SUBSCRIPTION_ID" 2>/dev/null; then
  echo "✅ Subscription set to $AZURE_SUBSCRIPTION_ID"
else
  echo "⚠️  Warning: Could not set Azure subscription $AZURE_SUBSCRIPTION_ID."
fi
end_step

# 2. Create resource group
start_step "Resource Group Setup"
if az group show --name $RESOURCE_GROUP &> /dev/null; then
  echo "✅ Resource group '$RESOURCE_GROUP' already exists. Skipping creation."
else
  echo "⚠️  Warning: Resource group '$RESOURCE_GROUP' check/creation bypassed (subscription may be disabled)."
fi
end_step

# 3. Azure Provider Registration
start_step "Azure Provider Registration"
echo "📝 Skipping Azure provider registration check (subscription may be disabled)..."
end_step

# 4. Check Docker Hub Username
start_step "Docker Hub Setup"
if [ -z "${DOCKER_HUB_USERNAME:-}" ]; then
  echo "❌ Error: Please set DOCKER_HUB_USERNAME in .env"
  exit 1
fi
echo "✅ Using Docker Hub user: $DOCKER_HUB_USERNAME"
if [ -s "$DOCKER_PAT_FILE" ]; then
  echo "🔐 Logging into Docker Hub..."
  container_runtime_login "$DOCKER_HUB_USERNAME" docker.io <"$DOCKER_PAT_FILE"
fi
rm -rf -- "$DOCKER_CREDENTIAL_DIR"
DOCKER_CREDENTIAL_DIR=""
DOCKER_PAT_FILE=""
end_step

# 5. Increment API version
start_step "API Version Management"
echo "🔢 Incrementing API version..."
begin_build_transaction
python3 ./increment_version.py
cp -p "$SCRIPT_DIR/webapp/config.py" "$EXPECTED_CONFIG"
NEW_VERSION=$(grep -E 'API_VERSION(:\s*\w+)?\s*=\s*' webapp/config.py | grep -o '"[^"]*"')
NEW_VERSION=${NEW_VERSION//\"/}
echo "✅ New API version: $NEW_VERSION"
end_step

# 6. Build and push image
start_step "Container Image Build & Push"
BUILD_VERSION=$(date +%Y%m%d%H%M%S)
echo "🔨 Building Container image with tags: latest, $BUILD_VERSION"
# Ensure we're in the correct directory (where Dockerfile is located)
cd "$SCRIPT_DIR"
BUILD_CONTEXT_DIR="$(mktemp -d "$SCRIPT_DIR/.container-build-context.XXXXXX")"
git ls-files --cached --others --exclude-standard -z \
  | tar --null -T - -cf - \
  | tar -xf - -C "$BUILD_CONTEXT_DIR"
cp .env.docker "$BUILD_CONTEXT_DIR/.env.docker"
# Registry pushes require the full registry host in the image name.
FULL_IMAGE_NAME="docker.io/$DOCKER_HUB_USERNAME/deep-research-agent:latest"

container_runtime_build --platform linux/amd64 -t "$FULL_IMAGE_NAME" "$BUILD_CONTEXT_DIR"
cleanup_build_context
BUILD_CONTEXT_DIR=""
container_runtime_push "$FULL_IMAGE_NAME"
if [ $? -ne 0 ]; then
  echo "❌ Container push failed for '$FULL_IMAGE_NAME'."
  exit 1
fi

VERSIONED_IMAGE_NAME="docker.io/$DOCKER_HUB_USERNAME/deep-research-agent:$BUILD_VERSION"
echo "🏷️  Tagging versioned image: $VERSIONED_IMAGE_NAME"
container_runtime_tag "$FULL_IMAGE_NAME" "$VERSIONED_IMAGE_NAME"
echo "🚀 Pushing versioned image..."
container_runtime_push "$VERSIONED_IMAGE_NAME"
if [ $? -ne 0 ]; then
  echo "❌ Container push failed for '$VERSIONED_IMAGE_NAME'."
  exit 1
fi
echo "✅ Image built and pushed successfully"
verify_build_owned_files_unchanged
MARKER_TEMP=$(mktemp "$SCRIPT_DIR/.build_version.tmp.XXXXXX")
chmod 600 "$MARKER_TEMP"
printf '%s\n' "$BUILD_VERSION" >"$MARKER_TEMP"
mv -f "$MARKER_TEMP" "$SCRIPT_DIR/.build_version"
MARKER_TEMP=""
BUILD_TRANSACTION_ARMED=false
end_step

print_timing_summary
