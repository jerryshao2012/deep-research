# Azure No-Cosmos Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Cosmos DB from Azure free-tier deployment and verify restored LangGraph thread listing after deployment.

**Architecture:** `langgraph dev` owns thread/run/checkpoint state in `.langgraph_api`; existing Azure Blob startup and background synchronization persist that directory. `../../../deploy.sh` retains SQLite only as compatibility storage for custom application routes.

**Tech Stack:** Bash, Azure CLI, Azure Container Apps, pytest, LangGraph CLI

---

### Task 1: Lock Azure Deployment Contract

**Files:**
- Modify: `../../../tests/test_azure_persistence_scripts.py`
- Test: `../../../tests/test_azure_persistence_scripts.py`

- [ ] **Step 1: Write failing test**

Add a test that reads `../../../deploy.sh`, uses a multiline regular expression to
require adjacent active YAML fields `name: DB_TYPE` and `value: sqlite`, and
rejects `az cosmosdb`, `COSMOSDB_`, `cosmosdb-`, and `value: cosmosdb`.

- [ ] **Step 2: Verify red**

Run:

```bash
uv run pytest tests/test_azure_persistence_scripts.py::test_azure_deploy_uses_sqlite_without_cosmos -q
```

Expected: failure because current deployment provisions and configures Cosmos.

### Task 2: Remove Cosmos From Azure Deployment

**Files:**
- Modify: `../../../deploy.sh`

- [ ] **Step 1: Remove Cosmos provisioning**

Delete Cosmos account creation, credential retrieval, and Key Vault writes.

- [ ] **Step 2: Remove runtime Cosmos configuration**

Delete Cosmos secret references and environment variables. Change:

```yaml
- name: DB_TYPE
  value: sqlite
```

Keep `MEMORY_TYPE=""`, Blob secrets, singleton scaling, and
`ALLOW_ALL_THREADS=false`.

- [ ] **Step 3: Verify green**

Run:

```bash
uv run pytest tests/test_azure_persistence_scripts.py::test_azure_deploy_uses_sqlite_without_cosmos -q
bash -n deploy.sh
```

Expected: both commands pass.

### Task 3: Regression Verification

**Files:**
- Test: `../../../tests/test_azure_persistence_scripts.py`

- [ ] **Step 1: Run Azure persistence suite**

```bash
uv run pytest tests/test_azure_persistence_scripts.py -q
```

- [ ] **Step 2: Compare against captured dirty baseline**

Capture `git status --short`, `git diff -- deploy.sh`, and
`git diff -- sync-files.sh` before implementation. Afterward, confirm
`../../../sync-files.sh` is byte-for-byte unchanged from that baseline and `../../../deploy.sh`
diff contains its prior `ALLOW_ALL_THREADS=false` change plus only approved
Cosmos removal/SQLite configuration.

### Task 4: Build, Deploy, and Probe

**Files:**
- Use: `../../../build.sh`
- Use: `../../../deploy.sh`

- [ ] **Step 1: Establish thread-list oracle and fence writers**

Stop local `langgraph dev`, inspect the known-good local `.langgraph_api`
catalog, and record expected thread IDs without exposing thread contents. Use
Azure CLI to resolve and deactivate the current active revision, then poll
until it has zero running replicas. This fences the existing five-second Azure
background uploader. Only then upload the quiescent state:

```bash
./sync-files.sh --upload
```

If no known-good catalog exists, create a uniquely named sentinel thread,
confirm it appears locally, stop the runtime, then upload. Do not accept
`200 []` as success. Do not send traffic to the stopped Azure app between
deactivation and deployment because it must not resume before Blob restore.

- [ ] **Step 2: Decide whether image build is needed**

Because runtime application source is unchanged and `../../../deploy.sh` supplies
configuration, compare `.build_version`, deployed image tag, and build commit
context. Skip `../../../build.sh` only if deployed/selected image is confirmed to
contain current `../../../entrypoint.sh` and `../../../azure_storage.py`; otherwise run:

```bash
bash build.sh
```

- [ ] **Step 3: Deploy**

```bash
bash deploy.sh
```

Expected: command exits zero and Container App reports a successful active
revision.

- [ ] **Step 4: Verify effective deployment configuration**

Use Azure CLI without printing secret values to assert:

- active revision is healthy and provisioning succeeded;
- `DB_TYPE=sqlite`;
- no Cosmos environment or secret references;
- Blob environment/secret references remain;
- `MEMORY_TYPE` is empty;
- `maxReplicas=1`.

- [ ] **Step 5: Resolve endpoint and verify health**

Resolve ingress FQDN through Azure CLI and require `/ok` success.

- [ ] **Step 6: Query thread catalog**

Call:

```bash
curl -fsS -X POST "https://<fqdn>/threads/search" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: <configured-key>" \
  --data '{"limit":100,"offset":0}'
```

Retrieve configured API key without echoing it. Treat `401` or `403` as an
authentication/configuration failure. Require a successful response containing
valid JSON, report returned thread count, and assert recorded baseline/sentinel
thread IDs are present. If expected IDs are missing, compare Blob
`.langgraph_api` contents and owner filtering before changing storage
architecture.
