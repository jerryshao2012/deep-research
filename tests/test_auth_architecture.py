"""Contract tests for authentication Clean Architecture boundaries."""

from __future__ import annotations


def test_legacy_auth_store_reexports_inward_contracts() -> None:
    """Compatibility imports remain stable while contracts move inward."""
    from webapp import auth_store
    from webapp.features.auth import (
        AccountRecord,
        AuthStore,
        AuthStoreError,
        ChallengeLimitError,
        ChallengeRecord,
        CredentialLimitError,
        CredentialRecord,
        DuplicateCredentialError,
        SessionDetail,
    )

    names = (
        AccountRecord,
        AuthStore,
        AuthStoreError,
        ChallengeLimitError,
        ChallengeRecord,
        CredentialLimitError,
        CredentialRecord,
        DuplicateCredentialError,
        SessionDetail,
    )
    assert all(getattr(auth_store, item.__name__) is item for item in names)


def test_auth_application_port_has_no_adapter_dependency() -> None:
    """Auth application boundary exposes a backend-neutral persistence port."""
    from webapp.features.auth.application.ports import AuthStore

    assert AuthStore.__module__ == "webapp.features.auth.application.ports"
    assert {
        "create_session",
        "validate_session",
        "refresh_session",
        "remove_session",
        "get_session_detail",
        "get_account",
        "list_credentials",
        "get_credential",
        "create_credential",
        "create_challenge",
        "claim_challenge",
        "consume_rate_limit",
        "close",
    }.issubset(AuthStore.__dict__)
