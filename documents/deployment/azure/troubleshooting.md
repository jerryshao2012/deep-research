# Troubleshoot Azure deployments

Use this runbook to diagnose Azure Container Apps failures without exposing secrets. Start with the checklist, then use the matching symptom/diagnostic/repair section.

## Run the debugging checklist first

```bash
source ./env.sh

# 1. Confirm account and target names
az account show --query '{subscription:name,id:id,tenant:tenantId}' --output table
printf 'resource_group=%s app=%s environment=%s vault=%s\n' \
  "$RESOURCE_GROUP" "$AGENT_NAME" "$ENV_NAME" "$KV_NAME"

# 2. Check provisioning and ingress
az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{state:properties.provisioningState,revision:properties.latestRevisionName,fqdn:properties.configuration.ingress.fqdn,external:properties.configuration.ingress.external,targetPort:properties.configuration.ingress.targetPort}' \
  --output table

# 3. Check active revisions
az containerapp revision list \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '[?properties.active==`true`].{name:name,state:properties.runningState,created:properties.createdTime}' \
  --output table

# 4. Read recent logs
az containerapp logs show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --tail 100

# 5. Test process-local health, then public health
az containerapp exec \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "sh -c 'curl --fail --silent --show-error http://127.0.0.1:2024/health'"

curl --fail --silent --show-error "$DEEP_RESEARCH_AGENT_URL/health" \
  | python3 -m json.tool

# 6. Inspect scale and resource configuration
az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{scale:properties.template.scale,resources:properties.template.containers[0].resources}'
```

Do not add `printenv`, secret values, access tokens, or `/storage/info` output to a support bundle. Capture timestamps, revision name, image tag, HTTP status, and redacted errors.

All prerequisite diagnostics in this runbook are read-only preflight. Current deployment updates existing app only: it does not grant roles or access policies, does not create identities, and does not create storage resources. If required resource or effective access is absent, stop and contact the Azure administrator with failing command and redacted output.

## Image and startup failures

### Image is missing or cannot be pulled

**Symptom:** revision stays failed or inactive; logs/events report registry authorization, manifest, or image pull errors.

**Diagnose:**

```bash
az containerapp revision list \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output table

az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{image:properties.template.containers[0].image,registries:properties.configuration.registries[].{server:server,username:username,passwordSecretRef:passwordSecretRef}}'

test -s .build_version && printf 'build_version=%s\n' "$(tr -d '\n' < .build_version)"
```

Confirm the timestamped tag exists in the intended Docker Hub repository without displaying the PAT.

**Repair:** rebuild only if the tag is absent, then deploy the resulting immutable version:

```bash
./build.sh
./deploy.sh
```

If the tag exists, refresh the `DOCKER-HUB-PAT` Key Vault version through the approved secret workflow and rerun `./deploy.sh`. The stale ACR repair commands do not apply to the current Docker Hub deployment.

### Container starts but expected version never becomes healthy

**Symptom:** `deploy.sh` reaches `/health` but reports no response or a version mismatch.

**Diagnose:**

```bash
EXPECTED_VERSION=$(python3 -c 'from webapp.config import API_VERSION; print(API_VERSION)')

curl --silent --show-error "$DEEP_RESEARCH_AGENT_URL/health" \
  | python3 -m json.tool

az containerapp revision list \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '[].{name:name,active:properties.active,image:properties.template.containers[0].image,state:properties.runningState}' \
  --output table
```

**Repair:** deploy the tag recorded in `.build_version`; if the configuration is already correct, restart the exact active revision:

```bash
REVISION="<exact-active-revision-name>"

az containerapp revision restart \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --revision "$REVISION"
```

Require the returned version to equal `$EXPECTED_VERSION` before closing the incident.

### Process exits during startup

**Symptom:** repeated restarts, failed revision, or no process-local response on port `2024`.

**Diagnose:**

```bash
az containerapp logs show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --tail 200

az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query 'properties.template.containers[0].{image:image,command:command,args:args,resources:resources}'
```

**Repair:** correct the first configuration or startup error, build a new immutable image for code/image changes, then run `./deploy.sh`. Do not mask a required Blob startup-sync failure by deleting state or removing storage variables.

## Identity and secret failures

### Key Vault reference returns 403 or will not synchronize

**Symptom:** logs or revision status report a Key Vault 403, secret synchronization failure, or missing required environment variable.

**Diagnose:**

```bash
IDENTITY_NAME="${AGENT_NAME}-identity"
IDENTITY_PRINCIPAL_ID=$(az identity show \
  --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query principalId --output tsv)

az keyvault show \
  --name "$KV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{rbac:properties.enableRbacAuthorization,policies:properties.accessPolicies[].objectId}'

az keyvault secret list \
  --vault-name "$KV_NAME" \
  --query '[].{name:name,enabled:attributes.enabled}' \
  --output table

az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query 'properties.configuration.secrets[].{name:name,keyVaultUrl:keyVaultUrl,identity:identity}'
```

**Repair:** stop before deployment mutation. Contact Azure administrator to restore effective secret `get` through vault's existing authorization model and confirm user-assigned identity remains assigned to backend app. Do not switch RBAC/access-policy mode or grant permissions from deployment workflow. For missing/disabled secret, secret owner creates or rotates version through approved secret system without putting value in command transcript. Rerun read-only checks, then deploy only after prerequisites pass.

### Provider authentication fails after deployment

**Symptom:** health works, but research requests return provider authentication or missing-configuration errors.

**Diagnose:** inspect only variable and secret-reference names:

```bash
az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query 'properties.template.containers[0].env[].{name:name,hasValue:value!=null,secretRef:secretRef}' \
  --output table
```

Compare the selected provider's required names with [Configuration](../../guides/configuration.md). Current Azure YAML does not inject the commented Azure OpenAI references.

**Repair:** contact secret owner or Azure administrator to preprovision missing Key Vault secret and effective backend-identity access through approved workflow. Add only secret-reference name to reviewed deployment configuration; never value. After read-only preflight passes, create revision with `./deploy.sh` and test smallest provider-dependent request.

## Networking and port failures

### Public endpoint is unreachable

**Symptom:** process-local health passes, but the managed FQDN times out or refuses traffic.

**Diagnose:**

```bash
az containerapp ingress show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output yaml

az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query 'properties.configuration.ingress.{external:external,fqdn:fqdn,targetPort:targetPort,transport:transport}'
```

**Repair:** for the current public architecture, restore external ingress to port `2024`:

```bash
az containerapp ingress update \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --type external \
  --target-port 2024 \
  --transport auto
```

Then verify `/health`. If policy requires internal ingress, do not make it public; test from a client inside the same environment instead.

### UI cannot resolve or reach the agent

**Symptom:** UI reports network failure; internal DNS lookup or HTTPS request fails.

**Diagnose:**

```bash
AGENT_FQDN=$(az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn --output tsv)

az containerapp exec \
  --name "$UI_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "sh -c 'getent hosts $AGENT_FQDN && curl --fail --silent --show-error https://$AGENT_FQDN/health'"
```

**Repair:** put both internal apps in the same Container Apps environment and configure the UI with the agent FQDN. For a Vercel UI, keep the agent external and fix exact origins/authentication; Vercel cannot directly access an internal ACA endpoint.

## Storage failures

### Blob startup synchronization fails

**Symptom:** container exits during startup with missing Blob configuration, authorization, container-not-found, or download errors.

**Diagnose:**

```bash
az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "properties.template.containers[0].env[?name=='STORAGE_ACCOUNT_NAME' || name=='STORAGE_ACCOUNT_KEY' || name=='AZURE_STORAGE_CONTAINER_NAME']"

az keyvault secret list \
  --vault-name "$KV_NAME" \
  --query "[?name=='STORAGE-ACCOUNT-NAME' || name=='STORAGE-ACCOUNT-KEY' || name=='AZURE-STORAGE-CONTAINER-NAME'].{name:name,enabled:attributes.enabled}" \
  --output table
```

**Repair:** contact Azure administrator to preprovision or restore expected storage account, `deep-research-blobs` container, Key Vault references, and identity access. Deployment does not create or repair them. If data exists in another container, back it up and follow [migration](storage.md#migrate-or-roll-back); do not point app at unverified empty container. Rerun read-only preflight before deployment.

### Auth database is not durable or SQLite reports locks

**Symptom:** sessions/passkeys disappear after restart, `/mnt/auth` is absent, or logs contain SQLite locking/permission errors.

**Diagnose:**

```bash
az containerapp env storage list \
  --name "$ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output table

az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{scale:properties.template.scale,volumes:properties.template.volumes,mounts:properties.template.containers[0].volumeMounts}'

az containerapp exec \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "sh -c 'ls -ld /mnt/auth && test -f /mnt/auth/auth.db && ls -l /mnt/auth/auth.db'"
```

**Repair:** contact Azure administrator to preprovision or restore `deep-research-auth` share and existing Container Apps environment storage `authsqlite` binding with expected account/share and `ReadWrite` access. Deployment does not create or change binding. After read-only preflight passes, deploy app config with `/mnt/auth`, `SQLITE_DB_PATH=/mnt/auth/auth.db`, `AUTH_SQLITE_JOURNAL_MODE=DELETE`, and `maxReplicas: 1`. Restore `auth.db` only from verified backup while app is stopped.

### Files or LangGraph threads appear stale

**Symptom:** deployed content differs from local files or expected thread IDs are missing.

**Diagnose:** stop assuming direction. Download first to a reviewed staging copy:

```bash
./sync-files.sh --download
```

Compare `sync/` with the expected source and record active writers/revision timestamps.

**Repair:** quiesce every local and Azure writer before uploading `.langgraph_api`. Back up remote and local state, select one known-good source, run `./sync-files.sh --upload`, then start one revision and verify authenticated `POST /threads/search`. Never merge SQLite/Blob state by copying files from two live writers.

## Resource and rate-limit failures

### Container is OOM-killed or CPU-bound

**Symptom:** revision restarts, logs mention out-of-memory, or latency rises with CPU/memory saturation.

**Diagnose:**

```bash
APP_RESOURCE_ID=$(az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)

az monitor metrics list \
  --resource "$APP_RESOURCE_ID" \
  --metric UsageNanoCores,WorkingSetBytes \
  --interval PT1H \
  --output table

az containerapp logs show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --tail 200
```

**Repair:** lower research concurrency or right-size the singleton:

```bash
az containerapp update \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --cpu 2.0 \
  --memory 4Gi \
  --set-env-vars MAX_CONCURRENT_RESEARCH_UNITS=2
```

Make the chosen value permanent in deployment configuration; the next `./deploy.sh` can overwrite an ad hoc update.

### Provider rate limits persist

**Symptom:** logs show repeated 429/rate-limit retries and requests fail after backoff.

**Diagnose:**

```bash
az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "properties.template.containers[0].env[?name=='MODEL_TPM' || name=='MODEL_RPM' || name=='MODEL_MAX_RETRIES' || name=='MODEL_INITIAL_BACKOFF']" \
  --output table

az containerapp logs show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --tail 200
```

**Repair:** set TPM/RPM to actual provider quota and reduce workload concurrency. Follow [Reliability](../../guides/reliability.md); do not merely increase retries because that can extend overload.

## Observability failures

### Logs or metrics are missing

**Symptom:** Container Apps log stream is empty, historical KQL has no rows, or Azure Monitor has no expected metric.

**Diagnose:**

```bash
az containerapp logs show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --tail 20

az monitor metrics list-definitions \
  --resource "$(az containerapp show --name "$AGENT_NAME" --resource-group "$RESOURCE_GROUP" --query id --output tsv)" \
  --output table

LOG_ANALYTICS_CUSTOMER_ID=$(az containerapp env show \
  --name "$ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.appLogsConfiguration.logAnalyticsConfiguration.customerId \
  --output tsv)

printf 'log_analytics_customer_id=%s\n' "$LOG_ANALYTICS_CUSTOMER_ID"

az monitor log-analytics workspace list \
  --query "[?customerId=='$LOG_ANALYTICS_CUSTOMER_ID'].{name:name,resourceGroup:resourceGroup,id:id}" \
  --output table
```

Current deployment uses existing Container Apps environment log destination. Commands above reveal effective customer ID and matching workspace without displaying shared key.

**Repair:** if environment has customer ID, contact workspace administrator to fix read permissions, retention, or query scope, then generate known health request and allow ingestion delay. If destination or customer ID is missing, contact environment administrator to configure approved existing Log Analytics destination outside deployment workflow. Use metric names returned by `list-definitions` when creating alerts.

### LangSmith traces are absent

**Symptom:** application requests succeed but no traces appear in the intended LangSmith project.

**Diagnose:** inspect names/references and outbound reachability without printing credentials:

```bash
az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "properties.template.containers[0].env[?name=='LANGCHAIN_TRACING_V2' || name=='LANGSMITH_ENDPOINT' || name=='LANGCHAIN_PROJECT' || name=='LANGCHAIN_API_KEY']"

az containerapp exec \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "sh -c 'curl --silent --show-error --output /dev/null --write-out \"HTTP status: %{http_code}\\n\" https://api.smith.langchain.com'"
```

Any HTTP response proves DNS resolution, TLS negotiation, and outbound egress succeeded. Diagnose authentication or application statuses, including `401`, `403`, and the root endpoint's expected `404`, separately.

**Repair:** rotate the LangChain key through Key Vault, verify the secret reference and project name, and create a new revision with `./deploy.sh`. If outbound network policy blocks the endpoint, allow only the required destination through the approved egress path.

## Related documentation

- [Azure deployment](README.md)
- [Azure operations](operations.md)
- [Azure storage](storage.md)
- [Azure security](security.md)
- [Reliability](../../guides/reliability.md)
