"""webapp package — public API surface for the Document Upload API.

This module replaces the former monolithic ``webapp.py``.  It:

1. Creates and configures the FastAPI application instance.
2. Re-exports every public symbol that external code (tests, and the
   deprecated ``server.py``) used to import so that no import path
   changes are needed.
"""

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
import types as _types
from pathlib import Path as _Path

import sys as _sys

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
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
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
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            from pathlib import Path as _Path

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
            print(f"✅ Checkpointer initialized: AsyncPostgresSaver")
    except Exception as _exc:
        print(f"⚠️  Persistent checkpointer setup skipped (using InMemorySaver): {_exc}")

    yield

    # Cleanup: close checkpointer connection if we opened one
    if _checkpointer_conn is not None:
        await _checkpointer_conn.close()
        print("✅ Checkpointer connection closed")


# ── Application factory ───────────────────────────────────────────────────────

app = FastAPI(
    title="Document Upload API",
    description="Upload documents to the deep research agent docs folder",
    version=API_VERSION,
    lifespan=_lifespan,
)

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
    print(f"\n💡 Usage example:")
    print(f"   curl -X POST http://{host}:{port}/documents/upload \\")
    print(f"     -H 'X-API-Key: {API_KEY}' \\")
    print(f"     -F 'folder=policy' \\")
    print(f"     -F 'files=@your_file.pdf'")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    _main()
