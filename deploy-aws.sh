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
SKIP_INFRA_SETUP=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-infra-setup)
      SKIP_INFRA_SETUP=true
      shift
      ;;
    --help|-h)
      echo "Usage: ./deploy-aws.sh [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --skip-infra-setup Fast deployment (skips IAM role and secrets creation checks)"
      echo "  --help, -h         Show this help message"
      echo ""
      echo "Examples:"
      echo "  ./deploy-aws.sh                                    # Full deployment to AWS App Runner"
      echo "  ./deploy-aws.sh --skip-infra-setup                 # Update service deployment only"
      echo ""
      echo "Note: To build the image, run:"
      echo "  ./build-aws.sh"
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
source ./env-aws.sh

echo "🚀 Starting Deep Research Agent deployment on AWS App Runner..."

# 1. Set AWS CLI Session
start_step "Verify AWS Authentication"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
if [ -z "$AWS_ACCOUNT_ID" ]; then
  echo "❌ Error: Not authenticated with AWS. Please run 'aws configure' or check credentials."
  exit 1
fi
echo "✅ Authenticated with AWS Account: $AWS_ACCOUNT_ID"
end_step

# 2. Verify image exists in ECR
start_step "Verify Container Image"
if ! aws ecr describe-images --repository-name "$ECR_REPO_NAME" --image-ids imageTag=latest --region "$AWS_REGION" &> /dev/null; then
  echo "⚠️  WARNING: Image 'latest' not found in ECR repository '$ECR_REPO_NAME'!"
  echo "   Please run './build-aws.sh' first to build and push the image."
  exit 1
fi
echo "✅ Verified image exists in ECR"

NEW_VERSION=$(grep -E 'API_VERSION(:\s*\w+)?\s*=\s*' webapp/config.py | grep -o '"[^"]*"' | tr -d '"')
echo "ℹ️  Current API version: $NEW_VERSION"
end_step

# 3. IAM Roles & Secrets Manager Setup
if [ "$SKIP_INFRA_SETUP" = false ]; then
  start_step "IAM App Runner Roles Setup"
  
  # 1. Create ECR Access Trust Policy JSON and Role
  TRUST_POLICY_FILE=$(mktemp)
  cat > "$TRUST_POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "build.apprunner.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

  ACCESS_ROLE_NAME="AppRunnerECRAccessRole-$SEED"
  ACCESS_ROLE_ARN=$(aws iam get-role --role-name "$ACCESS_ROLE_NAME" --query "Role.Arn" --output text 2>/dev/null || echo "")
  if [ -z "$ACCESS_ROLE_ARN" ] || [ "$ACCESS_ROLE_ARN" = "None" ]; then
    echo "✨ Creating IAM ECR Access Role '$ACCESS_ROLE_NAME' for App Runner..."
    ACCESS_ROLE_ARN=$(aws iam create-role --role-name "$ACCESS_ROLE_NAME" --assume-role-policy-document "file://$TRUST_POLICY_FILE" --query "Role.Arn" --output text)
    aws iam attach-role-policy --role-name "$ACCESS_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
    echo "⏳ Waiting for ECR access role IAM propagation..."
    sleep 5
  else
    echo "✅ App Runner ECR Access Role already exists: $ACCESS_ROLE_ARN"
  fi
  rm -f "$TRUST_POLICY_FILE"

  # 2. Create Instance Trust Policy JSON and Role (enables container to fetch secrets)
  INSTANCE_TRUST_FILE=$(mktemp)
  cat > "$INSTANCE_TRUST_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "tasks.apprunner.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

  INSTANCE_ROLE_NAME="AppRunnerInstanceRole-$SEED"
  INSTANCE_ROLE_ARN=$(aws iam get-role --role-name "$INSTANCE_ROLE_NAME" --query "Role.Arn" --output text 2>/dev/null || echo "")
  if [ -z "$INSTANCE_ROLE_ARN" ] || [ "$INSTANCE_ROLE_ARN" = "None" ]; then
    echo "✨ Creating IAM Instance Role '$INSTANCE_ROLE_NAME' for App Runner..."
    INSTANCE_ROLE_ARN=$(aws iam create-role --role-name "$INSTANCE_ROLE_NAME" --assume-role-policy-document "file://$INSTANCE_TRUST_FILE" --query "Role.Arn" --output text)
    echo "⏳ Waiting for Instance role IAM propagation..."
    sleep 5
  else
    echo "✅ App Runner Instance Role already exists: $INSTANCE_ROLE_ARN"
  fi
  rm -f "$INSTANCE_TRUST_FILE"
  end_step

  start_step "Secrets Manager Validation"
  if [ -f "./secrets-aws.sh" ]; then
    echo "🔑 Running secrets-aws.sh to populate/verify Secrets Manager config..."
    ./secrets-aws.sh
    SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRETS_MANAGER_NAME" --query ARN --output text --region "$AWS_REGION")
  else
    echo "⚠️  WARNING: secrets-aws.sh not found. Checking if '$SECRETS_MANAGER_NAME' exists in Secrets Manager..."
    SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRETS_MANAGER_NAME" --query ARN --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    if [ -z "$SECRET_ARN" ] || [ "$SECRET_ARN" = "None" ]; then
      echo "❌ Error: Secrets Manager Secret '$SECRETS_MANAGER_NAME' not found!"
      echo "   Please run 'cp secrets-aws.sh.example secrets-aws.sh', configure it, and run it first."
      exit 1
    fi
  fi
  echo "✅ Secrets Manager Secret ARN: $SECRET_ARN"

  # Attach inline policy to Instance Role allowing it to read the specific secret
  echo "🔐 Attaching secret retrieval policy to Instance Role..."
  INSTANCE_POLICY_FILE=$(mktemp)
  cat > "$INSTANCE_POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "$SECRET_ARN"
    }
  ]
}
EOF
  aws iam put-role-policy --role-name "$INSTANCE_ROLE_NAME" --policy-name "AppRunnerSecretAccess" --policy-document "file://$INSTANCE_POLICY_FILE"
  rm -f "$INSTANCE_POLICY_FILE"
  echo "✅ Instance Role policy updated."
  end_step

else
  echo "⏭️  Skipping infrastructure setup (--skip-infra-setup)"
  ACCESS_ROLE_ARN=$(aws iam get-role --role-name "AppRunnerECRAccessRole-$SEED" --query "Role.Arn" --output text --region "$AWS_REGION")
  INSTANCE_ROLE_NAME="AppRunnerInstanceRole-$SEED"
  INSTANCE_ROLE_ARN=$(aws iam get-role --role-name "$INSTANCE_ROLE_NAME" --query "Role.Arn" --output text --region "$AWS_REGION")
  SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRETS_MANAGER_NAME" --query ARN --output text --region "$AWS_REGION")
fi

# S3 Bucket for file sync
start_step "S3 Bucket Setup"
if aws s3api head-bucket --bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" 2>/dev/null; then
  echo "✅ S3 bucket already exists: $S3_BUCKET_NAME"
else
  echo "✨ Creating S3 bucket: $S3_BUCKET_NAME ..."
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" > /dev/null
  else
    aws s3api create-bucket --bucket "$S3_BUCKET_NAME" --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" > /dev/null
  fi
  echo "✅ S3 bucket created: $S3_BUCKET_NAME"
fi

# Grant Instance Role access to the S3 bucket
S3_POLICY_FILE=$(mktemp)
cat > "$S3_POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET_NAME}",
        "arn:aws:s3:::${S3_BUCKET_NAME}/*"
      ]
    }
  ]
}
EOF
aws iam put-role-policy --role-name "$INSTANCE_ROLE_NAME" --policy-name "AppRunnerS3Access" \
  --policy-document "file://$S3_POLICY_FILE" --region "$AWS_REGION"
rm -f "$S3_POLICY_FILE"
echo "✅ Instance Role granted S3 access to bucket"
end_step

# 4. App Runner Service Deployment
start_step "App Runner Service Deployment"
ECR_URL="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
SOURCE_CONFIG_FILE=$(mktemp)

cat > "$SOURCE_CONFIG_FILE" <<EOF
{
  "ImageRepository": {
    "ImageIdentifier": "${ECR_URL}/${ECR_REPO_NAME}:latest",
    "ImageConfiguration": {
      "Port": "2024",
      "RuntimeEnvironmentVariables": {
        "VERIFY_SSL": "false",
        "LOG_LEVEL": "INFO",
        "LANGCHAIN_TRACING_V2": "true",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "LANGCHAIN_PROJECT": "deep-research-production",
        "ENABLE_EVAL_TRACKING": "true",
        "MODEL_TPM": "120000",
        "MODEL_RPM": "500",
        "GRAPH_RECURSION_LIMIT": "200",
        "MAX_CONCURRENT_RESEARCH_UNITS": "3",
        "MAX_RESEARCHER_ITERATIONS": "3",
        "MAX_GLOB_DEPTH": "3",
        "MAX_FILES_TO_READ": "20",
        "MAX_TOTAL_SIZE_MB": "50",
        "MODEL_MAX_RETRIES": "5",
        "MODEL_INITIAL_BACKOFF": "1.0",
        "MODEL_MAX_BACKOFF": "60.0",
        "MODEL_BACKOFF_MULTIPLIER": "2.0",
        "MODEL_RETRY_JITTER": "true",
        "MEMORY_TYPE": "memory",
        "REPORTS_OUTPUT_FOLDER": "/deps/deep_research/output",
        "EVAL_HISTORY_FILE": "/deps/deep_research/output/eval_history/server_runs.jsonl",
        "DOC_FOLDER": "/deps/deep_research/docs",
        "INPUT_FOLDER": "/deps/deep_research/input",
        "S3_BUCKET_NAME": "${S3_BUCKET_NAME}",
        "AWS_REGION": "${AWS_REGION}"
      },
      "RuntimeEnvironmentSecrets": {
        "TAVILY_API_KEY": "${SECRET_ARN}:TAVILY-API-KEY::",
        "LANGCHAIN_API_KEY": "${SECRET_ARN}:LANGCHAIN-API-KEY::",
        "UPLOAD_API_KEY": "${SECRET_ARN}:UPLOAD-API-KEY::",
        "AWS_BEARER_TOKEN_BEDROCK": "${SECRET_ARN}:AWS-BEARER-TOKEN-BEDROCK::",
        "AWS_BEDROCK_ENDPOINT": "${SECRET_ARN}:AWS-BEDROCK-ENDPOINT::",
        "MODEL_NAME": "${SECRET_ARN}:MODEL-NAME::"
      }
    },
    "ImageRepositoryType": "ECR"
  },
  "AuthenticationConfiguration": {
    "AccessRoleArn": "${ACCESS_ROLE_ARN}"
  },
  "AutoDeploymentsEnabled": false
}
EOF

# Find existing App Runner Service
SERVICE_ARN=$(aws apprunner list-services --query "ServiceSummaryList[?ServiceName=='$APP_NAME'].ServiceArn" --output text --region "$AWS_REGION" 2>/dev/null || echo "")

if [ -n "$SERVICE_ARN" ] && [ "$SERVICE_ARN" != "None" ] && [ "$SERVICE_ARN" != "null" ]; then
  echo "📝 App Runner service '$APP_NAME' already exists. Updating configuration..."
  UPDATE_OUT=$(aws apprunner update-service \
    --service-arn "$SERVICE_ARN" \
    --source-configuration "file://$SOURCE_CONFIG_FILE" \
    --instance-configuration Cpu="2 vCPU",Memory="4 GB",InstanceRoleArn="$INSTANCE_ROLE_ARN" \
    --region "$AWS_REGION")
  
  # Check if an OperationId was returned (meaning config actually changed)
  OP_ID=$(echo "$UPDATE_OUT" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('OperationId', ''))" 2>/dev/null || echo "")
  
  if [ -z "$OP_ID" ]; then
    echo "ℹ️  Configuration unchanged. Triggering explicit deployment to pull latest image..."
    aws apprunner start-deployment --service-arn "$SERVICE_ARN" --region "$AWS_REGION" > /dev/null
  fi
else
  echo "✨ Creating new App Runner service '$APP_NAME'..."
  SERVICE_ARN=$(aws apprunner create-service \
    --service-name "$APP_NAME" \
    --source-configuration "file://$SOURCE_CONFIG_FILE" \
    --instance-configuration Cpu="2 vCPU",Memory="4 GB",InstanceRoleArn="$INSTANCE_ROLE_ARN" \
    --region "$AWS_REGION" \
    --query "Service.ServiceArn" \
    --output text)
fi

rm -f "$SOURCE_CONFIG_FILE"
echo "✅ Deployment triggered for Service: $SERVICE_ARN"
echo "⏳ Waiting for the deployment operation to initialize..."
# App Runner takes a few seconds to transition from RUNNING to OPERATION_IN_PROGRESS
for i in {1..12}; do
  STATUS=$(aws apprunner describe-service --service-arn "$SERVICE_ARN" --region "$AWS_REGION" --query "Service.Status" --output text 2>/dev/null || echo "")
  if [ "$STATUS" = "OPERATION_IN_PROGRESS" ]; then
    break
  fi
  sleep 5
done
end_step

# 5. Wait for Deployment to settle and get Endpoint
start_step "Retrieve Service Endpoint"
echo "⏳ Waiting for App Runner service deployment to finish (takes ~3-5 mins)..."
while true; do
  SERVICE_DETAILS=$(aws apprunner describe-service --service-arn "$SERVICE_ARN" --region "$AWS_REGION")
  STATUS=$(echo "$SERVICE_DETAILS" | python3 -c "import sys, json; print(json.load(sys.stdin)['Service']['Status'])" 2>/dev/null || echo "")
  
  echo "   Current Status: $STATUS"
  
  if [ "$STATUS" = "RUNNING" ]; then
    echo "✅ App Runner service is active and running!"
    break
  elif [ "$STATUS" = "OPERATION_IN_PROGRESS" ]; then
    sleep 20
  else
    echo "❌ Deployment failed with status: $STATUS"
    exit 1
  fi
done

RAW_URL=$(aws apprunner describe-service --service-arn "$SERVICE_ARN" --region "$AWS_REGION" --query "Service.ServiceUrl" --output text)
EXTERNAL_URL="https://$RAW_URL"
echo "🌐 Service Endpoint: $EXTERNAL_URL"
end_step

# 6. Update env-aws.sh with URL
if grep -q "^export DEEP_RESEARCH_AGENT_URL=" ./env-aws.sh; then
  awk -v url="$EXTERNAL_URL" '/^export DEEP_RESEARCH_AGENT_URL=/{print "export DEEP_RESEARCH_AGENT_URL=\"" url "\""; next} 1' ./env-aws.sh > ./env-aws.sh.tmp && mv ./env-aws.sh.tmp ./env-aws.sh
else
  echo "" >> ./env-aws.sh
  echo "# 4. Agent URL" >> ./env-aws.sh
  echo "export DEEP_RESEARCH_AGENT_URL=\"$EXTERNAL_URL\"" >> ./env-aws.sh
fi
echo "✅ env-aws.sh updated with DEEP_RESEARCH_AGENT_URL=$EXTERNAL_URL"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ AWS App Runner Deployment Complete!"
echo "═══════════════════════════════════════════════════════"
echo "🌐 Agent URL: $EXTERNAL_URL"
echo "🏥 Health Check: $EXTERNAL_URL/health"
echo ""
echo "📊 Next Steps:"
echo "   • Test API: curl -s $EXTERNAL_URL/health"
echo "   • Monitor Service: open https://console.aws.amazon.com/apprunner/home?region=$AWS_REGION#/services/$APP_NAME/service"
echo "   • Sync files: ./sync-files-aws.sh  (bi-directional sync with s3://${S3_BUCKET_NAME})"
echo "═══════════════════════════════════════════════════════"

# 7. Health verification
start_step "Health Check Verification"
echo "🔍 Testing health endpoint..."
MAX_RETRIES=10
RETRY_INTERVAL=10
VERSION_MATCHED=false
for i in $(seq 1 $MAX_RETRIES); do
  echo -n "   Attempt $i/$MAX_RETRIES... "
  HEALTH_RESPONSE=$(curl -s --max-time 5 "$EXTERNAL_URL/health" 2>/dev/null || echo "")
  if [ -z "$HEALTH_RESPONSE" ]; then
    echo "❌ No response"
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
  echo "⚠️  WARNING: Deployment completed but version match was not confirmed via health check."
else
  echo ""
  echo "✅ AWS Deployment verified successfully!"
fi
end_step

print_timing_summary
