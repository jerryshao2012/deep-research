export SEED="0312"
export APP_NAME="deep-research-agent-$SEED"
export AZURE_SUBSCRIPTION_ID="31fcb880-f153-4bac-b91c-c694854c65ce"

# 1. Create resource group
export RESOURCE_GROUP="resource-group-deep-agents-$SEED"
export LOCATION="canadacentral"

# 2. Create Container Apps environment
export ENV_NAME="env-name-deep-agents-$SEED"

# 3. Deploy agent
export AGENT_NAME="deep-research-agent-$SEED"
export BACKEND_APP_NAME="$AGENT_NAME"
export UI_APP_NAME="bmo-deepagent-ui-$SEED"

# Create Key Vault
export KV_NAME="kv-deep-agents-$SEED-bmo2"

# Create Storage Account (globally unique; lowercase letters and numbers only)
export STORAGE_ACCOUNT_NAME="stdeepagents${SEED}bmo2"

# 4. Agent URL
export DEEP_RESEARCH_AGENT_URL="https://deep-research-agent-0312.wonderfuldesert-92e45542.canadacentral.azurecontainerapps.io"
