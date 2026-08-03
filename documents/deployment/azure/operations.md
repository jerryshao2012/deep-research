# Operate Azure Container Apps

Use this guide for day-two Azure operations: networking, configuration, monitoring, scaling, load tests, releases, CI/CD, and cost control. Commands assume repository-root `env.sh` has been reviewed for the target subscription and resource names.

## Inspect the running service

```bash
source ./env.sh

az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{state:properties.provisioningState,fqdn:properties.configuration.ingress.fqdn,min:properties.template.scale.minReplicas,max:properties.template.scale.maxReplicas}' \
  --output table

az containerapp revision list \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '[].{name:name,active:properties.active,state:properties.runningState,created:properties.createdTime}' \
  --output table

curl --fail --silent --show-error "$DEEP_RESEARCH_AGENT_URL/health" \
  | python3 -m json.tool
```

The current script deploys external HTTPS ingress to container port `2024`, scales from zero, and caps the app at one replica.

## Manage container networking

### Use the current external endpoint

External ingress supports browser clients and a separately hosted Vercel UI. Allow only intended frontend origins through `FRONTEND_URLS` and the application authentication settings; see [Authentication](../../guides/authentication.md). Container Apps terminates HTTPS for its managed domain.

Verify ingress:

```bash
az containerapp ingress show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output yaml

az containerapp exec \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "sh -c 'curl --fail --silent --show-error http://127.0.0.1:2024/health'"
```

### Switch to internal ingress only with an in-environment client

An internal-only agent and UI must share the same Container Apps environment. Capture the agent FQDN and test from the UI container before disabling external ingress:

```bash
AGENT_FQDN=$(az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn \
  --output tsv)

az containerapp exec \
  --name "$UI_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "sh -c 'curl --fail --silent --show-error https://$AGENT_FQDN/health'"
```

Changing ingress is a service-impacting operation. After the in-environment check succeeds:

```bash
az containerapp ingress update \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --type internal \
  --target-port 2024
```

A Vercel-hosted browser client cannot reach an internal-only Container App directly. Custom domains and network restrictions are covered in [Security](security.md#restrict-network-access-and-tls).

## Manage configuration

`deploy.sh` generates the effective Container App YAML each time it runs. Direct `az containerapp update --set-env-vars` changes create a revision but may be overwritten by the next scripted deployment.

Inspect variable names and non-secret values without querying Key Vault values:

```bash
az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query 'properties.template.containers[0].env[].{name:name,value:value,secretRef:secretRef}' \
  --output table
```

Make permanent configuration changes in the deployment source, then rebuild only when application code or image content changed. Current supported deployment options are:

```text
./deploy.sh
./deploy.sh --skip-kv-access
```

The old `--skip-build` and `--sync-files` flags are not implemented. Build with `./build.sh`; synchronize with `./sync-files.sh`.

Use [Configuration](../../guides/configuration.md) as the canonical variable reference. Do not maintain a second copy here.

Azure App Configuration can centralize values, but this repository does not currently load `APP_CONFIG_ENDPOINT` or refresh App Configuration keys. Creating a store or setting that variable alone does not change runtime configuration; add and test application integration first.

## Monitor logs, traces, and metrics

### Stream and query logs

```bash
az containerapp logs show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --tail 100

az containerapp logs show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --follow \
  --tail 50
```

On first creation, the bare `az containerapp env create` in `deploy.sh` uses Container Apps' default Log Analytics destination, and Azure CLI provisions a generated workspace. Existing environments retain their configured destination. Discover the effective workspace customer ID, then find its workspace resource:

```bash
source ./env.sh

LOG_ANALYTICS_CUSTOMER_ID=$(az containerapp env show \
  --name "$ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.appLogsConfiguration.logAnalyticsConfiguration.customerId \
  --output tsv)

az monitor log-analytics workspace list \
  --query "[?customerId=='$LOG_ANALYTICS_CUSTOMER_ID'].{name:name,resourceGroup:resourceGroup,id:id}" \
  --output table
```

Query only the configured app. Double-quoted shell strings expand `$APP_NAME`; single quotes inside each string remain valid KQL string delimiters:

```bash
az monitor log-analytics query \
  --workspace "$LOG_ANALYTICS_CUSTOMER_ID" \
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '$APP_NAME' | order by TimeGenerated desc | take 100" \
  --output table

az monitor log-analytics query \
  --workspace "$LOG_ANALYTICS_CUSTOMER_ID" \
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '$APP_NAME' | where Log_s contains 'ERROR' or Log_s contains 'Exception' | summarize errors=count() by bin(TimeGenerated, 1h)" \
  --output table

az monitor log-analytics query \
  --workspace "$LOG_ANALYTICS_CUSTOMER_ID" \
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '$APP_NAME' | where Log_s contains 'Rate limit' | summarize retries=count() by bin(TimeGenerated, 5m)" \
  --output table
```

If the customer ID is empty or `null`, inspect `properties.appLogsConfiguration.destination` and the environment's provisioning history before querying. Microsoft documents the bare CLI create flow as generating a workspace automatically; see [Log and metrics in Azure Container Apps](https://learn.microsoft.com/azure/spring-apps/migration/migrate-to-azure-container-apps-monitoring).

### Track resource use

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
```

Create alerts from observed baseline values. Metric names and dimensions can differ by Container Apps environment; list definitions before creating rules:

```bash
az monitor metrics list-definitions \
  --resource "$APP_RESOURCE_ID" \
  --output table
```

### Use Application Insights and LangSmith deliberately

The current deployment script relies on the CLI-generated Log Analytics workspace and does not create Application Insights. Verify generated workspace ownership, access, retention, and cost controls. For production naming, retention, private access, or centralized ownership, explicitly provide an approved existing workspace when creating the Container Apps environment instead of relying on automatic generation.

LangSmith tracing is enabled in generated configuration, but usable traces still require a valid `LANGCHAIN_API_KEY`, reachable endpoint, and intended project. Confirm only the variable references and inspect the LangSmith project; never print the key.

Operational evaluation metrics are written to `output/eval_history/server_runs.jsonl` and synchronized through Blob behavior. See [Evaluation](../../guides/evaluation.md) for metric semantics and [Storage](storage.md) for durability limits.

## Respect the singleton scaling limit

Current Azure state is not safe for multiple writers. Keep `maxReplicas: 1` while using local `.langgraph_api` plus SQLite on Azure Files. Scale-to-zero is supported by the script, with cold-start latency as the tradeoff.

You may right-size one replica after load tests:

```bash
az containerapp update \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --cpu 2.0 \
  --memory 4Gi \
  --min-replicas 0 \
  --max-replicas 1
```

Do not apply the older guide's HTTP/CPU rules with `max-replicas 10`. Scale-out requires a concurrency-safe LangGraph catalog and durable multi-writer auth/application database first.

Tune `MAX_CONCURRENT_RESEARCH_UNITS`, provider TPM/RPM, CPU, and memory together. Higher agent concurrency can exhaust provider quota before CPU. See [Reliability](../../guides/reliability.md).

## Run load tests

Use an authenticated test account and a non-production environment. Start with health/readiness traffic, then a small representative research workload. Never place an API key in a checked-in k6 script.

```bash
export TEST_BASE_URL="https://<test-app-fqdn>"
export TEST_API_KEY="<temporary-test-key>"

k6 run \
  -e BASE_URL="$TEST_BASE_URL" \
  -e API_KEY="$TEST_API_KEY" \
  path/to/reviewed-load-test.js
```

Set explicit virtual-user and duration limits in the reviewed script. Watch CPU, memory, latency, error rate, model quotas, and Blob/SQLite errors during the run. A 200 response-time threshold from the retired guide is not a production SLO.

## Release, version, and roll back

`build.sh` increments `API_VERSION`, writes a timestamp tag to `.build_version`, and pushes `latest` plus the immutable timestamp tag. `deploy.sh` deploys the timestamped tag and checks `/health` for the expected version.

List revisions and their images:

```bash
az containerapp revision list \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '[].{revision:name,active:properties.active,image:properties.template.containers[0].image,created:properties.createdTime}' \
  --output table
```

For rollback, select a known-good immutable image tag, update the app, and require the matching health version before restoring traffic. Back up or quiesce state separately; an image rollback does not roll back Blob or SQLite data.

Restart only an explicitly selected revision:

```bash
REVISION="<exact-revision-name>"

az containerapp revision restart \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --revision "$REVISION"
```

## Build a CI/CD workflow

No maintained GitHub Actions workflow in this repository currently implements the Azure release. The retired ACR example conflicts with the live Docker Hub scripts.

A production pipeline should:

1. authenticate to Azure with workload identity federation, not a long-lived credential JSON;
2. build a `linux/amd64` image with a compatible builder and immutable tag;
3. publish to the approved registry without logging tokens;
4. update the Container App using the same identity, secret-reference, storage, and singleton configuration as `deploy.sh`;
5. wait for the Azure operation and active revision;
6. require `/health` to return the expected version;
7. preserve the previous revision for rollback.

`build.sh` invokes Apple's `container` CLI and is not directly portable to a standard Linux GitHub-hosted runner. Either use a compatible self-hosted runner or implement and test an equivalent builder step. Do not copy the stale ACR workflow and assume parity.

## Control cost

- Keep scale-to-zero for interruptible or low-traffic environments.
- Keep one maximum replica until persistence is redesigned.
- Right-size CPU and memory from at least a representative measurement window.
- Set Blob lifecycle, log retention, and Application Insights sampling deliberately.
- Remove unused revisions and images only after identifying rollback dependencies.
- Tag resources consistently and create subscription/resource-group budgets with real recipients.
- Use the [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/) for current regional prices; static prices in the old guide are obsolete.

The earlier Redis caching suggestion was not wired into this application and is not a supported cost switch.

## Keep useful CLI references

```bash
# List apps in the resource group
az containerapp list --resource-group "$RESOURCE_GROUP" --output table

# Show active revisions
az containerapp revision list \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '[?properties.active==`true`]' \
  --output table

# Show recent logs
az containerapp logs show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --tail 100

# Show resource ID for diagnostic tooling
az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv
```

Destructive commands such as app, share, vault, or resource-group deletion are intentionally omitted. Resolve exact resources and confirm recovery copies before deleting anything.

## Related documentation

- [Azure deployment](README.md)
- [Azure storage](storage.md)
- [Azure security](security.md)
- [Azure troubleshooting](troubleshooting.md)
- [Configuration](../../guides/configuration.md)
- [Reliability](../../guides/reliability.md)
- [Evaluation](../../guides/evaluation.md)
