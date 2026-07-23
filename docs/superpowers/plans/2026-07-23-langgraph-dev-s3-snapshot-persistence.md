# LangGraph Dev S3 Snapshot Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep AWS App Runner on `langgraph dev` while making its demo thread catalog recoverable from guarded, immutable S3 snapshot generations that cannot be replaced by empty or stale in-memory state.

**Architecture:** Add a focused `langgraph_snapshot.py` module for snapshot validation, immutable generation publication, manifest CAS fencing, restore, and CLI operations. Keep `s3_storage.py` responsible only for documents/output/input. Restore LangGraph snapshot before process import; start guarded publisher after runtime startup; enforce read-only rollout through LangGraph auth handlers and FastAPI middleware.

**Tech Stack:** Python 3.12, boto3/S3, LangGraph in-memory runtime pickle files, FastAPI, AWS App Runner, pytest, shell integration tests.

**Spec:** `docs/superpowers/specs/2026-07-23-langgraph-dev-s3-snapshot-persistence-design.md`

**Workspace note:** Implementation remains in current workspace because relevant files already contain staged/uncommitted persistence work. Before each edit, inspect the targeted diff and preserve unrelated changes. Use `git commit --only -- <paths>` so pre-staged files are not accidentally included.

---

## File Structure

- Create `langgraph_snapshot.py`: all guarded LangGraph snapshot validation, restore, publish, fencing, background publisher, and CLI behavior.
- Create `tests/test_langgraph_snapshot.py`: unit tests using a deterministic fake S3 client and synthetic pickle snapshots.
- Create `tests/test_aws_read_only_mode.py`: read-only auth and FastAPI mutation-blocking tests.
- Modify `s3_storage.py`: generic runtime-file synchronization only; remove `.langgraph_api` and unsafe raw database snapshot behavior.
- Modify `entrypoint.sh`: synchronous guarded restore before `langgraph dev`; fail closed.
- Modify `webapp/__init__.py`: no late startup restore; start generic sync and guarded publisher; add custom-route read-only middleware.
- Modify `auth.py`: reject LangGraph thread create/update/delete/run actions during read-only rollout.
- Modify `sync-files-aws.sh`: prohibit raw `.langgraph_api` upload and source documents/wiki from project runtime folders.
- Modify `tests/test_aws_persistence_scripts.py`: shell/config regression tests.
- Modify `.dockerignore`: include `uv.lock` in AWS build context.
- Modify `Dockerfile-aws`: install exact locked dependencies and retain `langgraph dev --no-reload`.
- Modify `deploy-aws.sh`: snapshot/read-only settings, health route, single-instance configuration, and guarded rollout helpers.
- Modify `document/AWS_DEPLOY.md`: demo persistence and first-deployment maintenance procedure.

### Task 1: Snapshot validation model

**Files:**
- Create: `langgraph_snapshot.py`
- Create: `tests/test_langgraph_snapshot.py`

- [ ] **Step 1: Write failing validation tests**

Add synthetic snapshot helpers and tests covering:

```python
def test_validate_snapshot_returns_thread_versions_and_checksums(tmp_path):
    snapshot = make_snapshot(tmp_path, threads=[thread("t1", updated_at="2026-07-23T10:00:00+00:00")])
    result = validate_snapshot(snapshot, require_non_empty=True)
    assert result.thread_ids == ("t1",)
    assert result.thread_versions == {"t1": "2026-07-23T10:00:00+00:00"}
    assert result.files[".langgraph_ops.pckl"].sha256


def test_validate_snapshot_rejects_empty_catalog_when_required(tmp_path):
    snapshot = make_snapshot(tmp_path, threads=[])
    with pytest.raises(SnapshotValidationError, match="empty thread catalog"):
        validate_snapshot(snapshot, require_non_empty=True)


def test_candidate_rejects_thread_shrink_and_timestamp_rollback(tmp_path):
    previous = generation_metadata(["t1", "t2"], {"t1": "2026-07-23T10:00:00+00:00"})
    with pytest.raises(SnapshotValidationError, match="removed published threads"):
        assert_candidate_is_monotonic(candidate_missing_t2, previous)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_langgraph_snapshot.py -v
```

Expected: collection/import failure because `langgraph_snapshot` does not exist.

- [ ] **Step 3: Implement minimal validation types and functions**

Implement:

```python
class SnapshotValidationError(RuntimeError): ...

@dataclass(frozen=True)
class FileMetadata:
    size: int
    sha256: str

@dataclass(frozen=True)
class GenerationMetadata:
    generation: str
    created_at: str
    thread_ids: tuple[str, ...]
    thread_versions: dict[str, str]
    runtime_versions: dict[str, str]
    files: dict[str, FileMetadata]

def validate_snapshot(path: Path, *, require_non_empty: bool) -> GenerationMetadata: ...
def assert_candidate_is_monotonic(candidate: GenerationMetadata, previous: GenerationMetadata) -> None: ...
```

Load every `*.pckl`, reject `.tmp`, normalize UUID/dict thread entries, reject pending/running runs, hash every file, and record exact Python/LangGraph package versions.

- [ ] **Step 4: Run validation tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_langgraph_snapshot.py -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit only Task 1 paths**

```bash
git add langgraph_snapshot.py tests/test_langgraph_snapshot.py
git commit --only -m "feat: validate LangGraph snapshot generations" -- langgraph_snapshot.py tests/test_langgraph_snapshot.py
```

### Task 2: Immutable S3 generation publication and restore

**Files:**
- Modify: `langgraph_snapshot.py`
- Modify: `tests/test_langgraph_snapshot.py`

- [ ] **Step 1: Write failing S3 generation tests**

Use `FakeS3Client` with `get_object`, `put_object`, `download_file`, `upload_file`, and conditional `IfMatch` behavior. Cover:

```python
def test_publish_uploads_generation_before_conditional_pointer(tmp_path): ...
def test_partial_generation_upload_does_not_move_pointer(tmp_path): ...
def test_stale_writer_etag_cannot_move_pointer(tmp_path): ...
def test_empty_bucket_bootstrap_uses_if_none_match_star(tmp_path): ...
def test_canonical_prefix_is_migrated_to_first_generation(tmp_path): ...
def test_prior_manifest_plus_invalid_generations_fails_closed(tmp_path): ...
def test_unknown_root_or_generation_schema_is_rejected(tmp_path): ...
def test_restore_falls_back_to_previous_generation(tmp_path): ...
def test_restore_rolls_back_existing_directory_on_install_failure(tmp_path): ...
def test_runtime_version_mismatch_is_rejected(tmp_path): ...
```

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest tests/test_langgraph_snapshot.py -v
```

Expected: missing publication/restore APIs.

- [ ] **Step 3: Implement pointer/generation contracts**

Add:

```python
ROOT_MANIFEST_KEY = ".langgraph_snapshots/manifest.json"
GENERATION_PREFIX = ".langgraph_snapshots/generations"

def load_pointer(client, bucket) -> tuple[dict, str | None]: ...
def publish_generation(..., expected_etag: str | None, writer_epoch: str, allow_shrink: bool = False) -> str: ...
def restore_snapshot(..., target_dir: Path, allow_canonical_bootstrap: bool = True) -> GenerationMetadata: ...
```

Each generation stores its own immutable `manifest.json`. Upload all pickle files and generation manifest before the root pointer. Use `put_object(IfMatch=expected_etag)` for pointer updates. Install restore through same-filesystem staging, old-directory rename, rollback, and cleanup.
For a genuinely empty bucket, use `put_object(IfNoneMatch="*")`. Validate root
and generation `schema_version`. If a prior root pointer exists but neither
active nor previous generation validates, fail closed. Canonical-prefix
migration must validate the old snapshot and commit the first immutable
generation before runtime startup.

- [ ] **Step 4: Run S3 tests and verify GREEN**

```bash
uv run pytest tests/test_langgraph_snapshot.py -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git commit --only -m "feat: publish immutable LangGraph snapshots" -- langgraph_snapshot.py tests/test_langgraph_snapshot.py
```

### Task 3: Writer epoch, stable publisher, and fence monitor

**Files:**
- Modify: `langgraph_snapshot.py`
- Modify: `tests/test_langgraph_snapshot.py`

- [ ] **Step 1: Write failing publisher tests**

Cover:

```python
def test_read_only_startup_skips_writer_claim(tmp_path): ...
def test_failed_writer_claim_aborts_runtime_controller(tmp_path): ...
def test_successful_publish_advances_lease_etag_without_self_fencing(tmp_path): ...
def test_publisher_requires_two_fingerprints_twelve_seconds_apart(tmp_path): ...
def test_publisher_defers_while_run_is_active(tmp_path): ...
def test_fence_loss_terminates_process(tmp_path): ...
def test_same_thread_older_updated_at_is_not_published(tmp_path): ...
def test_changing_source_during_staging_is_rejected(tmp_path): ...
def test_mixed_pickle_set_is_rejected(tmp_path): ...
def test_retention_keeps_active_previous_and_five_recent(tmp_path): ...
```

Inject clock, sleep, and terminate callbacks so tests run instantly.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_langgraph_snapshot.py -v
```

Expected: missing writer/publisher APIs.

- [ ] **Step 3: Implement guarded runtime controller**

Add:

```python
@dataclass
class RuntimeLease:
    writer_epoch: str
    etag: str
    lock: threading.Lock
    fenced: threading.Event

def claim_writer_epoch(client, bucket, pointer, etag, epoch) -> str: ...
def start_runtime_controller(...) -> RuntimeLease | None: ...
def start_snapshot_publisher(..., stability_seconds: float = 12.0) -> threading.Thread | None: ...
def start_fence_monitor(..., interval_seconds: float = 2.0, terminate=os._exit) -> threading.Thread | None: ...
```

Read-only mode returns without claim/publisher. Normal mode claims writer epoch
before readiness and aborts startup if claim fails. It never restores files;
`entrypoint.sh` is the sole restore path and runs before LangGraph import.
Publisher and monitor share `RuntimeLease`; successful pointer CAS atomically
updates the lease ETag before the monitor's next comparison so the process
cannot fence itself. Publisher requires idle catalog and stable fingerprints
separated by at least twelve seconds.

Stage on the same filesystem, fingerprint before copy, copy every pickle,
fingerprint source again, reject any difference, then validate staged files.
Retention runs only after pointer commit and preserves active, previous, and
five newest valid generations. Fence mismatch marks lease fenced and calls
terminate with nonzero exit code.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_langgraph_snapshot.py -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Add CLI tests and implementation**

Test and implement:

```bash
python -m langgraph_snapshot restore
python -m langgraph_snapshot publish --source .langgraph_api
python -m langgraph_snapshot publish --source /tmp/clean-state --allow-shrink
```

CLI must return nonzero on validation, restore, CAS, or upload failure.

- [ ] **Step 6: Commit Task 3**

```bash
git commit --only -m "feat: fence LangGraph snapshot writers" -- langgraph_snapshot.py tests/test_langgraph_snapshot.py
```

### Task 4: Separate generic S3 sync from LangGraph state

**Files:**
- Modify: `s3_storage.py`
- Modify: `entrypoint.sh`
- Modify: `sync-files-aws.sh`
- Modify: `tests/test_aws_persistence_scripts.py`

- [ ] **Step 1: Replace old shell expectations with failing guard tests**

Add assertions that:

- `_resolve_tracked_folders()` excludes `.langgraph_api`.
- `entrypoint.sh` invokes `python -m langgraph_snapshot restore` before `exec "$@"`.
- restore failure stops startup.
- `python -m s3_storage startup` dispatches and returns nonzero on a required
  download failure.
- `sync-files-aws.sh --upload` never performs raw S3 copy to `.langgraph_api/`.
- upload sources `docs/threads` and `docs/threads-wiki` from project-root `docs`.
- Docker command retains `--no-reload`.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_aws_persistence_scripts.py -v
```

Expected: failures showing current raw `.langgraph_api` synchronization.

- [ ] **Step 3: Implement generic sync separation**

Remove `.langgraph_api` from generic tracked folders and periodic scanner. Keep docs/output/input uploads. Remove late/unsafe raw DB copying if it is not used by `langgraph dev`.

Change `entrypoint.sh` AWS order:

```bash
python3 -m s3_storage startup
python3 -m langgraph_snapshot restore
exec "$@"
```

Do not append `|| continue` to snapshot restore.

Change manual upload so `.langgraph_api` delegates to guarded CLI, while documents/wiki come from project runtime tree.

Add an explicit `s3_storage.main()`/`if __name__ == "__main__"` dispatcher for
the `startup` command and test it through a subprocess. Do not document a module
command that has no executable dispatch.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_aws_persistence_scripts.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git commit --only -m "fix: guard AWS LangGraph state synchronization" -- s3_storage.py entrypoint.sh sync-files-aws.sh tests/test_aws_persistence_scripts.py
```

### Task 5: Read-only rollout and application lifecycle

**Files:**
- Modify: `auth.py`
- Modify: `webapp/__init__.py`
- Create: `tests/test_aws_read_only_mode.py`

- [ ] **Step 1: Write failing read-only tests**

Test:

```python
@pytest.mark.parametrize("handler", [
    on_create_thread,
    on_update_thread,
    on_delete_thread,
    on_create_run,
])
async def test_langgraph_mutations_return_503_in_read_only_mode(handler, monkeypatch): ...

def test_custom_app_blocks_protected_mutations_but_allows_health_and_auth(client, monkeypatch): ...
def test_lifespan_does_not_run_late_snapshot_restore(): ...
def test_read_only_runtime_starts_when_s3_denies_every_write(): ...
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_aws_read_only_mode.py -v
```

Expected: update/delete/run handlers and middleware missing; lifespan still performs late restore.

- [ ] **Step 3: Implement read-only guards**

Add `_reject_if_s3_read_only()` to `auth.py`; call it from thread create/update/delete/create_run authorization handlers before ownership logic.

Add FastAPI middleware to return HTTP 503 only for an explicit protected-route
matrix: document upload/delete, wiki ingest/mutation, and thread-state mutation
routes. Keep health, authentication refresh/logout, diagnostics, and read-only
skill endpoints available. LangGraph thread/run mutations remain protected by
auth handlers. Test a client whose S3 writes all raise `AccessDenied`; read-only
restore and health must still succeed.

Remove `startup_sync()` from webapp lifespan. Start generic file daemon and
`start_runtime_controller()`; normal-mode lifespan must not yield until writer
claim succeeds. Read-only mode skips all S3 writes.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_aws_read_only_mode.py tests/test_aws_persistence_scripts.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git commit --only -m "feat: enforce read-only App Runner rollout" -- auth.py webapp/__init__.py tests/test_aws_read_only_mode.py
```

### Task 6: Freeze AWS runtime and deployment configuration

**Files:**
- Modify: `.dockerignore`
- Modify: `Dockerfile-aws`
- Modify: `deploy-aws.sh`
- Modify: `tests/test_aws_persistence_scripts.py`

- [ ] **Step 1: Write failing source-contract tests**

Assert:

- `.dockerignore` does not exclude `uv.lock`.
- `Dockerfile-aws` installs `uv`, uses `uv sync --frozen`, puts
  `/deps/deep_research/.venv/bin` on `PATH`, and keeps the exact
  `langgraph dev --no-reload` command.
- App Runner receives `LANGGRAPH_S3_READ_ONLY`, snapshot prefix, and stability settings.
- deploy script attaches min/max-one autoscaling configuration and `/ok` health check.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_aws_persistence_scripts.py -v
```

Expected: frozen-install and rollout configuration assertions fail.

- [ ] **Step 3: Implement frozen build and configuration**

Include `uv.lock`, install with frozen resolution, set:

```dockerfile
ENV PATH="/deps/deep_research/.venv/bin:$PATH"
```

Preserve existing image/platform constraints, and add:

```text
LANGGRAPH_S3_READ_ONLY=true
LANGGRAPH_SNAPSHOT_PREFIX=.langgraph_snapshots
LANGGRAPH_SNAPSHOT_STABILITY_SECONDS=12
LANGGRAPH_FENCE_INTERVAL_SECONDS=2
```

Keep initial deployment read-only. Add an explicit deploy-script option to switch to guarded read-write only after verification.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_aws_persistence_scripts.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Build and smoke-test the AWS container**

```bash
docker build -f Dockerfile-aws -t deep-research:guarded-snapshot .
docker run --rm --entrypoint sh deep-research:guarded-snapshot -c \
  'command -v langgraph && langgraph dev --help >/dev/null'
```

Expected: `langgraph` resolves from `/deps/deep_research/.venv/bin` and exits
zero. Run a container command that prints Python and LangGraph package versions;
compare them with `runtime_versions` generated by a container-created fixture
before using local snapshots for AWS migration.

- [ ] **Step 6: Commit Task 6**

```bash
git commit --only -m "build: freeze AWS LangGraph demo runtime" -- .dockerignore Dockerfile-aws deploy-aws.sh tests/test_aws_persistence_scripts.py
```

### Task 7: Full local verification

**Files:**
- Modify if needed: implementation/test files from Tasks 1–6

- [ ] **Step 1: Run focused test suite**

```bash
uv run pytest tests/test_langgraph_snapshot.py tests/test_aws_read_only_mode.py tests/test_aws_persistence_scripts.py -v
```

Expected: zero failures.

- [ ] **Step 2: Run existing API contract tests**

```bash
uv run pytest tests/test_frontend_api_contract.py tests/test_server.py -v
```

Expected: zero failures.

- [ ] **Step 3: Run lint on changed Python files**

```bash
uv run ruff check langgraph_snapshot.py s3_storage.py auth.py webapp/__init__.py tests/test_langgraph_snapshot.py tests/test_aws_read_only_mode.py tests/test_aws_persistence_scripts.py
```

Expected: zero errors.

- [ ] **Step 4: Run local restart recovery**

1. Publish a temporary local generation through a fake/local S3 test fixture.
2. Start `langgraph dev --no-reload`.
3. Confirm `/threads/search` contains fixture thread.
4. Stop server, remove local `.langgraph_api`, restore generation, restart.
5. Confirm same thread detail/state returns 200.

- [ ] **Step 5: Review final diff**

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors; unrelated dirty files remain untouched.

### Task 8: Guarded AWS migration and deployment

**Files:**
- Modify: `document/AWS_DEPLOY.md`

- [ ] **Step 1: Document first guarded rollout**

Record exact pause, IAM temporary deny, resume, read-only deploy, verification, IAM restore, read-write deploy, and rollback commands without embedding secrets.

- [ ] **Step 2: Verify protected local snapshot**

Use `/tmp/deep-research-clean-seven-thread-state` and assert:

```text
thread_count=7
target_present=true
```

- [ ] **Step 3: Publish initial immutable generation while service is paused**

Run guarded CLI with `--allow-shrink`. Confirm root pointer, active generation manifest, checksums, runtime versions, and seven IDs.

- [ ] **Step 4: Upload missing target documents and wiki**

Upload:

```text
docs/threads/019f3a49-a376-7be2-901d-9f780579a865/
docs/threads-wiki/019f3a49-a376-7be2-901d-9f780579a865/
```

Verify nonzero S3 key counts.

- [ ] **Step 5: Delete artifacts for intentionally removed threads**

While service remains paused, delete `docs/threads/<thread_id>/` and
`docs/threads-wiki/<thread_id>/` prefixes for the six approved trash IDs only.
List each prefix before deletion, then verify zero keys afterward. Do not use a
recursive delete against `docs/threads/` or `docs/threads-wiki/` roots.

- [ ] **Step 6: Apply temporary IAM deny and resume current service**

Confirm old image cannot write `.langgraph_api/*` or snapshot manifest.

- [ ] **Step 7: Build and deploy guarded image read-only**

Wait for App Runner operation completion. Verify `/ok`, `/threads/search`, target detail/state, document list, and wiki status.

- [ ] **Step 8: Enable guarded read-write mode**

Restore IAM write permission, update environment to `LANGGRAPH_S3_READ_ONLY=false`, deploy, and wait for completion.

- [ ] **Step 9: Verify persistence beyond flush windows**

Check immediately, after at least thirty seconds, and after App Runner replacement:

```text
/ok = 200
/threads/search count >= 7
target thread detail = 200
target thread state = 200
documents list count > 0
wiki backing files count > 0
```

- [ ] **Step 10: Verify new thread survives restart**

Create one demo thread, wait for immutable generation publication, restart App Runner, and confirm the new ID remains listed.

- [ ] **Step 11: Final commit**

```bash
git commit --only -m "docs: document guarded AWS demo persistence" -- document/AWS_DEPLOY.md
```
