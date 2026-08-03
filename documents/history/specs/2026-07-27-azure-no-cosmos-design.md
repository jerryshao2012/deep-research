# Azure Free-Tier Deployment Without Cosmos DB

## Goal

Use LangGraph development runtime state as Azure demo's only thread catalog,
persist `.langgraph_api` through existing Azure Blob synchronization, and stop
provisioning or configuring Cosmos DB.

## Scope

- Remove Cosmos DB account provisioning from `../../../deploy.sh`.
- Remove Cosmos Key Vault references and container environment variables.
- Configure application compatibility database as local SQLite.
- Preserve `MEMORY_TYPE=""`, singleton scaling, Blob configuration,
  `ALLOW_ALL_THREADS=false`, and current `../../../sync-files.sh` changes.
- Keep optional Cosmos implementation in `../../../db.py` for other environments.

## Data Flow

1. `../../../entrypoint.sh` downloads `.langgraph_api` from Azure Blob before startup.
2. `langgraph dev` loads thread/run/checkpoint state from `.langgraph_api`.
3. Azure background synchronization publishes updated local state to Blob.
4. `../../../db.py` initializes SQLite only as compatibility storage for custom routes;
   it is not the LangGraph thread catalog.

## Deployment and Verification

Add a source-level regression test that rejects Cosmos provisioning,
Cosmos secrets, Cosmos environment variables, and non-SQLite `DB_TYPE` in
Azure deployment configuration. Match the active generated YAML structure,
not comments or unrelated substrings. Run the test red before editing
`../../../deploy.sh`, then green afterward.

Before deployment, capture expected thread IDs from the known-good local
`.langgraph_api` state. Deactivate the current Azure revision and wait until it
has no running replicas so its background uploader cannot overwrite Blob;
then upload the stopped/quiescent local state. After deployment, require a
successful active revision and inspect effective
Container App configuration: `DB_TYPE=sqlite`, no Cosmos environment or secret
references, Blob variables present, empty `MEMORY_TYPE`, and one maximum
replica. Resolve authentication without printing secrets, call
`POST /threads/search`, require valid JSON, and assert expected thread IDs are
present. Treat `401`/`403` as authentication failures rather than storage
failures.

## Safety

- Never print API keys, storage keys, or deployment secrets.
- Capture pre-edit status and scoped diffs, then preserve unrelated dirty
  worktree changes exactly.
- Keep one Azure Container Apps replica because local LangGraph state is not
  safe for concurrent writers.
- Treat endpoint authorization failures separately from persistence failures.
- Confirm deployed image tag is already the current Blob-capable image; run
  `../../../build.sh` only when this cannot be established.
- Never upload `.langgraph_api` while either local or Azure LangGraph runtime
  can write it.
