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
  echo "⏱️  Build Timing Summary"
  echo "═══════════════════════════════════════════════════════"
  for timing in "${STEP_TIMES[@]}"; do
    echo "   • $timing"
  done
  echo "───────────────────────────────────────────────────────"
  echo "   Total build time: ${TOTAL_DURATION}s"
  echo "═══════════════════════════════════════════════════════"
}

# Configuration
source ./env.sh
if [ -f "../.env" ]; then
  set -a
  source "../.env"
  set +a
fi
if [ -f "./.env" ]; then
  set -a
  source "./.env"
  set +a
fi

echo "🚀 Starting Deep Research Agent build..."

# 1. Set Azure Subscription
start_step "Set Azure Subscription"
AZURE_SUBSCRIPTION_ID="66fadccd-d26d-4dd0-b108-46b3c581cdb3"
az account set --subscription $AZURE_SUBSCRIPTION_ID
echo "✅ Subscription set to $AZURE_SUBSCRIPTION_ID"
end_step

# 2. Create resource group
start_step "Resource Group Setup"
if az group show --name $RESOURCE_GROUP &> /dev/null; then
  echo "✅ Resource group '$RESOURCE_GROUP' already exists. Skipping creation."
else
  az group create --name $RESOURCE_GROUP --location $LOCATION
fi
end_step

# 3. Azure Provider Registration
start_step "Azure Provider Registration"
echo "📝 Registering required Azure providers..."
az provider register -n Microsoft.OperationalInsights --wait
az provider register -n Microsoft.App --wait
az provider register -n Microsoft.KeyVault --wait
az provider register -n Microsoft.Storage --wait
az provider register -n Microsoft.ManagedIdentity --wait
echo "✅ Providers registered."
end_step

# 4. Check Docker Hub Username
start_step "Docker Hub Setup"
if [ -z "$DOCKER_HUB_USERNAME" ]; then
  echo "❌ Error: Please set DOCKER_HUB_USERNAME in .env"
  exit 1
fi
echo "✅ Using Docker Hub user: $DOCKER_HUB_USERNAME"
if [ -n "$DOCKER_HUB_PAT" ]; then
  # Ensure container service is started
  if ! container system status &>/dev/null; then
    echo "🚀 Container system is not running. Auto-starting..."
    container system start --disable-kernel-install
  fi
  echo "🔐 Logging into Docker Hub..."
  echo "$DOCKER_HUB_PAT" | container registry login -u "$DOCKER_HUB_USERNAME" --password-stdin
fi
end_step

# 5. Increment API version
start_step "API Version Management"
echo "🔢 Incrementing API version..."
python3 ./increment_version.py
NEW_VERSION=$(grep -E 'API_VERSION(:\s*\w+)?\s*=\s*' webapp/config.py | grep -o '"[^"]*"')
NEW_VERSION=${NEW_VERSION//\"/}
echo "✅ New API version: $NEW_VERSION"
end_step

# 6. Build and push image
start_step "Container Image Build & Push"
BUILD_VERSION=$(date +%Y%m%d%H%M%S)
echo $BUILD_VERSION > .build_version
echo "🔨 Building Container image with tags: latest, $BUILD_VERSION"
# Ensure we're in the correct directory (where Dockerfile is located)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# The container tool requires the full registry host in the image name for pushing.
FULL_IMAGE_NAME="docker.io/$DOCKER_HUB_USERNAME/deep-research-agent:latest"
# Ensure container service is started
if ! container system status &>/dev/null; then
  echo "🚀 Container system is not running. Auto-starting..."
  container system start --disable-kernel-install
fi

container build --platform linux/amd64 -t $FULL_IMAGE_NAME .
container image push $FULL_IMAGE_NAME
if [ $? -ne 0 ]; then
  echo "❌ Container push failed for '$FULL_IMAGE_NAME'."
  exit 1
fi

VERSIONED_IMAGE_NAME="docker.io/$DOCKER_HUB_USERNAME/deep-research-agent:$BUILD_VERSION"
echo "🏷️  Tagging versioned image: $VERSIONED_IMAGE_NAME"
container image tag $FULL_IMAGE_NAME $VERSIONED_IMAGE_NAME
echo "🚀 Pushing versioned image..."
container image push $VERSIONED_IMAGE_NAME
if [ $? -ne 0 ]; then
  echo "❌ Container push failed for '$VERSIONED_IMAGE_NAME'."
  exit 1
fi
echo "✅ Image built and pushed successfully"
end_step

print_timing_summary