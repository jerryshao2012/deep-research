"""Contract tests for durable OAuth account and session storage."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from webapp.auth_store import SQLiteAuthStore, create_auth_store


def _google_user(subject: str = "123", **overrides: object) -> dict:
    user = {
        "identity": f"google:{subject}",
        "email": "person@example.com",
        "name": "Person",
        "picture": "https://example.com/person.png",
        "provider": "google",
        "email_verified": True,
        "locale": "en",
    }
    user.update(overrides)
    return user


def _db_rows(path, statement: str, parameters: tuple = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(statement, parameters).fetchall()


def test_sqlite_schema_migration_is_idempotent(tmp_path):
    path = tmp_path / "auth.db"

    first = SQLiteAuthStore(path)
    first.close()
    second = SQLiteAuthStore(path)
    second.close()

    tables = {
        row["name"]
        for row in _db_rows(
            path,
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    indexes = {
        row["name"]
        for row in _db_rows(
            path,
            "SELECT name FROM sqlite_master WHERE type = 'index'",
        )
    }

    assert {"auth_accounts", "auth_sessions"} <= tables
    assert {
               "idx_auth_sessions_identity",
               "idx_auth_sessions_expires_at",
           } <= indexes


def test_sqlite_legacy_schema_adds_nullable_rp_id_idempotently(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE auth_credentials
               (
                   credential_id   TEXT PRIMARY KEY,
                   identity        TEXT    NOT NULL,
                   public_key      BLOB    NOT NULL,
                   sign_count      INTEGER NOT NULL,
                   transports_json TEXT    NOT NULL,
                   device_type     TEXT    NOT NULL,
                   backed_up       INTEGER NOT NULL,
                   label           TEXT,
                   created_at      REAL    NOT NULL,
                   last_used_at    REAL
               )"""
        )
        connection.execute(
            "INSERT INTO auth_credentials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy_credential",
                "google:123",
                b"key",
                0,
                "[]",
                "single_device",
                0,
                None,
                1.0,
                None,
            ),
        )

    SQLiteAuthStore(path).close()
    SQLiteAuthStore(path).close()

    with sqlite3.connect(path) as connection:
        column_rows = list(connection.execute("PRAGMA table_info(auth_credentials)"))
        columns = {row[1] for row in column_rows}
        rp_id = connection.execute(
            "SELECT rp_id FROM auth_credentials WHERE credential_id = ?",
            ("legacy_credential",),
        ).fetchone()[0]
    assert "rp_id" in columns
    assert sum(row[1] == "rp_id" for row in column_rows) == 1
    assert next(row[3] for row in column_rows if row[1] == "rp_id") == 0
    assert rp_id is None


def test_sqlite_partial_migration_preserves_bound_and_unbound_rows(tmp_path):
    path = tmp_path / "partial.db"
    store = SQLiteAuthStore(path)
    store.create_session(_google_user(), "google")
    store.create_credential(
        identity="google:123",
        rp_id="app.example.com",
        credential_id="unbound_credential",
        public_key=b"key",
        sign_count=0,
        transports=[],
        device_type="single_device",
        backed_up=False,
    )
    store.create_credential(
        identity="google:123",
        rp_id="other.example.com",
        credential_id="bound_credential",
        public_key=b"other-key",
        sign_count=0,
        transports=[],
        device_type="single_device",
        backed_up=False,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE auth_credentials SET rp_id = NULL WHERE credential_id = ?",
            ("unbound_credential",),
        )
    store.close()

    reopened = SQLiteAuthStore(path)
    assert reopened.get_credential("unbound_credential").rp_id is None
    assert reopened.get_credential("bound_credential").rp_id == "other.example.com"
    assert reopened.bind_credential_rp_id("unbound_credential", "app.example.com")
    reopened.close()

    final = SQLiteAuthStore(path)
    assert final.get_credential("unbound_credential").rp_id == "app.example.com"
    assert final.get_credential("bound_credential").rp_id == "other.example.com"


def _create_sqlite_credential(
        store: SQLiteAuthStore,
        credential_id: str,
        rp_id: str = "app.example.com",
):
    return store.create_credential(
        identity="google:123",
        rp_id=rp_id,
        credential_id=credential_id,
        public_key=b"key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
    )


def test_credential_round_trip_persists_rp_id(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    store.create_session(_google_user(), "google")

    created = _create_sqlite_credential(store, "credential_A")

    assert created.rp_id == "app.example.com"
    assert store.get_credential("credential_A").rp_id == "app.example.com"


def test_list_credentials_filters_by_rp_id_but_none_lists_all(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    store.create_session(_google_user(), "google")
    _create_sqlite_credential(store, "credential_A", "app.example.com")
    _create_sqlite_credential(store, "credential_B", "other.example.com")

    assert {
               item.credential_id
               for item in store.list_credentials("google:123", "app.example.com")
           } == {"credential_A"}
    assert {
               item.credential_id for item in store.list_credentials("google:123", None)
           } == {"credential_A", "credential_B"}


def test_bind_legacy_credential_rp_id_is_compare_and_swap(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    store.create_session(_google_user(), "google")
    _create_sqlite_credential(store, "credential_A")
    with store._lock:
        store._connection.execute(
            "UPDATE auth_credentials SET rp_id = '' WHERE credential_id = ?",
            ("credential_A",),
        )

    assert store.bind_credential_rp_id("credential_A", "app.example.com") is True
    assert store.bind_credential_rp_id("credential_A", "app.example.com") is True
    assert store.bind_credential_rp_id("credential_A", "other.example.com") is False
    assert store.get_credential("credential_A").rp_id == "app.example.com"


@pytest.mark.parametrize(
    "rp_id",
    [
        "",
        " ",
        "https://example.com",
        "example.com/",
        "com",
        "vercel.app",
        "127.0.0.1",
        "example.123",
        "example.0x10",
    ],
)
def test_invalid_credential_rp_id_is_rejected(tmp_path, rp_id):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    store.create_session(_google_user(), "google")

    with pytest.raises(ValueError, match="rp_id"):
        _create_sqlite_credential(store, "credential_A", rp_id)


def test_credential_rp_id_uses_shared_idna_normalization(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    store.create_session(_google_user(), "google")

    created = _create_sqlite_credential(
        store,
        "credential_A",
        "bücher.bmo-deepagent-ui.vercel.app.",
    )

    assert created.rp_id == "xn--bcher-kva.bmo-deepagent-ui.vercel.app"
    assert store.bind_credential_rp_id(
        "credential_A", "bücher.bmo-deepagent-ui.vercel.app"
    )


@pytest.mark.parametrize("rp_id", ["com", "vercel.app", "127.0.0.1", "example.123"])
def test_invalid_challenge_rp_id_is_rejected(tmp_path, rp_id):
    store = SQLiteAuthStore(tmp_path / "auth.db")

    with pytest.raises(ValueError, match="rp_id"):
        store.create_challenge(
            challenge=b"challenge",
            kind="authentication",
            identity=None,
            origin="https://app.example.com",
            rp_id=rp_id,
            proxy_id="web-bff",
            expires_at=time.time() + 300,
        )


@pytest.mark.parametrize(
    "rp_id",
    [
        "bmo-deepagent-ui-0312.azurewebsites.net",
        "bmo-deepagent-ui.vercel.app",
    ],
)
def test_requested_tenant_rp_ids_are_accepted_by_storage(tmp_path, rp_id):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    store.create_session(_google_user(), "google")

    assert _create_sqlite_credential(store, "credential_A", rp_id).rp_id == rp_id


def test_accounts_use_provider_subject_not_email_for_identity(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)

    google_token = store.create_session(_google_user("google-id"), "google")
    github_token = store.create_session(
        {
            "identity": "github:456",
            "email": "person@example.com",
            "name": "Person",
            "provider": "github",
        },
        "github",
    )

    accounts = _db_rows(
        path,
        "SELECT identity, provider, provider_subject, webauthn_user_handle "
        "FROM auth_accounts ORDER BY identity",
    )
    assert [
               (row["identity"], row["provider"], row["provider_subject"]) for row in accounts
           ] == [
               ("github:456", "github", "456"),
               ("google:google-id", "google", "google-id"),
           ]
    assert len({row["webauthn_user_handle"] for row in accounts}) == 2
    decoded_handles = [
        base64.urlsafe_b64decode(
            row["webauthn_user_handle"] + "=" * (-len(row["webauthn_user_handle"]) % 4)
        )
        for row in accounts
    ]
    assert all(len(handle) <= 64 for handle in decoded_handles)
    assert store.validate_session(google_token)["identity"] == "google:google-id"
    assert store.validate_session(github_token)["identity"] == "github:456"


def test_account_handle_is_stable_and_provider_identity_is_immutable(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)
    store.create_session(_google_user(), "google")
    original_handle = _db_rows(
        path,
        "SELECT webauthn_user_handle FROM auth_accounts WHERE identity = ?",
        ("google:123",),
    )[0]["webauthn_user_handle"]

    store.create_session(_google_user(name="Renamed"), "google")

    current_handle = _db_rows(
        path,
        "SELECT webauthn_user_handle FROM auth_accounts WHERE identity = ?",
        ("google:123",),
    )[0]["webauthn_user_handle"]
    assert current_handle == original_handle
    with pytest.raises(ValueError, match="provider identity"):
        store.create_session(_google_user(), "github")


@pytest.mark.parametrize(
    ("provider", "identity"),
    [
        ("google", "google:None"),
        ("google", "google:"),
        ("google", "google: "),
        ("github", "github:None"),
        ("github", "github:abc"),
        ("github", "github:0"),
    ],
)
def test_store_rejects_invalid_provider_subjects(tmp_path, provider, identity):
    store = SQLiteAuthStore(tmp_path / "auth.db")

    with pytest.raises(ValueError, match="provider identity"):
        store.create_session(
            {"identity": identity, "provider": provider},
            provider,
        )


def test_account_handle_collision_is_retried(tmp_path, monkeypatch):
    generated = iter(
        [
            "same-handle",
            "session-one",
            "same-handle",
            "replacement-handle",
            "session-two",
        ]
    )
    monkeypatch.setattr(
        "webapp.auth_store.secrets.token_urlsafe", lambda _: next(generated)
    )
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)

    store.create_session(_google_user("one"), "google")
    store.create_session(_google_user("two"), "google")

    handles = {
        row["webauthn_user_handle"]
        for row in _db_rows(path, "SELECT webauthn_user_handle FROM auth_accounts")
    }
    assert handles == {"same-handle", "replacement-handle"}


def test_only_allowlisted_bounded_profile_data_is_persisted(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)
    user = _google_user(
        email="e" * 400,
        name="n" * 300,
        raw_token={"access_token": "oauth-secret"},
        session_token="caller-session-secret",
        unknown={"unbounded": "x" * 10_000},
        bio="b" * 2_000,
    )

    token = store.create_session(user, "google")
    validated = store.validate_session(token)
    account = _db_rows(path, "SELECT * FROM auth_accounts")[0]
    profile = json.loads(account["profile_json"])

    assert len(account["email"]) == 320
    assert len(account["name"]) == 200
    assert len(profile["bio"]) == 500
    assert "unknown" not in profile
    assert "raw_token" not in validated
    assert "session_token" not in validated
    database_bytes = path.read_bytes()
    assert b"oauth-secret" not in database_bytes
    assert b"caller-session-secret" not in database_bytes
    assert b"unbounded" not in database_bytes


def test_session_token_is_stored_only_as_sha256_hash(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)

    token = store.create_session(_google_user(), "google")

    sessions = _db_rows(path, "SELECT token_hash FROM auth_sessions")
    assert sessions[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token.encode() not in path.read_bytes()
    assert store.validate_session(token)["identity"] == "google:123"


def test_sessions_are_visible_to_another_store_instance(tmp_path):
    path = tmp_path / "auth.db"
    first = SQLiteAuthStore(path)
    token = first.create_session(_google_user(), "google")
    first.close()

    second = SQLiteAuthStore(path)

    assert second.validate_session(token)["identity"] == "google:123"


def test_sqlite_path_is_expanded_once_for_directory_and_connection(
        tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))

    store = SQLiteAuthStore("~/nested/auth.db")

    expected = tmp_path / "nested" / "auth.db"
    assert store.path == str(expected)
    assert expected.exists()


def test_live_validation_does_not_wait_for_sqlite_writer(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)
    token = store.create_session(_google_user(), "google")
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(store.validate_session, token)

    try:
        user = future.result(timeout=0.5)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        executor.shutdown(wait=True)

    assert user["identity"] == "google:123"


def test_expired_session_is_rejected_and_deleted(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)
    token = store.create_session(_google_user(), "google")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
            (time.time() - 1, token_hash),
        )

    assert store.validate_session(token) is None
    assert _db_rows(path, "SELECT token_hash FROM auth_sessions") == []


def test_session_expires_at_exact_boundary(tmp_path, monkeypatch):
    import webapp.auth_store as auth_store

    now = 1_800_000_000.0
    monkeypatch.setattr(auth_store.time, "time", lambda: now)
    store = SQLiteAuthStore(tmp_path / "auth.db")
    token = store.create_session(_google_user(), "google")
    monkeypatch.setattr(
        auth_store.time,
        "time",
        lambda: now + auth_store.SESSION_LIFETIME_SECONDS,
    )

    assert store.validate_session(token) is None


def test_refresh_extends_expiry_without_resetting_authenticated_at(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)
    token = store.create_session(_google_user(), "google")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    original_authenticated_at = 1_700_000_000.0
    old_expiry = time.time() + 60
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE auth_sessions SET authenticated_at = ?, expires_at = ? "
            "WHERE token_hash = ?",
            (original_authenticated_at, old_expiry, token_hash),
        )

    assert store.refresh_session(token)["identity"] == "google:123"

    session = _db_rows(
        path,
        "SELECT authenticated_at, expires_at FROM auth_sessions WHERE token_hash = ?",
        (token_hash,),
    )[0]
    assert session["authenticated_at"] == original_authenticated_at
    assert session["expires_at"] > old_expiry


def test_validation_slides_nearly_expired_session_without_reauthentication(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)
    token = store.create_session(_google_user(), "google")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    original_authenticated_at = 1_700_000_000.0
    old_expiry = time.time() + 60
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE auth_sessions SET authenticated_at = ?, expires_at = ? "
            "WHERE token_hash = ?",
            (original_authenticated_at, old_expiry, token_hash),
        )

    assert store.validate_session(token)["identity"] == "google:123"

    session = _db_rows(
        path,
        "SELECT authenticated_at, expires_at FROM auth_sessions WHERE token_hash = ?",
        (token_hash,),
    )[0]
    assert session["authenticated_at"] == original_authenticated_at
    assert session["expires_at"] > old_expiry


def test_remove_session_returns_identity_and_revokes_token(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    token = store.create_session(_google_user(), "google")

    assert store.remove_session(token) == "google:123"
    assert store.validate_session(token) is None
    assert store.remove_session(token) is None


def test_cleanup_is_bounded(tmp_path):
    path = tmp_path / "auth.db"
    store = SQLiteAuthStore(path)
    for subject in range(5):
        store.create_session(_google_user(str(subject)), "google")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE auth_sessions SET expires_at = ?", (time.time() - 1,)
        )

    assert store.cleanup_expired_sessions(limit=2) == 2
    assert len(_db_rows(path, "SELECT token_hash FROM auth_sessions")) == 3


def test_non_oauth_auth_method_is_exposed_without_changing_provider(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")

    token = store.create_session(_google_user(), "google", auth_method="passkey")

    user = store.validate_session(token)
    assert user["provider"] == "google"
    assert user["auth_method"] == "passkey"


@pytest.mark.parametrize("auth_method", ["", "password"])
def test_auth_method_is_limited_to_supported_methods(tmp_path, auth_method):
    store = SQLiteAuthStore(tmp_path / "auth.db")

    with pytest.raises(ValueError, match="auth_method"):
        store.create_session(_google_user(), "google", auth_method=auth_method)


def test_oauth_user_manager_preserves_api_and_is_durable(tmp_path):
    from webapp.oauth_handler import OAuthUserManager

    path = tmp_path / "auth.db"
    first = OAuthUserManager(store=SQLiteAuthStore(path))
    token = first.create_session(_google_user(), "google")

    second = OAuthUserManager(store=SQLiteAuthStore(path))

    assert second.validate_session(token)["identity"] == "google:123"
    assert second.refresh_session(token)["identity"] == "google:123"
    assert second.remove_session(token) == "google:123"
    assert second.validate_session(token) is None


@pytest.mark.parametrize("subject", [None, "", " ", "None", "a\x00b", 123])
def test_google_callback_rejects_invalid_subject_before_session_creation(
        monkeypatch,
        subject,
):
    import webapp.oauth_handler as oauth_handler

    async def authorize_access_token(_request):
        return {"userinfo": {"sub": subject, "email": "person@example.com"}}

    monkeypatch.setattr(
        oauth_handler.google,
        "authorize_access_token",
        authorize_access_token,
    )
    monkeypatch.setattr(
        oauth_handler.user_manager,
        "create_session",
        lambda *_args, **_kwargs: pytest.fail("session creation must not run"),
    )

    with pytest.raises(Exception, match="Google OAuth failed: invalid Google subject"):
        asyncio.run(oauth_handler.handle_google_callback(SimpleNamespace()))


def test_google_callback_persists_session_off_event_loop(monkeypatch):
    import webapp.oauth_handler as oauth_handler

    async def authorize_access_token(_request):
        return {
            "userinfo": {
                "sub": "google-subject",
                "email": "person@example.com",
                "name": "Person",
            }
        }

    def create_session(_user_data, _provider):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return "session-token"

    monkeypatch.setattr(
        oauth_handler.google,
        "authorize_access_token",
        authorize_access_token,
    )
    monkeypatch.setattr(
        oauth_handler.user_manager,
        "create_session",
        create_session,
    )

    user_data = asyncio.run(oauth_handler.handle_google_callback(SimpleNamespace()))

    assert user_data["session_token"] == "session-token"


@pytest.mark.parametrize("subject", [None, "", "abc", 0, -1, True])
def test_github_callback_rejects_invalid_subject_before_session_creation(
        monkeypatch,
        subject,
):
    import webapp.oauth_handler as oauth_handler

    async def authorize_access_token(_request):
        return {"access_token": "not-persisted"}

    async def get(endpoint, **_kwargs):
        if endpoint == "user":
            return SimpleNamespace(json=lambda: {"id": subject, "login": "person"})
        return SimpleNamespace(json=lambda: [])

    monkeypatch.setattr(
        oauth_handler.github,
        "authorize_access_token",
        authorize_access_token,
    )
    monkeypatch.setattr(oauth_handler.github, "get", get)
    monkeypatch.setattr(
        oauth_handler.user_manager,
        "create_session",
        lambda *_args, **_kwargs: pytest.fail("session creation must not run"),
    )
    request = SimpleNamespace(
        session={"oauth_github_client_name": "__invalid_test_client__"}
    )

    with pytest.raises(Exception, match="GitHub OAuth failed: invalid GitHub subject"):
        asyncio.run(oauth_handler.handle_github_callback(request))


def test_github_callback_persists_session_off_event_loop(monkeypatch):
    import webapp.oauth_handler as oauth_handler

    async def authorize_access_token(_request):
        return {"access_token": "not-persisted"}

    async def get(endpoint, **_kwargs):
        if endpoint == "user":
            return SimpleNamespace(
                json=lambda: {"id": 123, "login": "person", "name": "Person"}
            )
        return SimpleNamespace(
            json=lambda: [{"email": "person@example.com", "primary": True}]
        )

    def create_session(_user_data, _provider):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return "session-token"

    monkeypatch.setattr(
        oauth_handler.github,
        "authorize_access_token",
        authorize_access_token,
    )
    monkeypatch.setattr(oauth_handler.github, "get", get)
    monkeypatch.setattr(
        oauth_handler.user_manager,
        "create_session",
        create_session,
    )
    request = SimpleNamespace(
        session={"oauth_github_client_name": "__invalid_test_client__"}
    )

    user_data = asyncio.run(oauth_handler.handle_github_callback(request))

    assert user_data["session_token"] == "session-token"


def test_langgraph_authentication_checks_session_off_event_loop(monkeypatch):
    from research_agent import auth

    def authenticate_credential(credential):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert credential == "session-token"
        return {
            "identity": "google:google-subject",
            "display_name": "Person",
            "is_authenticated": True,
        }

    monkeypatch.delenv("ALLOW_ALL_THREADS", raising=False)
    monkeypatch.setattr(auth, "authenticate_credential", authenticate_credential)

    user = asyncio.run(auth.authenticate({b"x-api-key": b"session-token"}))

    assert user["identity"] == "google:google-subject"


def test_fastapi_auth_helper_checks_session_off_event_loop(monkeypatch):
    import webapp.auth_helpers as auth_helpers

    def validate_session(token):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert token == "session-token"
        return {"identity": "google:google-subject"}

    monkeypatch.setattr(auth_helpers._cfg, "API_KEY", "static-api-key")
    monkeypatch.setattr(auth_helpers._cfg, "OAUTH_ENABLED", True)
    monkeypatch.setattr(
        auth_helpers._cfg,
        "user_manager",
        SimpleNamespace(validate_session=validate_session),
    )
    request = SimpleNamespace(headers={})

    async def authenticate():
        return await auth_helpers.is_authenticated("session-token", request)

    assert asyncio.run(authenticate()) is True


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        ("postgres", "PostgreSQL"),
        ("postgresql", "PostgreSQL"),
        ("cosmos", "Cosmos"),
        ("cosmosdb", "Cosmos"),
    ],
)
def test_store_factory_fails_precisely_when_adapter_config_is_missing(
        monkeypatch, backend, message
):
    for name in (
            "DATABASE_URL",
            "POSTGRES_URL",
            "POSTGRES_HOST",
            "POSTGRES_USER",
            "POSTGRES_DB",
            "COSMOS_CONNECTION_STRING",
            "COSMOSDB_ENDPOINT",
            "COSMOSDB_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match=message):
        create_auth_store(backend=backend)
