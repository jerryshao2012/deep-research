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
source ./env-aws.sh

echo "🚀 Starting Deep Research Agent AWS build..."

# 1. Set AWS CLI Session
start_step "Verify AWS Authentication"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
if [ -z "$AWS_ACCOUNT_ID" ]; then
  echo "❌ Error: Not authenticated with AWS. Please run 'aws configure' or check credentials."
  exit 1
fi
echo "✅ Authenticated with AWS Account: $AWS_ACCOUNT_ID"
end_step

# 2. Create ECR repository
start_step "ECR Repository Setup"
if aws ecr describe-repositories --repository-names "$ECR_REPO_NAME" --region "$AWS_REGION" &> /dev/null; then
  echo "✅ ECR Repository '$ECR_REPO_NAME' already exists. Skipping creation."
else
  echo "✨ Creating ECR Repository '$ECR_REPO_NAME'..."
  aws ecr create-repository --repository-name "$ECR_REPO_NAME" --region "$AWS_REGION" --image-scanning-configuration scanOnPush=true
fi
end_step

# 3. Increment API version
start_step "API Version Management"
echo "🔢 Incrementing API version..."
python3 ./increment_version.py
NEW_VERSION=$(grep -E 'API_VERSION(:\s*\w+)?\s*=\s*' webapp/config.py | grep -o '"[^"]*"')
NEW_VERSION=${NEW_VERSION//\"/}
echo "✅ New API version: $NEW_VERSION"
end_step

# 4. Build and push image
start_step "Docker Image Build & Push"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ECR_URL="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
IMAGE_TAG="$ECR_URL/$ECR_REPO_NAME:latest"

echo "🔨 Building Docker image ($IMAGE_TAG)..."
docker build --no-cache --platform linux/amd64 -f Dockerfile-aws -t "$IMAGE_TAG" .

echo "🔑 Logging in to AWS ECR..."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_URL"

echo "⬆️  Pushing image to ECR..."
docker push "$IMAGE_TAG"
echo "✅ Image built and pushed successfully"
end_step

print_timing_summary