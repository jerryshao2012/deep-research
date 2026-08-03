"""Durable, token-safe persistence for OAuth accounts and sessions."""

# ruff: noqa: D102, D107

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from webapp.webauthn_scope import normalize_rp_id

SESSION_LIFETIME_SECONDS = 24 * 60 * 60
SESSION_REFRESH_THRESHOLD_SECONDS = 60 * 60
DEFAULT_CLEANUP_LIMIT = 500
MAX_CLEANUP_LIMIT = 1_000
MAX_CREDENTIALS_PER_ACCOUNT = 10
MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT = 3
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ALLOWED_TRANSPORTS = frozenset(
    {"usb", "nfc", "ble", "smart-card", "hybrid", "internal"}
)
_ALLOWED_DEVICE_TYPES = frozenset({"single_device", "multi_device"})

_PROFILE_STRING_LIMITS = {
    "picture": 2_048,
    "avatar_url": 2_048,
    "username": 100,
    "locale": 32,
    "given_name": 200,
    "family_name": 200,
    "bio": 500,
    "location": 200,
    "company": 200,
    "blog": 2_048,
    "created_at": 100,
}
_PROFILE_BOOL_FIELDS = {"email_verified"}
_PROFILE_INTEGER_FIELDS = {"followers", "following", "public_repos"}


class AuthStoreError(RuntimeError):
    """Base error for durable authentication persistence failures."""


class DuplicateCredentialError(AuthStoreError):
    """Raised when a globally unique credential ID already exists."""


class CredentialLimitError(AuthStoreError):
    """Raised when an account already owns the maximum credential count."""


class ChallengeLimitError(AuthStoreError):
    """Raised when an account has too many registration challenges."""


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


class AuthStore(Protocol):
    """Backend-neutral contract implemented by all auth persistence adapters."""

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


def _validate_base64url(value: object, field: str, max_length: int = 2_048) -> str:
    if (
            not isinstance(value, str)
            or not value
            or len(value) > max_length
            or _BASE64URL_RE.fullmatch(value) is None
    ):
        raise ValueError(f"{field} must be an unpadded base64url string")
    return value


def _decode_binary(value: bytes | str, field: str, max_length: int) -> bytes:
    if isinstance(value, bytes):
        decoded = value
    elif isinstance(value, str):
        _validate_base64url(value, field, max_length * 2)
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{field} must contain valid base64url data") from exc
    else:
        raise ValueError(f"{field} must be bytes or base64url text")
    if not decoded or len(decoded) > max_length:
        raise ValueError(f"{field} has an invalid length")
    return decoded


def _normalize_transports(transports: Sequence[str]) -> tuple[str, ...]:
    if isinstance(transports, (str, bytes)) or not isinstance(transports, Sequence):
        raise ValueError("transports must be a sequence")
    normalized = tuple(dict.fromkeys(transports))
    if any(
            not isinstance(value, str) or value not in _ALLOWED_TRANSPORTS
            for value in normalized
    ):
        raise ValueError("transports contains an unsupported value")
    return normalized


def _validate_label(label: str | None) -> str | None:
    if label is None:
        return None
    if not isinstance(label, str) or len(label) > 100:
        raise ValueError("credential label must contain at most 100 characters")
    return label


def _validate_rp_id(rp_id: object) -> str:
    return normalize_rp_id(rp_id, "rp_id")


def _validate_rate_limit_inputs(
        scope: str, key: str, window_start: int, limit: int
) -> tuple[str, str, int, int]:
    if scope not in {"account", "proxy"}:
        raise ValueError("rate-limit scope is unsupported")
    if not isinstance(key, str) or not key or len(key) > 512:
        raise ValueError("rate-limit key must contain 1 to 512 characters")
    if (
            not isinstance(window_start, int)
            or isinstance(window_start, bool)
            or window_start < 0
    ):
        raise ValueError("rate-limit window must be a non-negative integer")
    if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1_000_000
    ):
        raise ValueError("rate-limit limit must be between 1 and 1000000")
    return scope, key, window_start, limit


def _validate_credential_inputs(
        *,
        sign_count: int,
        transports: Sequence[str],
        device_type: str,
        backed_up: bool,
        label: str | None,
) -> tuple[tuple[str, ...], str | None]:
    if (
            not isinstance(sign_count, int)
            or isinstance(sign_count, bool)
            or sign_count < 0
    ):
        raise ValueError("sign_count must be a non-negative integer")
    normalized_transports = _normalize_transports(transports)
    if not isinstance(device_type, str) or device_type not in _ALLOWED_DEVICE_TYPES:
        raise ValueError("device_type is unsupported")
    if not isinstance(backed_up, bool):
        raise ValueError("backed_up must be boolean")
    return normalized_transports, _validate_label(label)


def _validate_counter_update(
        *,
        expected_sign_count: int,
        expected_backed_up: bool,
        new_sign_count: int,
        backed_up: bool,
) -> None:
    for field, value in (
            ("expected_sign_count", expected_sign_count),
            ("new_sign_count", new_sign_count),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    for field, value in (
            ("expected_backed_up", expected_backed_up),
            ("backed_up", backed_up),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be boolean")


def _validate_challenge_inputs(
        *,
        challenge: bytes | str,
        kind: str,
        identity: str | None,
        origin: str,
        rp_id: str,
        proxy_id: str,
) -> tuple[bytes, str]:
    decoded = _decode_binary(challenge, "challenge", 1_024)
    if kind not in {"registration", "authentication"}:
        raise ValueError("challenge kind is unsupported")
    if kind == "registration" and not identity:
        raise ValueError("registration challenge requires identity")
    for field, value, limit in (
            ("origin", origin, 2_048),
            ("proxy_id", proxy_id, 255),
    ):
        if not isinstance(value, str) or not value or len(value) > limit:
            raise ValueError(f"{field} is invalid")
    try:
        normalized_rp_id = _validate_rp_id(rp_id)
    except ValueError as exc:
        raise ValueError("rp_id is invalid") from exc
    return decoded, normalized_rp_id


def _truncate(value: object, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sanitize_profile(user_data: Mapping[str, Any]) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for field, limit in _PROFILE_STRING_LIMITS.items():
        value = _truncate(user_data.get(field), limit)
        if value is not None:
            profile[field] = value
    for field in _PROFILE_BOOL_FIELDS:
        value = user_data.get(field)
        if isinstance(value, bool):
            profile[field] = value
    for field in _PROFILE_INTEGER_FIELDS:
        value = user_data.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            profile[field] = value
    return profile


class SQLiteAuthStore:
    """Thread-safe SQLite account and session adapter."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        """Open the database, apply idempotent schema, and clean stale rows."""
        raw_path = str(path)
        self.path = (
            raw_path
            if raw_path == ":memory:"
            else str(Path(raw_path).expanduser().resolve())
        )
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            journal_mode = os.environ.get("AUTH_SQLITE_JOURNAL_MODE", "WAL").upper()
            if journal_mode not in {"WAL", "DELETE"}:
                raise ValueError("AUTH_SQLITE_JOURNAL_MODE must be WAL or DELETE")
            self._connection.execute(f"PRAGMA journal_mode = {journal_mode}")
        self._migrate()
        self.cleanup_expired_sessions(limit=DEFAULT_CLEANUP_LIMIT)
        self.cleanup_challenges(limit=DEFAULT_CLEANUP_LIMIT)

    def _migrate(self) -> None:
        schema = """
                 CREATE TABLE IF NOT EXISTS auth_accounts
                 (
                     identity
                     TEXT
                     PRIMARY
                     KEY
                     CHECK (
                     length
                 (
                     identity
                 ) BETWEEN 3 AND 512),
                     provider TEXT NOT NULL CHECK
                 (
                     length
                 (
                     provider
                 ) BETWEEN 1 AND 32),
                     provider_subject TEXT NOT NULL CHECK
                 (
                     length
                 (
                     provider_subject
                 ) BETWEEN 1 AND 255),
                     email TEXT CHECK
                 (
                     email
                     IS
                     NULL
                     OR
                     length
                 (
                     email
                 ) <= 320),
                     name TEXT CHECK
                 (
                     name
                     IS
                     NULL
                     OR
                     length
                 (
                     name
                 ) <= 200),
                     avatar_url TEXT CHECK
                 (
                     avatar_url
                     IS
                     NULL
                     OR
                     length
                 (
                     avatar_url
                 ) <= 2048),
                     profile_json TEXT NOT NULL DEFAULT '{}',
                     webauthn_user_handle TEXT NOT NULL UNIQUE CHECK
                 (
                     length
                 (
                     webauthn_user_handle
                 ) BETWEEN 1 AND 86),
                     created_at REAL NOT NULL,
                     updated_at REAL NOT NULL,
                     UNIQUE
                 (
                     provider,
                     provider_subject
                 )
                     );

                 CREATE TABLE IF NOT EXISTS auth_sessions
                 (
                     token_hash
                     TEXT
                     PRIMARY
                     KEY
                     CHECK (
                     length
                 (
                     token_hash
                 ) = 64),
                     identity TEXT NOT NULL REFERENCES auth_accounts
                 (
                     identity
                 ) ON DELETE CASCADE,
                     created_at REAL NOT NULL,
                     authenticated_at REAL NOT NULL,
                     expires_at REAL NOT NULL,
                     auth_method TEXT NOT NULL CHECK
                 (
                     auth_method
                     IN
                 (
                     'oauth',
                     'passkey'
                 ))
                     );

                 CREATE TABLE IF NOT EXISTS auth_credentials
                 (
                     credential_id
                     TEXT
                     PRIMARY
                     KEY
                     CHECK (
                     length
                 (
                     credential_id
                 ) BETWEEN 1 AND 2048),
                     identity TEXT NOT NULL REFERENCES auth_accounts
                 (
                     identity
                 ) ON DELETE CASCADE,
                     rp_id TEXT,
                     public_key BLOB NOT NULL CHECK
                 (
                     length
                 (
                     public_key
                 ) BETWEEN 1 AND 16384),
                     sign_count INTEGER NOT NULL CHECK
                 (
                     sign_count
                     >=
                     0
                 ),
                     transports_json TEXT NOT NULL,
                     device_type TEXT NOT NULL CHECK
                 (
                     device_type
                     IN
                 (
                     'single_device',
                     'multi_device'
                 )),
                     backed_up INTEGER NOT NULL CHECK
                 (
                     backed_up
                     IN
                 (
                     0,
                     1
                 )),
                     label TEXT CHECK
                 (
                     label
                     IS
                     NULL
                     OR
                     length
                 (
                     label
                 ) <= 100),
                     created_at REAL NOT NULL,
                     last_used_at REAL
                     );

                 CREATE TABLE IF NOT EXISTS auth_challenges
                 (
                     ceremony_id
                     TEXT
                     PRIMARY
                     KEY
                     CHECK (
                     length
                 (
                     ceremony_id
                 ) BETWEEN 1 AND 128),
                     challenge BLOB NOT NULL CHECK
                 (
                     length
                 (
                     challenge
                 ) BETWEEN 1 AND 1024),
                     kind TEXT NOT NULL CHECK
                 (
                     kind
                     IN
                 (
                     'registration',
                     'authentication'
                 )),
                     identity TEXT REFERENCES auth_accounts
                 (
                     identity
                 ) ON DELETE CASCADE,
                     origin TEXT NOT NULL CHECK
                 (
                     length
                 (
                     origin
                 ) BETWEEN 1 AND 2048),
                     rp_id TEXT NOT NULL CHECK
                 (
                     length
                 (
                     rp_id
                 ) BETWEEN 1 AND 255),
                     proxy_id TEXT NOT NULL CHECK
                 (
                     length
                 (
                     proxy_id
                 ) BETWEEN 1 AND 255),
                     created_at REAL NOT NULL,
                     expires_at REAL NOT NULL,
                     consumed_at REAL
                     );

                 CREATE TABLE IF NOT EXISTS auth_rate_limits
                 (
                     scope
                     TEXT
                     NOT
                     NULL
                     CHECK (
                     scope
                     IN
                 (
                     'account',
                     'proxy'
                 )),
                     rate_key TEXT NOT NULL CHECK
                 (
                     length
                 (
                     rate_key
                 ) BETWEEN 1 AND 512),
                     window_start INTEGER NOT NULL CHECK
                 (
                     window_start
                     >=
                     0
                 ),
                     count INTEGER NOT NULL CHECK
                 (
                     count
                     >=
                     1
                 ),
                     updated_at REAL NOT NULL,
                     PRIMARY KEY
                 (
                     scope,
                     rate_key,
                     window_start
                 )
                     );

                 CREATE INDEX IF NOT EXISTS idx_auth_rate_limits_window
                     ON auth_rate_limits(window_start);

                 CREATE INDEX IF NOT EXISTS idx_auth_sessions_identity
                     ON auth_sessions(identity);
                 CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at
                     ON auth_sessions(expires_at);
                 CREATE INDEX IF NOT EXISTS idx_auth_credentials_identity
                     ON auth_credentials(identity, created_at);
                 CREATE INDEX IF NOT EXISTS idx_auth_challenges_expires_at
                     ON auth_challenges(expires_at);
                 CREATE INDEX IF NOT EXISTS idx_auth_challenges_registration_identity
                     ON auth_challenges(identity, kind, consumed_at, expires_at); \
                 """
        with self._lock:
            self._connection.executescript(schema)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    row[1]
                    for row in self._connection.execute(
                        "PRAGMA table_info(auth_credentials)"
                    ).fetchall()
                }
                if "rp_id" not in columns:
                    self._connection.execute(
                        "ALTER TABLE auth_credentials ADD COLUMN rp_id TEXT"
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _identity_parts(
            user_data: Mapping[str, Any], provider: str
    ) -> tuple[str, str, str]:
        normalized_provider = str(provider).strip().lower()
        identity = str(user_data.get("identity") or "")
        expected_prefix = f"{normalized_provider}:"
        if (
                not normalized_provider
                or len(normalized_provider) > 32
                or not identity.startswith(expected_prefix)
        ):
            raise ValueError("OAuth provider identity is missing or inconsistent")
        subject = identity[len(expected_prefix):]
        invalid_subject = (
                not subject
                or len(subject) > 255
                or len(identity) > 512
                or subject != subject.strip()
                or subject.lower() in {"none", "null"}
                or not subject.isascii()
                or any(
            ord(character) < 0x21 or ord(character) > 0x7E for character in subject
        )
        )
        if normalized_provider == "github":
            invalid_subject = (
                    invalid_subject or not subject.isdigit() or int(subject) <= 0
            )
        if invalid_subject:
            raise ValueError("OAuth provider identity is missing or inconsistent")
        return identity, normalized_provider, subject

    def _upsert_account(
            self,
            user_data: Mapping[str, Any],
            provider: str,
            now: float,
    ) -> str:
        identity, normalized_provider, subject = self._identity_parts(
            user_data, provider
        )
        email = _truncate(user_data.get("email"), 320)
        name = _truncate(user_data.get("name"), 200)
        avatar_url = _truncate(
            user_data.get("picture") or user_data.get("avatar_url"),
            2_048,
        )
        profile_json = json.dumps(
            _sanitize_profile(user_data),
            separators=(",", ":"),
            sort_keys=True,
        )
        stored = self._connection.execute(
            "SELECT provider, provider_subject FROM auth_accounts WHERE identity = ?",
            (identity,),
        ).fetchone()
        if stored is not None:
            if (
                    stored["provider"] != normalized_provider
                    or stored["provider_subject"] != subject
            ):
                raise ValueError(
                    "OAuth provider identity conflicts with an existing account"
                )
            self._connection.execute(
                """
                UPDATE auth_accounts
                SET email        = ?,
                    name         = ?,
                    avatar_url   = ?,
                    profile_json = ?,
                    updated_at   = ?
                WHERE identity = ?
                """,
                (email, name, avatar_url, profile_json, now, identity),
            )
            return identity

        for _ in range(5):
            handle = secrets.token_urlsafe(32)
            try:
                self._connection.execute(
                    """
                    INSERT INTO auth_accounts (identity, provider, provider_subject, email, name,
                                               avatar_url, profile_json, webauthn_user_handle,
                                               created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity,
                        normalized_provider,
                        subject,
                        email,
                        name,
                        avatar_url,
                        profile_json,
                        handle,
                        now,
                        now,
                    ),
                )
                return identity
            except sqlite3.IntegrityError as exc:
                handle_exists = self._connection.execute(
                    "SELECT 1 FROM auth_accounts WHERE webauthn_user_handle = ?",
                    (handle,),
                ).fetchone()
                if handle_exists is not None:
                    continue
                raise ValueError(
                    "OAuth provider identity conflicts with an existing account"
                ) from exc
        raise RuntimeError("Could not generate a globally unique WebAuthn user handle")

    def create_session(
            self,
            user_data: Mapping[str, Any],
            provider: str,
            auth_method: str = "oauth",
    ) -> str:
        """Upsert provider account and return a new raw 24-hour session token."""
        if auth_method not in {"oauth", "passkey"}:
            raise ValueError("auth_method must be 'oauth' or 'passkey'")
        now = time.time()
        self.cleanup_expired_sessions(limit=100)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                identity = self._upsert_account(user_data, provider, now)
                for _ in range(3):
                    token = secrets.token_urlsafe(32)
                    try:
                        self._connection.execute(
                            """
                            INSERT INTO auth_sessions (token_hash, identity, created_at, authenticated_at,
                                                       expires_at, auth_method)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                _token_hash(token),
                                identity,
                                now,
                                now,
                                now + SESSION_LIFETIME_SECONDS,
                                auth_method,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    self._connection.execute("COMMIT")
                    return token
                raise RuntimeError("Could not generate a unique session token")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def get_account(self, identity: str) -> AccountRecord | None:
        """Return sanitized account data without OAuth secrets."""
        if not isinstance(identity, str) or not identity:
            return None
        with self._lock:
            row = self._connection.execute(
                """SELECT identity, provider, email, name, avatar_url, profile_json, webauthn_user_handle
                   FROM auth_accounts
                   WHERE identity = ?""",
                (identity,),
            ).fetchone()
        if row is None:
            return None
        return AccountRecord(
            identity=row["identity"],
            provider=row["provider"],
            email=row["email"],
            name=row["name"],
            avatar_url=row["avatar_url"],
            profile=json.loads(row["profile_json"] or "{}"),
            webauthn_user_handle=_validate_base64url(
                row["webauthn_user_handle"],
                "webauthn_user_handle",
                86,
            ),
        )

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> dict[str, Any]:
        profile = json.loads(row["profile_json"] or "{}")
        user = {
            "identity": row["identity"],
            "email": row["email"],
            "name": row["name"],
            "provider": row["provider"],
            **profile,
        }
        if row["avatar_url"]:
            if row["provider"] == "google":
                user["picture"] = row["avatar_url"]
            else:
                user["avatar_url"] = row["avatar_url"]
        if row["auth_method"] != "oauth":
            user["auth_method"] = row["auth_method"]
        return user

    def _session_row(self, token_hash: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT s.token_hash,
                   s.created_at,
                   s.authenticated_at,
                   s.expires_at,
                   s.auth_method,
                   a.identity,
                   a.provider,
                   a.email,
                   a.name,
                   a.avatar_url,
                   a.profile_json
            FROM auth_sessions AS s
                     JOIN auth_accounts AS a ON a.identity = s.identity
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()

    def validate_session(self, session_token: str) -> dict[str, Any] | None:
        """Return sanitized user data for a valid session, sliding near expiry."""
        if not isinstance(session_token, str) or not session_token:
            return None
        digest = _token_hash(session_token)
        with self._lock:
            row = self._session_row(digest)
            if row is None:
                return None
            now = time.time()
            needs_write = (
                    now >= row["expires_at"]
                    or row["expires_at"] - now < SESSION_REFRESH_THRESHOLD_SECONDS
            )
            if not needs_write:
                return self._user_from_row(row)

        return self._validate_session_with_write(digest)

    def _validate_session_with_write(
            self,
            digest: str,
    ) -> dict[str, Any] | None:
        """Recheck and apply expiry or sliding-window state atomically."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._session_row(digest)
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                now = time.time()
                if now >= row["expires_at"]:
                    self._connection.execute(
                        "DELETE FROM auth_sessions WHERE token_hash = ?",
                        (digest,),
                    )
                    self._connection.execute("COMMIT")
                    return None
                if row["expires_at"] - now < SESSION_REFRESH_THRESHOLD_SECONDS:
                    self._connection.execute(
                        "UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
                        (now + SESSION_LIFETIME_SECONDS, digest),
                    )
                self._connection.execute("COMMIT")
                return self._user_from_row(row)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def refresh_session(self, session_token: str) -> dict[str, Any] | None:
        """Extend a live session by 24 hours without changing authentication time."""
        if not isinstance(session_token, str) or not session_token:
            return None
        digest = _token_hash(session_token)
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._session_row(digest)
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                if now >= row["expires_at"]:
                    self._connection.execute(
                        "DELETE FROM auth_sessions WHERE token_hash = ?",
                        (digest,),
                    )
                    self._connection.execute("COMMIT")
                    return None
                self._connection.execute(
                    "UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
                    (now + SESSION_LIFETIME_SECONDS, digest),
                )
                self._connection.execute("COMMIT")
                return self._user_from_row(row)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def remove_session(self, session_token: str) -> str | None:
        """Delete a session by hash and return its owning identity."""
        if not isinstance(session_token, str) or not session_token:
            return None
        digest = _token_hash(session_token)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT identity FROM auth_sessions WHERE token_hash = ?",
                    (digest,),
                ).fetchone()
                if row is not None:
                    self._connection.execute(
                        "DELETE FROM auth_sessions WHERE token_hash = ?",
                        (digest,),
                    )
                self._connection.execute("COMMIT")
                return row["identity"] if row is not None else None
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def cleanup_expired_sessions(self, limit: int = DEFAULT_CLEANUP_LIMIT) -> int:
        """Delete at most ``limit`` expired sessions and return deleted count."""
        bounded_limit = max(0, min(int(limit), MAX_CLEANUP_LIMIT))
        if bounded_limit == 0:
            return 0
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    """
                    DELETE
                    FROM auth_sessions
                    WHERE token_hash IN (SELECT token_hash
                                         FROM auth_sessions
                                         WHERE expires_at <= ?
                                         ORDER BY expires_at
                        LIMIT ?
                        )
                    """,
                    (now, bounded_limit),
                )
                self._connection.execute("COMMIT")
                return max(cursor.rowcount, 0)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def get_session_detail(self, session_token: str) -> SessionDetail | None:
        """Return non-secret session metadata when token is live."""
        if not isinstance(session_token, str) or not session_token:
            return None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT s.identity,
                       a.provider,
                       s.auth_method,
                       s.authenticated_at,
                       s.expires_at
                FROM auth_sessions AS s
                         JOIN auth_accounts AS a ON a.identity = s.identity
                WHERE s.token_hash = ?
                """,
                (_token_hash(session_token),),
            ).fetchone()
        if row is None or time.time() >= row["expires_at"]:
            return None
        return SessionDetail(
            identity=row["identity"],
            provider=row["provider"],
            auth_method=row["auth_method"],
            authenticated_at=row["authenticated_at"],
            expires_at=row["expires_at"],
        )

    @staticmethod
    def _credential_from_row(row: sqlite3.Row) -> CredentialRecord:
        return CredentialRecord(
            credential_id=row["credential_id"],
            identity=row["identity"],
            public_key=bytes(row["public_key"]),
            sign_count=row["sign_count"],
            transports=tuple(json.loads(row["transports_json"])),
            device_type=row["device_type"],
            backed_up=bool(row["backed_up"]),
            label=row["label"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            rp_id=row["rp_id"],
        )

    def list_credentials(
            self, identity: str, rp_id: str | None = None
    ) -> list[CredentialRecord]:
        """List an account's credentials in stable creation order."""
        if rp_id is not None:
            rp_id = _validate_rp_id(rp_id)
        where = "identity = ?" if rp_id is None else "identity = ? AND rp_id = ?"
        parameters = (identity,) if rp_id is None else (identity, rp_id)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT *
                FROM auth_credentials
                WHERE {where}
                ORDER BY created_at, credential_id
                """,
                parameters,
            ).fetchall()
        return [self._credential_from_row(row) for row in rows]

    def get_credential(self, credential_id: str) -> CredentialRecord | None:
        """Point-read a globally unique credential ID."""
        if not isinstance(credential_id, str) or not credential_id:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM auth_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        return self._credential_from_row(row) if row is not None else None

    def create_credential(
            self,
            *,
            identity: str,
            rp_id: str,
            credential_id: str,
            public_key: bytes | str,
            sign_count: int,
            transports: Sequence[str],
            device_type: str,
            backed_up: bool,
            label: str | None = None,
            created_at: float | None = None,
            last_used_at: float | None = None,
    ) -> CredentialRecord:
        """Create one credential while transactionally enforcing account cap."""
        credential_id = _validate_base64url(credential_id, "credential_id")
        rp_id = _validate_rp_id(rp_id)
        decoded_key = _decode_binary(public_key, "public_key", 16_384)
        normalized_transports, normalized_label = _validate_credential_inputs(
            sign_count=sign_count,
            transports=transports,
            device_type=device_type,
            backed_up=backed_up,
            label=label,
        )
        created = time.time() if created_at is None else float(created_at)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                account = self._connection.execute(
                    "SELECT 1 FROM auth_accounts WHERE identity = ?",
                    (identity,),
                ).fetchone()
                if account is None:
                    raise ValueError("credential account does not exist")
                count = self._connection.execute(
                    "SELECT COUNT(*) FROM auth_credentials WHERE identity = ?",
                    (identity,),
                ).fetchone()[0]
                if count >= MAX_CREDENTIALS_PER_ACCOUNT:
                    raise CredentialLimitError("account already has 10 credentials")
                try:
                    self._connection.execute(
                        """
                        INSERT INTO auth_credentials (credential_id, identity, rp_id, public_key, sign_count,
                                                      transports_json, device_type, backed_up, label,
                                                      created_at, last_used_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            credential_id,
                            identity,
                            rp_id,
                            decoded_key,
                            sign_count,
                            json.dumps(normalized_transports, separators=(",", ":")),
                            device_type,
                            int(backed_up),
                            normalized_label,
                            created,
                            last_used_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateCredentialError(
                        "credential ID already exists"
                    ) from exc
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        credential = self.get_credential(credential_id)
        if credential is None:
            raise AuthStoreError("credential insert did not persist")
        return credential

    def bind_credential_rp_id(self, credential_id: str, rp_id: str) -> bool:
        """Bind an unscoped legacy credential to exactly one RP ID."""
        rp_id = _validate_rp_id(rp_id)
        if not isinstance(credential_id, str) or not credential_id:
            return False
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """UPDATE auth_credentials
                       SET rp_id = ?
                       WHERE credential_id = ?
                         AND (rp_id IS NULL OR rp_id = '')""",
                    (rp_id, credential_id),
                )
                row = self._connection.execute(
                    "SELECT rp_id FROM auth_credentials WHERE credential_id = ?",
                    (credential_id,),
                ).fetchone()
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return row is not None and row["rp_id"] == rp_id

    def update_credential_state(
            self,
            credential_id: str,
            *,
            expected_sign_count: int,
            expected_backed_up: bool,
            new_sign_count: int,
            backed_up: bool,
            last_used_at: float | None = None,
    ) -> bool:
        """CAS credential counter, backup state, and last-use timestamp."""
        _validate_counter_update(
            expected_sign_count=expected_sign_count,
            expected_backed_up=expected_backed_up,
            new_sign_count=new_sign_count,
            backed_up=backed_up,
        )
        used_at = time.time() if last_used_at is None else float(last_used_at)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    """
                    UPDATE auth_credentials
                    SET sign_count   = ?,
                        backed_up    = ?,
                        last_used_at = ?
                    WHERE credential_id = ?
                      AND sign_count = ?
                      AND backed_up = ?
                    """,
                    (
                        new_sign_count,
                        int(backed_up),
                        used_at,
                        credential_id,
                        expected_sign_count,
                        int(expected_backed_up),
                    ),
                )
                self._connection.execute("COMMIT")
                return cursor.rowcount == 1
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def rename_credential(
            self,
            identity: str,
            credential_id: str,
            label: str,
    ) -> bool:
        """Rename an owned credential with a bounded label."""
        normalized_label = _validate_label(label)
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE auth_credentials
                SET label = ?
                WHERE identity = ? AND credential_id = ?
                """,
                (normalized_label, identity, credential_id),
            )
        return cursor.rowcount == 1

    def delete_credential(self, identity: str, credential_id: str) -> bool:
        """Delete an owned credential."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "DELETE FROM auth_credentials WHERE identity = ? AND credential_id = ?",
                    (identity, credential_id),
                )
                self._connection.execute("COMMIT")
                return cursor.rowcount == 1
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _challenge_from_row(
            row: sqlite3.Row,
            consumed_at: float | None = None,
    ) -> ChallengeRecord:
        return ChallengeRecord(
            ceremony_id=row["ceremony_id"],
            challenge=bytes(row["challenge"]),
            kind=row["kind"],
            identity=row["identity"],
            origin=row["origin"],
            rp_id=row["rp_id"],
            proxy_id=row["proxy_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            consumed_at=(row["consumed_at"] if consumed_at is None else consumed_at),
        )

    def create_challenge(
            self,
            *,
            challenge: bytes | str,
            kind: str,
            identity: str | None,
            origin: str,
            rp_id: str,
            proxy_id: str,
            expires_at: float,
            created_at: float | None = None,
    ) -> ChallengeRecord:
        """Persist a random-ID one-time ceremony challenge."""
        decoded_challenge, rp_id = _validate_challenge_inputs(
            challenge=challenge,
            kind=kind,
            identity=identity,
            origin=origin,
            rp_id=rp_id,
            proxy_id=proxy_id,
        )
        created = time.time() if created_at is None else float(created_at)
        self.cleanup_challenges(limit=100)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if identity is not None:
                    account = self._connection.execute(
                        "SELECT 1 FROM auth_accounts WHERE identity = ?",
                        (identity,),
                    ).fetchone()
                    if account is None:
                        raise ValueError("challenge account does not exist")
                if kind == "registration":
                    count = self._connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM auth_challenges
                        WHERE identity = ?
                          AND kind = 'registration'
                          AND consumed_at IS NULL
                          AND expires_at
                            > ?
                        """,
                        (identity, created),
                    ).fetchone()[0]
                    if count >= MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT:
                        raise ChallengeLimitError(
                            "account already has 3 registration challenges"
                        )
                for _ in range(3):
                    ceremony_id = secrets.token_urlsafe(32)
                    try:
                        self._connection.execute(
                            """
                            INSERT INTO auth_challenges (ceremony_id, challenge, kind, identity, origin,
                                                         rp_id, proxy_id, created_at, expires_at, consumed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                            """,
                            (
                                ceremony_id,
                                decoded_challenge,
                                kind,
                                identity,
                                origin,
                                rp_id,
                                proxy_id,
                                created,
                                float(expires_at),
                            ),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    row = self._connection.execute(
                        "SELECT * FROM auth_challenges WHERE ceremony_id = ?",
                        (ceremony_id,),
                    ).fetchone()
                    self._connection.execute("COMMIT")
                    return self._challenge_from_row(row)
                raise AuthStoreError("could not generate a unique ceremony ID")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def claim_challenge(self, ceremony_id: str) -> ChallengeRecord | None:
        """Atomically claim once and return the original stored record."""
        if not isinstance(ceremony_id, str) or not ceremony_id:
            return None
        consumed = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM auth_challenges WHERE ceremony_id = ?",
                    (ceremony_id,),
                ).fetchone()
                if row is None or row["consumed_at"] is not None:
                    self._connection.execute("COMMIT")
                    return None
                cursor = self._connection.execute(
                    """
                    UPDATE auth_challenges
                    SET consumed_at = ?
                    WHERE ceremony_id = ?
                      AND consumed_at IS NULL
                    """,
                    (consumed, ceremony_id),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return self._challenge_from_row(row, consumed) if cursor.rowcount == 1 else None

    def cleanup_challenges(self, limit: int = DEFAULT_CLEANUP_LIMIT) -> int:
        """Delete a bounded batch of expired challenge audit rows."""
        bounded_limit = max(0, min(int(limit), MAX_CLEANUP_LIMIT))
        if bounded_limit == 0:
            return 0
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    """
                    DELETE
                    FROM auth_challenges
                    WHERE ceremony_id IN (SELECT ceremony_id
                                          FROM auth_challenges
                                          WHERE expires_at <= ?
                                          ORDER BY expires_at
                        LIMIT ?
                        )
                    """,
                    (time.time(), bounded_limit),
                )
                self._connection.execute("COMMIT")
                return max(cursor.rowcount, 0)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def consume_rate_limit(
            self, scope: str, key: str, window_start: int, limit: int
    ) -> bool:
        """Atomically consume one durable fixed-window operation budget."""
        scope, key, window_start, limit = _validate_rate_limit_inputs(
            scope, key, window_start, limit
        )
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    DELETE
                    FROM auth_rate_limits
                    WHERE rowid IN (SELECT rowid
                                    FROM auth_rate_limits
                                    WHERE window_start < ?
                                    ORDER BY window_start
                        LIMIT 100
                        )
                    """,
                    (max(0, window_start - 2),),
                )
                cursor = self._connection.execute(
                    """
                    INSERT INTO auth_rate_limits (scope, rate_key, window_start, count, updated_at)
                    VALUES (?, ?, ?, 1, ?) ON CONFLICT(scope, rate_key, window_start) DO
                    UPDATE SET
                        count = auth_rate_limits.count + 1,
                        updated_at = excluded.updated_at
                    WHERE auth_rate_limits.count < ?
                    """,
                    (scope, key, window_start, now, limit),
                )
                self._connection.execute("COMMIT")
                return cursor.rowcount == 1
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def close(self) -> None:
        """Close this store's SQLite connection."""
        with self._lock:
            self._connection.close()


def __getattr__(name: str) -> Any:
    if name == "PostgresAuthStore":
        from webapp.auth_store_postgres import PostgresAuthStore

        return PostgresAuthStore
    if name == "CosmosAuthStore":
        from webapp.auth_store_cosmos import CosmosAuthStore

        return CosmosAuthStore
    raise AttributeError(name)


def create_auth_store(
        backend: str | None = None,
        *,
        sqlite_path: str | os.PathLike[str] | None = None,
        postgres_pool: Any = None,
        cosmos_containers: Mapping[str, Any] | None = None,
        migrate: bool = True,
) -> AuthStore:
    """Create configured auth store without backend branching at call sites."""
    selected = (
            backend
            or os.environ.get("AUTH_STORE_TYPE")
            or os.environ.get("DB_TYPE", "sqlite")
    ).lower()
    if selected == "sqlite":
        return SQLiteAuthStore(
            sqlite_path or os.environ.get("SQLITE_DB_PATH", ":memory:")
        )
    if selected in {"postgres", "postgresql"}:
        pool = postgres_pool
        owns_pool = pool is None
        if pool is None:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL auth store requires psycopg_pool"
                ) from exc
            conninfo = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
            if not conninfo:
                required = ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_DB")
                if not all(os.environ.get(name) for name in required):
                    raise ValueError("PostgreSQL auth store configuration is missing")
                conninfo = "host={host} port={port} user={user} password={password} dbname={dbname}".format(
                    host=os.environ["POSTGRES_HOST"],
                    port=os.environ.get("POSTGRES_PORT", "5432"),
                    user=os.environ["POSTGRES_USER"],
                    password=os.environ.get("POSTGRES_PASSWORD", ""),
                    dbname=os.environ["POSTGRES_DB"],
                )
            pool = ConnectionPool(conninfo=conninfo, min_size=1, max_size=10, open=True)
        from webapp.auth_store_postgres import PostgresAuthStore

        return PostgresAuthStore(
            pool=pool,
            migrate=migrate,
            owns_pool=owns_pool,
        )
    if selected in {"cosmos", "cosmosdb"}:
        containers = cosmos_containers
        owner = None
        if containers is None:
            connection_string = os.environ.get("COSMOS_CONNECTION_STRING")
            endpoint = os.environ.get("COSMOSDB_ENDPOINT")
            key = os.environ.get("COSMOSDB_KEY")
            if not connection_string and not (endpoint and key):
                raise ValueError("Cosmos auth store configuration is missing")
            try:
                from azure.cosmos import CosmosClient, PartitionKey
            except ImportError as exc:
                raise RuntimeError("Cosmos auth store requires azure-cosmos") from exc
            if connection_string:
                client = CosmosClient.from_connection_string(connection_string)
            else:
                client = CosmosClient(endpoint, credential=key)
            owner = client
            database = client.create_database_if_not_exists(
                id=os.environ.get("COSMOSDB_DB_NAME", "deep_research")
            )
            containers = {}
            for name in (
                    "accounts",
                    "sessions",
                    "credentials",
                    "challenges",
                    "rate_limits",
            ):
                options = {
                    "id": f"auth_{name}",
                    "partition_key": PartitionKey(path="/pk"),
                }
                if name == "accounts":
                    options["unique_key_policy"] = {
                        "uniqueKeys": [{"paths": ["/webauthn_user_handle"]}]
                    }
                if name == "rate_limits":
                    options["default_ttl"] = -1
                containers[name] = database.create_container_if_not_exists(**options)
        from webapp.auth_store_cosmos import CosmosAuthStore

        return CosmosAuthStore(containers=containers, owner=owner)
    raise ValueError(f"Unsupported auth store type: {selected}")
