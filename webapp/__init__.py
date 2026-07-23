"""webapp package — public API surface for the Document Upload API.

This module replaces the former monolithic ``webapp.py``.  It:

1. Creates and configures the FastAPI application instance.
2. Re-exports every public symbol that external code (tests, and the
   deprecated ``server.py``) used to import so that no import path
   changes are needed.
"""

# Imports below config bootstrap are intentional: LangGraph can load this file
# without normal package context.
# ruff: noqa: E402, T201

from __future__ import annotations

# ── Package bootstrap ─────────────────────────────────────────────────────────
# When langgraph's ``load_custom_app`` loads this file via
# ``spec_from_file_location("user_router_module", path)`` the module has no
# parent-package context, so relative imports (``from .config import …``) fail.
#
# Solution: import submodules by *file path* using ``importlib.util`` and
# register them under the ``webapp`` namespace in ``sys.modules``.  This works
# identically whether the package is loaded normally or by langgraph's
# file-based loader.
import importlib.util as _ilu
import sys as _sys
import types as _types
from pathlib import Path as _Path

_webapp_dir = _Path(__file__).resolve().parent
_deep_research_dir = _webapp_dir.parent

if str(_deep_research_dir) not in _sys.path:
    _sys.path.insert(0, str(_deep_research_dir))


def _import_submodule(name: str):
    """Import a webapp submodule by file path — no package context needed."""
    fqn = f"webapp.{name}"
    mod = _sys.modules.get(fqn)
    if mod is not None:
        return mod
    spec = _ilu.spec_from_file_location(fqn, _webapp_dir / f"{name}.py")
    mod = _ilu.module_from_spec(spec)
    _sys.modules[fqn] = mod
    spec.loader.exec_module(mod)
    return mod


# Ensure ``webapp`` exists in sys.modules so submodules can resolve their
# parent.  If we're being loaded normally, Python already created it; if
# langgraph loaded us via spec, we need a placeholder.
if "webapp" not in _sys.modules:
    _pkg = _types.ModuleType("webapp")
    _pkg.__path__ = [str(_webapp_dir)]
    _pkg.__package__ = "webapp"
    _pkg.__file__ = str(_webapp_dir / "__init__.py")
    _sys.modules["webapp"] = _pkg

# ── Load config (direct file import — no relative import needed) ──────────────
_config = _import_submodule("config")

API_KEY = _config.API_KEY
API_VERSION = _config.API_VERSION
DOCS_ROOT = _config.DOCS_ROOT
FRONTEND_ORIGINS = _config.FRONTEND_ORIGINS
OAUTH_ENABLED = _config.OAUTH_ENABLED

import logging
import math
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import NamedTuple

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger(__name__)

_PERSISTENCE_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_READ_ONLY_DETAIL = (
    "AWS demo is temporarily read-only during S3 persistence maintenance."
)
_PROTECTED_S3_MUTATIONS = (
    ("POST", re.compile(r"^/documents/upload/?$")),
    ("DELETE", re.compile(r"^/documents(?:/.*)?$")),
    (
        "POST",
        re.compile(
            r"^/threads/[^/]+/wiki/(?:ingest(?:/cancel)?|query|lint)/?$"
        ),
    ),
    ("DELETE", re.compile(r"^/threads/[^/]+/wiki/?$")),
    ("POST", re.compile(r"^/chat_threads/[^/]+/state/?$")),
    ("POST", re.compile(r"^/skills/upload/?$")),
    ("DELETE", re.compile(r"^/skills/[^/]+/?$")),
)


def _s3_read_only_enabled() -> bool:
    aws_mode = bool(
        os.environ.get("S3_BUCKET_NAME") and os.environ.get("AWS_REGION")
    )
    read_only = (
        os.environ.get("LANGGRAPH_S3_READ_ONLY", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    return aws_mode and read_only


def _is_protected_s3_mutation(method: str, path: str) -> bool:
    """Return whether a custom-app request writes persisted demo data."""
    normalized_method = method.upper()
    return any(
        normalized_method == protected_method and pattern.fullmatch(path)
        for protected_method, pattern in _PROTECTED_S3_MUTATIONS
    )


class _GenericS3Daemon(NamedTuple):
    stop_event: threading.Event
    thread: threading.Thread


class PersistenceWorkerShutdownError(RuntimeError):
    """Raised when an in-flight persistence worker exceeds shutdown timeout."""


def _generic_s3_upload_loop(
    stop_event: threading.Event,
    interval_seconds: float,
) -> None:
    """Periodically mirror generic runtime folders without LangGraph state."""
    from s3_storage import _resolve_tracked_folders, upload_directory_sync

    while not stop_event.wait(interval_seconds):
        try:
            for s3_prefix, local_path in _resolve_tracked_folders():
                if stop_event.is_set():
                    return
                upload_directory_sync(local_path, s3_prefix)
        except Exception:
            logger.exception("generic S3 upload cycle failed; retrying")


def _generic_s3_sync_interval_seconds() -> float:
    raw_interval = os.environ.get("S3_SYNC_INTERVAL_SECONDS", "5")
    try:
        interval_seconds = float(raw_interval)
    except ValueError as exc:
        raise ValueError("S3_SYNC_INTERVAL_SECONDS must be a number") from exc
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("S3_SYNC_INTERVAL_SECONDS must be finite and greater than zero")
    return interval_seconds


def _start_generic_s3_upload_daemon(
    interval_seconds: float | None = None,
) -> _GenericS3Daemon:
    if interval_seconds is None:
        interval_seconds = _generic_s3_sync_interval_seconds()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_generic_s3_upload_loop,
        args=(stop_event, interval_seconds),
        name="generic-s3-upload",
        daemon=True,
    )
    thread.start()
    return _GenericS3Daemon(stop_event=stop_event, thread=thread)


def _join_persistence_workers(
    workers: list[threading.Thread],
    *,
    timeout_seconds: float,
) -> None:
    timeout_seconds = float(timeout_seconds)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("persistence worker shutdown timeout must be positive")
    deadline = time.monotonic() + timeout_seconds
    for worker in workers:
        if worker.is_alive():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
    alive = [worker.name for worker in workers if worker.is_alive()]
    if alive:
        names = ", ".join(alive)
        raise PersistenceWorkerShutdownError(
            "persistence workers did not stop within "
            f"{timeout_seconds:g}s; in-flight S3 work remains: {names}"
        )


def _stop_generic_s3_upload_daemon(
    daemon: _GenericS3Daemon | None,
    *,
    timeout_seconds: float = _PERSISTENCE_WORKER_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    """Stop generic worker or fail after bounded in-flight S3 grace period."""
    if daemon is None:
        return
    daemon.stop_event.set()
    _join_persistence_workers(
        [daemon.thread],
        timeout_seconds=timeout_seconds,
    )


def _stop_runtime_controller(
    lease: object | None,
    *,
    timeout_seconds: float = _PERSISTENCE_WORKER_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    """Stop snapshot workers or fail after bounded in-flight S3 grace period."""
    if lease is None:
        return
    lease.stop_event.set()
    _join_persistence_workers(
        list(lease.threads),
        timeout_seconds=timeout_seconds,
    )


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    _runtime_lease = None
    _generic_s3_daemon = None
    try:
        import db
        db.init_db()
        print("✅ Database initialized via lifespan")
    except Exception as exc:
        print(f"⚠️  Database initialization skipped or failed: {exc}")

    # Set up persistent LangGraph checkpointer if MEMORY_TYPE=sqlite|postgres.
    # Module-level import defaults to InMemorySaver; we swap it here once the
    # event loop is running.
    _checkpointer_conn = None
    try:
        import os as _os
        _mem_type = _os.environ.get("MEMORY_TYPE", "memory").strip().lower()

        if _mem_type == "sqlite":
            from pathlib import Path as _Path

            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            _sqlite_path = _os.environ.get(
                "SQLITE_DB_PATH",
                str(_Path(__file__).resolve().parent / "checkpoints.db"),
            )
            _checkpointer_conn = await aiosqlite.connect(_sqlite_path)
            _saver = AsyncSqliteSaver(_checkpointer_conn)
            await _saver.setup()

            from agent import agent as _agent
            _agent.checkpointer = _saver
            print(f"✅ Checkpointer initialized: AsyncSqliteSaver → {_sqlite_path}")

        elif _mem_type in ("postgres", "postgresql"):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            _pg_uri = _os.environ["POSTGRES_URI"]
            _saver = AsyncPostgresSaver.from_conn_string(_pg_uri)
            # from_conn_string is not a context manager for postgres; returns directly
            if hasattr(_saver, "__aenter__"):
                async with _saver as _s:
                    await _s.setup()
                    from agent import agent as _agent
                    _agent.checkpointer = _s
            else:
                await _saver.setup()
                from agent import agent as _agent
                _agent.checkpointer = _saver
            print("✅ Checkpointer initialized: AsyncPostgresSaver")
    except Exception as _exc:
        print(f"⚠️  Persistent checkpointer setup skipped (using InMemorySaver): {_exc}")

    try:
        from s3_storage import is_s3_enabled

        if is_s3_enabled():
            from langgraph_snapshot import start_runtime_controller

            sync_interval_seconds = (
                None
                if _s3_read_only_enabled()
                else _generic_s3_sync_interval_seconds()
            )
            _runtime_lease = start_runtime_controller()
            if sync_interval_seconds is not None:
                _generic_s3_daemon = _start_generic_s3_upload_daemon(
                    sync_interval_seconds
                )

        yield
    finally:
        if _generic_s3_daemon is not None:
            _generic_s3_daemon.stop_event.set()
        if _runtime_lease is not None:
            _runtime_lease.stop_event.set()

        shutdown_errors: list[PersistenceWorkerShutdownError] = []
        for stop_worker, worker in (
            (_stop_generic_s3_upload_daemon, _generic_s3_daemon),
            (_stop_runtime_controller, _runtime_lease),
        ):
            try:
                stop_worker(worker)
            except PersistenceWorkerShutdownError as exc:
                shutdown_errors.append(exc)

        # Cleanup: close checkpointer connection if we opened one
        if _checkpointer_conn is not None:
            await _checkpointer_conn.close()
            print("✅ Checkpointer connection closed")
        if shutdown_errors:
            raise PersistenceWorkerShutdownError(
                "; ".join(str(error) for error in shutdown_errors)
            )


# ── Application factory ───────────────────────────────────────────────────────

app = FastAPI(
    title="Document Upload API",
    description="Upload documents to the deep research agent docs folder",
    version=API_VERSION,
    lifespan=_lifespan,
)


@app.middleware("http")
async def _enforce_s3_read_only(request: Request, call_next):
    if _s3_read_only_enabled() and _is_protected_s3_mutation(
        request.method,
        request.url.path,
    ):
        return JSONResponse(
            status_code=503,
            content={"detail": _READ_ONLY_DETAIL},
        )
    return await call_next(request)


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session middleware (for OAuth)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("OAUTH_SECRET_KEY", "oauth-session-secret-key-fallback-for-dev"),
)

# Thread-wiki routes (registered as a router, not inline functions)
from thread_wiki.routes import router as _wiki_router  # noqa: E402

app.include_router(_wiki_router)

# Register all webapp-owned routes (loaded by file path, no relative import)
_routes = _import_submodule("routes")
_routes.register_all_routes(app)


# ── __main__ support ──────────────────────────────────────────────────────────

def _main() -> None:
    """Entry point when running ``python -m webapp``."""
    import uvicorn

    host = os.environ.get("UPLOAD_HOST", "0.0.0.0")
    port = int(os.environ.get("UPLOAD_PORT", "8000"))

    print(f"🚀 Starting Document Upload API on {host}:{port}")
    print(f"📁 Documents root: {DOCS_ROOT}")
    print(f"🔑 API Key authentication: {'Enabled' if API_KEY else 'Disabled'}")
    print(f"📦 API Version: {API_VERSION}")
    print("\n💡 Usage example:")
    print(f"   curl -X POST http://{host}:{port}/documents/upload \\")
    print(f"     -H 'X-API-Key: {API_KEY}' \\")
    print("     -F 'folder=policy' \\")
    print("     -F 'files=@your_file.pdf'")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    _main()
