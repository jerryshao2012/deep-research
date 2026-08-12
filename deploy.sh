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

# Parse command-line arguments
SKIP_KV_ACCESS=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-kv-access)
      SKIP_KV_ACCESS=true
      shift
      ;;
    --help|-h)
      echo "Usage: ./deploy.sh [OPTIONS]"
      echo ""
      echo "Update existing deployment for managed passkey cutover."
      echo ""
      echo "Prerequisites (this script does not bootstrap them):"
      echo "  - Existing resource group and Container Apps environment"
      echo "  - Existing backend Container App"
      echo "  - Existing user-assigned managed identity"
      echo "  - Existing Key Vault with identity secret get access"
      echo "  - Pre-created Key Vault secret PASSKEY-PROXY-SECRET"
      echo "  - Existing provider/runtime secrets referenced by the app configuration"
      echo "  - Successful build producing .build_version and Docker Hub image"
      echo "  - Confirmed Google/GitHub OAuth URLs when endpoint metadata changes"
      echo ""
      echo "Options:"
      echo "  --skip-kv-access Skip Key Vault access policy updates (faster re-deployment)"
      echo "  --help, -h       Show this help message"
      echo ""
      echo "Examples:"
      echo "  OAUTH_REDIRECTS_CONFIRMED=true ./deploy.sh     # Update existing deployment after OAuth confirmation"
      echo "  ./deploy.sh --skip-kv-access                   # Update with current-user KV policy step skipped"
      echo ""
      echo "Note: For bi-directional file sync with Azure File Share, use:"
      echo "  ./sync-files.sh"
      echo "Note: To build the image, run:"
      echo "  ./build.sh"
      exit 0
      ;;
    *)
      echo "❌ Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Configuration
source "$SCRIPT_DIR/env.sh"
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
while IFS='=' read -r key value; do
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
if [[ "$IDENTITY_ROW" != *$'\t'* || "$IDENTITY_ROW" == *$'\n'* ]]; then
  echo "Error: existing backend user-assigned identity returned an invalid response" >&2
  exit 65
fi
USER_IDENTITY_ID="${IDENTITY_ROW%%$'\t'*}"
USER_IDENTITY_PRINCIPAL_ID="${IDENTITY_ROW#*$'\t'}"
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
UPDATE_YAML=$(mktemp /tmp/update-config-XXXXXX.yaml 2>/dev/null || mktemp)
python3 "$SCRIPT_DIR/scripts/render_azure_containerapp_config.py" \
  --docker-username "$DOCKER_HUB_USERNAME" \
  --build-version "$BUILD_VERSION" \
  --identity-id "$USER_IDENTITY_ID" \
  --key-vault-name "$KV_NAME" \
  --frontend-urls "$FRONTEND_URLS" \
  --storage-name "$SQLITE_ENV_STORAGE_NAME" \
  --restart-trigger "$RESTART_TRIGGER" \
  --revision-suffix "$REVISION_SUFFIX" \
  --output "$DESIRED_CONFIG_YAML"
python3 "$SCRIPT_DIR/scripts/merge_azure_containerapp_config.py" \
  --existing-json "$EXISTING_CONFIG_JSON" \
  --desired-yaml "$DESIRED_CONFIG_YAML" \
  --output-yaml "$UPDATE_YAML"
rm -f "$DESIRED_CONFIG_YAML"

KV_ACCESS_STDERR=$(mktemp)
set +e
KV_ACCESS_OBJECT_ID=$(az keyvault show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$KV_NAME" --resource-group "$RESOURCE_GROUP" --query "properties.accessPolicies[?objectId=='$USER_IDENTITY_PRINCIPAL_ID' && contains(permissions.secrets, 'get')].objectId | [0]" -o tsv 2>"$KV_ACCESS_STDERR")
KV_ACCESS_STATUS=$?
set -e
rm -f "$KV_ACCESS_STDERR"
if [[ "$KV_ACCESS_STATUS" != 0 ]]; then
  echo "Error: existing Key Vault '$KV_NAME' is required before managed passkey deployment" >&2
  exit "$KV_ACCESS_STATUS"
fi
if [[ "$KV_ACCESS_OBJECT_ID" != "$USER_IDENTITY_PRINCIPAL_ID" ]]; then
  echo "Error: backend identity lacks Key Vault secret get access" >&2
  exit 4
fi
PASSKEY_SECRET_STDOUT=$(mktemp)
PASSKEY_SECRET_STDERR=$(mktemp)
set +e
az keyvault secret show --subscription "$AZURE_SUBSCRIPTION_ID" --vault-name "$KV_NAME" --name PASSKEY-PROXY-SECRET --query id -o tsv >"$PASSKEY_SECRET_STDOUT" 2>"$PASSKEY_SECRET_STDERR"
PASSKEY_SECRET_STATUS=$?
set -e
if [[ "$PASSKEY_SECRET_STATUS" != 0 ]]; then
  cat "$PASSKEY_SECRET_STDOUT"
  cat "$PASSKEY_SECRET_STDERR" >&2
  rm -f "$PASSKEY_SECRET_STDOUT" "$PASSKEY_SECRET_STDERR" "$EXISTING_CONFIG_JSON"
  exit "$PASSKEY_SECRET_STATUS"
fi
PASSKEY_SECRET_ID=$(cat "$PASSKEY_SECRET_STDOUT")
rm -f "$PASSKEY_SECRET_STDOUT" "$PASSKEY_SECRET_STDERR"
if [[ -z "${PASSKEY_SECRET_ID//[[:space:]]/}" ]]; then
  echo "Error: PASSKEY-PROXY-SECRET id is empty" >&2
  rm -f "$EXISTING_CONFIG_JSON"
  exit 65
fi

echo "🚀 Starting Deep Research Agent deployment (using existing image)..."

echo "✅ Using Docker Hub user: $DOCKER_HUB_USERNAME"

# 1. Set Azure Subscription
start_step "Set Azure Subscription"
: "${AZURE_SUBSCRIPTION_ID:?Set AZURE_SUBSCRIPTION_ID in env.sh}"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
echo "✅ Subscription set to $AZURE_SUBSCRIPTION_ID"
end_step

# 1.5. Resource Group Setup
start_step "Resource Group Setup"
if az group show --name $RESOURCE_GROUP &> /dev/null; then
  echo "✅ Resource group '$RESOURCE_GROUP' already exists. Skipping creation."
else
  echo "✨ Creating Resource group '$RESOURCE_GROUP'..."
  az group create --name $RESOURCE_GROUP --location $LOCATION
fi
end_step

# 2. Verify image exists in ACR
start_step "Verify Container Image"
# No direct ACR check for Docker Hub image in bash
echo "✅ Verified image exists in ACR"

NEW_VERSION=$(grep -E 'API_VERSION(:\s*\w+)?\s*=\s*' webapp/config.py | grep -o '"[^"]*"')
NEW_VERSION=${NEW_VERSION//\"/}
echo "ℹ️  Current API version: $NEW_VERSION"
end_step

# 3. Create environment
start_step "Container Apps Environment Setup"
if az containerapp env show --name $ENV_NAME --resource-group $RESOURCE_GROUP &> /dev/null; then
  echo "✅ Container Apps environment '$ENV_NAME' already exists. Skipping creation."
else
  az containerapp env create \
    --name $ENV_NAME \
    --resource-group $RESOURCE_GROUP \
    --location $LOCATION
fi
end_step

# 4. Create Key Vault and store secrets
start_step "Key Vault Setup & Secrets"
echo "✅ Using required existing Key Vault '$KV_NAME'."

if [ "$SKIP_KV_ACCESS" = false ]; then
  echo "🔑 Ensuring Key Vault access configuration uses Access Policies..."
  az keyvault update --name $KV_NAME --resource-group $RESOURCE_GROUP --enable-rbac-authorization false 2>/dev/null || true

  echo "🔑 Granting current user access to manage secrets..."
  CURRENT_USER_OID=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || echo "")
  if [ -z "$CURRENT_USER_OID" ]; then
    echo "   Attempting to get Object ID from access token..."
    CURRENT_USER_OID=$(az account get-access-token --query accessToken -o tsv | python3 -c "import sys, jwt; print(jwt.decode(sys.stdin.read().strip(), options={'verify_signature': False}).get('oid', ''))" 2>/dev/null || echo "")
  fi

  if [ -n "$CURRENT_USER_OID" ]; then
    echo "   Setting Key Vault Access Policy for Object ID: $CURRENT_USER_OID..."
    az keyvault set-policy --name $KV_NAME --secret-permissions all --object-id "$CURRENT_USER_OID" 2>/dev/null || echo "   ⚠️  Could not set access policy."
  else
    echo "   ⚠️  Could not determine current user Object ID. Secret updates might fail."
  fi
else
  echo "⏭️  Skipping Key Vault access policy updates (--skip-kv-access)"
fi

if [ -f "./secrets.sh" ]; then
  echo "🔑 Running secrets.sh to populate API keys..."
  ./secrets.sh
  echo "💡 Tip: Create a secrets.sh file to automatically populate API keys."
fi

end_step

# 5. Setup Persistent Storage (Azure Blob Storage for Free Tier)
start_step "Persistent Storage Setup"
echo ""
echo "📦 Setting up Azure Blob Storage container..."
BLOB_CONTAINER_NAME="deep-research-blobs"
SQLITE_FILE_SHARE_NAME="deep-research-auth"
SQLITE_ENV_STORAGE_NAME="authsqlite"

EXISTING_STORAGE=$(az storage account list --resource-group $RESOURCE_GROUP --query "[?starts_with(name, 'stdeepagents')].name" -o tsv 2>/dev/null || echo "")
if [ -n "$EXISTING_STORAGE" ]; then
  echo "✅ Found existing storage account: $EXISTING_STORAGE"
  STORAGE_ACCOUNT_NAME=$EXISTING_STORAGE
  STORAGE_KEY=$(az storage account keys list --account-name $STORAGE_ACCOUNT_NAME --resource-group $RESOURCE_GROUP --query '[0].value' -o tsv)
  EXISTING_CONTAINER=$(az storage container list --account-name $STORAGE_ACCOUNT_NAME --account-key $STORAGE_KEY --query "[?name=='$BLOB_CONTAINER_NAME'].name" -o tsv 2>/dev/null || echo "")
  if [ -n "$EXISTING_CONTAINER" ]; then
    echo "✅ Blob container '$BLOB_CONTAINER_NAME' already exists. Skipping creation."
  else
    echo "📁 Creating Blob Container: $BLOB_CONTAINER_NAME"
    az storage container create --name $BLOB_CONTAINER_NAME --account-name $STORAGE_ACCOUNT_NAME --account-key $STORAGE_KEY
  fi
else
  echo "🗄️  Creating Storage Account: $STORAGE_ACCOUNT_NAME"
  az storage account create --name $STORAGE_ACCOUNT_NAME --resource-group $RESOURCE_GROUP --location $LOCATION --sku Standard_LRS --kind StorageV2 --access-tier Hot --allow-blob-public-access false
  STORAGE_KEY=$(az storage account keys list --account-name $STORAGE_ACCOUNT_NAME --resource-group $RESOURCE_GROUP --query '[0].value' -o tsv)
  echo "📁 Creating Blob Container: $BLOB_CONTAINER_NAME"
  az storage container create --name $BLOB_CONTAINER_NAME --account-name $STORAGE_ACCOUNT_NAME --account-key $STORAGE_KEY
fi

echo "📁 Ensuring persistent Azure File share for SQLite auth state..."
az storage share-rm create \
  --resource-group "$RESOURCE_GROUP" \
  --storage-account "$STORAGE_ACCOUNT_NAME" \
  --name "$SQLITE_FILE_SHARE_NAME" \
  --quota 1 > /dev/null
az containerapp env storage set \
  --name "$ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --storage-name "$SQLITE_ENV_STORAGE_NAME" \
  --azure-file-account-name "$STORAGE_ACCOUNT_NAME" \
  --azure-file-account-key "$STORAGE_KEY" \
  --azure-file-share-name "$SQLITE_FILE_SHARE_NAME" \
  --access-mode ReadWrite > /dev/null

echo "🔐 Storing storage credentials in Key Vault..."
az keyvault secret set --vault-name $KV_NAME --name STORAGE-ACCOUNT-NAME --value $STORAGE_ACCOUNT_NAME > /dev/null
az keyvault secret set --vault-name $KV_NAME --name STORAGE-ACCOUNT-KEY --value $STORAGE_KEY > /dev/null
az keyvault secret set --vault-name $KV_NAME --name AZURE-STORAGE-CONTAINER-NAME --value $BLOB_CONTAINER_NAME > /dev/null
echo "✅ Persistent storage setup complete"
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
rm -f "$EXISTING_CONFIG_JSON"
az containerapp update --name "$AGENT_NAME" --resource-group "$RESOURCE_GROUP" --yaml "$UPDATE_YAML"
rm -f "$UPDATE_YAML"
REVISION_READY=false
for i in {1..60}; do
  REVISION_STATE=$(az containerapp revision list --name "$AGENT_NAME" --resource-group "$RESOURCE_GROUP" --query "[?name=='${AGENT_NAME}--${REVISION_SUFFIX}'] | [0].[properties.runningState,properties.healthState]" -o tsv)
  if [[ "$REVISION_STATE" == $'Running\tHealthy' ]]; then
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

BACKEND_APP_NAME="$BACKEND_APP_NAME" UI_APP_NAME="$UI_APP_NAME" \
  "$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh" --record >/dev/null

print_timing_summary
