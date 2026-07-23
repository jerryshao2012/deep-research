# LangGraph Dev S3 Snapshot Persistence Design

## Goal

Keep `langgraph dev` on AWS App Runner for a demo deployment while preventing
its in-memory thread catalog from replacing valid S3 state with an empty or
partial `.langgraph_api` snapshot.

## Constraints

- Continue running `langgraph dev --no-reload`.
- Use existing AWS App Runner and S3 resources only.
- Do not require PostgreSQL, Redis, or another persistent service.
- Keep App Runner at one instance.
- Preserve existing document, output, input, and wiki S3 synchronization.
- Treat intentional thread deletion as a paused-service maintenance action.

## Root Cause

The in-memory LangGraph runtime loads `.langgraph_api/.langgraph_ops.pckl` once
at startup. If the file is missing, copied too late, corrupt, or incompatible,
the runtime starts with an empty thread list. Its persistence loop writes that
memory state to disk every ten seconds. The existing five-second S3 upload loop
then overwrites the valid remote snapshot with the empty local file.

This is not thread TTL cleanup. The in-memory thread TTL sweep is a no-op and
the deployment does not configure `LANGGRAPH_THREAD_TTL`.

## Design

### Startup restore

`entrypoint.sh` restores LangGraph state before starting the Python process.
The restore operation:

1. Downloads the S3 snapshot manifest.
2. Rejects snapshots produced by incompatible LangGraph runtime versions.
3. Downloads the immutable generation referenced by the manifest into a
   temporary local directory.
4. Verifies every declared file's size and SHA-256, loads every pickle, and
   validates the thread catalog.
5. Replaces the local directory on the same filesystem by renaming the current
   directory aside, renaming staging into place, and rolling back on failure.
6. Falls back to the manifest's previous generation, then the existing
   canonical `.langgraph_api/` prefix during the
   initial migration when no generation manifest exists.
7. Fails startup instead of launching with empty state when S3 contains a
   previously valid non-empty snapshot that cannot be restored.
8. Claims a new writer epoch using an S3 conditional manifest update
   (`If-Match` against the ETag read at startup). A process that loses the
   conditional update fails startup and never serves traffic.

The custom FastAPI lifespan must not perform a second startup download after
the LangGraph runtime has already loaded its in-memory catalog.

### Snapshot publication

The background publisher observes local `.langgraph_api` changes but never
uploads files directly over the active snapshot. For each publish:

1. Wait for the LangGraph persistence flush to settle.
2. Require two identical source file fingerprints on consecutive scans, with
   no `.tmp` files present.
3. Copy every persistence pickle into a same-filesystem temporary directory,
   then confirm source fingerprints did not change during the copy.
4. Load every copied pickle, calculate size and SHA-256, validate
   `.langgraph_ops.pckl`, and check that checkpoint files are readable.
5. Compare candidate thread IDs with the last published manifest.
6. Reject the candidate if any published thread disappeared.
7. Upload all files under an immutable generation prefix.
8. Re-read the manifest and require both the process writer epoch and expected
   ETag to match.
9. Update the manifest with S3 `If-Match` only after every generation file
   succeeds. A failed conditional update fences the publisher permanently.

Manifest update is the commit point. A partial generation is never restored.
Thread additions and state updates are accepted. Empty or shrinking catalogs
are rejected and logged.

### Manifest and writer fencing

The JSON manifest uses a versioned schema:

- `schema_version`
- `active_generation`
- `previous_generation`
- `writer_epoch`
- `created_at`
- `thread_ids`
- `runtime_versions`
- `files`, containing key, size, and SHA-256 for every pickle

Generation names combine UTC timestamp and UUID. App Runner replacement claims
a new random writer epoch with an ETag-conditional update. Every later publish
requires that same epoch and current ETag. An old instance cannot overwrite a
new instance's manifest even when both exist briefly during deployment.

Retain the active generation, previous generation, and the five most recent
valid generations. Empty-bucket bootstrap accepts only an explicitly supplied,
validated non-empty snapshot or a genuinely new demo with no prior manifest.

### Runtime compatibility

Pickles are valid only for the runtime that created them. The AWS image must
install from a frozen lock file instead of resolving lower-bound dependencies
during every build. The Docker build includes `uv.lock`, uses frozen
installation, and records exact `langgraph-api`, `langgraph-runtime-inmem`,
`langgraph`, and Python versions in each manifest. Startup refuses incompatible
snapshots instead of deleting them or booting empty.

### Intentional deletion

Normal runtime snapshots cannot shrink the thread catalog. Intentional
deletion uses this maintenance flow:

1. Pause App Runner.
2. Prepare and validate the desired snapshot locally.
3. Publish it with an explicit `allow_shrink` maintenance flag.
4. Resume App Runner and verify the expected IDs.

This matches the existing cleanup workflow for the six empty demo threads.

### Documents and wiki

`docs/`, `output/`, and `input/` retain file-level background uploads.
Generated thread documents and wiki files remain under their existing S3
prefixes. Upload uses project-root runtime folders rather than assuming the
files already exist under `sync-aws/`. `.langgraph_api` is removed from the
generic folder mirror and is handled only by the validated generation
publisher.

`sync-files-aws.sh` must refuse direct `.langgraph_api` overwrite and delegate
that prefix to the guarded snapshot publisher. Document and wiki upload must
include `docs/threads/<thread_id>/` and
`docs/threads-wiki/<thread_id>/` from the project runtime tree.

Remote deletion is intentionally maintenance-only for this demo. Paused-service
cleanup removes deleted thread artifacts explicitly so stale S3 files cannot
be restored by accident.

### App Runner

- Command remains:
  `langgraph dev --host 0.0.0.0 --port 2024 --no-reload --no-browser`
- Autoscaling remains minimum 1, maximum 1.
- Health check uses `/ok`.
- Startup restore is synchronous and must finish before `langgraph dev`.

## Failure Handling

- Invalid local pickle: reject publish; retain current manifest.
- Mixed or changing pickle set: retry after the files settle.
- Failed generation upload: retain current manifest.
- Failed manifest update: uploaded generation remains unused.
- Writer epoch or ETag mismatch: stop publisher; old instance is fenced.
- Runtime version mismatch: reject restore without touching local/S3 state.
- Invalid S3 generation at startup: try previous recorded generation or
  canonical migration snapshot; otherwise fail closed.
- App Runner replacement: new instance restores committed generation before
  serving requests.
- Old instance writes during deployment: immutable generations prevent it from
  corrupting the committed files; single-instance configuration limits this
  demo risk.

## Tests

- Startup restore occurs before `langgraph dev`.
- Duplicate lifespan restore is absent.
- Frozen AWS build installs manifest-compatible runtime versions.
- Valid thread additions publish a new generation.
- Same thread set with updated state publishes.
- Empty and shrinking catalogs are rejected.
- Old writer loses an ETag race and cannot move the manifest.
- Same-thread stale state cannot roll the manifest backward.
- Changing or mixed-generation pickle sets are rejected.
- Incompatible runtime versions are rejected.
- Partial generation upload does not change manifest.
- Manifest restore produces expected thread IDs.
- Corrupt active generation falls back to previous generation.
- Existing local directory replacement rolls back after failure.
- Canonical prefix migration produces initial manifest.
- `sync-files-aws.sh` cannot bypass guarded `.langgraph_api` publication.
- Document and wiki uploads remain unchanged.
- Nested document listing and wiki endpoints survive restart.
- Integration check creates a thread, waits for publication, restarts the local
  server, and confirms `/threads/search` and `/threads/{id}` still return it.

## Deployment Verification

1. Pause App Runner.
2. Publish the protected seven-thread snapshot as initial generation.
3. Upload missing target thread documents and wiki files.
4. Build and deploy the guarded image.
5. Resume service.
6. Confirm `/ok` returns 200.
7. Confirm `/threads/search` is non-empty and contains
   `019f3a49-a376-7be2-901d-9f780579a865`.
8. Confirm target thread detail/state return 200.
9. Confirm Documents and Wiki tabs have backing files.
10. Wait beyond both persistence intervals and repeat checks.
11. restart App Runner and repeat checks.
