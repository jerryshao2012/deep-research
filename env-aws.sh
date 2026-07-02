export SEED="0312"
export APP_NAME="deep-research-agent-$SEED"

# AWS Configuration
export AWS_REGION="us-east-1"
export AWS_PAGER=""

# 1. Build and push Docker image (ECR)
export ECR_REPO_NAME="deep-research-agent-$SEED"

# 2. App Runner Service
export APP_RUNNER_SERVICE_NAME="deep-research-agent-$SEED"

# 3. Secrets Management (Secrets Manager)
export SECRETS_MANAGER_NAME="kv-deep-agents-$SEED"

# 4. File Sync (S3 Bucket for bi-directional sync with ./sync-aws/)
export S3_BUCKET_NAME="deep-research-files-$SEED"

# 5. Agent URL (automatically populated by deploy-aws.sh)
export DEEP_RESEARCH_AGENT_URL="https://bh3z333bky.us-east-1.awsapprunner.com"
