export SEED="0312"
export APP_NAME="deep-research-agent-$SEED"

# 1. Create resource group
export RESOURCE_GROUP="resource-group-deep-agents-$SEED"
export LOCATION="canadacentral"

# 2. Create Container Apps environment
export ENV_NAME="env-name-deep-agents-$SEED"

# 3. Deploy agent
export AGENT_NAME="deep-research-agent-$SEED"

# Create Key Vault
export KV_NAME="kv-deep-agents-$SEED"

# 4. Agent URL
export DEEP_RESEARCH_AGENT_URL="https://deep-research-agent-0312.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io"
