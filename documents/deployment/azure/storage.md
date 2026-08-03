# Operate Azure storage and persistence

Use this guide to understand, verify, migrate, and recover the Azure deployment's persistent data. It follows current `deploy.sh`, `entrypoint.sh`, `azure_storage.py`, and the approved no-Cosmos architecture; older all-purpose Azure Files guidance no longer matches the deployment.

## Understand the storage split

The Container App uses two Azure Storage mechanisms with different consistency models:

| Data | Current store | Container path | Behavior |
| --- | --- | --- | --- |
| Documents | Blob prefix `docs/` | `/deps/deep_research/docs` | Downloaded at startup; application writes may upload asynchronously |
| Generated output and evaluation history | Blob prefix `output/` | `/deps/deep_research/output` | Downloaded at startup; application writes may upload asynchronously |
| Input files | Blob prefix `input/` | `/deps/deep_research/input` | Downloaded at startup; application writes may upload asynchronously |
| LangGraph thread/run/checkpoint catalog | Blob prefix `.langgraph_api/` | `/deps/deep_research/.langgraph_api` | Local LangGraph development-runtime files restored at startup and synchronized to Blob |
| Auth/session and compatibility-route SQLite data | Azure Files share `deep-research-auth` | `/mnt/auth/auth.db` | Direct read/write mount; `SQLITE_DB_PATH` points here |

The SQLite file contains authentication/session data and the compatibility database used by custom routes. It is not the LangGraph thread catalog. `DB_TYPE=sqlite` selects this application database; empty `MEMORY_TYPE` leaves LangGraph persistence to its development runtime.

Cosmos adapters remain available for other deployments, but current Azure scripts do not provision Cosmos, set Cosmos environment variables, or synchronize Cosmos records with `.langgraph_api`. Do not enable both as competing thread catalogs.

## Create and mount storage

`deploy.sh` performs the supported setup:

1. Create or find a StorageV2 account with public Blob access disabled.
2. Create the `deep-research-blobs` Blob container.
3. Create the 1 GiB `deep-research-auth` Azure Files share.
4. Register that share as Container Apps environment storage `authsqlite`.
5. Store the account name, account key, and Blob container name in Key Vault.
6. Mount the share as `auth-sqlite` at `/mnt/auth`.
7. set `SQLITE_DB_PATH=/mnt/auth/auth.db` and `AUTH_SQLITE_JOURNAL_MODE=DELETE`.

Blob prefixes do not need pre-created directories. `azure_storage.py` creates local `docs`, `output`, `input`, and `.langgraph_api` paths and downloads matching prefixes at startup.

The current Blob mode takes precedence in `entrypoint.sh`, so it does not symlink those four directories into a broad Azure Files mount. No Dockerfile rewrite or `cifs-utils` installation is required for the platform-managed `/mnt/auth` mount.

Inspect effective storage configuration without exposing account keys:

```bash
source ./env.sh

az containerapp env storage list \
  --name "$ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output table

az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{volumes:properties.template.volumes,mounts:properties.template.containers[0].volumeMounts}'

az containerapp show \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "properties.template.containers[0].env[?name=='SQLITE_DB_PATH' || name=='AZURE_STORAGE_CONTAINER_NAME' || name=='MEMORY_TYPE' || name=='DB_TYPE']"
```

Expected invariants: one `auth-sqlite` volume mounted at `/mnt/auth`, `SQLITE_DB_PATH=/mnt/auth/auth.db`, `DB_TYPE=sqlite`, empty `MEMORY_TYPE`, a configured Blob container reference, and at most one replica.

## Synchronize files safely

`sync-files.sh` retrieves storage credentials from Key Vault and stages data under local `sync/`. It covers `docs`, `output`, `input`, `.langgraph_api`, and the legacy `deep_research.db` file when present.

```bash
# Cloud to local only
./sync-files.sh --download

# Local to cloud only
./sync-files.sh --upload

# Download, then upload
./sync-files.sh
```

Important behavior:

- Before an upload, local folders are mirrored into `sync/` with `rsync --delete`; files missing from the source workspace are removed from that local staging subtree.
- Azure Blob upload uses overwrite semantics.
- After a download, staged content is mirrored back to local folders with `rsync --delete`.
- Blob startup sync downloads cloud content but does not make local files a transactional shared filesystem.
- Application uploads are fire-and-forget. Inspect logs for failures; request success alone does not prove Blob durability.

> [!WARNING]
> Never upload `.langgraph_api` while a local or Azure LangGraph process can write it. Stop or deactivate all writers, verify there is no running replica, take a backup, then publish. One live writer can overwrite newer state with an older snapshot.

For routine document transfer, prefer the directional flags instead of the no-flag round trip. Review `git status` after downloading because local tracked content can change.

## Monitor storage

Use identity-based reads where your Azure role permits them:

```bash
source ./env.sh

STORAGE_ACCOUNT_NAME=$(az keyvault secret show \
  --vault-name "$KV_NAME" \
  --name STORAGE-ACCOUNT-NAME \
  --query value --output tsv)

BLOB_CONTAINER_NAME=$(az keyvault secret show \
  --vault-name "$KV_NAME" \
  --name AZURE-STORAGE-CONTAINER-NAME \
  --query value --output tsv)

az storage blob list \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --container-name "$BLOB_CONTAINER_NAME" \
  --auth-mode login \
  --query '[].{name:name,bytes:properties.contentLength,modified:properties.lastModified}' \
  --output table

az monitor metrics list \
  --resource "$(az storage account show --name "$STORAGE_ACCOUNT_NAME" --resource-group "$RESOURCE_GROUP" --query id --output tsv)" \
  --metric UsedCapacity \
  --output table
```

If your account lacks data-plane role access, use `sync-files.sh --download` rather than printing or pasting the storage key. Configure capacity alerts against the storage account and choose thresholds from measured usage, not the dated price and capacity examples in the retired monolithic guide.

## Verify persistence

### Verify Blob-backed folders

1. Quiesce writers if testing `.langgraph_api`; ordinary document tests do not require this.
2. Upload a uniquely named harmless file under `docs/` or `input/`.
3. Confirm the Blob exists with `az storage blob list` or `./sync-files.sh --download`.
4. Create a new Container App revision or restart the active revision.
5. Use `az containerapp exec` to confirm startup restore recreated the file.

Example diagnostic:

```bash
az containerapp exec \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "find /deps/deep_research/docs -maxdepth 2 -type f | head -20"
```

### Verify the Azure Files auth mount

This check writes one explicitly scoped marker. Remove it after verification.

```bash
MARKER="persistence-check.txt"

az containerapp exec \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "sh -c 'date -u > /mnt/auth/$MARKER && cat /mnt/auth/$MARKER'"

az containerapp revision list \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output table
```

After a controlled revision restart, read the same marker. When the test passes, delete only that marker:

```bash
az containerapp exec \
  --name "$AGENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command "rm -f -- /mnt/auth/persistence-check.txt"
```

Also inspect recent logs for startup-sync, permission, SQLite lock, or missing-directory errors.

## Migrate or roll back

### Migrate Blob-backed runtime state

1. Record expected LangGraph thread IDs from the known-good source.
2. Stop local LangGraph processes.
3. Deactivate the active Azure revision and wait until it has no running replica.
4. Download a dated backup with `./sync-files.sh --download` and copy the staged `.langgraph_api` outside `sync/`.
5. Put the known-good `.langgraph_api` in repository root and run `./sync-files.sh --upload` while all writers remain stopped.
6. Deploy or reactivate one revision.
7. Verify `DB_TYPE=sqlite`, empty `MEMORY_TYPE`, no Cosmos variables, one maximum replica, and the expected thread IDs through authenticated `POST /threads/search`.

An endpoint `401` or `403` means credentials must be fixed before judging persistence.

### Migrate the auth SQLite file

Quiesce the Container App before copying `auth.db`; a live SQLite copy can be inconsistent. Retrieve the storage key into a shell variable without echoing it, download the existing file as a backup, and upload the replacement to the `deep-research-auth` share only after validating it locally. Keep `AUTH_SQLITE_JOURNAL_MODE=DELETE` on the Azure Files mount.

### Roll back

- Image/config rollback: route traffic to a previously verified Container App revision, then check `/health` and the API version.
- Blob rollback: deactivate all writers, restore the dated backup with `./sync-files.sh --upload`, then reactivate one revision.
- Auth rollback: stop the app, restore the backed-up `auth.db` to the file share, then start one revision and test login/session behavior.
- Storage removal: do not detach `/mnt/auth` or delete the share until its database is backed up and the deployment has a replacement auth store. Ephemeral fallback loses sessions and passkey records after restart.

Never delete a Blob container, file share, storage account, or revision as part of a rollback unless the exact resource and recovery copy have been independently verified.

## Measure storage performance

Benchmark only a disposable path, record file size and region, and remove only the generated test file. Azure Files results from the retired guide were environment-specific and are not guarantees. For production sizing, measure representative small-file and SQLite workloads and use Azure Monitor latency, transaction, throttling, and capacity metrics.

## Related documentation

- [Azure deployment](README.md)
- [Azure operations](operations.md)
- [Azure security](security.md)
- [Azure troubleshooting](troubleshooting.md#storage-failures)
- [Configuration](../../guides/configuration.md)
- [Authentication](../../guides/authentication.md)
