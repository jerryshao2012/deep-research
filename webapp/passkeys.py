"""WebAuthn passkey configuration and trusted-BFF service helpers."""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import math
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import cbor2
from fastapi import Request
from fastapi.responses import JSONResponse
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from webapp.features.auth import AuthStore, ChallengeRecord, CredentialRecord
from webapp.webauthn_scope import (
    ends_in_numeric_label,
    normalize_dns_name,
    normalize_rp_id,
)


class PasskeyConfigurationError(RuntimeError):
    """Raised when enabled passkey configuration is unsafe or incomplete."""


class InvalidPasskeyError(ValueError):
    """Raised with a generic message for every invalid ceremony response."""


class InvalidPasskeySessionError(PermissionError):
    """Raised after a ceremony is consumed when bearer session is absent."""


class ReauthenticationRequired(PermissionError):
    """Raised when a sensitive operation needs recent provider authentication."""

    def __init__(self, provider: str):
        """Preserve only provider needed by OAuth reauthentication UI."""
        self.provider = provider
        super().__init__("Recent authentication is required")


class PasskeyRateLimitError(RuntimeError):
    """Raised when account or proxy fixed-window budget is exhausted."""


_MONTH_NAMES = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_REMOVABLE_TRANSPORTS = frozenset({"usb", "nfc", "ble", "smart-card"})
_ECMASCRIPT_TRIM_CHARS = "".join(
    chr(code_point)
    for code_point in (
        0x0009,
        0x000A,
        0x000B,
        0x000C,
        0x000D,
        0x0020,
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)


def _normalize_passkey_label(label: object, *, allow_default: bool) -> str | None:
    """Trim and validate a user-supplied label by Unicode code points."""
    if label is None:
        if allow_default:
            return None
        raise InvalidPasskeyError("Invalid passkey response")
    if not isinstance(label, str):
        raise InvalidPasskeyError("Invalid passkey response")
    if any(0xD800 <= ord(value) <= 0xDFFF for value in label):
        raise InvalidPasskeyError("Invalid passkey response")
    normalized = label.strip(_ECMASCRIPT_TRIM_CHARS)
    if not normalized:
        if allow_default:
            return None
        raise InvalidPasskeyError("Invalid passkey response")
    if len(normalized) > 100:
        raise InvalidPasskeyError("Invalid passkey response")
    return normalized


def _default_passkey_label(
        *, device_type: object, transports: object, created_at: object
) -> str:
    """Build a privacy-safe, locale-independent label from credential metadata."""
    try:
        timestamp = float(created_at)
        if not math.isfinite(timestamp):
            raise ValueError
        created = datetime.fromtimestamp(timestamp, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return "Passkey"

    transport_values = (
        frozenset(value for value in transports if isinstance(value, str))
        if not isinstance(transports, (str, bytes)) and transports is not None
        else frozenset()
    )
    if device_type == "multi_device":
        prefix = "Synced passkey"
    elif device_type == "single_device" and "internal" in transport_values:
        prefix = "Device passkey"
    elif transport_values & _REMOVABLE_TRANSPORTS:
        prefix = "Security key"
    else:
        prefix = "Passkey"
    date = f"{_MONTH_NAMES[created.month - 1]} {created.day}, {created.year}"
    return f"{prefix} · {date}"


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default))
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise PasskeyConfigurationError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise PasskeyConfigurationError(f"{name} must be a positive integer")
    return parsed


def _is_localhost(hostname: str | None) -> bool:
    normalized = hostname or ""
    if normalized.endswith(".") and not normalized.endswith(".."):
        normalized = normalized[:-1]
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalize_dns_name(value: str, setting_name: str) -> str:
    try:
        return normalize_dns_name(value, setting_name)
    except ValueError as exc:
        raise PasskeyConfigurationError(str(exc)) from exc


def _ends_in_numeric_label(hostname: str) -> bool:
    return ends_in_numeric_label(hostname)


def _normalize_rp_id(value: str, setting_name: str = "PASSKEY_RP_IDS") -> str:
    try:
        return normalize_rp_id(value, setting_name)
    except ValueError as exc:
        raise PasskeyConfigurationError(str(exc)) from exc


def _normalize_origin_hostname(
    hostname: str, setting_name: str = "PASSKEY_ORIGINS"
) -> tuple[str, str]:
    try:
        normalized = str(ipaddress.ip_address(hostname))
    except ValueError:
        trailing_dot = hostname.endswith(".")
        normalized = _normalize_dns_name(hostname, setting_name)
        if _ends_in_numeric_label(normalized):
            origin_label = (
                "Passkey origin" if setting_name == "PASSKEY_ORIGINS" else setting_name
            )
            raise PasskeyConfigurationError(
                f"{origin_label} contains a noncanonical IP address"
            )
        serialized = f"{normalized}." if trailing_dot else normalized
        return normalized, serialized
    return normalized, normalized


def _validate_origin(
    origin: str, setting_name: str = "PASSKEY_ORIGINS"
) -> tuple[str, str]:
    if len(origin) > 2_048:
        raise PasskeyConfigurationError(
            f"{setting_name} entries must contain at most 2048 characters"
        )
    parsed = urlsplit(origin)
    hostname = parsed.hostname
    configured_origin = f"{parsed.scheme}://{parsed.netloc}"
    if (
        not parsed.scheme
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or configured_origin.rstrip("/") != origin.rstrip("/")
    ):
        raise PasskeyConfigurationError(
            f"{setting_name} entries must be exact origins without credentials, paths, queries, or fragments"
        )
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and _is_localhost(hostname)
    ):
        raise PasskeyConfigurationError(
            f"{setting_name} must use HTTPS outside localhost"
        )
    origin_label = (
        "Passkey origin" if setting_name == "PASSKEY_ORIGINS" else setting_name
    )
    try:
        port = parsed.port
    except ValueError as exc:
        raise PasskeyConfigurationError(
            f"{origin_label} must use a valid port"
        ) from exc
    if parsed.netloc.endswith(":"):
        raise PasskeyConfigurationError(f"{origin_label} must use a valid port")
    normalized_hostname, serialized_hostname = _normalize_origin_hostname(
        hostname, setting_name
    )
    serialized_hostname = (
        f"[{normalized_hostname}]"
        if ":" in normalized_hostname
        else serialized_hostname
    )
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    port_suffix = f":{port}" if port is not None and not default_port else ""
    normalized_origin = f"{parsed.scheme}://{serialized_hostname}{port_suffix}"
    return normalized_origin, normalized_hostname


def _has_oauth(environ: Mapping[str, str]) -> bool:
    google = bool(
        environ.get("GOOGLE_CLIENT_ID") and environ.get("GOOGLE_CLIENT_SECRET")
    )
    github = bool(
        environ.get("GITHUB_CLIENT_ID") and environ.get("GITHUB_CLIENT_SECRET")
    )

    def domains(value: str | None) -> set[str]:
        return {
            item.split(":", 1)[0].strip()
            for item in (value or "").split(",")
            if ":" in item and all(part.strip() for part in item.split(":", 1))
        }

    github_multi = bool(
        domains(environ.get("GITHUB_CLIENT_IDS"))
        & domains(environ.get("GITHUB_CLIENT_SECRETS"))
    )
    return google or github or github_multi


def _has_durable_store(environ: Mapping[str, str]) -> bool:
    backend = (
            environ.get("AUTH_STORE_TYPE") or environ.get("DB_TYPE") or "sqlite"
    ).lower()
    if backend == "sqlite":
        path = (environ.get("SQLITE_DB_PATH") or "").strip()
        return bool(path and path != ":memory:")
    if backend in {"postgres", "postgresql"}:
        return bool(
            environ.get("DATABASE_URL")
            or environ.get("POSTGRES_URL")
            or all(
                environ.get(name)
                for name in ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_DB")
            )
        )
    if backend in {"cosmos", "cosmosdb"}:
        return bool(
            environ.get("COSMOS_CONNECTION_STRING")
            or (environ.get("COSMOSDB_ENDPOINT") and environ.get("COSMOSDB_KEY"))
        )
    return False


@dataclass(frozen=True)
class PasskeyConfig:
    """Validated WebAuthn relying-party and trusted-proxy settings."""

    enabled: bool
    rp_ids: tuple[str, ...] = ()
    origin_rp_ids: tuple[tuple[str, str], ...] = ()
    rp_name: str = "BMO Deep Agent"
    origins: tuple[str, ...] = ()
    proxy_id: str = ""
    proxy_secret: str = ""
    challenge_ttl_seconds: int = 300
    recent_auth_seconds: int = 600
    authenticated_rate_limit: int = 20
    anonymous_rate_limit: int = 300
    oauth_cookie_secure: bool = False

    @property
    def rp_id(self) -> str:
        """Return sole RP ID for backward-compatible single-RP ceremonies."""
        if len(self.rp_ids) != 1:
            raise PasskeyConfigurationError("A singular passkey RP ID is unavailable")
        return self.rp_ids[0]

    def rp_id_for_origin(self, origin: str) -> str:
        """Return RP ID mapped to exact configured origin."""
        for configured_origin, rp_id in self.origin_rp_ids:
            if origin == configured_origin:
                return rp_id
        raise InvalidPasskeyError("Invalid passkey response")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> PasskeyConfig:
        """Parse configuration and fail closed when passkeys are enabled."""
        values = os.environ if environ is None else environ
        enabled = _enabled(values.get("PASSKEY_ENABLED"))
        if not enabled:
            return cls(enabled=False)
        ttl = _positive_int(values, "PASSKEY_CHALLENGE_TTL_SECONDS", 300)
        recent = _positive_int(values, "PASSKEY_RECENT_AUTH_SECONDS", 600)
        authenticated_limit = _positive_int(
            values, "PASSKEY_AUTHENTICATED_RATE_LIMIT", 20
        )
        anonymous_limit = _positive_int(values, "PASSKEY_ANONYMOUS_RATE_LIMIT", 300)
        derive_from_frontend_urls = _enabled(
            values.get("PASSKEY_DERIVE_FROM_FRONTEND_URLS")
        )
        if derive_from_frontend_urls:
            explicit_settings = (
                "PASSKEY_RP_ID",
                "PASSKEY_RP_IDS",
                "PASSKEY_ORIGINS",
            )
            if any(name in values for name in explicit_settings):
                raise PasskeyConfigurationError(
                    "PASSKEY_RP_ID, PASSKEY_RP_IDS, and PASSKEY_ORIGINS must not be set "
                    "when PASSKEY_DERIVE_FROM_FRONTEND_URLS is enabled"
                )
            frontend_values = values.get("FRONTEND_URLS", "").split(",")
            if any(not value.strip() for value in frontend_values):
                raise PasskeyConfigurationError(
                    "FRONTEND_URLS must contain comma-separated origins without empty entries"
                )
            parsed_origins = tuple(
                _validate_origin(value.strip(), "FRONTEND_URLS")
                for value in frontend_values
            )
            normalized_origins = tuple(origin for origin, _hostname in parsed_origins)
            if len(set(normalized_origins)) != len(normalized_origins):
                raise PasskeyConfigurationError(
                    "FRONTEND_URLS contains a duplicate origin after normalization"
                )
            origin_rp_ids = [
                (origin, _normalize_rp_id(hostname, "FRONTEND_URLS"))
                for origin, hostname in parsed_origins
            ]
            rp_ids = tuple(dict.fromkeys(rp_id for _origin, rp_id in origin_rp_ids))
        else:
            plural_present = "PASSKEY_RP_IDS" in values
            singular_present = "PASSKEY_RP_ID" in values
            if plural_present and singular_present:
                raise PasskeyConfigurationError(
                    "PASSKEY_RP_ID and PASSKEY_RP_IDS must not both be set"
                )
            if plural_present:
                raw_rp_ids = values.get("PASSKEY_RP_IDS", "").split(",")
                if any(not value.strip() for value in raw_rp_ids):
                    raise PasskeyConfigurationError(
                        "PASSKEY_RP_IDS must not contain empty entries"
                    )
                rp_ids = tuple(_normalize_rp_id(value) for value in raw_rp_ids)
                if len(set(rp_ids)) != len(rp_ids):
                    raise PasskeyConfigurationError(
                        "PASSKEY_RP_IDS contains a duplicate RP ID after normalization"
                    )
            else:
                raw_rp_id = values.get("PASSKEY_RP_ID", "")
                if not raw_rp_id.strip():
                    raise PasskeyConfigurationError("PASSKEY_RP_ID is required")
                rp_ids = (_normalize_rp_id(raw_rp_id, "PASSKEY_RP_ID"),)
        rp_name = (values.get("PASSKEY_RP_NAME") or "BMO Deep Agent").strip()
        if not rp_name or len(rp_name) > 200:
            raise PasskeyConfigurationError(
                "PASSKEY_RP_NAME is required and must contain at most 200 characters"
            )
        if not derive_from_frontend_urls:
            origin_values = [
                value.strip()
                for value in (values.get("PASSKEY_ORIGINS") or "").split(",")
                if value.strip()
            ]
            if not origin_values:
                raise PasskeyConfigurationError("PASSKEY_ORIGINS is required")
            parsed_origins = tuple(_validate_origin(value) for value in origin_values)
            origin_rp_ids = []
            for origin, hostname in parsed_origins:
                matches = tuple(
                    rp_id
                    for rp_id in rp_ids
                    if hostname == rp_id or hostname.endswith(f".{rp_id}")
                )
                if not matches:
                    raise PasskeyConfigurationError(
                        "Every passkey origin must match a configured RP ID"
                    )
                origin_rp_ids.append((origin, max(matches, key=len)))
            used_rp_ids = {rp_id for _origin, rp_id in origin_rp_ids}
            if used_rp_ids != set(rp_ids):
                raise PasskeyConfigurationError(
                    "Every passkey RP ID must be used by at least one origin"
                )

        proxy_id = (values.get("PASSKEY_PROXY_ID") or "").strip()
        if not proxy_id or len(proxy_id) > 255:
            raise PasskeyConfigurationError(
                "PASSKEY_PROXY_ID is required and must contain at most 255 characters"
            )
        proxy_secret = values.get("PASSKEY_PROXY_SECRET") or ""
        secret_length = len(proxy_secret.encode("utf-8"))
        if not 32 <= secret_length <= 4_096:
            raise PasskeyConfigurationError(
                "PASSKEY_PROXY_SECRET must contain between 32 and 4096 bytes"
            )
        oauth_secret = values.get("OAUTH_SECRET_KEY") or ""
        known_placeholders = {
            "<different-at-least-32-random-bytes>",
            "generate-a-random-secret-string-here",
            "your-secret-key-for-session-signing",
            "replace-with-a-secure-random-secret",
            "replace-with-at-least-32-random-bytes",
        }
        predictable = oauth_secret.strip().lower() in known_placeholders or (
                oauth_secret and len(set(oauth_secret)) == 1
        )
        if not 32 <= len(oauth_secret.encode("utf-8")) <= 4_096 or predictable:
            raise PasskeyConfigurationError(
                "OAUTH_SECRET_KEY must contain between 32 and 4096 bytes when passkeys are enabled"
            )
        if not _has_oauth(values):
            raise PasskeyConfigurationError(
                "OAuth configuration is required when passkeys are enabled"
            )
        if not _has_durable_store(values):
            raise PasskeyConfigurationError(
                "Passkeys require durable auth storage; SQLite must use SQLITE_DB_PATH"
            )
        return cls(
            enabled=True,
            rp_ids=rp_ids,
            origin_rp_ids=tuple(origin_rp_ids),
            rp_name=rp_name,
            origins=tuple(origin for origin, _hostname in parsed_origins),
            proxy_id=proxy_id,
            proxy_secret=proxy_secret,
            challenge_ttl_seconds=ttl,
            recent_auth_seconds=recent,
            authenticated_rate_limit=authenticated_limit,
            anonymous_rate_limit=anonymous_limit,
            oauth_cookie_secure=all(
                origin.startswith("https://") for origin, _hostname in parsed_origins
            ),
        )

    def is_recent_auth(
            self, authenticated_at: float, *, now: float | None = None
    ) -> bool:
        """Return whether session authentication happened inside recent-auth window."""
        current = time.time() if now is None else now
        return 0 <= current - authenticated_at <= self.recent_auth_seconds


def _decode_base64url(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise InvalidPasskeyError("Invalid passkey response")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise InvalidPasskeyError("Invalid passkey response") from exc


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class PasskeyService:
    """Run WebAuthn ceremonies against a durable authentication store."""

    def __init__(
            self,
            config: PasskeyConfig,
            store: AuthStore,
            *,
            clock: Callable[[], float] = time.time,
    ) -> None:
        """Create service using validated configuration and injected store."""
        if not config.enabled:
            raise PasskeyConfigurationError("Passkey service is disabled")
        self.config = config
        self.store = store
        self._clock = clock

    def _claim_expected_challenge(
            self,
            ceremony_id: str,
            *,
            kind: str,
            origin: str,
            proxy_id: str,
    ) -> ChallengeRecord | None:
        challenge = self.store.claim_challenge(ceremony_id)
        if challenge is None:
            return None
        try:
            rp_id = self.config.rp_id_for_origin(origin)
        except InvalidPasskeyError:
            return None
        if (
                self._clock() >= challenge.expires_at
                or challenge.kind != kind
                or challenge.origin != origin
                or challenge.rp_id != rp_id
                or challenge.proxy_id != proxy_id
        ):
            return None
        return challenge

    def _consume_rate(self, scope: str, key: str, limit: int) -> None:
        window_start = int(self._clock() // 60)
        if not self.store.consume_rate_limit(scope, key, window_start, limit):
            raise PasskeyRateLimitError("Passkey request rate limit exceeded")

    def _require_origin(self, origin: str) -> None:
        if origin not in self.config.origins:
            raise InvalidPasskeyError("Invalid passkey response")

    def _live_session(self, session_token: str, *, recent: bool = True):
        detail = self.store.get_session_detail(session_token)
        if detail is None:
            raise InvalidPasskeyError("Invalid or expired session")
        self._consume_rate(
            "account", detail.identity, self.config.authenticated_rate_limit
        )
        if recent and not self.config.is_recent_auth(
                detail.authenticated_at, now=self._clock()
        ):
            raise ReauthenticationRequired(detail.provider)
        return detail

    @staticmethod
    def _credential_json(credential: CredentialRecord) -> dict[str, Any]:
        try:
            label = _normalize_passkey_label(
                credential.label, allow_default=True
            )
        except InvalidPasskeyError:
            label = None
        if label is None:
            label = _default_passkey_label(
                device_type=credential.device_type,
                transports=credential.transports,
                created_at=credential.created_at,
            )
        return {
            "credential_id": credential.credential_id,
            "label": label,
            "transports": list(credential.transports),
            "device_type": credential.device_type,
            "backed_up": credential.backed_up,
            "created_at": credential.created_at,
            "last_used_at": credential.last_used_at,
        }

    def registration_options(
            self,
            *,
            session_token: str,
            origin: str,
            proxy_id: str,
    ) -> dict[str, Any]:
        """Create discoverable, user-verified registration options."""
        self._require_origin(origin)
        detail = self._live_session(session_token)
        account = self.store.get_account(detail.identity)
        if account is None:
            raise InvalidPasskeyError("Invalid or expired session")
        rp_id = self.config.rp_id_for_origin(origin)
        credentials = self.store.list_credentials(detail.identity)
        if any(not item.rp_id for item in credentials):
            if len(self.config.rp_ids) != 1:
                raise InvalidPasskeyError("Invalid passkey response")
            sole_rp_id = self.config.rp_ids[0]
            for item in credentials:
                if not item.rp_id and not self.store.bind_credential_rp_id(
                        item.credential_id, sole_rp_id
                ):
                    raise InvalidPasskeyError("Invalid passkey response")
            credentials = self.store.list_credentials(detail.identity)
            if any(not item.rp_id for item in credentials):
                raise InvalidPasskeyError("Invalid passkey response")
        challenge = secrets.token_bytes(32)
        now = self._clock()
        challenge_record = self.store.create_challenge(
            challenge=challenge,
            kind="registration",
            identity=detail.identity,
            origin=origin,
            rp_id=rp_id,
            proxy_id=proxy_id,
            created_at=now,
            expires_at=now + self.config.challenge_ttl_seconds,
        )
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name=self.config.rp_name,
            user_id=_decode_base64url(account.webauthn_user_handle),
            user_name=account.email or account.name or account.identity,
            user_display_name=account.name or account.email or account.identity,
            challenge=challenge,
            timeout=self.config.challenge_ttl_seconds * 1_000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                require_resident_key=True,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(
                    id=_decode_base64url(item.credential_id),
                    transports=[
                        AuthenticatorTransport(value) for value in item.transports
                    ],
                )
                for item in credentials
                if item.rp_id == rp_id
            ],
        )
        return {
            "ceremony_id": challenge_record.ceremony_id,
            "options": json.loads(options_to_json(options)),
        }

    def verify_registration(
            self,
            *,
            session_token: str | None,
            origin: str,
            proxy_id: str,
            ceremony_id: str,
            response: object,
            label: str | None = None,
    ) -> dict[str, Any]:
        """Consume and verify registration response, then persist public key."""
        challenge = self._claim_expected_challenge(
            ceremony_id,
            kind="registration",
            origin=origin,
            proxy_id=proxy_id,
        )
        if challenge is None:
            raise InvalidPasskeyError("Invalid passkey response")
        if not isinstance(session_token, str) or not session_token:
            raise InvalidPasskeySessionError("Invalid or expired session")
        detail = self._live_session(session_token)
        if challenge.identity != detail.identity:
            raise InvalidPasskeyError("Invalid passkey response")
        normalized_label = _normalize_passkey_label(label, allow_default=True)
        if not isinstance(response, Mapping):
            raise InvalidPasskeyError("Invalid passkey response")
        try:
            response_details = response.get("response")
            attestation_object = (
                response_details.get("attestationObject")
                if isinstance(response_details, Mapping)
                else None
            )
            attestation = cbor2.loads(_decode_base64url(attestation_object))
            if (
                    not isinstance(attestation, Mapping)
                    or attestation.get("fmt") != "none"
                    or attestation.get("attStmt") != {}
            ):
                raise InvalidPasskeyError("Invalid passkey response")
            verified = verify_registration_response(
                credential=dict(response),
                expected_challenge=challenge.challenge,
                expected_rp_id=challenge.rp_id,
                expected_origin=origin,
                require_user_verification=True,
            )
            credential_id = _encode_base64url(verified.credential_id)
            transports = (
                response_details.get("transports", [])
                if isinstance(response_details, Mapping)
                else []
            )
            allowed_transports = [
                value
                for value in transports
                if value in {"usb", "nfc", "ble", "smart-card", "hybrid", "internal"}
            ]
            created_at = self._clock()
            if normalized_label is None:
                normalized_label = _default_passkey_label(
                    device_type=verified.credential_device_type.value,
                    transports=allowed_transports,
                    created_at=created_at,
                )
            credential = self.store.create_credential(
                identity=detail.identity,
                rp_id=challenge.rp_id,
                credential_id=credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
                transports=allowed_transports,
                device_type=verified.credential_device_type.value,
                backed_up=verified.credential_backed_up,
                label=normalized_label,
                created_at=created_at,
            )
        except Exception as exc:
            raise InvalidPasskeyError("Invalid passkey response") from exc
        return self._credential_json(credential)

    def authentication_options(
            self,
            *,
            origin: str,
            proxy_id: str,
    ) -> dict[str, Any]:
        """Create identifier-free, user-verified authentication options."""
        self._require_origin(origin)
        rp_id = self.config.rp_id_for_origin(origin)
        self._consume_rate("proxy", proxy_id, self.config.anonymous_rate_limit)
        challenge = secrets.token_bytes(32)
        now = self._clock()
        challenge_record = self.store.create_challenge(
            challenge=challenge,
            kind="authentication",
            identity=None,
            origin=origin,
            rp_id=rp_id,
            proxy_id=proxy_id,
            created_at=now,
            expires_at=now + self.config.challenge_ttl_seconds,
        )
        options = generate_authentication_options(
            rp_id=rp_id,
            challenge=challenge,
            timeout=self.config.challenge_ttl_seconds * 1_000,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        options_json = json.loads(options_to_json(options))
        if options_json.get("allowCredentials") == []:
            options_json.pop("allowCredentials")
        return {
            "ceremony_id": challenge_record.ceremony_id,
            "options": options_json,
        }

    def verify_authentication(
            self,
            *,
            origin: str,
            proxy_id: str,
            ceremony_id: str,
            response: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Verify identifier-free assertion and issue a passkey session."""
        challenge = self._claim_expected_challenge(
            ceremony_id,
            kind="authentication",
            origin=origin,
            proxy_id=proxy_id,
        )
        if challenge is None:
            raise InvalidPasskeyError("Invalid passkey response")
        self._consume_rate("proxy", proxy_id, self.config.anonymous_rate_limit)
        try:
            credential_id = response.get("id")
            if not isinstance(credential_id, str):
                raise InvalidPasskeyError("Invalid passkey response")
            credential = self.store.get_credential(credential_id)
            if credential is None:
                raise InvalidPasskeyError("Invalid passkey response")
            if not credential.rp_id:
                if len(self.config.rp_ids) != 1 or not self.store.bind_credential_rp_id(
                        credential_id, self.config.rp_ids[0]
                ):
                    raise InvalidPasskeyError("Invalid passkey response")
                credential = self.store.get_credential(credential_id)
                if credential is None or not credential.rp_id:
                    raise InvalidPasskeyError("Invalid passkey response")
            if credential.rp_id != challenge.rp_id:
                raise InvalidPasskeyError("Invalid passkey response")
            account = self.store.get_account(credential.identity)
            if account is None:
                raise InvalidPasskeyError("Invalid passkey response")
            assertion = response.get("response")
            user_handle = (
                assertion.get("userHandle") if isinstance(assertion, Mapping) else None
            )
            if not isinstance(user_handle, str) or not secrets.compare_digest(
                    user_handle, account.webauthn_user_handle
            ):
                raise InvalidPasskeyError("Invalid passkey response")
            verified = verify_authentication_response(
                credential=dict(response),
                expected_challenge=challenge.challenge,
                expected_rp_id=challenge.rp_id,
                expected_origin=origin,
                credential_public_key=credential.public_key,
                credential_current_sign_count=credential.sign_count,
                require_user_verification=True,
            )
            updated = self.store.update_credential_state(
                credential_id,
                expected_sign_count=credential.sign_count,
                expected_backed_up=credential.backed_up,
                new_sign_count=verified.new_sign_count,
                backed_up=verified.credential_backed_up,
                last_used_at=self._clock(),
            )
            if not updated:
                raise InvalidPasskeyError("Invalid passkey response")
            user_data = {
                "identity": account.identity,
                "provider": account.provider,
                "email": account.email,
                "name": account.name,
            }
            if account.avatar_url:
                avatar_key = "picture" if account.provider == "google" else "avatar_url"
                user_data[avatar_key] = account.avatar_url
            user_data.update(account.profile)
            session_token = self.store.create_session(
                user_data, account.provider, auth_method="passkey"
            )
            user = self.store.validate_session(session_token)
            if user is None:
                raise InvalidPasskeyError("Invalid passkey response")
        except InvalidPasskeyError:
            raise
        except Exception as exc:
            raise InvalidPasskeyError("Invalid passkey response") from exc
        return {"ok": True, "user": user, "session_token": session_token}

    def list_credentials(self, session_token: str) -> list[dict[str, Any]]:
        """List public credential metadata for a recently authenticated account."""
        detail = self._live_session(session_token)
        return [
            self._credential_json(item)
            for item in self.store.list_credentials(detail.identity)
        ]

    def rename_credential(
            self, session_token: str, credential_id: str, label: str
    ) -> dict[str, Any]:
        """Rename one owned credential after recent authentication."""
        detail = self._live_session(session_token)
        normalized_label = _normalize_passkey_label(label, allow_default=False)
        if not self.store.rename_credential(
                detail.identity, credential_id, normalized_label
        ):
            raise InvalidPasskeyError("Passkey not found")
        credential = self.store.get_credential(credential_id)
        if credential is None:
            raise InvalidPasskeyError("Passkey not found")
        return self._credential_json(credential)

    def delete_credential(self, session_token: str, credential_id: str) -> bool:
        """Revoke one owned credential; OAuth remains recovery path."""
        detail = self._live_session(session_token)
        if not self.store.delete_credential(detail.identity, credential_id):
            raise InvalidPasskeyError("Passkey not found")
        return True


_MAX_PASSKEY_PAYLOAD_BYTES = 64 * 1024
_PASSKEY_MANAGEMENT_RETURN_PATH = "/chat?manage=passkeys"


def safe_oauth_return_path(candidate: str | None) -> str | None:
    """Allow only exact management return path; never interpret URLs."""
    return candidate if candidate == _PASSKEY_MANAGEMENT_RETURN_PATH else None


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:]
    return token if token else None


def _proxy_context(request: Request, config: PasskeyConfig) -> tuple[str, str] | None:
    proxy_id = request.headers.get("x-passkey-proxy-id", "")
    secret = request.headers.get("x-passkey-proxy-secret", "")
    origin = request.headers.get("x-passkey-origin", "")
    if (
            not hmac.compare_digest(proxy_id, config.proxy_id)
            or not hmac.compare_digest(secret, config.proxy_secret)
            or origin not in config.origins
    ):
        return None
    return proxy_id, origin


async def _bounded_json(request: Request) -> Mapping[str, Any] | JSONResponse:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_PASSKEY_PAYLOAD_BYTES:
                return JSONResponse({"code": "payload_too_large"}, status_code=413)
        except ValueError:
            return JSONResponse({"code": "invalid_request"}, status_code=400)
    body = await request.body()
    if len(body) > _MAX_PASSKEY_PAYLOAD_BYTES:
        return JSONResponse({"code": "payload_too_large"}, status_code=413)
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return JSONResponse({"code": "invalid_request"}, status_code=400)
    if not isinstance(parsed, Mapping):
        return JSONResponse({"code": "invalid_request"}, status_code=400)
    return parsed


def _route_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, InvalidPasskeySessionError):
        return JSONResponse({"code": "invalid_session"}, status_code=401)
    if isinstance(exc, ReauthenticationRequired):
        return JSONResponse(
            {"code": "reauth_required", "provider": exc.provider},
            status_code=403,
        )
    if isinstance(exc, PasskeyRateLimitError):
        return JSONResponse({"code": "rate_limited"}, status_code=429)
    return JSONResponse({"code": "invalid_passkey_response"}, status_code=400)


def register_passkey_routes(
        app, *, service: PasskeyService, config: PasskeyConfig
) -> None:
    """Register trusted-BFF-only passkey ceremony and management endpoints."""

    def context(request: Request) -> tuple[str, str] | JSONResponse:
        trusted = _proxy_context(request, config)
        if trusted is None:
            return JSONResponse({"code": "passkey_request_rejected"}, status_code=403)
        return trusted

    def protected_context(
            request: Request,
    ) -> tuple[str, str, str] | JSONResponse:
        trusted = context(request)
        if isinstance(trusted, JSONResponse):
            return trusted
        token = _bearer_token(request)
        if token is None:
            return JSONResponse({"code": "invalid_session"}, status_code=401)
        return trusted[0], trusted[1], token

    @app.post("/auth/passkeys/registration/options")
    async def passkey_registration_options(request: Request):
        protected = protected_context(request)
        if isinstance(protected, JSONResponse):
            return protected
        proxy_id, origin, token = protected
        try:
            return await asyncio.to_thread(
                service.registration_options,
                session_token=token, origin=origin, proxy_id=proxy_id
            )
        except Exception as exc:
            return _route_error(exc)

    @app.post("/auth/passkeys/registration/verify")
    async def passkey_registration_verify(request: Request):
        trusted = context(request)
        if isinstance(trusted, JSONResponse):
            return trusted
        payload = await _bounded_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        ceremony_id = payload.get("ceremony_id")
        if not isinstance(ceremony_id, str) or not ceremony_id:
            return JSONResponse({"code": "invalid_request"}, status_code=400)
        proxy_id, origin = trusted
        try:
            passkey = await asyncio.to_thread(
                service.verify_registration,
                session_token=_bearer_token(request),
                origin=origin,
                proxy_id=proxy_id,
                ceremony_id=ceremony_id,
                response=payload.get("response"),
                label=payload.get("label"),
            )
            return {"ok": True, "passkey": passkey}
        except Exception as exc:
            return _route_error(exc)

    @app.post("/auth/passkeys/authentication/options")
    async def passkey_authentication_options(request: Request):
        trusted = context(request)
        if isinstance(trusted, JSONResponse):
            return trusted
        proxy_id, origin = trusted
        try:
            return await asyncio.to_thread(
                service.authentication_options,
                origin=origin,
                proxy_id=proxy_id,
            )
        except Exception as exc:
            return _route_error(exc)

    @app.post("/auth/passkeys/authentication/verify")
    async def passkey_authentication_verify(request: Request):
        trusted = context(request)
        if isinstance(trusted, JSONResponse):
            return trusted
        payload = await _bounded_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        ceremony_id = payload.get("ceremony_id")
        response = payload.get("response")
        if not isinstance(ceremony_id, str) or not isinstance(response, Mapping):
            return JSONResponse({"code": "invalid_request"}, status_code=400)
        proxy_id, origin = trusted
        try:
            return await asyncio.to_thread(
                service.verify_authentication,
                origin=origin,
                proxy_id=proxy_id,
                ceremony_id=ceremony_id,
                response=response,
            )
        except Exception as exc:
            return _route_error(exc)

    @app.get("/auth/passkeys")
    async def passkey_list(request: Request):
        protected = protected_context(request)
        if isinstance(protected, JSONResponse):
            return protected
        _proxy_id, _origin, token = protected
        try:
            passkeys = await asyncio.to_thread(service.list_credentials, token)
            return {"passkeys": passkeys}
        except Exception as exc:
            return _route_error(exc)

    @app.patch("/auth/passkeys/{credential_id}")
    async def passkey_rename(credential_id: str, request: Request):
        protected = protected_context(request)
        if isinstance(protected, JSONResponse):
            return protected
        payload = await _bounded_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        label = payload.get("label")
        if not isinstance(label, str):
            return JSONResponse({"code": "invalid_request"}, status_code=400)
        _proxy_id, _origin, token = protected
        try:
            passkey = await asyncio.to_thread(
                service.rename_credential,
                token,
                credential_id,
                label,
            )
            return {"passkey": passkey}
        except Exception as exc:
            return _route_error(exc)

    @app.delete("/auth/passkeys/{credential_id}")
    async def passkey_delete(credential_id: str, request: Request):
        protected = protected_context(request)
        if isinstance(protected, JSONResponse):
            return protected
        _proxy_id, _origin, token = protected
        try:
            await asyncio.to_thread(service.delete_credential, token, credential_id)
            return {"ok": True}
        except Exception as exc:
            return _route_error(exc)
