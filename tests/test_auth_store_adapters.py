"""Client-boundary tests for PostgreSQL and Cosmos auth adapters."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from webapp.auth_store import (
    AccountRecord,
    ChallengeLimitError,
    CosmosAuthStore,
    CredentialLimitError,
    PostgresAuthStore,
    SQLiteAuthStore,
    create_auth_store,
)


class FakeCursor:
    def __init__(self, responses, statements):
        self.responses = responses
        self.statements = statements
        self.current = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None, **_kwargs):
        self.statements.append((str(statement), parameters))
        self.current = self.responses.pop(0) if self.responses else None
        return self

    def fetchone(self):
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self):
        if self.current is None:
            return []
        return self.current if isinstance(self.current, list) else [self.current]


class FakePgConnection:
    def __init__(self, responses, statements):
        self.responses = responses
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self, **_kwargs):
        return FakeCursor(self.responses, self.statements)


class FakePgPool:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.statements = []
        self.closed = False

    @contextmanager
    def connection(self):
        yield FakePgConnection(self.responses, self.statements)

    def close(self):
        self.closed = True


def _assert_executable_postgres_sql(statement, parameters=None):
    """Catch placeholder loss and token splitting hidden by permissive fakes."""
    normalized = " ".join(str(statement).split())
    assert ": :" not in normalized
    assert normalized.count("%s") == len(parameters or ())
    return normalized


def test_postgres_session_lookup_has_bound_predicate_before_optional_lock():
    pool = FakePgPool(responses=[None, None])
    store = PostgresAuthStore(pool=pool, migrate=False)

    assert store._session("raw-token") is None
    assert store._session("raw-token", lock=True) is None

    unlocked = _assert_executable_postgres_sql(*pool.statements[0])
    locked = _assert_executable_postgres_sql(*pool.statements[1])
    assert "WHERE s.token_hash=%s" in unlocked
    assert "FOR UPDATE" not in unlocked
    assert "WHERE s.token_hash=%s FOR UPDATE" in locked


def test_postgres_account_migration_uses_valid_jsonb_default_cast():
    pool = FakePgPool()

    PostgresAuthStore(pool=pool)

    account_table = next(
        statement
        for statement, _parameters in pool.statements
        if "CREATE TABLE IF NOT EXISTS auth_accounts" in statement
    )
    normalized = _assert_executable_postgres_sql(account_table)
    assert "DEFAULT '{}'::jsonb" in normalized


def test_postgres_counter_update_is_conditional_and_uses_returning():
    pool = FakePgPool(responses=[{"credential_id": "credential_A"}, None])
    store = PostgresAuthStore(pool=pool, migrate=False)

    assert store.update_credential_state(
        "credential_A",
        expected_sign_count=7,
        expected_backed_up=True,
        new_sign_count=8,
        backed_up=True,
        last_used_at=1234.5,
    )
    assert not store.update_credential_state(
        "credential_A",
        expected_sign_count=7,
        expected_backed_up=True,
        new_sign_count=9,
        backed_up=False,
        last_used_at=1235.5,
    )

    statement, parameters = pool.statements[0]
    assert "sign_count = %s" in statement
    assert "backed_up = %s" in statement
    assert "RETURNING credential_id" in statement
    assert parameters[-2:] == (7, True)


def test_postgres_claim_challenge_is_atomic_with_row_lock():
    challenge = {
        "ceremony_id": "ceremony_A",
        "challenge": b"challenge",
        "kind": "registration",
        "identity": "google:123",
        "origin": "https://app.example.com",
        "rp_id": "example.com",
        "proxy_id": "web-bff",
        "created_at": 1000.0,
        "expires_at": 2000.0,
        "consumed_at": None,
    }
    pool = FakePgPool(responses=[challenge, {"ceremony_id": "ceremony_A"}])
    store = PostgresAuthStore(pool=pool, migrate=False, clock=lambda: 1500.0)

    claimed = store.claim_challenge("ceremony_A")

    assert claimed.origin == "https://app.example.com"
    assert claimed.rp_id == "example.com"

    assert "FOR UPDATE" in pool.statements[0][0]
    assert "consumed_at IS NULL" in pool.statements[1][0]
    assert "RETURNING ceremony_id" in pool.statements[1][0]


def test_postgres_existing_schema_adds_rp_column():
    pool = FakePgPool()

    PostgresAuthStore(pool=pool)

    assert any(
        "ALTER TABLE auth_credentials ADD COLUMN IF NOT EXISTS rp_id TEXT" in statement
        for statement, _parameters in pool.statements
    )


def test_postgres_conditional_rp_binding_is_idempotent():
    pool = FakePgPool(
        responses=[
            {"credential_id": "credential_A"},
            {"rp_id": "app.example.com"},
            None,
            {"rp_id": "app.example.com"},
            None,
            {"rp_id": "app.example.com"},
        ]
    )
    store = PostgresAuthStore(pool=pool, migrate=False)

    assert store.bind_credential_rp_id("credential_A", "app.example.com") is True
    assert store.bind_credential_rp_id("credential_A", "app.example.com") is True
    assert store.bind_credential_rp_id("credential_A", "other.example.com") is False

    update = pool.statements[0]
    assert "rp_id IS NULL" in update[0]
    assert "RETURNING credential_id" in update[0]


def test_postgres_credential_filter_reads_exact_rp_id():
    row = {
        "credential_id": "credential_A",
        "identity": "google:123",
        "rp_id": "app.example.com",
        "public_key": b"public-key",
        "sign_count": 0,
        "transports_json": ["internal"],
        "device_type": "single_device",
        "backed_up": False,
        "label": None,
        "created_at": 1000.0,
        "last_used_at": None,
    }
    pool = FakePgPool(responses=[[row]])
    store = PostgresAuthStore(pool=pool, migrate=False)

    listed = store.list_credentials("google:123", "app.example.com")

    assert listed[0].rp_id == "app.example.com"
    assert "identity=%s AND rp_id=%s" in pool.statements[0][0]
    assert pool.statements[0][1] == ("google:123", "app.example.com")


@pytest.mark.parametrize(
    "method_name",
    ["cleanup_expired_sessions", "cleanup_challenges"],
)
def test_postgres_cleanup_rechecks_expiry_when_deleting_scanned_rows(method_name):
    pool = FakePgPool(responses=[[]])
    store = PostgresAuthStore(pool=pool, migrate=False, clock=lambda: 1500.0)

    assert getattr(store, method_name)(limit=5) == 0

    statement, parameters = pool.statements[0]
    assert statement.replace(" ", "").count("expires_at<=%s") == 2
    assert parameters == (1500.0, 5, 1500.0)


class FakeCosmosContainer:
    def __init__(self, items=None):
        self.items = dict(items or {})
        self.calls = []
        self.conflict = False
        self.conflict_replacement = None
        self.query_reads = 0
        self.before_create = None
        self.before_delete = None
        self.before_replace = None
        self.create_error = None
        self.replace_conflict_calls = set()
        self._replace_calls = 0
        self._etag = 0
        self._lock = threading.Lock()

    def _next_etag(self):
        self._etag += 1
        return f"etag-{self._etag}"

    def read_item(self, item, partition_key):
        self.calls.append(("read", item, partition_key))
        with self._lock:
            if item not in self.items:
                error = RuntimeError("not found")
                error.status_code = 404
                raise error
            return dict(self.items[item])

    def create_item(self, body):
        self.calls.append(("create", body["id"], body["pk"]))
        if self.before_create is not None:
            self.before_create(body)
        if self.create_error is not None:
            error, self.create_error = self.create_error, None
            raise error
        with self._lock:
            if body["id"] in self.items:
                error = RuntimeError("conflict")
                error.status_code = 409
                raise error
            self.items[body["id"]] = dict(body, _etag=self._next_etag())
            return dict(self.items[body["id"]])

    def replace_item(self, item, body, partition_key, etag, match_condition):
        self.calls.append(
            ("replace", item, partition_key, etag, match_condition, dict(body))
        )
        if self.before_replace is not None:
            self.before_replace(item, body)
        with self._lock:
            self._replace_calls += 1
            if (
                    self.conflict
                    or self._replace_calls in self.replace_conflict_calls
                    or self.items[item].get("_etag") != etag
            ):
                if self.conflict_replacement is not None:
                    self.items[item] = dict(self.conflict_replacement)
                error = RuntimeError("precondition failed")
                error.status_code = 412
                raise error
            self.items[item] = dict(body, _etag=self._next_etag())
            return dict(self.items[item])

    def delete_item(self, item, partition_key, **_kwargs):
        self.calls.append(("delete", item, partition_key))
        if self.before_delete is not None:
            self.before_delete(item)
        with self._lock:
            current = self.items.get(item)
            if current is None:
                error = RuntimeError("not found")
                error.status_code = 404
                raise error
            if _kwargs.get("etag") not in {None, current.get("_etag")}:
                error = RuntimeError("precondition failed")
                error.status_code = 412
                raise error
            self.items.pop(item)

    def query_items(self, **_kwargs):
        self.calls.append(("query", _kwargs))

        def rows():
            parameters = {
                parameter["name"]: parameter["value"]
                for parameter in _kwargs.get("parameters", ())
            }
            query = _kwargs.get("query", "")
            with self._lock:
                snapshot = list(self.items.values())
            for item in snapshot:
                if "expires_at <= @now" in query and not (
                        item.get("expires_at", float("inf")) <= parameters["@now"]
                ):
                    continue
                if "c.identity=@identity" in query and item.get(
                        "identity"
                ) != parameters.get("@identity"):
                    continue
                if "c.rp_id=@rp_id" in query and item.get("rp_id") != parameters.get(
                        "@rp_id"
                ):
                    continue
                if "c.identity = @identity" in query and item.get(
                        "identity"
                ) != parameters.get("@identity"):
                    continue
                if (
                        "c.kind = 'registration'" in query
                        and item.get("kind") != "registration"
                ):
                    continue
                if (
                        "IS_NULL(c.consumed_at)" in query
                        and item.get("consumed_at") is not None
                ):
                    continue
                if "c.expires_at > @now" in query and not (
                        item.get("expires_at", 0) > parameters["@now"]
                ):
                    continue
                self.query_reads += 1
                yield dict(item)

        return rows()


def _cosmos_store(*, credential=None, challenge=None, clock=None):
    containers = {
        "accounts": FakeCosmosContainer(),
        "sessions": FakeCosmosContainer(),
        "credentials": FakeCosmosContainer(
            {credential["id"]: credential} if credential else None
        ),
        "challenges": FakeCosmosContainer(
            {challenge["id"]: challenge} if challenge else None
        ),
        "rate_limits": FakeCosmosContainer(),
    }
    return CosmosAuthStore(
        containers=containers,
        clock=clock or (lambda: 1500.0),
    ), containers


def test_cosmos_credential_counter_update_uses_etag_and_partition_key():
    credential = {
        "id": "credential_A",
        "pk": "credential_A",
        "identity": "google:123",
        "credential_id": "credential_A",
        "public_key": "cHVibGljLWtleQ",
        "sign_count": 7,
        "transports": ["internal"],
        "device_type": "multi_device",
        "backed_up": True,
        "label": None,
        "created_at": 1000.0,
        "last_used_at": None,
        "_etag": "credential-etag",
    }
    store, containers = _cosmos_store(credential=credential)

    assert store.update_credential_state(
        "credential_A",
        expected_sign_count=7,
        expected_backed_up=True,
        new_sign_count=8,
        backed_up=True,
        last_used_at=1500.0,
    )
    containers["credentials"].conflict = True
    assert not store.update_credential_state(
        "credential_A",
        expected_sign_count=8,
        expected_backed_up=True,
        new_sign_count=9,
        backed_up=True,
        last_used_at=1501.0,
    )

    replace = next(
        call for call in containers["credentials"].calls if call[0] == "replace"
    )
    assert replace[2] == "credential_A"
    assert replace[3] == "credential-etag"
    assert replace[4] is not None


def test_cosmos_claim_challenge_is_atomic_with_etag():
    challenge = {
        "id": "ceremony_A",
        "pk": "ceremony_A",
        "ceremony_id": "ceremony_A",
        "challenge": "Y2hhbGxlbmdl",
        "kind": "registration",
        "identity": "google:123",
        "origin": "https://app.example.com",
        "rp_id": "example.com",
        "proxy_id": "web-bff",
        "created_at": 1000.0,
        "expires_at": 2000.0,
        "consumed_at": None,
        "_etag": "challenge-etag",
    }
    store, containers = _cosmos_store(challenge=challenge)

    claimed = store.claim_challenge("ceremony_A")
    assert claimed.origin == "https://app.example.com"
    assert store.claim_challenge("ceremony_A") is None

    replace = next(
        call for call in containers["challenges"].calls if call[0] == "replace"
    )
    assert replace[2] == "ceremony_A"
    assert replace[3] == "challenge-etag"
    assert replace[5]["consumed_at"] == 1500.0


def test_cosmos_legacy_binding_retries_etag_conflict():
    credential = {
        "id": "credential_A",
        "pk": "credential_A",
        "identity": "google:123",
        "credential_id": "credential_A",
        "public_key": "cHVibGljLWtleQ",
        "sign_count": 7,
        "transports": ["internal"],
        "device_type": "multi_device",
        "backed_up": True,
        "label": None,
        "created_at": 1000.0,
        "last_used_at": None,
        "_etag": "credential-etag",
    }
    store, containers = _cosmos_store(credential=credential)
    containers["credentials"].replace_conflict_calls = {1}

    assert store.get_credential("credential_A").rp_id is None
    assert store.bind_credential_rp_id("credential_A", "app.example.com") is True
    assert store.bind_credential_rp_id("credential_A", "app.example.com") is True
    replace_count = len(
        [call for call in containers["credentials"].calls if call[0] == "replace"]
    )
    assert store.bind_credential_rp_id("credential_A", "other.example.com") is False
    assert (
            len([call for call in containers["credentials"].calls if call[0] == "replace"])
            == replace_count
    )
    assert containers["credentials"].items["credential_A"]["rp_id"] == "app.example.com"


def test_cosmos_multi_rp_rejection_does_not_replace_document():
    credential = {
        "id": "credential_A",
        "pk": "credential_A",
        "identity": "google:123",
        "credential_id": "credential_A",
        "rp_id": "app.example.com",
        "public_key": "cHVibGljLWtleQ",
        "sign_count": 0,
        "transports": [],
        "device_type": "single_device",
        "backed_up": False,
        "label": None,
        "created_at": 1000.0,
        "last_used_at": None,
        "_etag": "credential-etag",
    }
    store, containers = _cosmos_store(credential=credential)

    assert store.bind_credential_rp_id("credential_A", "other.example.com") is False
    assert not any(call[0] == "replace" for call in containers["credentials"].calls)
    assert containers["credentials"].items["credential_A"]["rp_id"] == "app.example.com"


def test_cosmos_claim_challenge_cas_loss_does_not_mutate_record():
    challenge = {
        "id": "ceremony_A",
        "pk": "ceremony_A",
        "ceremony_id": "ceremony_A",
        "challenge": "Y2hhbGxlbmdl",
        "kind": "authentication",
        "identity": None,
        "origin": "https://app.example.com",
        "rp_id": "example.com",
        "proxy_id": "web-bff",
        "created_at": 1000.0,
        "expires_at": 2000.0,
        "consumed_at": None,
        "_etag": "challenge-etag",
    }
    store, containers = _cosmos_store(challenge=challenge)
    containers["challenges"].conflict = True

    assert store.claim_challenge("ceremony_A") is None
    assert containers["challenges"].items["ceremony_A"]["consumed_at"] is None


def test_cosmos_registration_cap_reconciles_stale_expired_count():
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 0,
        "registration_challenge_count": 3,
        "_etag": "account-etag",
    }

    created = store.create_challenge(
        challenge=b"challenge",
        kind="registration",
        identity="google:123",
        origin="https://app.example.com",
        rp_id="example.com",
        proxy_id="web-bff",
        expires_at=1800.0,
    )

    assert created.identity == "google:123"
    assert (
            containers["accounts"].items["google:123"]["registration_challenge_count"] == 1
    )


def test_cosmos_point_documents_use_canonical_partition_keys():
    store, containers = _cosmos_store()

    token = store.create_session(
        {"identity": "google:123", "provider": "google"},
        "google",
    )
    digest = hashlib.sha256(token.encode()).hexdigest()
    credential = store.create_credential(
        identity="google:123",
        rp_id="example.com",
        credential_id="credential_A",
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="multi_device",
        backed_up=True,
    )

    assert containers["accounts"].items["google:123"]["pk"] == "google:123"
    assert containers["sessions"].items[digest]["pk"] == digest
    assert containers["credentials"].items["credential_A"]["pk"] == "credential_A"
    assert store.get_credential("credential_A") == credential
    assert store.list_credentials("google:123") == [credential]
    assert credential.rp_id == "example.com"
    assert store.list_credentials("google:123", "example.com") == [credential]


def test_cosmos_cleanup_stops_reading_at_bound():
    store, containers = _cosmos_store()
    containers["sessions"].items.update(
        {
            f"hash-{index}": {
                "id": f"hash-{index}",
                "pk": f"hash-{index}",
                "expires_at": 1000.0,
            }
            for index in range(20)
        }
    )

    assert store.cleanup_expired_sessions(limit=3) == 3
    assert containers["sessions"].query_reads == 3
    assert len(containers["sessions"].items) == 17


def test_cosmos_cleanup_does_not_delete_session_refreshed_after_scan():
    store, containers = _cosmos_store()
    containers["sessions"].items["hash"] = {
        "id": "hash",
        "pk": "hash",
        "expires_at": 1000.0,
        "_etag": "expired-etag",
    }

    def refresh_before_delete(item):
        containers["sessions"].before_delete = None
        containers["sessions"].items[item].update(
            expires_at=3000.0,
            _etag="refreshed-etag",
        )

    containers["sessions"].before_delete = refresh_before_delete

    assert store.cleanup_expired_sessions(limit=1) == 0
    assert containers["sessions"].items["hash"]["expires_at"] == 3000.0


def test_cosmos_challenge_cleanup_releases_registration_reservation():
    challenge = {
        "id": "ceremony_A",
        "pk": "ceremony_A",
        "ceremony_id": "ceremony_A",
        "challenge": "Y2hhbGxlbmdl",
        "kind": "registration",
        "identity": "google:123",
        "origin": "https://app.example.com",
        "rp_id": "example.com",
        "proxy_id": "web-bff",
        "created_at": 1000.0,
        "expires_at": 1200.0,
        "consumed_at": None,
        "_etag": "challenge-etag",
    }
    store, containers = _cosmos_store(challenge=challenge)
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 0,
        "registration_challenge_count": 1,
        "registration_challenge_reservations": [
            {"ceremony_id": "ceremony_A", "expires_at": 1200.0}
        ],
        "_etag": "account-etag",
    }

    assert store.cleanup_challenges(limit=1) == 1
    account = containers["accounts"].items["google:123"]
    assert account["registration_challenge_count"] == 0
    assert account["registration_challenge_reservations"] == []


def test_cosmos_fourth_concurrent_registration_reservation_is_rejected():
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 0,
        "registration_challenge_count": 0,
        "registration_challenge_reservations": [],
        "_etag": "account-etag",
    }
    initial_replace_barrier = threading.Barrier(4)
    create_barrier = threading.Barrier(4)
    release_creates = threading.Event()
    callback_lock = threading.Lock()
    initial_replace_calls = 0

    def align_initial_account_replacements(item, _body):
        nonlocal initial_replace_calls
        if item != "google:123":
            return
        with callback_lock:
            initial_replace_calls += 1
            should_wait = initial_replace_calls <= 3
        if should_wait:
            initial_replace_barrier.wait(timeout=5)

    def pause_challenge_creates(_body):
        create_barrier.wait(timeout=5)
        assert release_creates.wait(timeout=5)

    containers["accounts"].before_replace = align_initial_account_replacements
    containers["challenges"].before_create = pause_challenge_creates

    def create_one():
        return store.create_challenge(
            challenge=b"challenge",
            kind="registration",
            identity="google:123",
            origin="https://app.example.com",
            rp_id="example.com",
            proxy_id="web-bff",
            expires_at=1800.0,
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(create_one) for _ in range(3)]
        initial_replace_barrier.wait(timeout=5)
        create_barrier.wait(timeout=5)
        with pytest.raises(ChallengeLimitError):
            create_one()
        release_creates.set()
        assert len([future.result(timeout=5) for future in futures]) == 3

    account = containers["accounts"].items["google:123"]
    assert account["registration_challenge_count"] == 3
    assert len(account["registration_challenge_reservations"]) == 3


def test_cosmos_stale_credential_count_is_reconciled_before_cap_rejection():
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 10,
        "credential_reservations": [],
        "_etag": "account-etag",
    }

    credential = store.create_credential(
        identity="google:123",
        rp_id="example.com",
        credential_id="credential_A",
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="multi_device",
        backed_up=True,
    )

    assert credential.credential_id == "credential_A"
    assert containers["accounts"].items["google:123"]["credential_count"] == 1


def test_cosmos_credential_cap_reconciliation_counts_actual_credentials():
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 10,
        "credential_reservations": [],
        "_etag": "account-etag",
    }
    for index in range(10):
        credential_id = f"actual_{index}"
        containers["credentials"].items[credential_id] = {
            "id": credential_id,
            "pk": credential_id,
            "credential_id": credential_id,
            "identity": "google:123",
            "_etag": f"actual-etag-{index}",
        }

    with pytest.raises(CredentialLimitError):
        store.create_credential(
            identity="google:123",
            rp_id="example.com",
            credential_id="credential_overflow",
            public_key=b"public-key",
            sign_count=0,
            transports=["internal"],
            device_type="multi_device",
            backed_up=True,
        )


def test_cosmos_challenge_cap_reconciliation_counts_actual_challenges():
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 0,
        "registration_challenge_count": 3,
        "registration_challenge_reservations": [],
        "_etag": "account-etag",
    }
    for index in range(3):
        ceremony_id = f"actual_{index}"
        containers["challenges"].items[ceremony_id] = {
            "id": ceremony_id,
            "pk": ceremony_id,
            "ceremony_id": ceremony_id,
            "kind": "registration",
            "identity": "google:123",
            "expires_at": 1800.0,
            "consumed_at": None,
            "_etag": f"challenge-etag-{index}",
        }

    with pytest.raises(ChallengeLimitError):
        store.create_challenge(
            challenge=b"challenge",
            kind="registration",
            identity="google:123",
            origin="https://app.example.com",
            rp_id="example.com",
            proxy_id="web-bff",
            expires_at=1800.0,
        )


def test_cosmos_failed_credential_create_retries_reservation_rollback():
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 0,
        "credential_reservations": [],
        "_etag": "account-etag",
    }
    create_error = RuntimeError("injected create failure")
    create_error.status_code = 500
    containers["credentials"].create_error = create_error
    containers["accounts"].replace_conflict_calls = {2}

    with pytest.raises(RuntimeError, match="injected create failure"):
        store.create_credential(
            identity="google:123",
            rp_id="example.com",
            credential_id="credential_A",
            public_key=b"public-key",
            sign_count=0,
            transports=["internal"],
            device_type="multi_device",
            backed_up=True,
        )

    account = containers["accounts"].items["google:123"]
    assert account["credential_count"] == 0
    assert account["credential_reservations"] == []


def test_cosmos_inflight_credential_reservation_enforces_hard_cap():
    store, containers = _cosmos_store()
    slots = [
        {"credential_id": f"existing_{index}", "expires_at": None} for index in range(9)
    ]
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 9,
        "credential_reservations": slots,
        "_etag": "account-etag",
    }
    for index in range(9):
        credential_id = f"existing_{index}"
        containers["credentials"].items[credential_id] = {
            "id": credential_id,
            "pk": credential_id,
            "credential_id": credential_id,
            "identity": "google:123",
            "public_key": "cHVibGljLWtleQ",
            "sign_count": 0,
            "transports": ["internal"],
            "device_type": "multi_device",
            "backed_up": True,
            "label": None,
            "created_at": 1000.0,
            "last_used_at": None,
            "_etag": f"credential-{index}",
        }
    create_started = threading.Event()
    release_create = threading.Event()

    def pause_create(_body):
        create_started.set()
        assert release_create.wait(timeout=5)

    containers["credentials"].before_create = pause_create

    def create_tenth():
        return store.create_credential(
            identity="google:123",
            rp_id="example.com",
            credential_id="credential_tenth",
            public_key=b"public-key",
            sign_count=0,
            transports=["internal"],
            device_type="multi_device",
            backed_up=True,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(create_tenth)
        assert create_started.wait(timeout=5)
        with pytest.raises(CredentialLimitError):
            store.create_credential(
                identity="google:123",
                rp_id="example.com",
                credential_id="credential_overflow",
                public_key=b"public-key",
                sign_count=0,
                transports=["internal"],
                device_type="multi_device",
                backed_up=True,
            )
        release_create.set()
        assert future.result(timeout=5).credential_id == "credential_tenth"

    assert containers["accounts"].items["google:123"]["credential_count"] == 10


def test_cosmos_expired_credential_lease_stays_reserved_while_writer_is_paused():
    now = [1500.0]
    store, containers = _cosmos_store(clock=lambda: now[0])
    slots = [
        {"credential_id": f"existing_{index}", "expires_at": None} for index in range(9)
    ]
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 9,
        "credential_reservations": slots,
        "_etag": "account-etag",
    }
    for slot in slots:
        credential_id = slot["credential_id"]
        containers["credentials"].items[credential_id] = {
            "id": credential_id,
            "pk": credential_id,
            "credential_id": credential_id,
            "identity": "google:123",
            "_etag": f"etag-{credential_id}",
        }
    paused = threading.Event()
    resume = threading.Event()

    def pause_first_writer(body):
        if body["credential_id"] == "credential_tenth":
            paused.set()
            assert resume.wait(timeout=5)
        else:
            pytest.fail("expired in-flight reservation bypassed hard cap")

    containers["credentials"].before_create = pause_first_writer

    def create_tenth():
        return store.create_credential(
            identity="google:123",
            rp_id="app.example.com",
            credential_id="credential_tenth",
            public_key=b"public-key",
            sign_count=0,
            transports=[],
            device_type="single_device",
            backed_up=False,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(create_tenth)
        assert paused.wait(timeout=5)
        now[0] = 1801.0
        with pytest.raises(CredentialLimitError):
            store.create_credential(
                identity="google:123",
                rp_id="app.example.com",
                credential_id="credential_overflow",
                public_key=b"public-key",
                sign_count=0,
                transports=[],
                device_type="single_device",
                backed_up=False,
            )
        resume.set()
        assert future.result(timeout=5).credential_id == "credential_tenth"

    assert containers["accounts"].items["google:123"]["credential_count"] == 10


def test_cosmos_expired_registration_lease_stays_reserved_while_writer_is_paused():
    now = [1500.0]
    store, containers = _cosmos_store(clock=lambda: now[0])
    reservations = []
    for index in range(2):
        ceremony_id = f"existing_{index}"
        reservations.append(
            {
                "ceremony_id": ceremony_id,
                "expires_at": 5000.0,
                "reserved_until": None,
            }
        )
        containers["challenges"].items[ceremony_id] = {
            "id": ceremony_id,
            "pk": ceremony_id,
            "ceremony_id": ceremony_id,
            "kind": "registration",
            "identity": "google:123",
            "expires_at": 5000.0,
            "consumed_at": None,
            "_etag": f"etag-{ceremony_id}",
        }
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 0,
        "registration_challenge_count": 2,
        "registration_challenge_reservations": reservations,
        "_etag": "account-etag",
    }
    paused = threading.Event()
    resume = threading.Event()
    create_calls = 0

    def pause_first_writer(_body):
        nonlocal create_calls
        create_calls += 1
        if create_calls == 1:
            paused.set()
            assert resume.wait(timeout=5)
        else:
            pytest.fail("expired in-flight challenge reservation bypassed hard cap")

    containers["challenges"].before_create = pause_first_writer

    def create_third():
        return store.create_challenge(
            challenge=b"challenge",
            kind="registration",
            identity="google:123",
            origin="https://app.example.com",
            rp_id="app.example.com",
            proxy_id="web-bff",
            expires_at=5000.0,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(create_third)
        assert paused.wait(timeout=5)
        now[0] = 1801.0
        with pytest.raises(ChallengeLimitError):
            create_third()
        resume.set()
        assert future.result(timeout=5).kind == "registration"

    account = containers["accounts"].items["google:123"]
    assert account["registration_challenge_count"] == 3
    assert len(account["registration_challenge_reservations"]) == 3


def _cosmos_orphan_repair_store():
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 4,
        "credential_reservations": [
            {"credential_id": "old_missing", "expires_at": 1000.0},
            {"credential_id": "old_existing", "expires_at": 900.0},
            {"credential_id": "young_missing", "expires_at": 1400.0},
            {"credential_id": "committed_missing", "expires_at": None},
        ],
        "registration_challenge_count": 4,
        "registration_challenge_reservations": [
            {
                "ceremony_id": "old_challenge_missing",
                "expires_at": 5000.0,
                "reserved_until": 1000.0,
            },
            {
                "ceremony_id": "old_challenge_existing",
                "expires_at": 5000.0,
                "reserved_until": 900.0,
            },
            {
                "ceremony_id": "young_challenge_missing",
                "expires_at": 5000.0,
                "reserved_until": 1400.0,
            },
            {
                "ceremony_id": "committed_challenge_missing",
                "expires_at": 5000.0,
                "reserved_until": None,
            },
        ],
        "_etag": "account-etag",
    }
    containers["credentials"].items["old_existing"] = {
        "id": "old_existing",
        "pk": "old_existing",
        "credential_id": "old_existing",
        "identity": "google:123",
        "_etag": "credential-etag",
    }
    containers["challenges"].items["old_challenge_existing"] = {
        "id": "old_challenge_existing",
        "pk": "old_challenge_existing",
        "ceremony_id": "old_challenge_existing",
        "identity": "google:123",
        "kind": "registration",
        "_etag": "challenge-etag",
    }
    return store, containers


def test_cosmos_orphan_repair_requires_quiesced_confirmation_without_mutation():
    store, containers = _cosmos_orphan_repair_store()
    before = dict(containers["accounts"].items["google:123"])

    with pytest.raises(ValueError, match="quiesced"):
        store.reclaim_orphan_reservations(
            "google:123",
            cutoff=1300.0,
            confirmed_quiesced=False,
        )

    assert containers["accounts"].items["google:123"] == before


def test_cosmos_orphan_repair_dry_run_reports_without_mutation():
    store, containers = _cosmos_orphan_repair_store()
    before = dict(containers["accounts"].items["google:123"])

    result = store.reclaim_orphan_reservations(
        "google:123",
        cutoff=1300.0,
        confirmed_quiesced=True,
        apply=False,
    )

    assert result == {
        "identity": "google:123",
        "cutoff": 1300.0,
        "apply": False,
        "include_committed_missing": False,
        "credential_orphans": 1,
        "challenge_orphans": 1,
        "reclaimed": 0,
    }
    assert containers["accounts"].items["google:123"] == before


def test_cosmos_orphan_repair_retains_existing_young_and_committed_reservations():
    store, containers = _cosmos_orphan_repair_store()

    result = store.reclaim_orphan_reservations(
        "google:123",
        cutoff=1300.0,
        confirmed_quiesced=True,
        apply=True,
    )

    account = containers["accounts"].items["google:123"]
    assert result["reclaimed"] == 2
    assert account["credential_count"] == 3
    assert {item["credential_id"] for item in account["credential_reservations"]} == {
        "old_existing",
        "young_missing",
        "committed_missing",
    }
    assert account["registration_challenge_count"] == 3
    assert {
               item["ceremony_id"] for item in account["registration_challenge_reservations"]
           } == {
               "old_challenge_existing",
               "young_challenge_missing",
               "committed_challenge_missing",
           }


def test_cosmos_orphan_repair_retries_etag_conflict():
    store, containers = _cosmos_orphan_repair_store()
    containers["accounts"].replace_conflict_calls = {1}

    result = store.reclaim_orphan_reservations(
        "google:123",
        cutoff=1300.0,
        confirmed_quiesced=True,
        apply=True,
    )

    assert result["reclaimed"] == 2
    assert (
            len([call for call in containers["accounts"].calls if call[0] == "replace"])
            == 2
    )


def test_cosmos_orphan_repair_is_bounded_across_both_reservation_lists():
    store, containers = _cosmos_orphan_repair_store()

    result = store.reclaim_orphan_reservations(
        "google:123",
        cutoff=1300.0,
        limit=1,
        confirmed_quiesced=True,
        apply=True,
    )

    account = containers["accounts"].items["google:123"]
    assert result["reclaimed"] == 1
    assert result["credential_orphans"] == 1
    assert result["challenge_orphans"] == 0
    assert account["credential_count"] == 3
    assert account["registration_challenge_count"] == 4


def test_cosmos_orphan_repair_opt_in_reclaims_committed_missing_after_delete_finalize_failure():
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "provider_subject": "123",
        "credential_count": 1,
        "credential_reservations": [
            {"credential_id": "credential_A", "expires_at": None}
        ],
        "registration_challenge_count": 0,
        "registration_challenge_reservations": [],
        "_etag": "account-etag",
    }
    containers["credentials"].items["credential_A"] = {
        "id": "credential_A",
        "pk": "credential_A",
        "credential_id": "credential_A",
        "identity": "google:123",
        "_etag": "credential-etag",
    }
    containers["accounts"].replace_conflict_calls = set(range(1, 9))

    assert store.delete_credential("google:123", "credential_A") is True
    assert "credential_A" not in containers["credentials"].items
    account = containers["accounts"].items["google:123"]
    assert account["credential_reservations"] == [
        {"credential_id": "credential_A", "expires_at": None}
    ]
    containers["accounts"].replace_conflict_calls.clear()

    conservative = store.reclaim_orphan_reservations(
        "google:123",
        cutoff=1300.0,
        confirmed_quiesced=True,
        apply=True,
    )
    assert conservative["reclaimed"] == 0
    assert account["credential_count"] == 1

    dry_run = store.reclaim_orphan_reservations(
        "google:123",
        cutoff=1300.0,
        confirmed_quiesced=True,
        include_committed_missing=True,
        apply=False,
    )
    assert dry_run["credential_orphans"] == 1
    assert dry_run["reclaimed"] == 0
    assert account["credential_count"] == 1

    applied = store.reclaim_orphan_reservations(
        "google:123",
        cutoff=1300.0,
        confirmed_quiesced=True,
        include_committed_missing=True,
        apply=True,
    )
    assert applied["reclaimed"] == 1
    assert containers["accounts"].items["google:123"]["credential_count"] == 0


def test_cosmos_orphan_repair_never_reclaims_committed_marker_with_document():
    store, containers = _cosmos_orphan_repair_store()
    containers["credentials"].items["committed_missing"] = {
        "id": "committed_missing",
        "pk": "committed_missing",
        "credential_id": "committed_missing",
        "identity": "google:123",
        "_etag": "committed-etag",
    }
    containers["challenges"].items["committed_challenge_missing"] = {
        "id": "committed_challenge_missing",
        "pk": "committed_challenge_missing",
        "ceremony_id": "committed_challenge_missing",
        "identity": "google:123",
        "kind": "registration",
        "_etag": "committed-challenge-etag",
    }

    result = store.reclaim_orphan_reservations(
        "google:123",
        cutoff=0.0,
        confirmed_quiesced=True,
        include_committed_missing=True,
        apply=True,
    )

    assert result["reclaimed"] == 0
    account = containers["accounts"].items["google:123"]
    assert any(
        item["credential_id"] == "committed_missing"
        for item in account["credential_reservations"]
    )
    assert any(
        item["ceremony_id"] == "committed_challenge_missing"
        for item in account["registration_challenge_reservations"]
    )


def _cosmos_session_store(*, expires_at=2000.0, auth_method="oauth"):
    store, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = {
        "id": "google:123",
        "pk": "google:123",
        "identity": "google:123",
        "provider": "google",
        "email": "person@example.com",
        "name": "Person",
        "profile": {},
        "_etag": "account-etag",
    }
    digest = "a" * 64
    containers["sessions"].items[digest] = {
        "id": digest,
        "pk": digest,
        "token_hash": digest,
        "identity": "google:123",
        "provider": "google",
        "auth_method": auth_method,
        "authenticated_at": 1000.0,
        "created_at": 1000.0,
        "expires_at": expires_at,
        "_etag": "session-etag",
    }
    return store, containers, digest


def test_cosmos_rejects_invalid_session_auth_method():
    store, _containers = _cosmos_store()

    with pytest.raises(ValueError, match="auth_method"):
        store.create_session(
            {"identity": "google:123", "provider": "google"},
            "google",
            auth_method="password",
        )


def test_cosmos_live_session_validation_is_read_only(monkeypatch):
    store, containers, digest = _cosmos_session_store(expires_at=6000.0)
    monkeypatch.setattr("webapp.auth_store_cosmos._token_hash", lambda _token: digest)

    user = store.validate_session("raw-token")

    assert user["identity"] == "google:123"
    assert not any(call[0] == "replace" for call in containers["sessions"].calls)


def test_cosmos_near_expiry_validation_slides_without_reauthentication(monkeypatch):
    store, containers, digest = _cosmos_session_store(expires_at=1550.0)
    monkeypatch.setattr("webapp.auth_store_cosmos._token_hash", lambda _token: digest)

    assert store.validate_session("raw-token")["identity"] == "google:123"

    session = containers["sessions"].items[digest]
    assert session["expires_at"] == 1500.0 + 24 * 60 * 60
    assert session["authenticated_at"] == 1000.0


def test_cosmos_session_slide_rechecks_after_etag_conflict(monkeypatch):
    store, containers, digest = _cosmos_session_store(expires_at=1550.0)
    monkeypatch.setattr("webapp.auth_store_cosmos._token_hash", lambda _token: digest)
    winner = dict(
        containers["sessions"].items[digest],
        expires_at=3000.0,
        _etag="winner-etag",
    )
    containers["sessions"].conflict = True
    containers["sessions"].conflict_replacement = winner

    assert store.validate_session("raw-token")["identity"] == "google:123"
    assert any(call[0] == "replace" for call in containers["sessions"].calls)
    assert containers["sessions"].items[digest]["authenticated_at"] == 1000.0


def test_cosmos_account_upsert_refreshes_avatar_and_sanitized_profile():
    store, containers = _cosmos_store()
    store.create_session(
        {
            "identity": "google:123",
            "provider": "google",
            "picture": "https://old.example/avatar.png",
            "locale": "en",
        },
        "google",
    )

    store.create_session(
        {
            "identity": "google:123",
            "provider": "google",
            "picture": "https://new.example/avatar.png",
            "locale": "fr",
            "oauth_token": "must-not-persist",
        },
        "google",
    )

    account = containers["accounts"].items["google:123"]
    assert account["avatar_url"] == "https://new.example/avatar.png"
    assert account["profile"]["locale"] == "fr"
    assert "oauth_token" not in account["profile"]


@pytest.fixture(params=("sqlite", "postgres", "cosmos"))
def adapter_for_validation(request, tmp_path):
    if request.param == "sqlite":
        store = SQLiteAuthStore(tmp_path / "validation.db")
        try:
            yield store
        finally:
            store.close()
        return
    if request.param == "postgres":
        yield PostgresAuthStore(pool=FakePgPool(), migrate=False)
        return
    store, _containers = _cosmos_store()
    yield store


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sign_count": -1}, "sign_count must be a non-negative integer"),
        ({"sign_count": True}, "sign_count must be a non-negative integer"),
        ({"transports": "internal"}, "transports must be a sequence"),
        ({"transports": ["invalid"]}, "transports contains an unsupported value"),
        ({"device_type": "invalid"}, "device_type is unsupported"),
        ({"device_type": []}, "device_type is unsupported"),
        ({"backed_up": 1}, "backed_up must be boolean"),
        ({"label": 7}, "credential label must contain at most 100 characters"),
        ({"label": "x" * 101}, "credential label must contain at most 100 characters"),
    ],
)
def test_credential_validation_is_identical_across_adapters(
        adapter_for_validation,
        overrides,
        message,
):
    values = {
        "identity": "google:missing",
        "rp_id": "example.com",
        "credential_id": "credential_A",
        "public_key": b"public-key",
        "sign_count": 0,
        "transports": ["internal"],
        "device_type": "multi_device",
        "backed_up": True,
        "label": None,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        adapter_for_validation.create_credential(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
                {"expected_sign_count": -1},
                "expected_sign_count must be a non-negative integer",
        ),
        ({"new_sign_count": -1}, "new_sign_count must be a non-negative integer"),
        ({"expected_backed_up": 1}, "expected_backed_up must be boolean"),
        ({"backed_up": 1}, "backed_up must be boolean"),
    ],
)
def test_counter_update_validation_is_identical_across_adapters(
        adapter_for_validation,
        overrides,
        message,
):
    values = {
        "expected_sign_count": 0,
        "expected_backed_up": True,
        "new_sign_count": 1,
        "backed_up": True,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        adapter_for_validation.update_credential_state("credential_A", **values)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("origin", "", "origin is invalid"),
        ("origin", "x" * 2049, "origin is invalid"),
        ("rp_id", "", "rp_id is invalid"),
        ("rp_id", "x" * 256, "rp_id is invalid"),
        ("proxy_id", "", "proxy_id is invalid"),
        ("proxy_id", "x" * 256, "proxy_id is invalid"),
    ],
)
def test_challenge_bounds_are_identical_across_adapters(
        adapter_for_validation,
        field,
        value,
        message,
):
    values = {
        "challenge": b"challenge",
        "kind": "authentication",
        "identity": None,
        "origin": "https://app.example.com",
        "rp_id": "example.com",
        "proxy_id": "web-bff",
        "expires_at": 1800.0,
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        adapter_for_validation.create_challenge(**values)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (("unknown", "key", 1, 1), "scope"),
        (("proxy", "", 1, 1), "key"),
        (("proxy", "key", -1, 1), "window"),
        (("proxy", "key", 1, 0), "limit"),
    ],
)
def test_rate_limit_validation_is_identical_across_adapters(
        adapter_for_validation, values, message
):
    with pytest.raises(ValueError, match=message):
        adapter_for_validation.consume_rate_limit(*values)


def test_postgres_rate_limit_uses_atomic_upsert():
    allowed_pool = FakePgPool(responses=[None, {"count": 1}])
    allowed = PostgresAuthStore(pool=allowed_pool, migrate=False, clock=lambda: 12.0)
    denied_pool = FakePgPool(responses=[None, None])
    denied = PostgresAuthStore(pool=denied_pool, migrate=False, clock=lambda: 12.0)

    assert allowed.consume_rate_limit("proxy", "web-bff", 100, 2)
    assert not denied.consume_rate_limit("proxy", "web-bff", 100, 2)
    statement, parameters = allowed_pool.statements[1]
    assert "ON CONFLICT(scope, rate_key, window_start)" in statement
    assert "auth_rate_limits.count<%s" in statement.replace(" ", "").replace("\n", "")
    assert parameters == ("proxy", "web-bff", 100, 12.0, 2)


def test_cosmos_rate_limit_uses_etag_cas_and_persists_count():
    store, containers = _cosmos_store()

    assert store.consume_rate_limit("proxy", "web-bff", 100, 2)
    assert store.consume_rate_limit("proxy", "web-bff", 100, 2)
    assert not store.consume_rate_limit("proxy", "web-bff", 100, 2)

    docs = list(containers["rate_limits"].items.values())
    assert docs[0]["count"] == 2
    assert docs[0]["ttl"] == 180
    assert any(call[0] == "replace" for call in containers["rate_limits"].calls)


class CloseTracker:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_adapter_close_only_closes_owned_resources():
    injected_pool = FakePgPool()
    PostgresAuthStore(pool=injected_pool, migrate=False).close()
    assert not injected_pool.closed

    owned_pool = FakePgPool()
    PostgresAuthStore(pool=owned_pool, migrate=False, owns_pool=True).close()
    assert owned_pool.closed

    owner = CloseTracker()
    containers = {
        name: FakeCosmosContainer()
        for name in ("accounts", "sessions", "credentials", "challenges")
    }
    CosmosAuthStore(containers=containers, owner=owner).close()
    assert owner.closed


def test_account_lookup_contract_exposes_immutable_webauthn_handle(tmp_path):
    sqlite = SQLiteAuthStore(tmp_path / "accounts.db")
    try:
        sqlite.create_session(
            {
                "identity": "google:123",
                "provider": "google",
                "email": "person@example.com",
                "name": "Person",
                "picture": "https://example.com/avatar.png",
                "locale": "en",
            },
            "google",
        )
        sqlite_account = sqlite.get_account("google:123")
    finally:
        sqlite.close()

    assert isinstance(sqlite_account, AccountRecord)
    assert sqlite_account.identity == "google:123"
    assert sqlite_account.provider == "google"
    assert sqlite_account.profile == {
        "picture": "https://example.com/avatar.png",
        "locale": "en",
    }
    assert sqlite_account.webauthn_user_handle

    postgres_row = {
        "identity": "google:123",
        "provider": "google",
        "email": "person@example.com",
        "name": "Person",
        "avatar_url": "https://example.com/avatar.png",
        "profile_json": {"locale": "en"},
        "webauthn_user_handle": sqlite_account.webauthn_user_handle,
    }
    postgres = PostgresAuthStore(
        pool=FakePgPool(responses=[postgres_row]),
        migrate=False,
    )
    postgres_account = postgres.get_account("google:123")
    assert postgres_account == AccountRecord(
        identity="google:123",
        provider="google",
        email="person@example.com",
        name="Person",
        avatar_url="https://example.com/avatar.png",
        profile={"locale": "en"},
        webauthn_user_handle=sqlite_account.webauthn_user_handle,
    )

    cosmos, containers = _cosmos_store()
    containers["accounts"].items["google:123"] = dict(
        postgres_row,
        id="google:123",
        pk="google:123",
        profile={"locale": "en"},
        _etag="account-etag",
    )
    assert cosmos.get_account("google:123") == postgres_account


def test_factory_selects_injected_postgres_and_cosmos_adapters(tmp_path):
    pool = FakePgPool()
    postgres = create_auth_store(backend="postgres", postgres_pool=pool, migrate=False)
    assert isinstance(postgres, PostgresAuthStore)
    assert postgres.pool is pool

    containers = {
        name: FakeCosmosContainer()
        for name in ("accounts", "sessions", "credentials", "challenges")
    }
    cosmos = create_auth_store(backend="cosmosdb", cosmos_containers=containers)
    assert isinstance(cosmos, CosmosAuthStore)
    assert cosmos.containers == containers

    sqlite = create_auth_store(backend="sqlite", sqlite_path=tmp_path / "auth.db")
    assert sqlite.path == str((tmp_path / "auth.db").resolve())


def _exercise_real_adapter_contract(store):
    suffix = secrets.token_hex(8)
    identity = f"google:{suffix}"
    user = {"identity": identity, "provider": "google", "name": "Integration"}
    token = store.create_session(user, "google")
    assert store.validate_session(token)["identity"] == identity
    assert store.get_session_detail(token).authenticated_at <= time.time()
    assert store.refresh_session(token)["identity"] == identity
    assert store.remove_session(token) == identity
    assert store.validate_session(token) is None

    credential_ids = [f"cred_{suffix}_{index}" for index in range(10)]
    for credential_id in credential_ids:
        store.create_credential(
            identity=identity,
            rp_id="integration.example.com",
            credential_id=credential_id,
            public_key=b"integration-public-key",
            sign_count=0,
            transports=["internal"],
            device_type="multi_device",
            backed_up=True,
        )
    with pytest.raises(CredentialLimitError):
        store.create_credential(
            identity=identity,
            rp_id="integration.example.com",
            credential_id=f"cred_{suffix}_overflow",
            public_key=b"integration-public-key",
            sign_count=0,
            transports=["internal"],
            device_type="multi_device",
            backed_up=True,
        )
    first = credential_ids[0]
    assert store.get_credential(first).identity == identity
    assert len(store.list_credentials(identity)) == 10
    assert store.rename_credential(identity, first, "Renamed")
    assert store.update_credential_state(
        first,
        expected_sign_count=0,
        expected_backed_up=True,
        new_sign_count=1,
        backed_up=True,
    )
    assert not store.update_credential_state(
        first,
        expected_sign_count=0,
        expected_backed_up=True,
        new_sign_count=2,
        backed_up=True,
    )
    for credential_id in credential_ids:
        assert store.delete_credential(identity, credential_id)

    challenge = store.create_challenge(
        challenge=b"integration-challenge",
        kind="registration",
        identity=identity,
        origin="https://integration.example",
        rp_id="integration.example.com",
        proxy_id="integration-bff",
        expires_at=time.time() + 300,
    )
    assert store.claim_challenge(challenge.ceremony_id)
    assert store.claim_challenge(challenge.ceremony_id) is None
    for _ in range(3):
        store.create_challenge(
            challenge=b"integration-challenge",
            kind="registration",
            identity=identity,
            origin="https://integration.example",
            rp_id="integration.example.com",
            proxy_id="integration-bff",
            expires_at=time.time() + 300,
        )
    with pytest.raises(ChallengeLimitError):
        store.create_challenge(
            challenge=b"integration-challenge",
            kind="registration",
            identity=identity,
            origin="https://integration.example",
            rp_id="integration.example.com",
            proxy_id="integration-bff",
            expires_at=time.time() + 300,
        )


@pytest.mark.skipif(
    not os.environ.get("AUTH_STORE_POSTGRES_TEST_URL"),
    reason="AUTH_STORE_POSTGRES_TEST_URL is not configured",
)
def test_postgres_real_integration_schema():
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=os.environ["AUTH_STORE_POSTGRES_TEST_URL"],
        min_size=1,
        max_size=2,
        open=True,
    )
    try:
        _exercise_real_adapter_contract(PostgresAuthStore(pool=pool))
    finally:
        pool.close()


@pytest.mark.skipif(
    not (
            os.environ.get("AUTH_STORE_COSMOS_TEST_CONNECTION_STRING")
            or (
                    os.environ.get("AUTH_STORE_COSMOS_TEST_ENDPOINT")
                    and os.environ.get("AUTH_STORE_COSMOS_TEST_KEY")
            )
    ),
    reason="explicit Cosmos auth-store test endpoint is not configured",
)
def test_cosmos_real_integration_containers(monkeypatch):
    monkeypatch.setenv(
        "COSMOSDB_DB_NAME",
        os.environ.get("AUTH_STORE_COSMOS_TEST_DB", "deep_research_auth_tests"),
    )
    if os.environ.get("AUTH_STORE_COSMOS_TEST_CONNECTION_STRING"):
        monkeypatch.setenv(
            "COSMOS_CONNECTION_STRING",
            os.environ["AUTH_STORE_COSMOS_TEST_CONNECTION_STRING"],
        )
    else:
        monkeypatch.setenv(
            "COSMOSDB_ENDPOINT", os.environ["AUTH_STORE_COSMOS_TEST_ENDPOINT"]
        )
        monkeypatch.setenv("COSMOSDB_KEY", os.environ["AUTH_STORE_COSMOS_TEST_KEY"])
    store = create_auth_store(backend="cosmosdb")
    assert isinstance(store, CosmosAuthStore)
    _exercise_real_adapter_contract(store)
