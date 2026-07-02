"""FastAPI endpoint authentication helpers.

Validates static API keys (`X-API-Key`) and session tokens from OAuth providers
to authorize access to route modules.
"""

from __future__ import annotations

from fastapi import Request

import webapp.config as _cfg


def is_authenticated(x_api_key: str | None, request: Request | None = None) -> bool:
    """Return True when the request carries a valid API key or OAuth session token.

    Check order:
    1. Static API key (``X-API-Key`` header).
    2. OAuth session token (via ``X-API-Key`` or ``Authorization: Bearer`` header).

    API_KEY is read through the config module at call time so that test
    monkey-patching of ``webapp.config.API_KEY`` is always honoured.
    """
    # 1. Static API key
    if x_api_key and x_api_key == _cfg.API_KEY:
        return True

    # 2. OAuth session token
    if _cfg.OAUTH_ENABLED and _cfg.user_manager is not None:
        token = x_api_key
        if not token and request:
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header[7:]
        if token and _cfg.user_manager.validate_session(token):
            return True

    return False
