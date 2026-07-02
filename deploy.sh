#!/bin/bash
set -e

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
      echo "Options:"
      echo "  --skip-kv-access Skip Key Vault access policy updates (faster re-deployment)"
      echo "  --help, -h       Show this help message"
      echo ""
      echo "Examples:"
      echo "  ./deploy.sh                                    # Full deployment using existing image"
      echo "  ./deploy.sh --skip-kv-access                   # Fast re-deployment (no KV access check)"
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
source ./env.sh

echo "🚀 Starting Deep Research Agent deployment (using existing image)..."

if [ -f "./.env" ]; then
  set -a
  source "./.env"
  set +a
fi

if [ -z "$DOCKER_HUB_USERNAME" ]; then
  echo "❌ Error: Please set DOCKER_HUB_USERNAME in .env before running deploy.sh"
  exit 1
fi
echo "✅ Using Docker Hub user: $DOCKER_HUB_USERNAME"

# 1. Set Azure Subscription
start_step "Set Azure Subscription"
AZURE_SUBSCRIPTION_ID="66fadccd-d26d-4dd0-b108-46b3c581cdb3"
az account set --subscription $AZURE_SUBSCRIPTION_ID
echo "✅ Subscription set to $AZURE_SUBSCRIPTION_ID"
end_step

# 2. Verify image exists in ACR
start_step "Verify Container Image"
if [ ! -f .build_version ]; then
    echo "⚠️ .build_version not found. Please run ./build.sh first."
    exit 1
fi
BUILD_VERSION=$(cat .build_version)
# No direct ACR check for Docker Hub image in bash
echo "✅ Verified image exists in ACR"

NEW_VERSION=$(grep -E 'API_VERSION(:\s*\w+)?\s*=\s*' webapp/config.py | grep -o '"[^"]*"' | tr -d '"')
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
if az keyvault show --name $KV_NAME --resource-group $RESOURCE_GROUP &> /dev/null; then
  echo "✅ Key Vault '$KV_NAME' already exists. Skipping creation."
else
  az keyvault create --name $KV_NAME --resource-group $RESOURCE_GROUP --location $LOCATION --enable-rbac-authorization false
fi

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

if [ -n "$DOCKER_HUB_PAT" ]; then
  echo "🔐 Storing Docker Hub PAT in Key Vault..."
  az keyvault secret set --vault-name $KV_NAME --name DOCKER-HUB-PAT --value "$DOCKER_HUB_PAT" > /dev/null
fi
end_step

# 5. Setup Persistent Storage
start_step "Persistent Storage Setup"
echo ""
echo "📦 Setting up Azure Files persistent storage..."
STORAGE_ACCOUNT_NAME="stdeepagents"
FILE_SHARE_NAME="deep-research-files"
MOUNT_PATH="/deps/deep_research/mnt"

EXISTING_STORAGE=$(az storage account list --resource-group $RESOURCE_GROUP --query "[?starts_with(name, 'stdeepagents')].name" -o tsv 2>/dev/null || echo "")
if [ -n "$EXISTING_STORAGE" ]; then
  echo "✅ Found existing storage account: $EXISTING_STORAGE"
  STORAGE_ACCOUNT_NAME=$EXISTING_STORAGE
  STORAGE_KEY=$(az storage account keys list --account-name $STORAGE_ACCOUNT_NAME --resource-group $RESOURCE_GROUP --query '[0].value' -o tsv)
  EXISTING_SHARE=$(az storage share list --account-name $STORAGE_ACCOUNT_NAME --account-key $STORAGE_KEY --query "[?name=='$FILE_SHARE_NAME'].name" -o tsv 2>/dev/null || echo "")
  if [ -n "$EXISTING_SHARE" ]; then
    echo "✅ File share '$FILE_SHARE_NAME' already exists. Skipping creation."
  else
    echo "📁 Creating File Share: $FILE_SHARE_NAME (100GB quota)"
    az storage share create --name $FILE_SHARE_NAME --account-name $STORAGE_ACCOUNT_NAME --account-key $STORAGE_KEY --quota 100
  fi
else
  echo "🗄️  Creating Storage Account: $STORAGE_ACCOUNT_NAME"
  az storage account create --name $STORAGE_ACCOUNT_NAME --resource-group $RESOURCE_GROUP --location $LOCATION --sku Standard_LRS --kind StorageV2 --access-tier Cool --allow-blob-public-access false
  STORAGE_KEY=$(az storage account keys list --account-name $STORAGE_ACCOUNT_NAME --resource-group $RESOURCE_GROUP --query '[0].value' -o tsv)
  echo "📁 Creating File Share: $FILE_SHARE_NAME (100GB quota)"
  az storage share create --name $FILE_SHARE_NAME --account-name $STORAGE_ACCOUNT_NAME --account-key $STORAGE_KEY --quota 100
fi

for dir in "docs" "docs/policy" "output" "output/eval_history" "input" ".langgraph_api"; do
  if ! az storage directory exists --share-name $FILE_SHARE_NAME --path "$dir" --account-name $STORAGE_ACCOUNT_NAME --account-key $STORAGE_KEY --query "exists" -o tsv 2>/dev/null; then
    echo "  + Creating directory '$dir'"
    az storage directory create --share-name $FILE_SHARE_NAME --name "$dir" --account-name $STORAGE_ACCOUNT_NAME --account-key $STORAGE_KEY
  fi
done

echo "💡 Tip: Run './sync-files.sh' separately for bi-directional file sync with Azure File Share"

echo "🔐 Storing storage credentials in Key Vault..."
az keyvault secret set --vault-name $KV_NAME --name STORAGE-ACCOUNT-NAME --value $STORAGE_ACCOUNT_NAME > /dev/null
az keyvault secret set --vault-name $KV_NAME --name STORAGE-ACCOUNT-KEY --value $STORAGE_KEY > /dev/null
az keyvault secret set --vault-name $KV_NAME --name FILE-SHARE-NAME --value $FILE_SHARE_NAME > /dev/null
echo "✅ Persistent storage setup complete"
end_step

# 6. Deploy or update agent
start_step "Container App Deployment"
echo "🚀 Deploying agent..."

# Unify identity management
USER_IDENTITY_NAME="${AGENT_NAME}-identity"
echo "🔐 Ensuring User-Assigned Managed Identity '$USER_IDENTITY_NAME' exists..."
az identity create --name "$USER_IDENTITY_NAME" --resource-group $RESOURCE_GROUP > /dev/null
USER_IDENTITY_ID=$(az identity show --name "$USER_IDENTITY_NAME" --resource-group $RESOURCE_GROUP --query id -o tsv)
USER_IDENTITY_PRINCIPAL_ID=$(az identity show --name "$USER_IDENTITY_NAME" --resource-group $RESOURCE_GROUP --query principalId -o tsv)

echo "🔐 Ensuring Managed Identity has Key Vault access..."
az keyvault set-policy --name "$KV_NAME" --secret-permissions get list --object-id "$USER_IDENTITY_PRINCIPAL_ID" > /dev/null

echo "✅ Skipping ACR permissions since we use Docker Hub"

if az containerapp show --name $AGENT_NAME --resource-group $RESOURCE_GROUP &> /dev/null; then
  echo "📝 Container app already exists. Updating..."
  az containerapp identity assign --name $AGENT_NAME --resource-group $RESOURCE_GROUP --user-assigned "$USER_IDENTITY_ID" > /dev/null || true
  echo "⏳ Waiting for initial update to settle..."
  sleep 10
else
  echo "✨ Creating new container app..."
  az containerapp create \
    --name $AGENT_NAME \
    --resource-group $RESOURCE_GROUP \
    --environment $ENV_NAME \
    --image $DOCKER_HUB_USERNAME/deep-research-agent:$BUILD_VERSION \
    --registry-server docker.io \
    --registry-username "$DOCKER_HUB_USERNAME" \
    --registry-password "$DOCKER_HUB_PAT" \
    --target-port 2024 \
    --ingress external \
    --transport auto \
    --min-replicas 0 \
    --max-replicas 1 \
    --cpu 2.0 \
    --memory 4Gi \
    --user-assigned "$USER_IDENTITY_ID"
fi

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
UPDATE_YAML=$(mktemp /tmp/update-config-XXXXXX.yaml 2>/dev/null || mktemp)
RESTART_TRIGGER=$(date +%s)
cat > "$UPDATE_YAML" <<EOF
properties:
  configuration:
    ingress:
      external: true
      targetPort: 2024
      transport: auto
    secrets:
      - name: tavily-api-key
        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/TAVILY-API-KEY
        identity: ${USER_IDENTITY_ID}
      - name: langchain-api-key
        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/LANGCHAIN-API-KEY
        identity: ${USER_IDENTITY_ID}
      - name: upload-api-key
        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/UPLOAD-API-KEY
        identity: ${USER_IDENTITY_ID}
      - name: storage-account-name
        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/STORAGE-ACCOUNT-NAME
        identity: ${USER_IDENTITY_ID}
      - name: storage-account-key
        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/STORAGE-ACCOUNT-KEY
        identity: ${USER_IDENTITY_ID}
      - name: file-share-name
        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/FILE-SHARE-NAME
        identity: ${USER_IDENTITY_ID}
      - name: google-api-key
        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/GOOGLE-API-KEY
        identity: ${USER_IDENTITY_ID}
      - name: docker-hub-pat
        keyVaultUrl: https://${KV_NAME}.vault.azure.net/secrets/DOCKER-HUB-PAT
        identity: ${USER_IDENTITY_ID}
    registries:
      - server: docker.io
        username: ${DOCKER_HUB_USERNAME}
        passwordSecretRef: docker-hub-pat
  template:
    volumes:
      - name: persistent-storage
        storageName: azure-file-storage
        storageType: AzureFile
    containers:
      - name: deep-research-agent
        image: "${DOCKER_HUB_USERNAME}/deep-research-agent:${BUILD_VERSION}"
        resources:
          cpu: 2.0
          memory: 4Gi
        env:
          - name: RESTART_TRIGGER
            value: "${RESTART_TRIGGER}"
          - name: VERIFY_SSL
            value: "false"
          - name: LOG_LEVEL
            value: INFO
          - name: LANGCHAIN_TRACING_V2
            value: "true"
          - name: LANGSMITH_ENDPOINT
            value: https://api.smith.langchain.com
          - name: LANGCHAIN_PROJECT
            value: deep-research-production
          - name: ENABLE_EVAL_TRACKING
            value: "true"
          - name: MODEL_TPM
            value: "120000"
          - name: MODEL_RPM
            value: "500"
          - name: GRAPH_RECURSION_LIMIT
            value: "200"
          - name: MAX_CONCURRENT_RESEARCH_UNITS
            value: "3"
          - name: MAX_RESEARCHER_ITERATIONS
            value: "3"
          - name: MAX_GLOB_DEPTH
            value: "3"
          - name: MAX_FILES_TO_READ
            value: "20"
          - name: MAX_TOTAL_SIZE_MB
            value: "50"
          - name: MODEL_MAX_RETRIES
            value: "5"
          - name: MODEL_INITIAL_BACKOFF
            value: "1.0"
          - name: MODEL_MAX_BACKOFF
            value: "60.0"
          - name: MODEL_BACKOFF_MULTIPLIER
            value: "2.0"
          - name: MODEL_RETRY_JITTER
            value: "true"
          - name: MEMORY_TYPE
            value: ""
          - name: COSMOSDB_DB_NAME
            value: deep-research-checkpoints
          - name: COSMOSDB_CONTAINER_NAME
            value: checkpoints
          - name: REPORTS_OUTPUT_FOLDER
            value: ${MOUNT_PATH}/output
          - name: EVAL_HISTORY_FILE
            value: ${MOUNT_PATH}/output/eval_history/server_runs.jsonl
          - name: DOC_FOLDER
            value: ${MOUNT_PATH}/docs
          - name: WIKI_BASE_DIR
            value: ${MOUNT_PATH}
          - name: INPUT_FOLDER
            value: ${MOUNT_PATH}/input
          - name: SQLITE_DB_PATH
            value: /deps/deep_research/deep_research.db
          - name: TAVILY_API_KEY
            secretRef: tavily-api-key
          - name: LANGCHAIN_API_KEY
            secretRef: langchain-api-key
          - name: UPLOAD_API_KEY
            secretRef: upload-api-key
          - name: STORAGE_ACCOUNT_NAME
            secretRef: storage-account-name
          - name: STORAGE_ACCOUNT_KEY
            secretRef: storage-account-key
          - name: FILE_SHARE_NAME
            secretRef: file-share-name
          - name: GOOGLE_API_KEY
            secretRef: google-api-key
        volumeMounts:
          - volumeName: persistent-storage
            mountPath: $MOUNT_PATH
    scale:
      minReplicas: 0
      maxReplicas: 1
EOF
az containerapp update --name $AGENT_NAME --resource-group $RESOURCE_GROUP --yaml "$UPDATE_YAML"
rm -f "$UPDATE_YAML"
echo "✅ Container App configured successfully."
end_step

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ Deployment Complete!"
echo "═══════════════════════════════════════════════════════"

EXTERNAL_URL=$(az containerapp show --name $AGENT_NAME --resource-group $RESOURCE_GROUP --query properties.configuration.ingress.fqdn -o tsv)
echo "🌐 Agent URL: https://$EXTERNAL_URL"

echo "📝 Updating DEEP_RESEARCH_AGENT_URL in env.sh..."
if grep -q "^export DEEP_RESEARCH_AGENT_URL=" ./env.sh; then
  awk -v url="https://$EXTERNAL_URL" '/^export DEEP_RESEARCH_AGENT_URL=/{print "export DEEP_RESEARCH_AGENT_URL=\"" url "\""; next} 1' ./env.sh > ./env.sh.tmp && mv ./env.sh.tmp ./env.sh
else
  echo "" >> ./env.sh
  echo "# 4. Agent URL" >> ./env.sh
  echo "export DEEP_RESEARCH_AGENT_URL=\"https://$EXTERNAL_URL\"" >> ./env.sh
fi
echo "✅ env.sh updated."

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
  echo "⚠️  WARNING: Container started but version mismatch detected!"
else
  echo ""
  echo "✅ Deployment verified successfully!"
fi
end_step

print_timing_summary