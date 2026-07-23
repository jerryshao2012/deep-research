#!/bin/bash
set -e

# Timer tracking
TOTAL_START_TIME=$(date +%s)
STEP_TIMES=()
TEMP_FILES=()

cleanup_temp_files() {
  if [ "${#TEMP_FILES[@]}" -gt 0 ]; then
    rm -f -- "${TEMP_FILES[@]}"
  fi
}
trap cleanup_temp_files EXIT

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

verify_app_runner_readiness() {
  local base_url="$1"
  local readiness_status

  if ! readiness_status=$(curl \
    --silent \
    --show-error \
    --max-time 10 \
    --output /dev/null \
    --write-out "%{http_code}" \
    "${base_url}/ok"); then
    echo "❌ App Runner readiness request failed: ${base_url}/ok" >&2
    return 1
  fi

  case "$readiness_status" in
    2??)
      echo "✅ App Runner readiness confirmed (HTTP $readiness_status)"
      ;;
    *)
      echo "❌ App Runner readiness failed with HTTP $readiness_status: ${base_url}/ok" >&2
      return 1
      ;;
  esac
}

wait_for_app_runner_operation() {
  local service_arn="$1"
  local operation_id="$2"
  local max_polls="${APP_RUNNER_OPERATION_MAX_POLLS:-72}"
  local poll_seconds="${APP_RUNNER_OPERATION_POLL_SECONDS:-5}"
  local attempt
  local operation_status

  if [ -z "$operation_id" ]; then
    echo "❌ App Runner operation ID is required." >&2
    return 1
  fi

  for ((attempt = 1; attempt <= max_polls; attempt++)); do
    if ! operation_status=$(aws apprunner list-operations \
      --service-arn "$service_arn" \
      --max-results 20 \
      --region "$AWS_REGION" \
      --query "OperationSummaryList[?Id=='${operation_id}'] | [0].Status" \
      --output text); then
      echo "❌ Failed to query App Runner operation $operation_id." >&2
      return 1
    fi

    case "$operation_status" in
      SUCCEEDED)
        echo "✅ App Runner operation $operation_id succeeded."
        return 0
        ;;
      FAILED|ROLLBACK_FAILED|ROLLBACK_SUCCEEDED)
        echo "❌ App Runner operation $operation_id ended with $operation_status." >&2
        return 1
        ;;
      PENDING|IN_PROGRESS|ROLLBACK_IN_PROGRESS|None|null|"")
        ;;
      *)
        echo "❌ App Runner operation $operation_id returned unknown status: $operation_status" >&2
        return 1
        ;;
    esac

    if [ "$attempt" -lt "$max_polls" ]; then
      sleep "$poll_seconds"
    fi
  done

  echo "❌ Timed out waiting for App Runner operation $operation_id." >&2
  return 1
}

resolve_singleton_autoscaling_configuration() {
  local configuration_name="${1:-deep-research-singleton-${SEED}}"
  local configuration_arn
  local configuration_state

  configuration_arn=$(
    aws apprunner list-auto-scaling-configurations \
      --auto-scaling-configuration-name "$configuration_name" \
      --latest-only \
      --region "$AWS_REGION" \
      --query "AutoScalingConfigurationSummaryList[0].AutoScalingConfigurationArn" \
      --output text 2>/dev/null || echo ""
  )
  if [ -n "$configuration_arn" ] \
    && [ "$configuration_arn" != "None" ] \
    && [ "$configuration_arn" != "null" ]; then
    configuration_state=$(
      aws apprunner describe-auto-scaling-configuration \
        --auto-scaling-configuration-arn "$configuration_arn" \
        --region "$AWS_REGION" \
        --query "AutoScalingConfiguration.[Status,MinSize,MaxSize]" \
        --output text 2>/dev/null || echo ""
    )
    if [ "$configuration_state" != $'ACTIVE\t1\t1' ]; then
      echo "⚠️  Existing auto scaling revision is not an active singleton; creating a corrected revision." >&2
      configuration_arn=""
    fi
  fi

  if [ -z "$configuration_arn" ] \
    || [ "$configuration_arn" = "None" ] \
    || [ "$configuration_arn" = "null" ]; then
    echo "✨ Creating singleton App Runner auto scaling configuration..." >&2
    configuration_arn=$(
      aws apprunner create-auto-scaling-configuration \
        --auto-scaling-configuration-name "$configuration_name" \
        --min-size 1 \
        --max-size 1 \
        --region "$AWS_REGION" \
        --query "AutoScalingConfiguration.AutoScalingConfigurationArn" \
        --output text
    )
  fi

  if [ -z "$configuration_arn" ] \
    || [ "$configuration_arn" = "None" ] \
    || [ "$configuration_arn" = "null" ]; then
    echo "❌ Unable to resolve singleton App Runner auto scaling configuration." >&2
    return 1
  fi
  printf '%s\n' "$configuration_arn"
}

verify_deployed_version() {
  local base_url="$1"
  local expected_version="$2"
  local max_retries="${APP_RUNNER_VERSION_MAX_RETRIES:-10}"
  local poll_seconds="${APP_RUNNER_VERSION_POLL_SECONDS:-10}"
  local attempt
  local health_response
  local response_version

  for ((attempt = 1; attempt <= max_retries; attempt++)); do
    health_response=$(curl \
      --silent \
      --show-error \
      --fail \
      --max-time 5 \
      "${base_url}/health" 2>/dev/null || true)
    response_version=""
    if [ -n "$health_response" ]; then
      response_version=$(echo "$health_response" | python3 -c \
        "import sys, json; print(json.load(sys.stdin).get('version', ''))" \
        2>/dev/null || echo "")
    fi
    if [ "$response_version" = "$expected_version" ]; then
      echo "✅ Deployed API version $response_version confirmed."
      return 0
    fi
    if [ "$attempt" -lt "$max_retries" ]; then
      sleep "$poll_seconds"
    fi
  done

  echo "❌ Deployed version mismatch after $max_retries attempts (expected: $expected_version, got: ${response_version:-unknown})." >&2
  return 1
}

# Parse command-line arguments
SKIP_INFRA_SETUP=false
LANGGRAPH_S3_READ_ONLY="true"

while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-infra-setup)
      SKIP_INFRA_SETUP=true
      shift
      ;;
    --read-write)
      LANGGRAPH_S3_READ_ONLY="false"
      shift
      ;;
    --help|-h)
      echo "Usage: ./deploy-aws.sh [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --skip-infra-setup Fast deployment (skips IAM role and secrets creation checks)"
      echo "  --read-write       Enable guarded S3 writes after read-only verification"
      echo "  --help, -h         Show this help message"
      echo ""
      echo "Examples:"
      echo "  ./deploy-aws.sh                                    # Full deployment to AWS App Runner"
      echo "  ./deploy-aws.sh --skip-infra-setup                 # Update service deployment only"
      echo "  ./deploy-aws.sh --skip-infra-setup --read-write    # Verified guarded read-write rollout"
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
echo "🔒 LangGraph S3 read-only rollout: $LANGGRAPH_S3_READ_ONLY"

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
if [ ! -f .build_version ]; then
  echo "⚠️ .build_version not found. Please run ./build-aws.sh first."
  exit 1
fi
BUILD_VERSION=$(cat .build_version)

if ! aws ecr describe-images --repository-name "$ECR_REPO_NAME" --image-ids imageTag="$BUILD_VERSION" --region "$AWS_REGION" &> /dev/null; then
  echo "⚠️  WARNING: Image '$BUILD_VERSION' not found in ECR repository '$ECR_REPO_NAME'!"
  echo "   Please run './build-aws.sh' first to build and push the image."
  exit 1
fi
echo "✅ Verified image '$BUILD_VERSION' exists in ECR"

NEW_VERSION=$(grep -E 'API_VERSION(:\s*\w+)?\s*=\s*' webapp/config.py | grep -o '"[^"]*"')
NEW_VERSION=${NEW_VERSION//\"/}
echo "ℹ️  Current API version: $NEW_VERSION"
end_step

# 3. IAM Roles & Secrets Manager Setup
if [ "$SKIP_INFRA_SETUP" = false ]; then
  start_step "IAM App Runner Roles Setup"
  
  # 1. Create ECR Access Trust Policy JSON and Role
  TRUST_POLICY_FILE=$(mktemp)
  TEMP_FILES+=("$TRUST_POLICY_FILE")
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
  TEMP_FILES+=("$INSTANCE_TRUST_FILE")
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
  TEMP_FILES+=("$INSTANCE_POLICY_FILE")
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
TEMP_FILES+=("$S3_POLICY_FILE")
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

# App Runner must never scale this in-memory demo beyond one writer.
start_step "App Runner Singleton Auto Scaling"
AUTOSCALING_CONFIGURATION_NAME="deep-research-singleton-${SEED}"
AUTOSCALING_CONFIGURATION_ARN=$(
  resolve_singleton_autoscaling_configuration \
    "$AUTOSCALING_CONFIGURATION_NAME"
)
echo "✅ Singleton auto scaling configuration: $AUTOSCALING_CONFIGURATION_ARN"
end_step

# 4. App Runner Service Deployment
start_step "App Runner Service Deployment"
ECR_URL="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
SOURCE_CONFIG_FILE=$(mktemp)
TEMP_FILES+=("$SOURCE_CONFIG_FILE")

cat > "$SOURCE_CONFIG_FILE" <<EOF
{
  "ImageRepository": {
    "ImageIdentifier": "${ECR_URL}/${ECR_REPO_NAME}:${BUILD_VERSION}",
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
        "MEMORY_TYPE": "",
        "REPORTS_OUTPUT_FOLDER": "/deps/deep_research/output",
        "EVAL_HISTORY_FILE": "/deps/deep_research/output/eval_history/server_runs.jsonl",
        "DOC_FOLDER": "/deps/deep_research/docs",
        "INPUT_FOLDER": "/deps/deep_research/input",
        "WIKI_BASE_DIR": "/deps/deep_research",
        "SQLITE_DB_PATH": "/deps/deep_research/deep_research.db",
        "S3_BUCKET_NAME": "${S3_BUCKET_NAME}",
        "AWS_REGION": "${AWS_REGION}",
        "LANGGRAPH_S3_READ_ONLY": "${LANGGRAPH_S3_READ_ONLY}",
        "LANGGRAPH_SNAPSHOT_PREFIX": ".langgraph_snapshots",
        "LANGGRAPH_SNAPSHOT_STABILITY_SECONDS": "12",
        "LANGGRAPH_SNAPSHOT_SCAN_INTERVAL_SECONDS": "2",
        "LANGGRAPH_FENCE_INTERVAL_SECONDS": "2",
        "LANGGRAPH_SNAPSHOT_RETENTION_COUNT": "5"
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
  SERVICE_STATUS=$(aws apprunner describe-service \
    --service-arn "$SERVICE_ARN" \
    --region "$AWS_REGION" \
    --query "Service.Status" \
    --output text)
  if [ "$SERVICE_STATUS" != "RUNNING" ]; then
    echo "❌ Error: App Runner service must be RUNNING before update; current status: $SERVICE_STATUS"
    echo "   Resume a paused service and wait for RUNNING, then rerun this command."
    exit 1
  fi
  echo "📝 App Runner service '$APP_NAME' already exists. Updating configuration..."
  UPDATE_OUT=$(aws apprunner update-service \
    --service-arn "$SERVICE_ARN" \
    --source-configuration "file://$SOURCE_CONFIG_FILE" \
    --instance-configuration Cpu="2 vCPU",Memory="4 GB",InstanceRoleArn="$INSTANCE_ROLE_ARN" \
    --auto-scaling-configuration-arn "$AUTOSCALING_CONFIGURATION_ARN" \
    --health-check-configuration "Protocol=HTTP,Path=/ok,Interval=5,Timeout=2,HealthyThreshold=1,UnhealthyThreshold=5" \
    --region "$AWS_REGION")
  
  # Check if an OperationId was returned (meaning config actually changed)
  OP_ID=$(echo "$UPDATE_OUT" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('OperationId', ''))" 2>/dev/null || echo "")
  
  if [ -z "$OP_ID" ]; then
    echo "ℹ️  Configuration unchanged. Triggering explicit deployment to pull latest image..."
    START_OUT=$(aws apprunner start-deployment \
      --service-arn "$SERVICE_ARN" \
      --region "$AWS_REGION")
    OP_ID=$(echo "$START_OUT" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('OperationId', ''))" 2>/dev/null || echo "")
  fi
else
  echo "✨ Creating new App Runner service '$APP_NAME'..."
  CREATE_OUT=$(aws apprunner create-service \
    --service-name "$APP_NAME" \
    --source-configuration "file://$SOURCE_CONFIG_FILE" \
    --instance-configuration Cpu="2 vCPU",Memory="4 GB",InstanceRoleArn="$INSTANCE_ROLE_ARN" \
    --auto-scaling-configuration-arn "$AUTOSCALING_CONFIGURATION_ARN" \
    --health-check-configuration "Protocol=HTTP,Path=/ok,Interval=5,Timeout=2,HealthyThreshold=1,UnhealthyThreshold=5" \
    --region "$AWS_REGION")
  SERVICE_ARN=$(echo "$CREATE_OUT" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('Service', {}).get('ServiceArn', ''))" 2>/dev/null || echo "")
  OP_ID=$(echo "$CREATE_OUT" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('OperationId', ''))" 2>/dev/null || echo "")
fi

rm -f "$SOURCE_CONFIG_FILE"
if [ -z "$SERVICE_ARN" ] || [ -z "$OP_ID" ]; then
  echo "❌ App Runner deployment response did not include service ARN and operation ID." >&2
  exit 1
fi
echo "✅ Deployment triggered for Service: $SERVICE_ARN (operation: $OP_ID)"
wait_for_app_runner_operation "$SERVICE_ARN" "$OP_ID"
end_step

# 5. Verify deployed service and get Endpoint
start_step "Retrieve Service Endpoint"
SERVICE_DETAILS=$(aws apprunner describe-service \
  --service-arn "$SERVICE_ARN" \
  --region "$AWS_REGION")
STATUS=$(echo "$SERVICE_DETAILS" | python3 -c "import sys, json; print(json.load(sys.stdin).get('Service', {}).get('Status', ''))" 2>/dev/null || echo "")
RAW_URL=$(echo "$SERVICE_DETAILS" | python3 -c "import sys, json; print(json.load(sys.stdin).get('Service', {}).get('ServiceUrl', ''))" 2>/dev/null || echo "")
if [ "$STATUS" != "RUNNING" ] || [ -z "$RAW_URL" ]; then
  echo "❌ App Runner operation succeeded but service is not ready (status: ${STATUS:-unknown})." >&2
  exit 1
fi
EXTERNAL_URL="https://$RAW_URL"
echo "🌐 Service Endpoint: $EXTERNAL_URL"
end_step

start_step "App Runner Readiness Verification"
verify_app_runner_readiness "$EXTERNAL_URL"
end_step

start_step "Deployed Version Verification"
verify_deployed_version "$EXTERNAL_URL" "$NEW_VERSION"
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
echo "🏥 Health Check: $EXTERNAL_URL/ok"
echo ""
echo "📊 Next Steps:"
echo "   • Test API: curl -s $EXTERNAL_URL/ok"
echo "   • Monitor Service: open https://console.aws.amazon.com/apprunner/home?region=$AWS_REGION#/services/$APP_NAME/service"
echo "   • Sync files: ./sync-files-aws.sh  (bi-directional sync with s3://${S3_BUCKET_NAME})"
echo "═══════════════════════════════════════════════════════"

print_timing_summary
