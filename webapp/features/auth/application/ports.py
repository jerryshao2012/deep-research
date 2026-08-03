"""Ports required by authentication application workflows."""

# ruff: noqa: D102

from __future__ import annotations

from typing import Any, Mapping, Protocol

from webapp.features.auth.domain.models import (
    AccountRecord,
    ChallengeRecord,
    CredentialRecord,
    SessionDetail,
)


class AuthStore(Protocol):
    """Backend-neutral contract implemented by auth persistence adapters."""

    def create_session(
        self,
        user_data: Mapping[str, Any],
        provider: str,
        auth_method: str = "oauth",
    ) -> str: ...

    def validate_session(self, session_token: str) -> dict[str, Any] | None: ...

    def refresh_session(self, session_token: str) -> dict[str, Any] | None: ...

    def remove_session(self, session_token: str) -> str | None: ...

    def get_session_detail(self, session_token: str) -> SessionDetail | None: ...

    def get_account(self, identity: str) -> AccountRecord | None: ...

    def list_credentials(
        self, identity: str, rp_id: str | None = None
    ) -> list[CredentialRecord]: ...

    def get_credential(self, credential_id: str) -> CredentialRecord | None: ...

    def create_credential(self, **kwargs: Any) -> CredentialRecord: ...

    def bind_credential_rp_id(self, credential_id: str, rp_id: str) -> bool: ...

    def update_credential_state(self, credential_id: str, **kwargs: Any) -> bool: ...

    def rename_credential(
        self, identity: str, credential_id: str, label: str
    ) -> bool: ...

    def delete_credential(self, identity: str, credential_id: str) -> bool: ...

    def create_challenge(self, **kwargs: Any) -> ChallengeRecord: ...

    def claim_challenge(self, ceremony_id: str) -> ChallengeRecord | None: ...

    def consume_rate_limit(
        self, scope: str, key: str, window_start: int, limit: int
    ) -> bool: ...

    def close(self) -> None: ...
