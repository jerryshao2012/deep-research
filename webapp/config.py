"""Global configuration constants for the webapp package.

Computed once at import time from environment variables. All other modules
import from here rather than reading os.environ directly.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

from webapp.passkeys import PasskeyConfig, PasskeyService

logger = logging.getLogger(__name__)

# ── Environment ──────────────────────────────────────────────────────────────

load_dotenv()

# Absolute path to the docs directory (sibling of this package inside deep_research/).
# On mounted deployments (Azure), the base is auto-detected from DOC_FOLDER or
# WIKI_BASE_DIR env vars set by deploy.sh.
_BASE = Path(
    os.environ.get(
        "WIKI_BASE_DIR",
        os.environ.get(
            "DOC_FOLDER",
            str(Path(__file__).resolve().parent.parent / "docs"),
        ),
    )
)
# If _BASE points to a "docs" directory, use it directly; otherwise append "docs".
if _BASE.name == "docs":
    DOCS_ROOT: Path = _BASE
else:
    DOCS_ROOT: Path = _BASE / "docs"

# Semantic API version — bump on every public-facing change
API_VERSION: str = "1.8.134"

# ── Authentication ────────────────────────────────────────────────────────────

API_KEY: str = os.environ.get("UPLOAD_API_KEY") or os.environ.get(
    "LANGCHAIN_API_KEY", ""
)

if not API_KEY:
    API_KEY = secrets.token_urlsafe(32)
    logger.warning(
        "UPLOAD_API_KEY not set; using a process-local generated key. "
        "Set UPLOAD_API_KEY for production."
    )

OAUTH_SESSION_SECRET: str = os.environ.get("OAUTH_SECRET_KEY") or secrets.token_urlsafe(
    32
)

# ── OAuth (optional dependency) ───────────────────────────────────────────────

OAUTH_ENABLED: bool = False

try:
    from webapp.oauth_handler import (
        get_oauth_login_url,
        handle_github_callback,
        handle_google_callback,
        handle_logout,
        user_manager,
    )

    OAUTH_ENABLED = True
except ImportError:
    # Expose None-typed names so downstream imports never break.
    get_oauth_login_url = None  # type: ignore[assignment]
    handle_github_callback = None  # type: ignore[assignment]
    handle_google_callback = None  # type: ignore[assignment]
    handle_logout = None  # type: ignore[assignment]
    user_manager = None  # type: ignore[assignment]
    logger.warning("OAuth dependencies not installed; OAuth login is disabled")

# ── Passkeys (disabled by default) ───────────────────────────────────────────

PASSKEY_CONFIG = PasskeyConfig.from_environ()
PASSKEY_ENABLED: bool = PASSKEY_CONFIG.enabled
if PASSKEY_ENABLED and user_manager is None:
    raise RuntimeError("Passkeys require installed OAuth dependencies")
PASSKEY_SERVICE = (
    PasskeyService(PASSKEY_CONFIG, user_manager.store)
    if PASSKEY_ENABLED and user_manager is not None
    else None
)

# ── CORS ──────────────────────────────────────────────────────────────────────

_frontend_origins: list[str] = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://smith.langchain.com",
    "https://bmo-deepagent-ui.vercel.app",
    "https://bmo-deepagent-qqkdniiw0-agentic-ui.vercel.app",
    "https://bmo-deepagent-ui-0312.azurewebsites.net/"
]

_env_frontend_urls = os.environ.get("FRONTEND_URLS", "")
if _env_frontend_urls:
    _frontend_origins[0:0] = [
        origin.strip().rstrip("/")
        for origin in _env_frontend_urls.split(",")
        if origin.strip()
    ]

# Deduplicate while preserving order and removing empties.
FRONTEND_ORIGINS: list[str] = list(
    dict.fromkeys(origin for origin in _frontend_origins if origin)
)
