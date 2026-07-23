from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "handler_name",
    [
        "on_create_thread",
        "on_update_thread",
        "on_delete_thread",
        "on_create_run",
    ],
)
def test_langgraph_mutations_return_503_in_read_only_mode(
    handler_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import auth

    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("LANGGRAPH_S3_READ_ONLY", "true")
    handler = getattr(auth, handler_name)

    with pytest.raises(auth.Auth.exceptions.HTTPException) as exc_info:
        asyncio.run(handler(SimpleNamespace(), {}))

    assert exc_info.value.status_code == 503
    assert "read-only" in exc_info.value.detail.lower()


@pytest.mark.parametrize(
    "handler_name",
    [
        "on_create_thread",
        "on_update_thread",
        "on_delete_thread",
        "on_create_run",
    ],
)
def test_langgraph_mutations_remain_allowed_outside_read_only_mode(
    handler_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import auth

    monkeypatch.delenv("LANGGRAPH_S3_READ_ONLY", raising=False)
    handler = getattr(auth, handler_name)

    assert asyncio.run(handler(SimpleNamespace(), {})) is None


def test_read_only_flag_does_not_change_non_aws_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import auth

    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("LANGGRAPH_S3_READ_ONLY", "true")

    assert asyncio.run(auth.on_create_thread(SimpleNamespace(), {})) is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/documents/upload"),
        ("DELETE", "/documents/example.pdf"),
        ("DELETE", "/documents/folder/threads/demo"),
        ("POST", "/threads/thread-1/wiki/ingest"),
        ("POST", "/threads/thread-1/wiki/ingest/cancel"),
        ("POST", "/threads/thread-1/wiki/query"),
        ("POST", "/threads/thread-1/wiki/lint"),
        ("DELETE", "/threads/thread-1/wiki"),
        ("POST", "/chat_threads/thread-1/state"),
        ("POST", "/skills/upload"),
        ("DELETE", "/skills/custom-skill"),
    ],
)
def test_read_only_route_matrix_contains_persistent_custom_mutations(
    method: str,
    path: str,
) -> None:
    import webapp

    assert webapp._is_protected_s3_mutation(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health"),
        ("GET", "/storage/info"),
        ("GET", "/documents/list"),
        ("GET", "/threads/thread-1/wiki/graph"),
        ("GET", "/skills"),
        ("POST", "/auth/session/refresh"),
        ("POST", "/auth/logout"),
        ("POST", "/threads/search"),
        ("POST", "/assistants/search"),
        ("POST", "/skills/upload/extra"),
        ("POST", "/skills"),
        ("DELETE", "/skills"),
        ("DELETE", "/skills/custom-skill/extra"),
        ("GET", "/skills/upload"),
        ("PUT", "/skills/upload"),
        ("GET", "/documents/upload"),
        ("PUT", "/documents/upload"),
        ("GET", "/threads/thread-1/wiki/ingest"),
        ("PATCH", "/chat_threads/thread-1/state"),
    ],
)
def test_read_only_route_matrix_does_not_block_unrelated_or_read_routes(
    method: str,
    path: str,
) -> None:
    import webapp

    assert not webapp._is_protected_s3_mutation(method, path)


def test_custom_app_returns_503_only_for_protected_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp

    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("LANGGRAPH_S3_READ_ONLY", "true")
    with TestClient(webapp.app) as client:
        blocked = client.post("/documents/upload")
        blocked_skill_upload = client.post("/skills/upload")
        blocked_skill_delete = client.delete("/skills/custom-skill")
        health = client.get("/health")
        search = client.post("/threads/search")
        logout = client.post("/auth/logout")

    assert blocked.status_code == 503
    assert "read-only" in blocked.json()["detail"].lower()
    assert blocked_skill_upload.status_code == 503
    assert blocked_skill_delete.status_code == 503
    assert health.status_code == 200
    assert search.status_code != 503
    assert logout.json()["detail"] != webapp._READ_ONLY_DETAIL


def test_custom_app_allows_mutations_when_read_only_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp

    monkeypatch.delenv("LANGGRAPH_S3_READ_ONLY", raising=False)
    with TestClient(webapp.app) as client:
        response = client.post("/documents/upload")

    assert response.status_code == 422


def test_aws_read_write_lifespan_claims_writer_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langgraph_snapshot
    import s3_storage
    import webapp

    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("LANGGRAPH_S3_READ_ONLY", raising=False)
    events: list[str] = []
    lease = SimpleNamespace(stop_event=threading.Event())
    daemon = SimpleNamespace(stop_event=threading.Event())

    def claim_writer():
        events.append("claim")
        return lease

    def start_generic_daemon(_interval_seconds: float):
        events.append("generic-daemon")
        return daemon

    monkeypatch.setattr(langgraph_snapshot, "start_runtime_controller", claim_writer)
    monkeypatch.setattr(webapp, "_start_generic_s3_upload_daemon", start_generic_daemon)
    monkeypatch.setattr(
        webapp,
        "_stop_runtime_controller",
        lambda actual: events.append(f"stop-runtime:{actual is lease}"),
    )
    monkeypatch.setattr(
        webapp,
        "_stop_generic_s3_upload_daemon",
        lambda actual: events.append(f"stop-generic:{actual is daemon}"),
    )
    monkeypatch.setattr(
        s3_storage,
        "startup_sync",
        lambda: pytest.fail("lifespan performed a late startup download"),
    )

    async def exercise() -> None:
        async with webapp._lifespan(webapp.app):
            events.append("yield")

    asyncio.run(exercise())

    assert events[:3] == ["claim", "generic-daemon", "yield"]
    assert events[-2:] == ["stop-generic:True", "stop-runtime:True"]


def test_writer_claim_failure_prevents_lifespan_from_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langgraph_snapshot
    import webapp

    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("LANGGRAPH_S3_READ_ONLY", raising=False)
    daemon_started = False

    def fail_claim():
        raise langgraph_snapshot.SnapshotPublishError("writer epoch conflict")

    def start_generic_daemon(_interval_seconds: float):
        nonlocal daemon_started
        daemon_started = True

    monkeypatch.setattr(langgraph_snapshot, "start_runtime_controller", fail_claim)
    monkeypatch.setattr(webapp, "_start_generic_s3_upload_daemon", start_generic_daemon)

    async def exercise() -> None:
        async with webapp._lifespan(webapp.app):
            pytest.fail("lifespan yielded after writer claim failure")

    with pytest.raises(
        langgraph_snapshot.SnapshotPublishError,
        match="writer epoch conflict",
    ):
        asyncio.run(exercise())
    assert daemon_started is False


def test_lifespan_does_not_run_late_snapshot_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langgraph_snapshot
    import s3_storage
    import webapp

    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("LANGGRAPH_S3_READ_ONLY", "true")
    monkeypatch.setattr(
        s3_storage,
        "startup_sync",
        lambda: pytest.fail("lifespan performed a late generic download"),
    )
    monkeypatch.setattr(
        langgraph_snapshot,
        "restore_snapshot",
        lambda *args, **kwargs: pytest.fail("lifespan restored after runtime load"),
    )

    async def exercise() -> None:
        async with webapp._lifespan(webapp.app):
            pass

    asyncio.run(exercise())


def test_read_only_runtime_starts_when_s3_denies_every_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langgraph_snapshot
    import s3_storage
    import webapp

    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("LANGGRAPH_S3_READ_ONLY", "true")

    def deny_write_client():
        raise PermissionError("AccessDenied: PutObject")

    monkeypatch.setattr(s3_storage, "_get_client", deny_write_client)
    monkeypatch.setattr(langgraph_snapshot, "_build_s3_client", deny_write_client)
    monkeypatch.setattr(
        webapp,
        "_start_generic_s3_upload_daemon",
        lambda: pytest.fail("read-only mode started generic S3 writes"),
    )

    with TestClient(webapp.app) as client:
        health = client.get("/health")
        blocked = client.post("/chat_threads/thread-1/state", json={"values": {}})

    assert health.status_code == 200
    assert blocked.status_code == 503


def test_non_aws_lifespan_does_not_start_s3_controllers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langgraph_snapshot
    import webapp

    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("LANGGRAPH_S3_READ_ONLY", raising=False)
    monkeypatch.setattr(
        langgraph_snapshot,
        "start_runtime_controller",
        lambda: pytest.fail("non-AWS lifecycle started snapshot controller"),
    )
    monkeypatch.setattr(
        webapp,
        "_start_generic_s3_upload_daemon",
        lambda: pytest.fail("non-AWS lifecycle started generic S3 daemon"),
    )

    async def exercise() -> None:
        async with webapp._lifespan(webapp.app):
            pass

    asyncio.run(exercise())


def test_generic_s3_upload_loop_mirrors_only_tracked_folders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import s3_storage
    import webapp

    tracked = [
        ("docs", tmp_path / "docs"),
        ("output", tmp_path / "output"),
        ("input", tmp_path / "input"),
    ]
    uploads: list[tuple[object, str]] = []

    class StopAfterOneScan:
        calls = 0

        def wait(self, _interval_seconds: float) -> bool:
            self.calls += 1
            return self.calls > 1

        def is_set(self) -> bool:
            return False

    monkeypatch.setattr(s3_storage, "_resolve_tracked_folders", lambda: tracked)
    monkeypatch.setattr(
        s3_storage,
        "upload_directory_sync",
        lambda path, prefix: uploads.append((path, prefix)),
    )

    webapp._generic_s3_upload_loop(StopAfterOneScan(), 5.0)

    assert uploads == [(path, prefix) for prefix, path in tracked]


def test_generic_s3_upload_loop_retries_after_cycle_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import s3_storage
    import webapp

    resolve_calls = 0
    uploads: list[tuple[object, str]] = []

    class StopAfterTwoScans:
        calls = 0

        def wait(self, _interval_seconds: float) -> bool:
            self.calls += 1
            return self.calls > 2

        def is_set(self) -> bool:
            return False

    def resolve_tracked_folders():
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 1:
            raise RuntimeError("temporary scan failure")
        return [("docs", tmp_path / "docs")]

    monkeypatch.setattr(s3_storage, "_resolve_tracked_folders", resolve_tracked_folders)
    monkeypatch.setattr(
        s3_storage,
        "upload_directory_sync",
        lambda path, prefix: uploads.append((path, prefix)),
    )

    webapp._generic_s3_upload_loop(StopAfterTwoScans(), 5.0)

    assert resolve_calls == 2
    assert uploads == [(tmp_path / "docs", "docs")]


def test_sleeping_persistence_workers_stop_promptly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import langgraph_snapshot
    import webapp

    lease = langgraph_snapshot.RuntimeLease(
        writer_epoch="writer-1",
        etag='"etag-1"',
    )
    publisher_scanned = threading.Event()
    fence_checked = threading.Event()

    def deferred_fingerprint(_source_dir):
        publisher_scanned.set()
        raise langgraph_snapshot.SnapshotPublishError("not ready")

    def current_pointer(_client, _bucket):
        fence_checked.set()
        return {"writer_epoch": lease.writer_epoch}, lease.etag

    monkeypatch.setattr(
        langgraph_snapshot,
        "_snapshot_fingerprint",
        deferred_fingerprint,
    )
    monkeypatch.setattr(langgraph_snapshot, "load_pointer", current_pointer)

    langgraph_snapshot.start_snapshot_publisher(
        object(),
        "bucket",
        tmp_path,
        lease,
        scan_interval_seconds=30.0,
    )
    langgraph_snapshot.start_fence_monitor(
        object(),
        "bucket",
        lease,
        interval_seconds=30.0,
    )
    assert publisher_scanned.wait(timeout=1.0)
    assert fence_checked.wait(timeout=1.0)

    started = time.monotonic()
    webapp._stop_runtime_controller(lease, timeout_seconds=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert all(not worker.is_alive() for worker in lease.threads)


def test_sleeping_generic_worker_stops_promptly() -> None:
    import webapp

    daemon = webapp._start_generic_s3_upload_daemon(interval_seconds=30.0)

    started = time.monotonic()
    webapp._stop_generic_s3_upload_daemon(daemon, timeout_seconds=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert not daemon.thread.is_alive()


def test_blocked_runtime_worker_shutdown_is_bounded_and_explicit() -> None:
    import langgraph_snapshot
    import webapp

    release = threading.Event()
    entered = threading.Event()

    def blocked_s3_call() -> None:
        entered.set()
        release.wait()

    worker = threading.Thread(target=blocked_s3_call, name="blocked-s3", daemon=True)
    lease = langgraph_snapshot.RuntimeLease(
        writer_epoch="writer-1",
        etag='"etag-1"',
        threads=[worker],
    )
    worker.start()
    assert entered.wait(timeout=1.0)

    started = time.monotonic()
    try:
        with pytest.raises(
            webapp.PersistenceWorkerShutdownError,
            match="blocked-s3",
        ):
            webapp._stop_runtime_controller(lease, timeout_seconds=0.05)
    finally:
        release.set()
        worker.join(timeout=1.0)

    assert time.monotonic() - started < 0.5


def test_blocked_generic_worker_shutdown_is_bounded_and_explicit() -> None:
    import webapp

    release = threading.Event()
    entered = threading.Event()

    def blocked_s3_call() -> None:
        entered.set()
        release.wait()

    worker = threading.Thread(target=blocked_s3_call, name="blocked-generic", daemon=True)
    daemon = webapp._GenericS3Daemon(
        stop_event=threading.Event(),
        thread=worker,
    )
    worker.start()
    assert entered.wait(timeout=1.0)

    started = time.monotonic()
    try:
        with pytest.raises(
            webapp.PersistenceWorkerShutdownError,
            match="blocked-generic",
        ):
            webapp._stop_generic_s3_upload_daemon(
                daemon,
                timeout_seconds=0.05,
            )
    finally:
        release.set()
        worker.join(timeout=1.0)

    assert time.monotonic() - started < 0.5


def test_invalid_generic_sync_interval_prevents_writer_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langgraph_snapshot
    import webapp

    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("LANGGRAPH_S3_READ_ONLY", raising=False)
    monkeypatch.setenv("S3_SYNC_INTERVAL_SECONDS", "nan")
    claimed = False

    def claim_writer():
        nonlocal claimed
        claimed = True

    monkeypatch.setattr(langgraph_snapshot, "start_runtime_controller", claim_writer)

    async def exercise() -> None:
        async with webapp._lifespan(webapp.app):
            pytest.fail("lifespan yielded with invalid sync interval")

    with pytest.raises(ValueError, match="finite and greater than zero"):
        asyncio.run(exercise())
    assert claimed is False
