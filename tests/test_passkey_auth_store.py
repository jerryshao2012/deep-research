"""Passkey persistence contract exercised against real SQLite."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from webapp.auth_store import (
    ChallengeLimitError,
    CredentialLimitError,
    DuplicateCredentialError,
    SQLiteAuthStore,
)


def _user(subject: str = "123") -> dict:
    return {
        "identity": f"google:{subject}",
        "provider": "google",
        "email": f"{subject}@example.com",
        "name": f"User {subject}",
    }


@pytest.fixture
def store(tmp_path):
    auth_store = SQLiteAuthStore(tmp_path / "auth.db")
    auth_store.create_session(_user(), "google")
    return auth_store


def _create_credential(store, credential_id: str = "credential_A", **overrides):
    values = {
        "identity": "google:123",
        "rp_id": "example.com",
        "credential_id": credential_id,
        "public_key": b"public-key-bytes",
        "sign_count": 7,
        "transports": ["internal", "hybrid"],
        "device_type": "multi_device",
        "backed_up": True,
        "label": "Laptop",
    }
    values.update(overrides)
    return store.create_credential(**values)


def test_passkey_schema_migration_is_idempotent(tmp_path):
    path = tmp_path / "auth.db"
    SQLiteAuthStore(path).close()
    SQLiteAuthStore(path).close()

    with sqlite3.connect(path) as connection:
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }

    assert {"auth_credentials", "auth_challenges"} <= objects
    assert {
               "idx_auth_credentials_identity",
               "idx_auth_challenges_expires_at",
               "idx_auth_challenges_registration_identity",
           } <= objects


def test_credential_rp_round_trip(store):
    created = _create_credential(store)

    assert created.credential_id == "credential_A"
    assert created.rp_id == "example.com"
    assert created.public_key == b"public-key-bytes"
    assert created.transports == ("internal", "hybrid")
    assert created.sign_count == 7
    assert created.backed_up is True
    assert created.label == "Laptop"
    assert store.get_credential("credential_A") == created
    assert store.list_credentials("google:123") == [created]


def test_credential_rp_filter(store):
    first = _create_credential(store, "credential_A", rp_id="app.example.com")
    second = _create_credential(store, "credential_B", rp_id="other.example.com")

    assert store.list_credentials("google:123", "app.example.com") == [first]
    assert store.list_credentials("google:123", "other.example.com") == [second]
    assert store.list_credentials("google:123", None) == [first, second]


@pytest.mark.parametrize(
    "rp_id", [None, "", " ", "HTTPS://EXAMPLE.COM", "example.com/"]
)
def test_new_credentials_require_canonical_nonempty_rp_id(store, rp_id):
    with pytest.raises(ValueError, match="rp_id"):
        _create_credential(store, rp_id=rp_id)


def test_bind_credential_rp_id_is_compare_and_set(store):
    created = _create_credential(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE auth_credentials SET rp_id = NULL WHERE credential_id = ?",
            (created.credential_id,),
        )

    assert store.bind_credential_rp_id("credential_A", "app.example.com") is True
    assert store.bind_credential_rp_id("credential_A", "app.example.com") is True
    assert store.bind_credential_rp_id("credential_A", "other.example.com") is False
    assert store.get_credential("credential_A").rp_id == "app.example.com"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credential_id", "not base64!"),
        ("public_key", b""),
        ("sign_count", -1),
        ("transports", ["telepathy"]),
        ("device_type", "unknown"),
        ("label", "x" * 101),
    ],
)
def test_credential_fields_are_validated(store, field, value):
    with pytest.raises(ValueError):
        _create_credential(store, **{field: value})


def test_credential_id_is_globally_unique(store):
    _create_credential(store)
    store.create_session(_user("456"), "google")

    with pytest.raises(DuplicateCredentialError):
        _create_credential(store, identity="google:456")

    assert store.get_credential("credential_A").identity == "google:123"


def test_credential_limit_is_enforced_transactionally(store):
    for index in range(10):
        _create_credential(store, credential_id=f"credential_{index}")

    with pytest.raises(CredentialLimitError):
        _create_credential(store, credential_id="credential_overflow")

    assert len(store.list_credentials("google:123")) == 10


def test_credential_counter_update_uses_expected_state(store):
    _create_credential(store)
    used_at = time.time()

    updated = store.update_credential_state(
        "credential_A",
        expected_sign_count=7,
        expected_backed_up=True,
        new_sign_count=8,
        backed_up=True,
        last_used_at=used_at,
    )
    stale = store.update_credential_state(
        "credential_A",
        expected_sign_count=7,
        expected_backed_up=True,
        new_sign_count=9,
        backed_up=False,
        last_used_at=used_at + 1,
    )

    assert updated is True
    assert stale is False
    credential = store.get_credential("credential_A")
    assert credential.sign_count == 8
    assert credential.backed_up is True
    assert credential.last_used_at == used_at


def test_rename_and_delete_credential(store):
    _create_credential(store)

    assert store.rename_credential("google:123", "credential_A", "Security key")
    assert store.get_credential("credential_A").label == "Security key"
    assert not store.rename_credential("google:456", "credential_A", "Other")
    assert store.delete_credential("google:123", "credential_A")
    assert store.get_credential("credential_A") is None
    assert not store.delete_credential("google:123", "credential_A")


def test_session_detail_exposes_recent_auth_fields_without_token(store):
    token = store.create_session(
        _user(),
        "google",
        auth_method="passkey",
    )

    detail = store.get_session_detail(token)

    assert detail.identity == "google:123"
    assert detail.provider == "google"
    assert detail.auth_method == "passkey"
    assert detail.authenticated_at <= time.time()
    assert not hasattr(detail, "token")
    assert store.get_session_detail("unknown") is None


def test_claim_challenge_returns_stored_record_exactly_once(store):
    now = time.time()
    created = store.create_challenge(
        challenge=b"challenge-bytes",
        kind="registration",
        identity="google:123",
        origin="https://app.example.com",
        rp_id="example.com",
        proxy_id="web-bff",
        expires_at=now + 300,
    )

    consumed = store.claim_challenge(created.ceremony_id)

    assert consumed is not None
    assert consumed.challenge == b"challenge-bytes"
    assert consumed.proxy_id == "web-bff"
    assert consumed.consumed_at is not None
    assert store.claim_challenge(created.ceremony_id) is None


def test_claim_challenge_does_not_apply_expectations(store):
    created = store.create_challenge(
        challenge=b"challenge-bytes",
        kind="registration",
        identity="google:123",
        origin="https://app.example.com",
        rp_id="example.com",
        proxy_id="web-bff",
        expires_at=time.time() + 300,
    )
    claimed = store.claim_challenge(created.ceremony_id)

    assert claimed.kind == "registration"
    assert claimed.origin == "https://app.example.com"
    assert claimed.rp_id == "example.com"
    assert store.claim_challenge(created.ceremony_id) is None


def test_expired_challenge_is_claimed_for_service_validation(store):
    created = store.create_challenge(
        challenge=b"challenge-bytes",
        kind="authentication",
        identity=None,
        origin="https://app.example.com",
        rp_id="example.com",
        proxy_id="web-bff",
        expires_at=time.time() - 1,
    )

    claimed = store.claim_challenge(created.ceremony_id)
    assert claimed is not None
    assert claimed.expires_at < time.time()
    with sqlite3.connect(store.path) as connection:
        consumed_at = connection.execute(
            "SELECT consumed_at FROM auth_challenges WHERE ceremony_id = ?",
            (created.ceremony_id,),
        ).fetchone()[0]
    assert consumed_at is not None


def test_registration_challenge_cap_ignores_expired_rows(store):
    common = {
        "challenge": b"challenge-bytes",
        "kind": "registration",
        "identity": "google:123",
        "origin": "https://app.example.com",
        "rp_id": "example.com",
        "proxy_id": "web-bff",
    }
    for _ in range(3):
        store.create_challenge(**common, expires_at=time.time() + 300)

    with pytest.raises(ChallengeLimitError):
        store.create_challenge(**common, expires_at=time.time() + 300)

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE auth_challenges SET expires_at = ? WHERE identity = ?",
            (time.time() - 1, "google:123"),
        )
    assert store.create_challenge(**common, expires_at=time.time() + 300)


def test_failed_challenge_consumption_never_mints_session(store):
    created = store.create_challenge(
        challenge=b"challenge-bytes",
        kind="authentication",
        identity=None,
        origin="https://app.example.com",
        rp_id="example.com",
        proxy_id="web-bff",
        expires_at=time.time() + 300,
    )
    with sqlite3.connect(store.path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0]

    assert store.claim_challenge(created.ceremony_id) is not None

    with sqlite3.connect(store.path) as connection:
        after = connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0]
    assert after == before


def test_claim_challenge_is_atomic_under_concurrency(tmp_path):
    path = tmp_path / "auth.db"
    creator = SQLiteAuthStore(path)
    created = creator.create_challenge(
        challenge=b"challenge-bytes",
        kind="authentication",
        identity=None,
        origin="https://app.example.com",
        rp_id="example.com",
        proxy_id="web-bff",
        expires_at=time.time() + 300,
    )
    stores = [SQLiteAuthStore(path) for _ in range(4)]

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(
            executor.map(
                lambda index: stores[index % len(stores)].claim_challenge(
                    created.ceremony_id
                ),
                range(20),
            )
        )

    assert sum(result is not None for result in results) == 1


def test_challenge_cleanup_is_bounded(store):
    for _ in range(4):
        store.create_challenge(
            challenge=b"challenge-bytes",
            kind="authentication",
            identity=None,
            origin="https://app.example.com",
            rp_id="example.com",
            proxy_id="web-bff",
            expires_at=time.time() + 300,
        )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE auth_challenges SET expires_at = ?",
            (time.time() - 1,),
        )

    assert store.cleanup_challenges(limit=2) == 2
    with sqlite3.connect(store.path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM auth_challenges"
        ).fetchone()[0]
    assert remaining == 2


def test_rate_limit_is_atomic_and_survives_sqlite_restart(tmp_path):
    path = tmp_path / "auth.db"
    first = SQLiteAuthStore(path)

    assert first.consume_rate_limit("proxy", "web-bff", 100, 2)
    first.close()
    second = SQLiteAuthStore(path)
    assert second.consume_rate_limit("proxy", "web-bff", 100, 2)
    assert not second.consume_rate_limit("proxy", "web-bff", 100, 2)
    assert second.consume_rate_limit("proxy", "web-bff", 101, 2)


def test_sqlite_can_use_network_safe_delete_journal_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_SQLITE_JOURNAL_MODE", "DELETE")

    store = SQLiteAuthStore(tmp_path / "auth.db")

    with store._lock:
        mode = store._connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"


def test_rate_limit_is_atomic_across_sqlite_connections(tmp_path):
    path = tmp_path / "auth.db"
    stores = [SQLiteAuthStore(path) for _ in range(4)]

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(
            executor.map(
                lambda index: stores[index % len(stores)].consume_rate_limit(
                    "account", "google:123", 100, 7
                ),
                range(20),
            )
        )

    assert results.count(True) == 7
    assert results.count(False) == 13
