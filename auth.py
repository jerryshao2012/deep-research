"""Authentication helper for LangGraph Platform.

Validates incoming credentials (supporting Google/GitHub OAuth session tokens and
standard API keys) to authorize access to thread endpoints.
"""

import os
from typing import Any, Set

from fastapi import HTTPException
from langgraph_sdk import Auth

from logger_utils import setup_logger
from webapp.oauth_handler import user_manager

logger = setup_logger(__name__)

auth = Auth()

# Track users who have already been logged for first-time OAuth authentication
_logged_oauth_users: Set[str] = set()

_READ_ONLY_DETAIL = (
    "AWS demo is temporarily read-only during S3 persistence maintenance."
)


def _reject_if_s3_read_only() -> None:
    """Reject LangGraph mutations during the AWS read-only rollout."""
    aws_mode = bool(
        os.environ.get("S3_BUCKET_NAME") and os.environ.get("AWS_REGION")
    )
    read_only = (
        os.environ.get("LANGGRAPH_S3_READ_ONLY", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if aws_mode and read_only:
        raise Auth.exceptions.HTTPException(
            status_code=503,
            detail=_READ_ONLY_DETAIL,
        )


@auth.on.threads.create
async def on_create_thread(ctx: Any, value: Any) -> None:
    """Authorize thread creation."""
    _reject_if_s3_read_only()


@auth.on.threads.update
async def on_update_thread(ctx: Any, value: Any) -> None:
    """Authorize thread updates."""
    _reject_if_s3_read_only()


@auth.on.threads.delete
async def on_delete_thread(ctx: Any, value: Any) -> None:
    """Authorize thread deletion."""
    _reject_if_s3_read_only()


@auth.on.threads.create_run
async def on_create_run(ctx: Any, value: Any) -> None:
    """Authorize run creation."""
    _reject_if_s3_read_only()


def authenticate_credential(credential: str) -> Auth.types.MinimalUserDict:
    """Authenticate a decoded credential string (API key or OAuth session token).

    Returns user identity with metadata or raises fastapi.HTTPException.
    """
    # First, try to validate as OAuth session token
    user_data = user_manager.validate_session(credential)
    if user_data:
        # OAuth authentication successful - return full user metadata
        identity_ = user_data["identity"]
        display_name = user_data.get("name", identity_)

        # Log only on first successful authentication for this user
        if identity_ not in _logged_oauth_users:
            logger.info(f"OAuth user data: {user_data}")
            logger.info(
                f"OAuth authentication successful for provider: {user_data.get('provider')} as {identity_}"
            )
            _logged_oauth_users.add(identity_)

        return {
            "identity": identity_,
            "display_name": display_name,
            "is_authenticated": True,
        }
    else:
        # Session validation failed - clean up logged users tracking if session was expired
        # Check if this credential was previously a valid session (by checking if it's not an API key)
        expected_key = os.environ.get("LANGCHAIN_API_KEY") or os.environ.get(
            "UPLOAD_API_KEY"
        )
        if expected_key and credential != expected_key:
            # This might have been an expired session token, remove from tracking
            # We can't directly map token to identity here, but we can trigger cleanup
            user_manager.cleanup_expired_sessions()
            logger.debug(
                "Session validation failed for credential, cleaned up expired sessions"
            )

    # If not a valid session token, try API key authentication
    expected_key = os.environ.get("LANGCHAIN_API_KEY") or os.environ.get(
        "UPLOAD_API_KEY"
    )

    if not expected_key:
        raise HTTPException(
            status_code=500,
            detail="Server configuration error: LANGCHAIN_API_KEY not set.",
        )

    if credential != expected_key:
        raise HTTPException(status_code=401, detail="Invalid API key or session token.")

    # API key authentication successful
    logger.info("API key authentication successful")
    return {"identity": "admin", "display_name": "Admin", "is_authenticated": True}


@auth.authenticate
async def authenticate(headers: dict) -> Auth.types.MinimalUserDict:
    """Authenticate requests using API key or OAuth session token.

    Supports:
    1. 'x-api-key' or 'X-API-Key' header (API key authentication)
    2. 'Authorization: Bearer <key>' header (API key or OAuth session token)
    3. OAuth session tokens from Google/GitHub login

    Returns user identity with metadata based on authentication method.

    NOTE: For local testing, set ALLOW_ALL_THREADS=true to bypass identity filtering.
    """
    # Check for test mode bypass
    if os.environ.get("ALLOW_ALL_THREADS", "").lower() == "true":
        if not getattr(authenticate, "_logged_test_mode", False):
            logger.warning(
                "TEST MODE: Allowing access to all threads regardless of identity"
            )
            authenticate._logged_test_mode = True
        return {"identity": "test-admin", "permissions": ["threads:read:all"]}

    # Try to get authentication credentials from headers
    api_key_bytes = headers.get(b"x-api-key") or headers.get(b"X-API-Key")

    # Check Authorization header
    if not api_key_bytes:
        auth_header = headers.get(b"authorization") or headers.get(b"Authorization")
        if auth_header and auth_header.startswith(b"Bearer "):
            api_key_bytes = auth_header[7:]

    if not api_key_bytes:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Missing authentication. Please provide 'x-api-key', 'Authorization: Bearer', or OAuth session token.",
        )

    try:
        credential = api_key_bytes.decode("utf-8")
    except Exception:
        raise Auth.exceptions.HTTPException(
            status_code=401, detail="Invalid credential format."
        )

    try:
        return authenticate_credential(credential)
    except HTTPException as e:
        raise Auth.exceptions.HTTPException(status_code=e.status_code, detail=e.detail)
