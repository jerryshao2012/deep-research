#!/bin/bash
set -e
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Timer tracking
TOTAL_START_TIME=$(date +%s)
STEP_TIMES=()

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
  echo "⏱️  Deployment Timing Summary"
  echo "═══════════════════════════════════════════════════════"
  for timing in "${STEP_TIMES[@]}"; do
    echo "   • $timing"
  done
  echo "───────────────────────────────────────────────────────"
  echo "   Total deployment time: ${TOTAL_DURATION}s"
  echo "═══════════════════════════════════════════════════════"
}

normalize_azure_tsv_array() {
  local row="$1"
  if [[ "$row" == *$'\n'* ]]; then
    [[ "$row" != *$'\t'* ]] || return 1
    row="${row//$'\n'/$'\t'}"
  fi
  printf '%s' "$row"
}

print_usage() {
  echo "Usage: ./deploy.sh [--oauth-redirects-confirmed] [--help]"
  echo ""
  echo "Update existing deployment for managed passkey cutover."
  echo ""
  echo "Prerequisites (this script does not bootstrap them):"
  echo "  - Existing resource group and Container Apps environment"
  echo "  - Existing backend Container App"
  echo "  - Existing user-assigned managed identity assigned to the backend app"
  echo "  - Existing Key Vault with identity secret get access"
  echo "  - Pre-created Key Vault secret PASSKEY-PROXY-SECRET"
  echo "  - Existing provider/runtime secrets referenced by the app configuration"
  echo "  - Existing storage account, Blob container, Azure Files share, and Container Apps environment storage"
  echo "  - Successful build producing .build_version and Docker Hub image"
  echo "  - Confirmed Google/GitHub OAuth URLs when endpoint metadata changes"
  echo ""
  echo "Options:"
  echo "  --oauth-redirects-confirmed  Confirm provider URLs for changed endpoints"
  echo "  --help, -h                   Show this help message"
  echo ""
  echo "Examples:"
  echo "  ./deploy.sh --oauth-redirects-confirmed"
  echo "  OAUTH_REDIRECTS_CONFIRMED=true ./deploy.sh"
  echo ""
  echo "Note: For bi-directional file sync with Azure File Share, use:"
  echo "  ./sync-files.sh"
  echo "Note: To build the image, run:"
  echo "  ./build.sh"
}

# Parse command-line arguments
unset CLI_OAUTH_REDIRECTS_CONFIRMED CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN \
  DEPLOY_ORIGINAL_ARGUMENT_COUNT
CLI_OAUTH_REDIRECTS_CONFIRMED=false
CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN=false
DEPLOY_ORIGINAL_ARGUMENT_COUNT="$#"
while [[ $# -gt 0 ]]; do
  case $1 in
    --help|-h)
      if [[ "$DEPLOY_ORIGINAL_ARGUMENT_COUNT" -ne 1 ]]; then
        echo "Error: --help must be used alone." >&2
        exit 64
      fi
      print_usage
      exit 0
      ;;
    --oauth-redirects-confirmed)
      if [[ "$CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN" == true ]]; then
        echo "Error: --oauth-redirects-confirmed may be supplied only once." >&2
        exit 64
      fi
      CLI_OAUTH_REDIRECTS_CONFIRMED=true
      CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN=true
      shift
      ;;
    *)
      echo "Error: unknown argument '$1'." >&2
      exit 64
      ;;
  esac
done

# Configuration
unset CALLER_OAUTH_REDIRECTS_CONFIRMED
CALLER_OAUTH_REDIRECTS_CONFIRMED="${OAUTH_REDIRECTS_CONFIRMED-}"
unset OAUTH_REDIRECTS_CONFIRMED

unset ENV_CONFIG_OUTPUT CONFIG_LINE_COUNT config_line
if ENV_CONFIG_OUTPUT=$(BASH_ENV=/dev/null ENV=/dev/null \
  OAUTH_REDIRECTS_CONFIRMED= CALLER_OAUTH_REDIRECTS_CONFIRMED= \
  CLI_OAUTH_REDIRECTS_CONFIRMED= CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN= \
  DEPLOY_ORIGINAL_ARGUMENT_COUNT= \
  /bin/bash --noprofile --norc -c '
set +x
if source "$1" >/dev/null 2>/dev/null; then
  :
else
  exit $?
fi
set +x
builtin trap - EXIT ERR DEBUG RETURN
builtin printf "SEED=%s\n" "${SEED-}"
builtin printf "AZURE_SUBSCRIPTION_ID=%s\n" "${AZURE_SUBSCRIPTION_ID-}"
builtin printf "RESOURCE_GROUP=%s\n" "${RESOURCE_GROUP-}"
builtin printf "LOCATION=%s\n" "${LOCATION-}"
builtin printf "ENV_NAME=%s\n" "${ENV_NAME-}"
builtin printf "AGENT_NAME=%s\n" "${AGENT_NAME-}"
builtin printf "BACKEND_APP_NAME=%s\n" "${BACKEND_APP_NAME-}"
builtin printf "UI_APP_NAME=%s\n" "${UI_APP_NAME-}"
builtin printf "KV_NAME=%s\n" "${KV_NAME-}"
builtin printf "STORAGE_ACCOUNT_NAME=%s\n" "${STORAGE_ACCOUNT_NAME-}"
' deploy-env "$SCRIPT_DIR/env.sh" 2>/dev/null); then
  :
else
  status=$?
  echo "Error: env.sh configuration failed." >&2
  exit "$status"
fi

CONFIG_LINE_COUNT=0
while IFS= read -r config_line || [[ -n "$config_line" ]]; do
  CONFIG_LINE_COUNT=$((CONFIG_LINE_COUNT + 1))
  case "$CONFIG_LINE_COUNT:$config_line" in
    1:SEED=*) SEED="${config_line#SEED=}" ;;
    2:AZURE_SUBSCRIPTION_ID=*) AZURE_SUBSCRIPTION_ID="${config_line#AZURE_SUBSCRIPTION_ID=}" ;;
    3:RESOURCE_GROUP=*) RESOURCE_GROUP="${config_line#RESOURCE_GROUP=}" ;;
    4:LOCATION=*) LOCATION="${config_line#LOCATION=}" ;;
    5:ENV_NAME=*) ENV_NAME="${config_line#ENV_NAME=}" ;;
    6:AGENT_NAME=*) AGENT_NAME="${config_line#AGENT_NAME=}" ;;
    7:BACKEND_APP_NAME=*) BACKEND_APP_NAME="${config_line#BACKEND_APP_NAME=}" ;;
    8:UI_APP_NAME=*) UI_APP_NAME="${config_line#UI_APP_NAME=}" ;;
    9:KV_NAME=*) KV_NAME="${config_line#KV_NAME=}" ;;
    10:STORAGE_ACCOUNT_NAME=*) STORAGE_ACCOUNT_NAME="${config_line#STORAGE_ACCOUNT_NAME=}" ;;
    *)
      echo "Error: env.sh returned invalid configuration." >&2
      exit 65
      ;;
  esac
done <<< "$ENV_CONFIG_OUTPUT"
if [[ "$CONFIG_LINE_COUNT" -ne 10 ]]; then
  echo "Error: env.sh returned incomplete configuration." >&2
  exit 65
fi
export SEED AZURE_SUBSCRIPTION_ID RESOURCE_GROUP LOCATION ENV_NAME AGENT_NAME \
  BACKEND_APP_NAME UI_APP_NAME KV_NAME STORAGE_ACCOUNT_NAME
unset ENV_CONFIG_OUTPUT CONFIG_LINE_COUNT config_line

if [[ "$CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN" == true ]]; then
  OAUTH_REDIRECTS_CONFIRMED="$CLI_OAUTH_REDIRECTS_CONFIRMED"
else
  OAUTH_REDIRECTS_CONFIRMED="$CALLER_OAUTH_REDIRECTS_CONFIRMED"
fi
unset CALLER_OAUTH_REDIRECTS_CONFIRMED CLI_OAUTH_REDIRECTS_CONFIRMED \
  CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN DEPLOY_ORIGINAL_ARGUMENT_COUNT

: "${KV_NAME:?Set KV_NAME in env.sh}"
: "${STORAGE_ACCOUNT_NAME:?Set STORAGE_ACCOUNT_NAME in env.sh}"
: "${BACKEND_APP_NAME:?Set BACKEND_APP_NAME in env.sh}"
: "${UI_APP_NAME:?Set UI_APP_NAME in env.sh}"

if [ -z "${DOCKER_HUB_USERNAME:-}" ] && [ -f "$SCRIPT_DIR/.env" ]; then
  DOCKER_HUB_USERNAME=$(python3 "$SCRIPT_DIR/scripts/load_docker_credentials.py" --input "$SCRIPT_DIR/.env" --username)
fi

python3 "$SCRIPT_DIR/scripts/sanitize_passkey_dotenv.py" --input "$SCRIPT_DIR/.env.docker" --check

RESOLVER_STDOUT="$(mktemp)"
RESOLVER_STDERR="$(mktemp)"
set +e
BACKEND_APP_NAME="$BACKEND_APP_NAME" UI_APP_NAME="$UI_APP_NAME" \
  "$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh" >"$RESOLVER_STDOUT" 2>"$RESOLVER_STDERR"
RESOLVER_STATUS=$?
set -e
cat "$RESOLVER_STDERR" >&2
if [ "$RESOLVER_STATUS" -ne 0 ]; then
  cat "$RESOLVER_STDOUT"
  if [[ "$RESOLVER_STATUS" == 3 ]]; then
    echo "Error: existing Container Apps environment '$ENV_NAME' in resource group '$RESOURCE_GROUP' is required and must be readable" >&2
  fi
  rm -f "$RESOLVER_STDOUT" "$RESOLVER_STDERR"
  exit "$RESOLVER_STATUS"
fi
RESOLVER_OUTPUT="$(cat "$RESOLVER_STDOUT")"
rm -f "$RESOLVER_STDOUT" "$RESOLVER_STDERR"

RESOLVED_AZURE_ENVIRONMENT_ID=""
RESOLVED_AZURE_ENVIRONMENT_DEFAULT_DOMAIN=""
RESOLVED_BACKEND_APP_NAME=""
RESOLVED_UI_APP_NAME=""
RESOLVED_BACKEND_URL=""
RESOLVED_AZURE_UI_URL=""
RESOLVED_FRONTEND_URLS=""
RESOLVED_GOOGLE_CALLBACK_URL=""
RESOLVED_GITHUB_CALLBACK_URL=""
RESOLVED_GITHUB_HOMEPAGE_URL=""
RESOLVED_CHANGED=""
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
    AZURE_ENVIRONMENT_ID) RESOLVED_AZURE_ENVIRONMENT_ID="$value" ;;
    AZURE_ENVIRONMENT_DEFAULT_DOMAIN) RESOLVED_AZURE_ENVIRONMENT_DEFAULT_DOMAIN="$value" ;;
    BACKEND_APP_NAME) RESOLVED_BACKEND_APP_NAME="$value" ;;
    UI_APP_NAME) RESOLVED_UI_APP_NAME="$value" ;;
    BACKEND_URL) RESOLVED_BACKEND_URL="$value" ;;
    AZURE_UI_URL) RESOLVED_AZURE_UI_URL="$value" ;;
    FRONTEND_URLS) RESOLVED_FRONTEND_URLS="$value" ;;
    GOOGLE_CALLBACK_URL) RESOLVED_GOOGLE_CALLBACK_URL="$value" ;;
    GITHUB_CALLBACK_URL) RESOLVED_GITHUB_CALLBACK_URL="$value" ;;
    GITHUB_HOMEPAGE_URL) RESOLVED_GITHUB_HOMEPAGE_URL="$value" ;;
    CHANGED) RESOLVED_CHANGED="$value" ;;
    *)
      echo "Error: unexpected resolver output key: $key" >&2
      exit 65
      ;;
  esac
  SEEN_RESOLVER_KEYS="${SEEN_RESOLVER_KEYS}${key}|"
done <<< "$RESOLVER_OUTPUT"
for key in RESOLVED_AZURE_ENVIRONMENT_ID RESOLVED_AZURE_ENVIRONMENT_DEFAULT_DOMAIN RESOLVED_BACKEND_APP_NAME RESOLVED_UI_APP_NAME RESOLVED_BACKEND_URL RESOLVED_AZURE_UI_URL RESOLVED_FRONTEND_URLS RESOLVED_GOOGLE_CALLBACK_URL RESOLVED_GITHUB_CALLBACK_URL RESOLVED_GITHUB_HOMEPAGE_URL RESOLVED_CHANGED; do
  if [[ -z "${!key}" ]]; then
    echo "Error: missing resolver output key: ${key#RESOLVED_}" >&2
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
FRONTEND_URLS="$RESOLVED_FRONTEND_URLS"
if [[ "$RESOLVED_CHANGED" == true && "${OAUTH_REDIRECTS_CONFIRMED:-}" != true ]]; then
  echo "Deployment blocked: set OAUTH_REDIRECTS_CONFIRMED=true for this process after updating the exact OAuth URLs above." >&2
  exit 3
fi

RESOURCE_GROUP_STDERR=$(mktemp)
set +e
RESOURCE_GROUP_ID=$(az group show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$RESOURCE_GROUP" --query id -o tsv 2>"$RESOURCE_GROUP_STDERR")
RESOURCE_GROUP_STATUS=$?
set -e
rm -f "$RESOURCE_GROUP_STDERR"
if [[ "$RESOURCE_GROUP_STATUS" != 0 ]]; then
  echo "Error: existing resource group '$RESOURCE_GROUP' is required and must be readable" >&2
  exit "$RESOURCE_GROUP_STATUS"
fi
if [[ -z "${RESOURCE_GROUP_ID//[[:space:]]/}" ]]; then
  echo "Error: existing resource group '$RESOURCE_GROUP' returned an empty resource ID" >&2
  exit 65
fi

USER_IDENTITY_NAME="${AGENT_NAME}-identity"
IDENTITY_STDERR=$(mktemp)
set +e
IDENTITY_ROW=$(az identity show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$USER_IDENTITY_NAME" --resource-group "$RESOURCE_GROUP" --query '[id,principalId]' -o tsv 2>"$IDENTITY_STDERR")
IDENTITY_STATUS=$?
set -e
rm -f "$IDENTITY_STDERR"
if [[ "$IDENTITY_STATUS" != 0 ]]; then
  echo "Error: existing user-assigned managed identity '$USER_IDENTITY_NAME' is required before managed passkey deployment" >&2
  exit "$IDENTITY_STATUS"
fi
if ! IDENTITY_ROW=$(normalize_azure_tsv_array "$IDENTITY_ROW"); then
  echo "Error: existing backend user-assigned identity returned an invalid response" >&2
  exit 65
fi
IDENTITY_REMAINDER="${IDENTITY_ROW#*$'\t'}"
if [[ "$IDENTITY_ROW" != *$'\t'* || "$IDENTITY_REMAINDER" == *$'\t'* ]]; then
  echo "Error: existing backend user-assigned identity returned an invalid response" >&2
  exit 65
fi
USER_IDENTITY_ID="${IDENTITY_ROW%%$'\t'*}"
USER_IDENTITY_PRINCIPAL_ID="$IDENTITY_REMAINDER"
if [[ -z "$USER_IDENTITY_ID" || -z "$USER_IDENTITY_PRINCIPAL_ID" ]]; then
  echo "Error: existing backend user-assigned identity is incomplete" >&2
  exit 65
fi

EXISTING_CONFIG_JSON=$(mktemp /tmp/existing-config-XXXXXX.json 2>/dev/null || mktemp)
APP_CONFIG_STDERR=$(mktemp)
set +e
az containerapp show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$AGENT_NAME" --resource-group "$RESOURCE_GROUP" --output json >"$EXISTING_CONFIG_JSON" 2>"$APP_CONFIG_STDERR"
APP_CONFIG_STATUS=$?
set -e
if [[ "$APP_CONFIG_STATUS" != 0 ]]; then
  if [[ "$APP_CONFIG_STATUS" == 3 ]]; then
    echo "Error: existing Container App '$AGENT_NAME' is required before managed passkey deployment" >&2
  else
  echo "Error: Container App configuration query failed (status $APP_CONFIG_STATUS); response suppressed" >&2
  fi
  rm -f "$EXISTING_CONFIG_JSON" "$APP_CONFIG_STDERR"
  exit "$APP_CONFIG_STATUS"
fi
rm -f "$APP_CONFIG_STDERR"

set +e
python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    config = json.load(stream)
identities = config.get("identity", {}).get("userAssignedIdentities", {})
if not isinstance(identities, dict):
    raise SystemExit(2)
expected = sys.argv[2].casefold()
raise SystemExit(0 if any(str(key).casefold() == expected for key in identities) else 3)
' "$EXISTING_CONFIG_JSON" "$USER_IDENTITY_ID"
IDENTITY_ASSIGNMENT_STATUS=$?
set -e
if [[ "$IDENTITY_ASSIGNMENT_STATUS" != 0 ]]; then
  echo "Error: required user-assigned managed identity '$USER_IDENTITY_NAME' must already be assigned to the existing Container App '$AGENT_NAME'" >&2
  rm -f "$EXISTING_CONFIG_JSON"
  exit 4
fi

set +e
MANAGED_CONTAINER_NAME=$(python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    config = json.load(stream)
containers = config.get("properties", {}).get("template", {}).get("containers")
if not isinstance(containers, list) or len(containers) != 1:
    raise SystemExit(2)
name = containers[0].get("name") if isinstance(containers[0], dict) else None
if not isinstance(name, str) or not name:
    raise SystemExit(2)
print(name)
' "$EXISTING_CONFIG_JSON")
CONTAINER_TOPOLOGY_STATUS=$?
set -e
if [[ "$CONTAINER_TOPOLOGY_STATUS" != 0 || -z "$MANAGED_CONTAINER_NAME" ]]; then
  echo "Error: existing backend Container App must contain exactly one named application container" >&2
  rm -f "$EXISTING_CONFIG_JSON"
  exit 4
fi

validate_existing_app_metadata() {
APP_METADATA_STDOUT=$(mktemp)
APP_METADATA_STDERR=$(mktemp)
set +e
az containerapp secret list --subscription "$AZURE_SUBSCRIPTION_ID" --name "$AGENT_NAME" --resource-group "$RESOURCE_GROUP" --query '[].{name:name,keyVaultUrl:keyVaultUrl,identity:identity}' --output json >"$APP_METADATA_STDOUT" 2>"$APP_METADATA_STDERR"
APP_METADATA_STATUS=$?
set -e
rm -f "$APP_METADATA_STDERR"
if [[ "$APP_METADATA_STATUS" != 0 ]]; then
  echo "Error: existing Container App secret-reference metadata is required and must be readable" >&2
  rm -f "$APP_METADATA_STDOUT" "$EXISTING_CONFIG_JSON"
  exit "$APP_METADATA_STATUS"
fi
set +e
python3 - "$APP_METADATA_STDOUT" "$EXISTING_CONFIG_JSON" "$USER_IDENTITY_ID" "$KV_NAME" "${DOCKER_HUB_USERNAME:-}" <<'PY'
import json
import sys

secret_path, app_path, identity, vault, username = sys.argv[1:]
with open(secret_path, encoding="utf-8") as stream:
    secrets = json.load(stream)
with open(app_path, encoding="utf-8") as stream:
    app = json.load(stream)
required = {
    "tavily-api-key": "TAVILY-API-KEY",
    "langchain-api-key": "LANGCHAIN-API-KEY",
    "upload-api-key": "UPLOAD-API-KEY",
    "storage-account-name": "STORAGE-ACCOUNT-NAME",
    "storage-account-key": "STORAGE-ACCOUNT-KEY",
    "azure-storage-container-name": "AZURE-STORAGE-CONTAINER-NAME",
    "google-api-key": "GOOGLE-API-KEY",
    "docker-hub-pat": "DOCKER-HUB-PAT",
    "passkey-proxy-secret": "PASSKEY-PROXY-SECRET",
}
if not isinstance(secrets, list):
    raise SystemExit(2)
by_name = {}
for item in secrets:
    if not isinstance(item, dict) or set(item) != {"name", "keyVaultUrl", "identity"}:
        raise SystemExit(2)
    name = item.get("name")
    if not isinstance(name, str) or name in by_name:
        raise SystemExit(2)
    by_name[name] = item
for name, vault_name in required.items():
    expected = {
        "name": name,
        "keyVaultUrl": f"https://{vault}.vault.azure.net/secrets/{vault_name}",
        "identity": identity,
    }
    if by_name.get(name) != expected:
        raise SystemExit(3)
configuration = app.get("properties", {}).get("configuration", {})
registries = configuration.get("registries")
if not isinstance(registries, list):
    raise SystemExit(2)
matching = [
    item for item in registries
    if isinstance(item, dict) and str(item.get("server", "")).casefold() == "docker.io"
]
expected_registry = {
    "server": "docker.io",
    "username": username,
    "passwordSecretRef": "docker-hub-pat",
}
if len(matching) != 1 or matching[0] != expected_registry:
    raise SystemExit(4)
PY
APP_METADATA_VALIDATE_STATUS=$?
set -e
rm -f "$APP_METADATA_STDOUT"
if [[ "$APP_METADATA_VALIDATE_STATUS" != 0 ]]; then
  echo "Error: existing Container App secret references or Docker Hub registry metadata do not match required immutable configuration" >&2
  rm -f "$EXISTING_CONFIG_JSON"
  exit 4
fi
}

if [ -z "$DOCKER_HUB_USERNAME" ]; then
  echo "❌ Error: Please set DOCKER_HUB_USERNAME before running deploy.sh" >&2
  rm -f "$EXISTING_CONFIG_JSON"
  exit 1
fi
if [ ! -f "$SCRIPT_DIR/.build_version" ]; then
  echo "Error: .build_version not found; run ./build.sh first" >&2
  rm -f "$EXISTING_CONFIG_JSON"
  exit 1
fi
BUILD_VERSION=$(cat "$SCRIPT_DIR/.build_version")
SQLITE_ENV_STORAGE_NAME="authsqlite"
RESTART_TRIGGER=$(date +%s)
REVISION_SUFFIX="passkeys-$(date +%Y%m%d%H%M%S)"
DESIRED_CONFIG_YAML=$(mktemp /tmp/desired-config-XXXXXX.yaml 2>/dev/null || mktemp)
UPDATE_PATCH_JSON=$(mktemp /tmp/update-config-XXXXXX.json 2>/dev/null || mktemp)
uv run python "$SCRIPT_DIR/scripts/render_azure_containerapp_config.py" \
  --docker-username "$DOCKER_HUB_USERNAME" \
  --build-version "$BUILD_VERSION" \
  --identity-id "$USER_IDENTITY_ID" \
  --container-name "$MANAGED_CONTAINER_NAME" \
  --key-vault-name "$KV_NAME" \
  --frontend-urls "$FRONTEND_URLS" \
  --storage-name "$SQLITE_ENV_STORAGE_NAME" \
  --restart-trigger "$RESTART_TRIGGER" \
  --revision-suffix "$REVISION_SUFFIX" \
  --output "$DESIRED_CONFIG_YAML"
generate_update_patch() {
uv run python "$SCRIPT_DIR/scripts/merge_azure_containerapp_config.py" \
  --existing-json "$EXISTING_CONFIG_JSON" \
  --desired-yaml "$DESIRED_CONFIG_YAML" \
  --output-yaml "$UPDATE_PATCH_JSON"
rm -f "$DESIRED_CONFIG_YAML"
}

KV_ACCESS_STDERR=$(mktemp)
set +e
KV_ACCESS_ROW=$(az keyvault show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$KV_NAME" --resource-group "$RESOURCE_GROUP" --query "join('|', [id, to_string(properties.enableRbacAuthorization), to_string(length(properties.accessPolicies[?objectId=='$USER_IDENTITY_PRINCIPAL_ID' && (contains(permissions.secrets, 'get') || contains(permissions.secrets, 'all'))]))])" -o tsv 2>"$KV_ACCESS_STDERR")
KV_ACCESS_STATUS=$?
set -e
rm -f "$KV_ACCESS_STDERR"
if [[ "$KV_ACCESS_STATUS" != 0 ]]; then
  echo "Error: existing Key Vault '$KV_NAME' is required before managed passkey deployment" >&2
  exit "$KV_ACCESS_STATUS"
fi
IFS='|' read -r KEY_VAULT_RESOURCE_ID KEY_VAULT_RBAC_ENABLED KEY_VAULT_POLICY_MATCHES <<<"$KV_ACCESS_ROW"
if [[ -z "$KEY_VAULT_RESOURCE_ID" || "$KEY_VAULT_RBAC_ENABLED" != "true" && "$KEY_VAULT_RBAC_ENABLED" != "false" || ! "$KEY_VAULT_POLICY_MATCHES" =~ ^[0-9]+$ ]]; then
  echo "Error: existing Key Vault returned invalid authorization metadata" >&2
  exit 65
fi
if [[ "$KEY_VAULT_RBAC_ENABLED" == "false" ]]; then
  if [[ "$KEY_VAULT_POLICY_MATCHES" == 0 ]]; then
    echo "Error: backend identity lacks Key Vault secret get access under the vault access-policy mode" >&2
    exit 4
  fi
else
  ROLE_IDS_STDERR=$(mktemp)
  set +e
  ROLE_DEFINITION_IDS=$(az role assignment list --subscription "$AZURE_SUBSCRIPTION_ID" --assignee-object-id "$USER_IDENTITY_PRINCIPAL_ID" --scope "$KEY_VAULT_RESOURCE_ID" --include-inherited --query '[].roleDefinitionId' -o tsv 2>"$ROLE_IDS_STDERR")
  ROLE_IDS_STATUS=$?
  set -e
  rm -f "$ROLE_IDS_STDERR"
  if [[ "$ROLE_IDS_STATUS" != 0 ]]; then
    echo "Error: unable to verify existing Key Vault RBAC access" >&2
    exit "$ROLE_IDS_STATUS"
  fi
  KEY_VAULT_RBAC_ALLOWED=false
  while IFS= read -r ROLE_DEFINITION_ID; do
    [[ -n "$ROLE_DEFINITION_ID" ]] || continue
    ROLE_JSON=$(mktemp)
    set +e
    az role definition list --subscription "$AZURE_SUBSCRIPTION_ID" --name "$ROLE_DEFINITION_ID" -o json >"$ROLE_JSON" 2>/dev/null
    ROLE_DEFINITION_STATUS=$?
    if [[ "$ROLE_DEFINITION_STATUS" == 0 ]]; then
      python3 "$SCRIPT_DIR/scripts/evaluate_keyvault_rbac.py" <"$ROLE_JSON" >/dev/null 2>&1
      ROLE_ACCESS_STATUS=$?
    else
      ROLE_ACCESS_STATUS=2
    fi
    set -e
    rm -f "$ROLE_JSON"
    if [[ "$ROLE_DEFINITION_STATUS" != 0 || "$ROLE_ACCESS_STATUS" == 2 ]]; then
      echo "Error: unable to validate existing Key Vault RBAC role definition" >&2
      if [[ "$ROLE_DEFINITION_STATUS" != 0 ]]; then
        exit "$ROLE_DEFINITION_STATUS"
      fi
      exit 65
    fi
    if [[ "$ROLE_ACCESS_STATUS" == 0 ]]; then
      KEY_VAULT_RBAC_ALLOWED=true
    fi
  done <<<"$ROLE_DEFINITION_IDS"
  if [[ "$KEY_VAULT_RBAC_ALLOWED" != true ]]; then
    echo "Error: backend identity lacks effective Key Vault secret read data access" >&2
    exit 4
  fi
fi

REQUIRED_KEY_VAULT_SECRETS=(
  TAVILY-API-KEY
  LANGCHAIN-API-KEY
  UPLOAD-API-KEY
  STORAGE-ACCOUNT-NAME
  STORAGE-ACCOUNT-KEY
  AZURE-STORAGE-CONTAINER-NAME
  GOOGLE-API-KEY
  DOCKER-HUB-PAT
  PASSKEY-PROXY-SECRET
)
for REQUIRED_SECRET_NAME in "${REQUIRED_KEY_VAULT_SECRETS[@]}"; do
  SECRET_VERSIONS_JSON=$(mktemp)
  SECRET_VERSIONS_STDERR=$(mktemp)
  set +e
  az keyvault secret list-versions --subscription "$AZURE_SUBSCRIPTION_ID" --vault-name "$KV_NAME" --name "$REQUIRED_SECRET_NAME" --query '[].{id:id,name:name,version:version,enabled:attributes.enabled}' --output json >"$SECRET_VERSIONS_JSON" 2>"$SECRET_VERSIONS_STDERR"
  SECRET_VERSIONS_STATUS=$?
  set -e
  rm -f "$SECRET_VERSIONS_STDERR"
  if [[ "$SECRET_VERSIONS_STATUS" != 0 ]]; then
    echo "Error: required pre-created Key Vault secret '$REQUIRED_SECRET_NAME' is missing or unreadable" >&2
    rm -f "$SECRET_VERSIONS_JSON" "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
    exit "$SECRET_VERSIONS_STATUS"
  fi
  set +e
  python3 "$SCRIPT_DIR/scripts/validate_keyvault_secret_versions.py" "$SECRET_VERSIONS_JSON" "$KV_NAME" "$REQUIRED_SECRET_NAME"
  SECRET_VERSIONS_VALIDATION_STATUS=$?
  set -e
  rm -f "$SECRET_VERSIONS_JSON"
  if [[ "$SECRET_VERSIONS_VALIDATION_STATUS" != 0 ]]; then
    echo "Error: required pre-created Key Vault secret '$REQUIRED_SECRET_NAME' has invalid version metadata" >&2
    rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
    exit "$SECRET_VERSIONS_VALIDATION_STATUS"
  fi
done
unset REQUIRED_SECRET_NAME SECRET_VERSIONS_STATUS SECRET_VERSIONS_VALIDATION_STATUS

BLOB_CONTAINER_NAME="deep-research-blobs"
STORAGE_FILE_SHARE_NAME="deep-research-auth"

STORAGE_ACCOUNT_STDERR=$(mktemp)
set +e
STORAGE_ACCOUNT_ID=$(az storage account show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$STORAGE_ACCOUNT_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv 2>"$STORAGE_ACCOUNT_STDERR")
STORAGE_ACCOUNT_STATUS=$?
set -e
rm -f "$STORAGE_ACCOUNT_STDERR"
if [[ "$STORAGE_ACCOUNT_STATUS" != 0 || -z "${STORAGE_ACCOUNT_ID//[[:space:]]/}" ]]; then
  echo "Error: existing storage account '$STORAGE_ACCOUNT_NAME' is required and must be readable" >&2
  rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  if [[ "$STORAGE_ACCOUNT_STATUS" != 0 ]]; then exit "$STORAGE_ACCOUNT_STATUS"; else exit 65; fi
fi

BLOB_CONTAINER_STDERR=$(mktemp)
set +e
BLOB_CONTAINER_RESULT=$(az storage container-rm show --subscription "$AZURE_SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" --storage-account "$STORAGE_ACCOUNT_NAME" --name "$BLOB_CONTAINER_NAME" --query name -o tsv 2>"$BLOB_CONTAINER_STDERR")
BLOB_CONTAINER_STATUS=$?
set -e
rm -f "$BLOB_CONTAINER_STDERR"
if [[ "$BLOB_CONTAINER_STATUS" != 0 || "$BLOB_CONTAINER_RESULT" != "$BLOB_CONTAINER_NAME" ]]; then
  echo "Error: existing Blob container '$BLOB_CONTAINER_NAME' in storage account '$STORAGE_ACCOUNT_NAME' is required and must be readable" >&2
  rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  if [[ "$BLOB_CONTAINER_STATUS" != 0 ]]; then exit "$BLOB_CONTAINER_STATUS"; else exit 65; fi
fi

FILE_SHARE_STDERR=$(mktemp)
set +e
FILE_SHARE_RESULT=$(az storage share-rm show --subscription "$AZURE_SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" --storage-account "$STORAGE_ACCOUNT_NAME" --name "$STORAGE_FILE_SHARE_NAME" --query name -o tsv 2>"$FILE_SHARE_STDERR")
FILE_SHARE_STATUS=$?
set -e
rm -f "$FILE_SHARE_STDERR"
if [[ "$FILE_SHARE_STATUS" != 0 || "$FILE_SHARE_RESULT" != "$STORAGE_FILE_SHARE_NAME" ]]; then
  echo "Error: existing Azure Files share '$STORAGE_FILE_SHARE_NAME' is required and must be readable" >&2
  rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  if [[ "$FILE_SHARE_STATUS" != 0 ]]; then exit "$FILE_SHARE_STATUS"; else exit 65; fi
fi

ENV_STORAGE_STDERR=$(mktemp)
set +e
ENV_STORAGE_RESULT=$(az containerapp env storage show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$ENV_NAME" --resource-group "$RESOURCE_GROUP" --storage-name "$SQLITE_ENV_STORAGE_NAME" --query '[name,properties.azureFile.accountName,properties.azureFile.shareName,properties.azureFile.accessMode]' -o tsv 2>"$ENV_STORAGE_STDERR")
ENV_STORAGE_STATUS=$?
set -e
rm -f "$ENV_STORAGE_STDERR"
if [[ "$ENV_STORAGE_STATUS" != 0 ]]; then
  echo "Error: existing Container Apps environment storage '$SQLITE_ENV_STORAGE_NAME' is required and must be readable" >&2
  rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  exit "$ENV_STORAGE_STATUS"
fi
if ! ENV_STORAGE_RESULT=$(normalize_azure_tsv_array "$ENV_STORAGE_RESULT"); then
  echo "Error: Container Apps environment storage '$SQLITE_ENV_STORAGE_NAME' returned an invalid response" >&2
  rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  exit 65
fi
EXPECTED_ENV_STORAGE_RESULT="${SQLITE_ENV_STORAGE_NAME}"$'\t'"${STORAGE_ACCOUNT_NAME}"$'\t'"${STORAGE_FILE_SHARE_NAME}"$'\t'"ReadWrite"
if [[ "$ENV_STORAGE_RESULT" != "$EXPECTED_ENV_STORAGE_RESULT" ]]; then
  echo "Error: Container Apps environment storage '$SQLITE_ENV_STORAGE_NAME' does not match required Azure Files binding" >&2
  rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  exit 65
fi

generate_update_patch
validate_existing_app_metadata

echo "🚀 Starting Deep Research Agent deployment (using existing image)..."

echo "✅ Using Docker Hub user: $DOCKER_HUB_USERNAME"

# 1. Set Azure Subscription
start_step "Set Azure Subscription"
: "${AZURE_SUBSCRIPTION_ID:?Set AZURE_SUBSCRIPTION_ID in env.sh}"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
echo "✅ Subscription set to $AZURE_SUBSCRIPTION_ID"
end_step

# 2. Verify image exists in ACR
start_step "Verify Container Image"
# No direct ACR check for Docker Hub image in bash
echo "✅ Verified image exists in ACR"

NEW_VERSION=$(grep -E 'API_VERSION(:\s*\w+)?\s*=\s*' webapp/config.py | grep -o '"[^"]*"')
NEW_VERSION=${NEW_VERSION//\"/}
echo "ℹ️  Current API version: $NEW_VERSION"
end_step

# 6. Deploy or update agent
start_step "Container App Deployment"
echo "🚀 Deploying agent..."

# Unify identity management
echo "🔐 Using existing User-Assigned Managed Identity '$USER_IDENTITY_NAME'."

echo "✅ Skipping ACR permissions since we use Docker Hub"

echo "📝 Updating required existing Container App '$AGENT_NAME'."

echo "⏳ Waiting for any active provisioning operations to complete..."
for i in {1..60}; do
  STATE=$(az containerapp show --name $AGENT_NAME --resource-group $RESOURCE_GROUP --query properties.provisioningState -o tsv 2>/dev/null || echo "Unknown")
  if [[ "$STATE" == "Succeeded" || "$STATE" == "Failed" || "$STATE" == "Canceled" ]]; then
    echo "✅ Provisioning state: $STATE"
    break
  fi
  echo "   Current state: $STATE... waiting 5s ($i/60)"
  sleep 5
done

echo "⚙️  Applying comprehensive configuration update..."
#      - name: azure-openai-endpoint
#        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/AZURE-OPENAI-ENDPOINT
#        identity: ${USER_IDENTITY_ID}
#      - name: azure-openai-deployment
#        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/AZURE-OPENAI-DEPLOYMENT
#        identity: ${USER_IDENTITY_ID}
#      - name: azure-openai-api-key
#        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/AZURE-OPENAI-API-KEY
#        identity: ${USER_IDENTITY_ID}

#          - name: AZURE_OPENAI_ENDPOINT
#            secretRef: azure-openai-endpoint
#          - name: AZURE_OPENAI_DEPLOYMENT
#            secretRef: azure-openai-deployment
#          - name: AZURE_OPENAI_API_KEY
#            secretRef: azure-openai-api-key
APP_RESOURCE_ID=$(python3 -c 'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8")).get("id"); print(value) if isinstance(value,str) and value.startswith("/") else sys.exit(2)' "$EXISTING_CONFIG_JSON")
CURRENT_TEMPLATE_JSON=$(mktemp)
CURRENT_TEMPLATE_STDERR=$(mktemp)
set +e
az containerapp show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$AGENT_NAME" --resource-group "$RESOURCE_GROUP" --query properties.template --output json >"$CURRENT_TEMPLATE_JSON" 2>"$CURRENT_TEMPLATE_STDERR"
CURRENT_TEMPLATE_STATUS=$?
set -e
rm -f "$CURRENT_TEMPLATE_STDERR"
if [[ "$CURRENT_TEMPLATE_STATUS" != 0 ]]; then
  echo "Error: final Container App template query failed (status $CURRENT_TEMPLATE_STATUS); response suppressed" >&2
  rm -f "$CURRENT_TEMPLATE_JSON" "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  exit "$CURRENT_TEMPLATE_STATUS"
fi
set +e
python3 - "$EXISTING_CONFIG_JSON" "$CURRENT_TEMPLATE_JSON" <<'PY'
import json
import sys

def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

def reject_constant(_value):
    raise ValueError("non-finite JSON number")

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        initial_app = json.load(
            stream, object_pairs_hook=strict_object, parse_constant=reject_constant
        )
    with open(sys.argv[2], encoding="utf-8") as stream:
        current_template = json.load(
            stream, object_pairs_hook=strict_object, parse_constant=reject_constant
        )
except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
    raise SystemExit(2)
initial_template = initial_app.get("properties", {}).get("template")
if not isinstance(initial_template, dict) or not isinstance(current_template, dict):
    raise SystemExit(2)
canonical_initial = json.dumps(initial_template, sort_keys=True, separators=(",", ":"))
canonical_current = json.dumps(current_template, sort_keys=True, separators=(",", ":"))
raise SystemExit(0 if canonical_initial == canonical_current else 3)
PY
TEMPLATE_COMPARE_STATUS=$?
set -e
rm -f "$CURRENT_TEMPLATE_JSON"
if [[ "$TEMPLATE_COMPARE_STATUS" == 2 ]]; then
  echo "Error: final Container App template query returned invalid metadata" >&2
  rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  exit 65
fi
if [[ "$TEMPLATE_COMPARE_STATUS" != 0 ]]; then
  echo "Error: concurrent Container App template change detected; refusing to patch stale template" >&2
  rm -f "$EXISTING_CONFIG_JSON" "$UPDATE_PATCH_JSON"
  exit 70
fi
rm -f "$EXISTING_CONFIG_JSON"
az rest --method patch \
  --uri "${APP_RESOURCE_ID}?api-version=2025-07-01" \
  --headers Content-Type=application/merge-patch+json \
  --body "@$UPDATE_PATCH_JSON" \
  --output none
rm -f "$UPDATE_PATCH_JSON"
REVISION_READY=false
for i in {1..60}; do
  REVISION_STATE=$(az containerapp revision list --name "$AGENT_NAME" --resource-group "$RESOURCE_GROUP" --query "[?name=='${AGENT_NAME}--${REVISION_SUFFIX}'] | [0].[properties.runningState,properties.healthState]" -o tsv)
  if ! REVISION_STATE=$(normalize_azure_tsv_array "$REVISION_STATE"); then
    echo "Error: Container App revision health query returned an invalid response" >&2
    exit 65
  fi
  if [[ "$REVISION_STATE" == $'Running\tHealthy' || "$REVISION_STATE" == $'RunningAtMaxScale\tHealthy' ]]; then
    REVISION_READY=true
    break
  fi
  if [[ "$REVISION_STATE" == Failed* || "$REVISION_STATE" == *$'\tUnhealthy' ]]; then
    break
  fi
  sleep 5
done
if [[ "$REVISION_READY" != true ]]; then
  echo "❌ Revision ${AGENT_NAME}--${REVISION_SUFFIX} did not become Running and Healthy." >&2
  exit 1
fi
echo "✅ Container App configured successfully."
end_step

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ Deployment Complete!"
echo "═══════════════════════════════════════════════════════"

EXTERNAL_URL=$(az containerapp show --name $AGENT_NAME --resource-group $RESOURCE_GROUP --query properties.configuration.ingress.fqdn -o tsv)
echo "🌐 Agent URL: https://$EXTERNAL_URL"

echo "🏥 Health Check: https://$EXTERNAL_URL/health"
echo ""
echo "📊 Next Steps:"
echo "   • Test API: curl -s https://$EXTERNAL_URL/health"
echo "   • View logs: az containerapp logs show --name $AGENT_NAME --resource-group $RESOURCE_GROUP --tail 50"
echo "   • Monitor: https://portal.azure.com/#@/resource/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/overview"
echo "═══════════════════════════════════════════════════════"

start_step "Health Check Verification"
echo ""
echo "🔍 Testing health endpoint (waiting for container to start)..."
MAX_RETRIES=30
RETRY_INTERVAL=10
VERSION_MATCHED=false
for i in $(seq 1 $MAX_RETRIES); do
  echo -n "   Attempt $i/$MAX_RETRIES... "
  HEALTH_RESPONSE=$(curl -s --max-time 5 "https://$EXTERNAL_URL/health" 2>/dev/null || echo "")
  if [ -z "$HEALTH_RESPONSE" ]; then
    echo "❌ No response (container may still be starting)"
  else
    RESPONSE_VERSION=$(echo "$HEALTH_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('version', ''))" 2>/dev/null || echo "")
    if [ "$RESPONSE_VERSION" = "$NEW_VERSION" ]; then
      echo "✅ Version $RESPONSE_VERSION matched!"
      VERSION_MATCHED=true
      echo ""
      echo "📊 Health Check Response:"
      echo "$HEALTH_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$HEALTH_RESPONSE"
      break
    else
      echo "⚠️  Version mismatch (expected: $NEW_VERSION, got: ${RESPONSE_VERSION:-unknown})"
    fi
  fi
  if [ $i -lt $MAX_RETRIES ]; then
    echo "   Waiting ${RETRY_INTERVAL}s before next attempt..."
    sleep $RETRY_INTERVAL
  fi
done

if [ "$VERSION_MATCHED" = false ]; then
  echo ""
  echo "❌ Container health/version verification failed." >&2
  exit 1
else
  echo ""
  echo "✅ Deployment verified successfully!"
fi
end_step

if FINAL_RESOLVER_OUTPUT=$(BACKEND_APP_NAME="$BACKEND_APP_NAME" UI_APP_NAME="$UI_APP_NAME" \
  "$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh"); then :; else
  status=$?
  echo "Error: final endpoint comparison failed; metadata was not recorded." >&2
  exit "$status"
fi
if [[ "$FINAL_RESOLVER_OUTPUT" != "$RESOLVER_OUTPUT" ]]; then
  echo "Error: endpoint assignments changed during deployment; metadata was not recorded." >&2
  exit 1
fi
RESOLVER_EXPECTED_PATH=$(umask 077; mktemp)
printf '%s\n' "$RESOLVER_OUTPUT" >"$RESOLVER_EXPECTED_PATH"
if FINAL_RESOLVER_OUTPUT=$(BACKEND_APP_NAME="$BACKEND_APP_NAME" UI_APP_NAME="$UI_APP_NAME" \
  "$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh" --record-if-current "$RESOLVER_EXPECTED_PATH"); then
  rm -f "$RESOLVER_EXPECTED_PATH"
else
  status=$?
  rm -f "$RESOLVER_EXPECTED_PATH"
  echo "Error: endpoint metadata recording failed." >&2
  exit "$status"
fi
if [[ "$FINAL_RESOLVER_OUTPUT" != "$RESOLVER_OUTPUT" ]]; then
  echo "Error: endpoint assignments changed while recording metadata." >&2
  exit 1
fi
unset FINAL_RESOLVER_OUTPUT RESOLVER_OUTPUT RESOLVER_EXPECTED_PATH

print_timing_summary
