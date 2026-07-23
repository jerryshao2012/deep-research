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
2. Downloads the immutable generation referenced by the manifest into a
   temporary local directory.
3. Loads `.langgraph_ops.pckl` and validates its thread catalog.
4. Atomically replaces the local `.langgraph_api` directory only after every
   snapshot file has downloaded and validation succeeds.
5. Falls back to the existing canonical `.langgraph_api/` prefix during the
   initial migration when no generation manifest exists.
6. Fails startup instead of launching with empty state when S3 contains a
   previously valid non-empty snapshot that cannot be restored.

The custom FastAPI lifespan must not perform a second startup download after
the LangGraph runtime has already loaded its in-memory catalog.

### Snapshot publication

The background publisher observes local `.langgraph_api` changes but never
uploads files directly over the active snapshot. For each publish:

1. Wait for the LangGraph persistence flush to settle.
2. Copy local pickle files into a temporary directory.
3. Load and validate `.langgraph_ops.pckl`.
4. Compare candidate thread IDs with the last published manifest.
5. Reject the candidate if any published thread disappeared.
6. Upload all files under an immutable generation prefix.
7. Update the manifest only after every generation file succeeds.

Manifest update is the commit point. A partial generation is never restored.
Thread additions and state updates are accepted. Empty or shrinking catalogs
are rejected and logged.

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
prefixes. `.langgraph_api` is removed from the generic folder mirror and is
handled only by the validated generation publisher.

### App Runner

- Command remains:
  `langgraph dev --host 0.0.0.0 --port 2024 --no-reload --no-browser`
- Autoscaling remains minimum 1, maximum 1.
- Health check uses `/ok`.
- Startup restore is synchronous and must finish before `langgraph dev`.

## Failure Handling

- Invalid local pickle: reject publish; retain current manifest.
- Failed generation upload: retain current manifest.
- Failed manifest update: uploaded generation remains unused.
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
- Valid thread additions publish a new generation.
- Same thread set with updated state publishes.
- Empty and shrinking catalogs are rejected.
- Partial generation upload does not change manifest.
- Manifest restore produces expected thread IDs.
- Canonical prefix migration produces initial manifest.
- Document and wiki uploads remain unchanged.
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
