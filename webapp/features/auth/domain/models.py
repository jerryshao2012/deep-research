"""Authentication records independent of persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class AccountRecord:
    """Sanitized durable account data required by authentication ceremonies."""

    identity: str
    provider: str
    email: str | None
    name: str | None
    avatar_url: str | None
    profile: Mapping[str, Any]
    webauthn_user_handle: str


@dataclass(frozen=True)
class CredentialRecord:
    """Backend-neutral persisted passkey credential."""

    credential_id: str
    identity: str
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    device_type: str
    backed_up: bool
    label: str | None
    created_at: float
    last_used_at: float | None
    rp_id: str | None = None


@dataclass(frozen=True)
class ChallengeRecord:
    """Backend-neutral one-time WebAuthn ceremony challenge."""

    ceremony_id: str
    challenge: bytes
    kind: str
    identity: str | None
    origin: str
    rp_id: str
    proxy_id: str
    created_at: float
    expires_at: float
    consumed_at: float | None


@dataclass(frozen=True)
class SessionDetail:
    """Session metadata used for recent-auth and authorization checks."""

    identity: str
    provider: str
    auth_method: str
    authenticated_at: float
    expires_at: float
