"""PostgreSQL durable authentication adapter."""

# ruff: noqa: D102, D107

import json
import secrets
import time
from typing import Any, Mapping, Sequence

from webapp.auth_store import (
    DEFAULT_CLEANUP_LIMIT,
    MAX_CLEANUP_LIMIT,
    MAX_CREDENTIALS_PER_ACCOUNT,
    MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT,
    SESSION_LIFETIME_SECONDS,
    SESSION_REFRESH_THRESHOLD_SECONDS,
    AccountRecord,
    AuthStoreError,
    ChallengeLimitError,
    ChallengeRecord,
    CredentialLimitError,
    CredentialRecord,
    DuplicateCredentialError,
    SessionDetail,
    SQLiteAuthStore,
    _decode_binary,
    _sanitize_profile,
    _token_hash,
    _truncate,
    _validate_base64url,
    _validate_challenge_inputs,
    _validate_counter_update,
    _validate_credential_inputs,
    _validate_label,
    _validate_rate_limit_inputs,
    _validate_rp_id,
)


def _postgres_dict_row() -> Any:
    from psycopg.rows import dict_row

    return dict_row


class PostgresAuthStore:
    """PostgreSQL auth adapter using an injectable psycopg connection pool."""

    def __init__(
            self,
            *,
            pool: Any,
            migrate: bool = True,
            clock: Any = time.time,
            owns_pool: bool = False,
    ) -> None:
        self.pool = pool
        self._clock = clock
        self._owns_pool = owns_pool
        if migrate:
            self._migrate()

    def _migrate(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS auth_accounts
            (
                identity
                TEXT
                PRIMARY
                KEY,
                provider
                TEXT
                NOT
                NULL,
                provider_subject
                TEXT
                NOT
                NULL,
                email
                TEXT,
                name
                TEXT,
                avatar_url
                TEXT,
                profile_json
                JSONB
                NOT
                NULL
                DEFAULT
                '{}'
                :
                :
                jsonb,
                webauthn_user_handle
                TEXT
                NOT
                NULL
                UNIQUE,
                created_at
                DOUBLE
                PRECISION
                NOT
                NULL,
                updated_at
                DOUBLE
                PRECISION
                NOT
                NULL,
                UNIQUE
               (
                provider,
                provider_subject
               ))""",
            """CREATE TABLE IF NOT EXISTS auth_sessions
            (
                token_hash
                TEXT
                PRIMARY
                KEY,
                identity
                TEXT
                NOT
                NULL
                REFERENCES
                auth_accounts
               (
                identity
               ) ON DELETE CASCADE,
                created_at DOUBLE PRECISION NOT NULL, authenticated_at DOUBLE PRECISION NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL, auth_method TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS auth_credentials
            (
                credential_id
                TEXT
                PRIMARY
                KEY,
                identity
                TEXT
                NOT
                NULL
                REFERENCES
                auth_accounts
               (
                identity
               ) ON DELETE CASCADE,
                rp_id TEXT,
                public_key BYTEA NOT NULL, sign_count BIGINT NOT NULL,
                transports_json JSONB NOT NULL, device_type TEXT NOT NULL,
                backed_up BOOLEAN NOT NULL, label TEXT,
                created_at DOUBLE PRECISION NOT NULL, last_used_at DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS auth_challenges
            (
                ceremony_id
                TEXT
                PRIMARY
                KEY,
                challenge
                BYTEA
                NOT
                NULL,
                kind
                TEXT
                NOT
                NULL,
                identity
                TEXT
                REFERENCES
                auth_accounts
               (
                identity
               ) ON DELETE CASCADE,
                origin TEXT NOT NULL, rp_id TEXT NOT NULL, proxy_id TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
                consumed_at DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS auth_rate_limits
            (
                scope
                TEXT
                NOT
                NULL,
                rate_key
                TEXT
                NOT
                NULL,
                window_start
                BIGINT
                NOT
                NULL,
                count
                BIGINT
                NOT
                NULL,
                updated_at
                DOUBLE
                PRECISION
                NOT
                NULL,
                PRIMARY
                KEY
               (
                scope,
                rate_key,
                window_start
               ))""",
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at ON auth_sessions(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_auth_credentials_identity ON auth_credentials(identity, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_auth_challenges_expires_at ON auth_challenges(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_auth_challenges_registration_identity ON auth_challenges(identity, kind, consumed_at, expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_auth_rate_limits_window ON auth_rate_limits(window_start)",
            "ALTER TABLE auth_credentials ADD COLUMN IF NOT EXISTS rp_id TEXT",
        ]
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            for statement in statements:
                cursor.execute(statement)

    @staticmethod
    def _profile_row(row: Mapping[str, Any]) -> dict[str, Any]:
        profile = row.get("profile_json") or {}
        if isinstance(profile, str):
            profile = json.loads(profile)
        user = {
            "identity": row["identity"],
            "email": row.get("email"),
            "name": row.get("name"),
            "provider": row["provider"],
            **profile,
        }
        if row.get("avatar_url"):
            user["picture" if row["provider"] == "google" else "avatar_url"] = row[
                "avatar_url"
            ]
        if row.get("auth_method") not in {None, "oauth"}:
            user["auth_method"] = row["auth_method"]
        return user

    @staticmethod
    def _account_record(row: Mapping[str, Any]) -> AccountRecord:
        profile = row.get("profile_json") or {}
        if isinstance(profile, str):
            profile = json.loads(profile)
        return AccountRecord(
            identity=row["identity"],
            provider=row["provider"],
            email=row.get("email"),
            name=row.get("name"),
            avatar_url=row.get("avatar_url"),
            profile=dict(profile),
            webauthn_user_handle=_validate_base64url(
                row["webauthn_user_handle"],
                "webauthn_user_handle",
                86,
            ),
        )

    def get_account(self, identity: str) -> AccountRecord | None:
        if not isinstance(identity, str) or not identity:
            return None
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                """SELECT identity, provider, email, name, avatar_url, profile_json, webauthn_user_handle
                   FROM auth_accounts
                   WHERE identity =%s""",
                (identity,),
            )
            row = cursor.fetchone()
        return self._account_record(row) if row else None

    def _upsert_account(
            self, cursor: Any, user_data: Mapping[str, Any], provider: str, now: float
    ) -> str:
        identity, provider, subject = SQLiteAuthStore._identity_parts(
            user_data, provider
        )
        values = (
            identity,
            provider,
            subject,
            _truncate(user_data.get("email"), 320),
            _truncate(user_data.get("name"), 200),
            _truncate(user_data.get("picture") or user_data.get("avatar_url"), 2048),
            json.dumps(_sanitize_profile(user_data)),
            now,
            now,
        )
        for _ in range(5):
            handle = secrets.token_urlsafe(32)
            cursor.execute(
                """INSERT INTO auth_accounts
                   (identity, provider, provider_subject, email, name, avatar_url,
                    profile_json, webauthn_user_handle, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING identity""",
                values[:7] + (handle,) + values[7:],
            )
            if cursor.fetchone() is not None:
                return identity
            cursor.execute(
                "SELECT provider, provider_subject FROM auth_accounts WHERE identity = %s FOR UPDATE",
                (identity,),
            )
            existing = cursor.fetchone()
            if existing is None:
                continue
            if (
                    existing["provider"] != provider
                    or existing["provider_subject"] != subject
            ):
                raise ValueError(
                    "OAuth provider identity conflicts with an existing account"
                )
            cursor.execute(
                """UPDATE auth_accounts
                   SET email=%s,
                       name=%s,
                       avatar_url=%s,
                       profile_json=%s::jsonb, updated_at=%s
                   WHERE identity =%s""",
                (values[3], values[4], values[5], values[6], now, identity),
            )
            return identity
        raise AuthStoreError(
            "could not generate a globally unique WebAuthn user handle"
        )

    def create_session(
            self, user_data: Mapping[str, Any], provider: str, auth_method: str = "oauth"
    ) -> str:
        if auth_method not in {"oauth", "passkey"}:
            raise ValueError("auth_method must be 'oauth' or 'passkey'")
        now = self._clock()
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            identity = self._upsert_account(cursor, user_data, provider, now)
            for _ in range(3):
                token = secrets.token_urlsafe(32)
                cursor.execute(
                    """INSERT INTO auth_sessions
                       VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING token_hash""",
                    (
                        _token_hash(token),
                        identity,
                        now,
                        now,
                        now + SESSION_LIFETIME_SECONDS,
                        auth_method,
                    ),
                )
                if cursor.fetchone() is not None:
                    return token
        raise AuthStoreError("could not generate a unique session token")

    def _session(self, token: str, *, lock: bool = False) -> Mapping[str, Any] | None:
        suffix = " FOR UPDATE" if lock else ""
        statement = """SELECT s.*, a.provider, a.email, a.name, a.avatar_url, a.profile_json
                       FROM auth_sessions s
                                JOIN auth_accounts a ON a.identity = s.identity
                       WHERE s.token_hash = %s"""
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(statement + suffix, (_token_hash(token),))
            return cursor.fetchone()

    def validate_session(self, session_token: str) -> dict[str, Any] | None:
        if not session_token:
            return None
        row = self._session(session_token)
        if row is None or self._clock() >= row["expires_at"]:
            return None
        if row["expires_at"] - self._clock() < SESSION_REFRESH_THRESHOLD_SECONDS:
            return self.refresh_session(session_token)
        return self._profile_row(row)

    def refresh_session(self, session_token: str) -> dict[str, Any] | None:
        now = self._clock()
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                """UPDATE auth_sessions
                   SET expires_at=%s
                   WHERE token_hash = %s
                     AND expires_at > %s RETURNING identity, auth_method, authenticated_at, expires_at""",
                (now + SESSION_LIFETIME_SECONDS, _token_hash(session_token), now),
            )
            session = cursor.fetchone()
            if session is None:
                return None
            cursor.execute(
                "SELECT identity,provider,email,name,avatar_url,profile_json FROM auth_accounts WHERE identity=%s",
                (session["identity"],),
            )
            account = dict(cursor.fetchone())
            account["auth_method"] = session["auth_method"]
            return self._profile_row(account)

    def remove_session(self, session_token: str) -> str | None:
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                "DELETE FROM auth_sessions WHERE token_hash=%s RETURNING identity",
                (_token_hash(session_token),),
            )
            row = cursor.fetchone()
            return row["identity"] if row else None

    def cleanup_expired_sessions(self, limit: int = DEFAULT_CLEANUP_LIMIT) -> int:
        limit = max(0, min(int(limit), MAX_CLEANUP_LIMIT))
        if limit == 0:
            return 0
        now = self._clock()
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                """WITH doomed AS (SELECT token_hash FROM auth_sessions WHERE expires_at <= %s LIMIT %s)
                DELETE
                FROM auth_sessions AS target
                WHERE token_hash IN (SELECT token_hash FROM doomed)
                  AND target.expires_at <= %s RETURNING token_hash""",
                (now, limit, now),
            )
            return len(cursor.fetchall())

    def get_session_detail(self, session_token: str) -> SessionDetail | None:
        row = self._session(session_token)
        if row is None or self._clock() >= row["expires_at"]:
            return None
        return SessionDetail(
            row["identity"],
            row["provider"],
            row["auth_method"],
            row["authenticated_at"],
            row["expires_at"],
        )

    @staticmethod
    def _credential(row: Mapping[str, Any]) -> CredentialRecord:
        transports = row["transports_json"]
        if isinstance(transports, str):
            transports = json.loads(transports)
        return CredentialRecord(
            row["credential_id"],
            row["identity"],
            bytes(row["public_key"]),
            row["sign_count"],
            tuple(transports),
            row["device_type"],
            bool(row["backed_up"]),
            row.get("label"),
            row["created_at"],
            row.get("last_used_at"),
            row.get("rp_id"),
        )

    def list_credentials(
            self, identity: str, rp_id: str | None = None
    ) -> list[CredentialRecord]:
        if rp_id is not None:
            rp_id = _validate_rp_id(rp_id)
        where = "identity=%s" if rp_id is None else "identity=%s AND rp_id=%s"
        parameters = (identity,) if rp_id is None else (identity, rp_id)
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                f"SELECT * FROM auth_credentials WHERE {where} ORDER BY created_at,credential_id",
                parameters,
            )
            return [self._credential(row) for row in cursor.fetchall()]

    def get_credential(self, credential_id: str) -> CredentialRecord | None:
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                "SELECT * FROM auth_credentials WHERE credential_id=%s",
                (credential_id,),
            )
            row = cursor.fetchone()
            return self._credential(row) if row else None

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
        credential_id = _validate_base64url(credential_id, "credential_id")
        rp_id = _validate_rp_id(rp_id)
        key = _decode_binary(public_key, "public_key", 16384)
        normalized, label = _validate_credential_inputs(
            sign_count=sign_count,
            transports=transports,
            device_type=device_type,
            backed_up=backed_up,
            label=label,
        )
        created_at = self._clock() if created_at is None else float(created_at)
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                "SELECT identity FROM auth_accounts WHERE identity=%s FOR UPDATE",
                (identity,),
            )
            if cursor.fetchone() is None:
                raise ValueError("credential account does not exist")
            cursor.execute(
                "SELECT COUNT(*) AS count FROM auth_credentials WHERE identity=%s",
                (identity,),
            )
            if cursor.fetchone()["count"] >= MAX_CREDENTIALS_PER_ACCOUNT:
                raise CredentialLimitError("account already has 10 credentials")
            cursor.execute(
                """INSERT INTO auth_credentials
                   (credential_id, identity, rp_id, public_key, sign_count,
                    transports_json, device_type, backed_up, label, created_at, last_used_at)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING *""",
                (
                    credential_id,
                    identity,
                    rp_id,
                    key,
                    sign_count,
                    json.dumps(normalized),
                    device_type,
                    backed_up,
                    label,
                    created_at,
                    last_used_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise DuplicateCredentialError("credential ID already exists")
            return self._credential(row)

    def bind_credential_rp_id(self, credential_id: str, rp_id: str) -> bool:
        rp_id = _validate_rp_id(rp_id)
        if not isinstance(credential_id, str) or not credential_id:
            return False
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                """UPDATE auth_credentials
                   SET rp_id=%s
                   WHERE credential_id = %s
                     AND (rp_id IS NULL OR rp_id = '') RETURNING credential_id""",
                (rp_id, credential_id),
            )
            cursor.fetchone()
            cursor.execute(
                "SELECT rp_id FROM auth_credentials WHERE credential_id=%s",
                (credential_id,),
            )
            row = cursor.fetchone()
            return row is not None and row.get("rp_id") == rp_id

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
        _validate_counter_update(
            expected_sign_count=expected_sign_count,
            expected_backed_up=expected_backed_up,
            new_sign_count=new_sign_count,
            backed_up=backed_up,
        )
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                """UPDATE auth_credentials
                   SET sign_count   = %s,
                       backed_up    = %s,
                       last_used_at = %s
                   WHERE credential_id = %s
                     AND sign_count = %s
                     AND backed_up = %s RETURNING credential_id""",
                (
                    new_sign_count,
                    backed_up,
                    self._clock() if last_used_at is None else last_used_at,
                    credential_id,
                    expected_sign_count,
                    expected_backed_up,
                ),
            )
            return cursor.fetchone() is not None

    def rename_credential(self, identity: str, credential_id: str, label: str) -> bool:
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                "UPDATE auth_credentials SET label=%s WHERE identity=%s AND credential_id=%s RETURNING credential_id",
                (_validate_label(label), identity, credential_id),
            )
            return cursor.fetchone() is not None

    def delete_credential(self, identity: str, credential_id: str) -> bool:
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                "DELETE FROM auth_credentials WHERE identity=%s AND credential_id=%s RETURNING credential_id",
                (identity, credential_id),
            )
            return cursor.fetchone() is not None

    @staticmethod
    def _challenge(
            row: Mapping[str, Any], consumed_at: float | None = None
    ) -> ChallengeRecord:
        return ChallengeRecord(
            row["ceremony_id"],
            bytes(row["challenge"]),
            row["kind"],
            row.get("identity"),
            row["origin"],
            row["rp_id"],
            row["proxy_id"],
            row["created_at"],
            row["expires_at"],
            row.get("consumed_at") if consumed_at is None else consumed_at,
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
        data, rp_id = _validate_challenge_inputs(
            challenge=challenge,
            kind=kind,
            identity=identity,
            origin=origin,
            rp_id=rp_id,
            proxy_id=proxy_id,
        )
        created_at = self._clock() if created_at is None else created_at
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            if identity:
                cursor.execute(
                    "SELECT identity FROM auth_accounts WHERE identity=%s FOR UPDATE",
                    (identity,),
                )
                if cursor.fetchone() is None:
                    raise ValueError("challenge account does not exist")
            if kind == "registration":
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM auth_challenges WHERE identity=%s AND kind='registration' AND consumed_at IS NULL AND expires_at>%s",
                    (identity, created_at),
                )
                if (
                        cursor.fetchone()["count"]
                        >= MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT
                ):
                    raise ChallengeLimitError("too many registration challenges")
            for _ in range(3):
                ceremony_id = secrets.token_urlsafe(32)
                cursor.execute(
                    """INSERT INTO auth_challenges
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL) ON CONFLICT DO NOTHING RETURNING *""",
                    (
                        ceremony_id,
                        data,
                        kind,
                        identity,
                        origin,
                        rp_id,
                        proxy_id,
                        created_at,
                        expires_at,
                    ),
                )
                row = cursor.fetchone()
                if row:
                    return self._challenge(row)
        raise AuthStoreError("could not generate ceremony ID")

    def claim_challenge(self, ceremony_id: str) -> ChallengeRecord | None:
        if not isinstance(ceremony_id, str) or not ceremony_id:
            return None
        now = self._clock()
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                "SELECT * FROM auth_challenges WHERE ceremony_id=%s FOR UPDATE",
                (ceremony_id,),
            )
            row = cursor.fetchone()
            if not row or row.get("consumed_at") is not None:
                return None
            cursor.execute(
                "UPDATE auth_challenges SET consumed_at=%s WHERE ceremony_id=%s AND consumed_at IS NULL RETURNING ceremony_id",
                (now, ceremony_id),
            )
            if cursor.fetchone() is None:
                return None
        return self._challenge(row, now)

    def cleanup_challenges(self, limit: int = DEFAULT_CLEANUP_LIMIT) -> int:
        bounded = max(0, min(int(limit), MAX_CLEANUP_LIMIT))
        if bounded == 0:
            return 0
        now = self._clock()
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                """WITH doomed AS (SELECT ceremony_id FROM auth_challenges WHERE expires_at <= %s LIMIT %s)
                DELETE
                FROM auth_challenges AS target
                WHERE ceremony_id IN (SELECT ceremony_id FROM doomed)
                  AND target.expires_at <= %s RETURNING ceremony_id""",
                (now, bounded, now),
            )
            return len(cursor.fetchall())

    def consume_rate_limit(
            self, scope: str, key: str, window_start: int, limit: int
    ) -> bool:
        """Atomically consume one database-backed fixed-window budget."""
        scope, key, window_start, limit = _validate_rate_limit_inputs(
            scope, key, window_start, limit
        )
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=_postgres_dict_row()) as cursor,
        ):
            cursor.execute(
                """WITH doomed AS (SELECT scope, rate_key, window_start
                                   FROM auth_rate_limits
                                   WHERE window_start < %s
                       LIMIT 100
                       )
                DELETE
                FROM auth_rate_limits AS target USING doomed
                WHERE target.scope=doomed.scope
                  AND target.rate_key=doomed.rate_key
                  AND target.window_start=doomed.window_start""",
                (max(0, window_start - 2),),
            )
            cursor.execute(
                """INSERT INTO auth_rate_limits (scope, rate_key, window_start, count, updated_at)
                   VALUES (%s, %s, %s, 1, %s) ON CONFLICT(scope, rate_key, window_start) DO
                UPDATE SET
                    count =auth_rate_limits.count+1,
                    updated_at=EXCLUDED.updated_at
                WHERE auth_rate_limits.count
                    < %s
                    RETURNING count""",
                (scope, key, window_start, self._clock(), limit),
            )
            return cursor.fetchone() is not None

    def close(self) -> None:
        """Close only pools created and explicitly owned by this adapter."""
        if self._owns_pool:
            self.pool.close()
