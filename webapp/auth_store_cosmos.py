"""Azure Cosmos DB durable authentication adapter."""

# ruff: noqa: D102, D107

import base64
import hashlib
import secrets
import time
from itertools import islice
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

_CAS_RETRIES = 8
_RESERVATION_TTL_SECONDS = 5 * 60


def _cosmos_status(error: Exception) -> int | None:
    return getattr(error, "status_code", None)


def _cosmos_match_condition() -> Any:
    try:
        from azure.core import MatchConditions

        return MatchConditions.IfNotModified
    except ImportError:
        return "IfNotModified"


class CosmosAuthStore:
    """Cosmos adapter using point reads and ETag conditional replacements."""

    def __init__(
            self,
            *,
            containers: Mapping[str, Any],
            clock: Any = time.time,
            owner: Any | None = None,
    ) -> None:
        self.containers = dict(containers)
        self._clock = clock
        self._owner = owner

    def _read(self, name: str, item: str, pk: str) -> dict[str, Any] | None:
        try:
            return self.containers[name].read_item(item=item, partition_key=pk)
        except Exception as exc:
            if _cosmos_status(exc) == 404:
                return None
            raise

    def _replace(self, name: str, doc: dict[str, Any]) -> bool:
        try:
            self.containers[name].replace_item(
                item=doc["id"],
                body={k: v for k, v in doc.items() if k != "_etag"},
                partition_key=doc["pk"],
                etag=doc["_etag"],
                match_condition=_cosmos_match_condition(),
            )
            return True
        except Exception as exc:
            if _cosmos_status(exc) in {404, 412}:
                return False
            raise

    def _account(self, user_data: Mapping[str, Any], provider: str) -> dict[str, Any]:
        identity, provider, subject = SQLiteAuthStore._identity_parts(
            user_data, provider
        )
        now = self._clock()
        existing = self._read("accounts", identity, identity)
        if existing:
            if (existing["provider"], existing["provider_subject"]) != (
                    provider,
                    subject,
            ):
                raise ValueError("provider identity conflict")
            updated = dict(
                existing,
                email=_truncate(user_data.get("email"), 320),
                name=_truncate(user_data.get("name"), 200),
                avatar_url=_truncate(
                    user_data.get("picture") or user_data.get("avatar_url"),
                    2048,
                ),
                profile=_sanitize_profile(user_data),
                updated_at=now,
            )
            if not self._replace("accounts", updated):
                raise AuthStoreError("account update conflict")
            return updated
        for _ in range(5):
            handle = secrets.token_urlsafe(32)
            reservation_id = f"handle:{handle}"
            try:
                self.containers["accounts"].create_item(
                    body={
                        "id": reservation_id,
                        "pk": reservation_id,
                        "type": "webauthn_handle",
                        "identity": identity,
                    }
                )
            except Exception as exc:
                if _cosmos_status(exc) == 409:
                    continue
                raise
            doc = {
                "id": identity,
                "pk": identity,
                "identity": identity,
                "provider": provider,
                "provider_subject": subject,
                "email": _truncate(user_data.get("email"), 320),
                "name": _truncate(user_data.get("name"), 200),
                "avatar_url": _truncate(
                    user_data.get("picture") or user_data.get("avatar_url"), 2048
                ),
                "profile": _sanitize_profile(user_data),
                "webauthn_user_handle": handle,
                "credential_count": 0,
                "credential_reservations": [],
                "registration_challenge_count": 0,
                "registration_challenge_reservations": [],
                "created_at": now,
                "updated_at": now,
            }
            try:
                return self.containers["accounts"].create_item(body=doc)
            except Exception:
                self.containers["accounts"].delete_item(
                    item=reservation_id,
                    partition_key=reservation_id,
                )
                raise
        raise AuthStoreError("could not reserve a globally unique user handle")

    @staticmethod
    def _account_record(doc: Mapping[str, Any]) -> AccountRecord:
        return AccountRecord(
            identity=doc["identity"],
            provider=doc["provider"],
            email=doc.get("email"),
            name=doc.get("name"),
            avatar_url=doc.get("avatar_url"),
            profile=dict(doc.get("profile") or {}),
            webauthn_user_handle=_validate_base64url(
                doc["webauthn_user_handle"],
                "webauthn_user_handle",
                86,
            ),
        )

    def get_account(self, identity: str) -> AccountRecord | None:
        if not isinstance(identity, str) or not identity:
            return None
        doc = self._read("accounts", identity, identity)
        return self._account_record(doc) if doc else None

    def create_session(
            self, user_data: Mapping[str, Any], provider: str, auth_method: str = "oauth"
    ) -> str:
        if auth_method not in {"oauth", "passkey"}:
            raise ValueError("auth_method must be 'oauth' or 'passkey'")
        account = self._account(user_data, provider)
        now = self._clock()
        for _ in range(3):
            token = secrets.token_urlsafe(32)
            digest = _token_hash(token)
            doc = {
                "id": digest,
                "pk": digest,
                "token_hash": digest,
                "identity": account["identity"],
                "provider": account["provider"],
                "auth_method": auth_method,
                "authenticated_at": now,
                "created_at": now,
                "expires_at": now + SESSION_LIFETIME_SECONDS,
            }
            try:
                self.containers["sessions"].create_item(body=doc)
                return token
            except Exception as exc:
                if _cosmos_status(exc) != 409:
                    raise
        raise AuthStoreError("session token collision")

    def get_session_detail(self, token: str) -> SessionDetail | None:
        doc = self._read("sessions", _token_hash(token), _token_hash(token))
        if not doc or self._clock() >= doc["expires_at"]:
            return None
        return SessionDetail(
            doc["identity"],
            doc["provider"],
            doc["auth_method"],
            doc["authenticated_at"],
            doc["expires_at"],
        )

    def validate_session(self, token: str) -> dict[str, Any] | None:
        digest = _token_hash(token)
        doc = self._read("sessions", digest, digest)
        now = self._clock()
        if not doc or now >= doc["expires_at"]:
            return None
        if doc["expires_at"] - now < SESSION_REFRESH_THRESHOLD_SECONDS:
            original_etag = doc.get("_etag")
            candidate = dict(doc, expires_at=now + SESSION_LIFETIME_SECONDS)
            if self._replace("sessions", candidate):
                doc = candidate
            else:
                current = self._read("sessions", digest, digest)
                if (
                        not current
                        or now >= current["expires_at"]
                        or current.get("_etag") == original_etag
                ):
                    return None
                doc = current
        account = self._read("accounts", doc["identity"], doc["identity"])
        if not account:
            return None
        return {
            "identity": doc["identity"],
            "provider": doc["provider"],
            "email": account.get("email"),
            "name": account.get("name"),
            **account.get("profile", {}),
            **({"auth_method": "passkey"} if doc["auth_method"] == "passkey" else {}),
        }

    def refresh_session(self, token: str) -> dict[str, Any] | None:
        digest = _token_hash(token)
        doc = self._read("sessions", digest, digest)
        now = self._clock()
        if not doc or now >= doc["expires_at"]:
            return None
        original_etag = doc.get("_etag")
        candidate = dict(doc, expires_at=now + SESSION_LIFETIME_SECONDS)
        if not self._replace("sessions", candidate):
            current = self._read("sessions", digest, digest)
            if (
                    not current
                    or now >= current["expires_at"]
                    or current.get("_etag") == original_etag
            ):
                return None
        return self.validate_session(token)

    def remove_session(self, token: str) -> str | None:
        digest = _token_hash(token)
        doc = self._read("sessions", digest, digest)
        if not doc:
            return None
        try:
            self.containers["sessions"].delete_item(
                item=digest,
                partition_key=digest,
                etag=doc.get("_etag"),
                match_condition=_cosmos_match_condition(),
            )
            return doc["identity"]
        except Exception as exc:
            if _cosmos_status(exc) in {404, 412}:
                return None
            raise

    def cleanup_expired_sessions(self, limit: int = DEFAULT_CLEANUP_LIMIT) -> int:
        return self._cleanup("sessions", limit)

    def cleanup_challenges(self, limit: int = DEFAULT_CLEANUP_LIMIT) -> int:
        return self._cleanup("challenges", limit)

    def _cleanup(self, name: str, limit: int) -> int:
        bounded = max(0, min(int(limit), MAX_CLEANUP_LIMIT))
        if bounded == 0:
            return 0
        now = self._clock()
        rows = islice(
            self.containers[name].query_items(
                query="SELECT * FROM c WHERE c.expires_at <= @now",
                parameters=[{"name": "@now", "value": now}],
                enable_cross_partition_query=True,
                max_item_count=bounded,
            ),
            bounded,
        )
        count = 0
        for row in rows:
            current = self._read(name, row["id"], row["pk"])
            if not current or current.get("expires_at", float("inf")) > now:
                continue
            try:
                self.containers[name].delete_item(
                    item=current["id"],
                    partition_key=current["pk"],
                    etag=current.get("_etag"),
                    match_condition=_cosmos_match_condition(),
                )
                count += 1
                if (
                        name == "challenges"
                        and current.get("kind") == "registration"
                        and current.get("identity")
                ):
                    self._release_registration_reservation(
                        current["identity"],
                        current["id"],
                    )
            except Exception as exc:
                if _cosmos_status(exc) not in {404, 412}:
                    raise
        return count

    @staticmethod
    def _credential(doc: Mapping[str, Any]) -> CredentialRecord:
        return CredentialRecord(
            doc["credential_id"],
            doc["identity"],
            _decode_binary(doc["public_key"], "public_key", 16384),
            doc["sign_count"],
            tuple(doc["transports"]),
            doc["device_type"],
            doc["backed_up"],
            doc.get("label"),
            doc["created_at"],
            doc.get("last_used_at"),
            doc.get("rp_id"),
        )

    def get_credential(self, cid: str) -> CredentialRecord | None:
        doc = self._read("credentials", cid, cid)
        return self._credential(doc) if doc else None

    def list_credentials(
            self, identity: str, rp_id: str | None = None
    ) -> list[CredentialRecord]:
        if rp_id is not None:
            rp_id = _validate_rp_id(rp_id)
        query = "SELECT * FROM c WHERE c.identity=@identity"
        parameters = [{"name": "@identity", "value": identity}]
        if rp_id is not None:
            query += " AND c.rp_id=@rp_id"
            parameters.append({"name": "@rp_id", "value": rp_id})
        rows = self.containers["credentials"].query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True,
        )
        return sorted(
            (self._credential(r) for r in rows),
            key=lambda r: (r.created_at, r.credential_id),
        )

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
        doc = self._read("credentials", credential_id, credential_id)
        if not doc or (doc["sign_count"], doc["backed_up"]) != (
                expected_sign_count,
                expected_backed_up,
        ):
            return False
        doc.update(
            sign_count=new_sign_count,
            backed_up=backed_up,
            last_used_at=self._clock() if last_used_at is None else last_used_at,
        )
        return self._replace("credentials", doc)

    def _legacy_credential_slots(self, identity: str) -> list[dict[str, Any]]:
        rows = self.containers["credentials"].query_items(
            query="SELECT c.credential_id FROM c WHERE c.identity=@identity",
            parameters=[{"name": "@identity", "value": identity}],
            enable_cross_partition_query=True,
            max_item_count=MAX_CREDENTIALS_PER_ACCOUNT + 1,
        )
        return [
            {"credential_id": row["credential_id"], "expires_at": None}
            for row in islice(rows, MAX_CREDENTIALS_PER_ACCOUNT + 1)
        ]

    def _credential_slots(
            self,
            account: Mapping[str, Any],
            *,
            verify_committed: bool = False,
    ) -> list[dict[str, Any]]:
        raw_slots = account.get("credential_reservations")
        if not isinstance(raw_slots, list):
            return self._legacy_credential_slots(account["identity"])
        now = self._clock()
        slots: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_slots:
            if not isinstance(raw, Mapping):
                continue
            credential_id = raw.get("credential_id")
            if (
                    not isinstance(credential_id, str)
                    or not credential_id
                    or credential_id in seen
            ):
                continue
            expires_at = raw.get("expires_at")
            should_verify = (verify_committed and expires_at is None) or (
                    isinstance(expires_at, (int, float)) and expires_at <= now
            )
            if should_verify:
                credential = self._read(
                    "credentials",
                    credential_id,
                    credential_id,
                )
                if credential and credential.get("identity") == account["identity"]:
                    expires_at = None
            elif expires_at is not None and not isinstance(expires_at, (int, float)):
                expires_at = None
            seen.add(credential_id)
            slots.append({"credential_id": credential_id, "expires_at": expires_at})
        return slots

    def _reconciled_credential_slots(
            self,
            account: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Reconcile visible writes without dropping unresolved writer reservations.

        A crashed writer can leave an orphan that consumes capacity until an explicit
        rollback or operator repair. This fail-closed tradeoff prevents a paused writer
        from resuming after its lease and exceeding the hard credential cap.
        """
        merged = {
            slot["credential_id"]: slot
            for slot in self._credential_slots(account, verify_committed=True)
        }
        for slot in self._legacy_credential_slots(account["identity"]):
            merged.setdefault(slot["credential_id"], slot)
        return list(merged.values())

    def _reserve_credential(self, identity: str, credential_id: str) -> None:
        for _ in range(_CAS_RETRIES):
            account = self._read("accounts", identity, identity)
            if account is None:
                raise ValueError("credential account does not exist")
            slots = self._credential_slots(account)
            if any(slot["credential_id"] == credential_id for slot in slots):
                raise DuplicateCredentialError("credential exists")
            if (
                    account.get("credential_count", 0) >= MAX_CREDENTIALS_PER_ACCOUNT
                    or len(slots) >= MAX_CREDENTIALS_PER_ACCOUNT
            ):
                slots = self._reconciled_credential_slots(account)
                if len(slots) >= MAX_CREDENTIALS_PER_ACCOUNT:
                    raise CredentialLimitError("account already has 10 credentials")
            slots.append(
                {
                    "credential_id": credential_id,
                    "expires_at": self._clock() + _RESERVATION_TTL_SECONDS,
                }
            )
            updated = dict(
                account,
                credential_count=len(slots),
                credential_reservations=slots,
            )
            if self._replace("accounts", updated):
                return
        raise CredentialLimitError("credential cap update conflict")

    def _finish_credential_reservation(
            self,
            identity: str,
            credential_id: str,
            *,
            committed: bool,
    ) -> bool:
        for _ in range(_CAS_RETRIES):
            account = self._read("accounts", identity, identity)
            if account is None:
                return False
            slots = self._credential_slots(account)
            found = False
            updated_slots: list[dict[str, Any]] = []
            for slot in slots:
                if slot["credential_id"] != credential_id:
                    updated_slots.append(slot)
                    continue
                found = True
                if committed:
                    updated_slots.append(
                        {"credential_id": credential_id, "expires_at": None}
                    )
            if not found and committed:
                credential = self._read("credentials", credential_id, credential_id)
                if credential and credential.get("identity") == identity:
                    updated_slots.append(
                        {"credential_id": credential_id, "expires_at": None}
                    )
            updated = dict(
                account,
                credential_count=len(updated_slots),
                credential_reservations=updated_slots,
            )
            if self._replace("accounts", updated):
                return True
        return False

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
        decoded_key = _decode_binary(public_key, "public_key", 16384)
        normalized_transports, normalized_label = _validate_credential_inputs(
            sign_count=sign_count,
            transports=transports,
            device_type=device_type,
            backed_up=backed_up,
            label=label,
        )
        self._reserve_credential(identity, credential_id)
        doc = {
            "id": credential_id,
            "pk": credential_id,
            "credential_id": credential_id,
            "identity": identity,
            "rp_id": rp_id,
            "public_key": base64.urlsafe_b64encode(decoded_key).rstrip(b"=").decode(),
            "sign_count": sign_count,
            "transports": list(normalized_transports),
            "device_type": device_type,
            "backed_up": backed_up,
            "label": normalized_label,
            "created_at": self._clock() if created_at is None else created_at,
            "last_used_at": last_used_at,
        }
        try:
            created = self._credential(
                self.containers["credentials"].create_item(body=doc)
            )
        except Exception as exc:
            self._finish_credential_reservation(
                identity,
                credential_id,
                committed=False,
            )
            if _cosmos_status(exc) == 409:
                raise DuplicateCredentialError("credential exists") from exc
            raise
        self._finish_credential_reservation(
            identity,
            credential_id,
            committed=True,
        )
        return created

    def bind_credential_rp_id(self, credential_id: str, rp_id: str) -> bool:
        rp_id = _validate_rp_id(rp_id)
        if not isinstance(credential_id, str) or not credential_id:
            return False
        for _ in range(_CAS_RETRIES):
            doc = self._read("credentials", credential_id, credential_id)
            if doc is None:
                return False
            current = doc.get("rp_id")
            if current not in {None, ""}:
                return current == rp_id
            updated = dict(doc, rp_id=rp_id)
            if self._replace("credentials", updated):
                return True
        return False

    def rename_credential(self, identity: str, credential_id: str, label: str) -> bool:
        doc = self._read("credentials", credential_id, credential_id)
        if not doc or doc["identity"] != identity:
            return False
        doc["label"] = _validate_label(label)
        return self._replace("credentials", doc)

    def delete_credential(self, identity: str, credential_id: str) -> bool:
        doc = self._read("credentials", credential_id, credential_id)
        if not doc or doc["identity"] != identity:
            return False
        try:
            self.containers["credentials"].delete_item(
                item=credential_id,
                partition_key=credential_id,
                etag=doc.get("_etag"),
                match_condition=_cosmos_match_condition(),
            )
        except Exception:
            return False
        self._finish_credential_reservation(
            identity,
            credential_id,
            committed=False,
        )
        return True

    def _legacy_registration_reservations(
            self,
            identity: str,
    ) -> list[dict[str, Any]]:
        now = self._clock()
        rows = self.containers["challenges"].query_items(
            query=(
                "SELECT c.id, c.expires_at FROM c WHERE c.identity = @identity "
                "AND c.kind = 'registration' AND IS_NULL(c.consumed_at) "
                "AND c.expires_at > @now"
            ),
            parameters=[
                {"name": "@identity", "value": identity},
                {"name": "@now", "value": now},
            ],
            enable_cross_partition_query=True,
            max_item_count=MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT + 1,
        )
        return [
            {
                "ceremony_id": row["id"],
                "expires_at": row["expires_at"],
                "reserved_until": None,
            }
            for row in islice(rows, MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT + 1)
        ]

    def _registration_reservations(
            self,
            account: Mapping[str, Any],
            *,
            verify_committed: bool = False,
    ) -> list[dict[str, Any]]:
        raw_reservations = account.get("registration_challenge_reservations")
        if not isinstance(raw_reservations, list):
            return self._legacy_registration_reservations(account["identity"])
        now = self._clock()
        reservations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_reservations:
            if not isinstance(raw, Mapping):
                continue
            ceremony_id = raw.get("ceremony_id")
            expires_at = raw.get("expires_at")
            reserved_until = raw.get("reserved_until")
            if (
                    not isinstance(ceremony_id, str)
                    or not ceremony_id
                    or ceremony_id in seen
            ):
                continue
            should_verify = verify_committed and reserved_until is None
            should_verify = should_verify or (
                    isinstance(reserved_until, (int, float)) and reserved_until <= now
            )
            should_verify = should_verify or (
                    isinstance(expires_at, (int, float)) and expires_at <= now
            )
            if should_verify:
                challenge = self._read("challenges", ceremony_id, ceremony_id)
                if (
                        challenge
                        and challenge.get("identity") == account["identity"]
                        and challenge.get("kind") == "registration"
                ):
                    if (
                            challenge.get("consumed_at") is not None
                            or challenge.get("expires_at", 0) <= now
                    ):
                        continue
                    expires_at = challenge["expires_at"]
                    reserved_until = None
            elif reserved_until is not None and not isinstance(
                    reserved_until, (int, float)
            ):
                reserved_until = None
            seen.add(ceremony_id)
            reservations.append(
                {
                    "ceremony_id": ceremony_id,
                    "expires_at": expires_at,
                    "reserved_until": reserved_until,
                }
            )
        return reservations

    def _reconciled_registration_reservations(
            self,
            account: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Keep unresolved leases fail-closed while merging visible challenges.

        Orphan reservations may reduce availability after a crashed writer, but they
        cannot be discarded on time alone because that writer may only be paused.
        Explicit rollback/cleanup remains the safe release path.
        """
        merged = {
            reservation["ceremony_id"]: reservation
            for reservation in self._registration_reservations(
                account,
                verify_committed=True,
            )
        }
        for reservation in self._legacy_registration_reservations(account["identity"]):
            merged.setdefault(reservation["ceremony_id"], reservation)
        return list(merged.values())

    def _reserve_registration_challenge(
            self,
            identity: str,
            ceremony_id: str,
            expires_at: float,
    ) -> None:
        for _ in range(_CAS_RETRIES):
            account = self._read("accounts", identity, identity)
            if account is None:
                raise ValueError("challenge account does not exist")
            reservations = self._registration_reservations(account)
            if (
                    account.get("registration_challenge_count", 0)
                    >= MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT
                    or len(reservations) >= MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT
            ):
                reservations = self._reconciled_registration_reservations(account)
                if len(reservations) >= MAX_REGISTRATION_CHALLENGES_PER_ACCOUNT:
                    raise ChallengeLimitError("too many registration challenges")
            if any(
                    reservation["ceremony_id"] == ceremony_id
                    for reservation in reservations
            ):
                raise AuthStoreError("challenge reservation collision")
            reservations.append(
                {
                    "ceremony_id": ceremony_id,
                    "expires_at": expires_at,
                    "reserved_until": self._clock() + _RESERVATION_TTL_SECONDS,
                }
            )
            updated = dict(
                account,
                registration_challenge_count=len(reservations),
                registration_challenge_reservations=reservations,
            )
            if self._replace("accounts", updated):
                return
        raise ChallengeLimitError("registration challenge cap update conflict")

    def _finish_registration_reservation(
            self,
            identity: str,
            ceremony_id: str,
            *,
            committed: bool,
    ) -> bool:
        for _ in range(_CAS_RETRIES):
            account = self._read("accounts", identity, identity)
            if account is None:
                return False
            reservations = self._registration_reservations(account)
            found = False
            updated_reservations: list[dict[str, Any]] = []
            for reservation in reservations:
                if reservation["ceremony_id"] != ceremony_id:
                    updated_reservations.append(reservation)
                    continue
                found = True
                if committed:
                    updated_reservations.append(dict(reservation, reserved_until=None))
            if not found and committed:
                challenge = self._read("challenges", ceremony_id, ceremony_id)
                if challenge and challenge.get("identity") == identity:
                    updated_reservations.append(
                        {
                            "ceremony_id": ceremony_id,
                            "expires_at": challenge["expires_at"],
                            "reserved_until": None,
                        }
                    )
            updated = dict(
                account,
                registration_challenge_count=len(updated_reservations),
                registration_challenge_reservations=updated_reservations,
            )
            if self._replace("accounts", updated):
                return True
        return False

    def _release_registration_reservation(
            self,
            identity: str,
            ceremony_id: str,
    ) -> bool:
        return self._finish_registration_reservation(
            identity,
            ceremony_id,
            committed=False,
        )

    @staticmethod
    def _challenge(
            doc: Mapping[str, Any], consumed: float | None = None
    ) -> ChallengeRecord:
        return ChallengeRecord(
            doc["ceremony_id"],
            _decode_binary(doc["challenge"], "challenge", 1024),
            doc["kind"],
            doc.get("identity"),
            doc["origin"],
            doc["rp_id"],
            doc["proxy_id"],
            doc["created_at"],
            doc["expires_at"],
            doc.get("consumed_at") if consumed is None else consumed,
        )

    def claim_challenge(self, ceremony_id: str) -> ChallengeRecord | None:
        if not isinstance(ceremony_id, str) or not ceremony_id:
            return None
        doc = self._read("challenges", ceremony_id, ceremony_id)
        now = self._clock()
        if not doc or doc.get("consumed_at") is not None:
            return None
        doc["consumed_at"] = now
        if not self._replace("challenges", doc):
            return None
        if doc["kind"] == "registration" and doc.get("identity"):
            self._release_registration_reservation(
                doc["identity"],
                ceremony_id,
            )
        return self._challenge(doc, now)

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
        decoded_challenge, rp_id = _validate_challenge_inputs(
            challenge=challenge,
            kind=kind,
            identity=identity,
            origin=origin,
            rp_id=rp_id,
            proxy_id=proxy_id,
        )
        created = self._clock() if created_at is None else created_at
        encoded_challenge = (
            base64.urlsafe_b64encode(decoded_challenge).rstrip(b"=").decode()
        )
        for _ in range(3):
            ceremony = secrets.token_urlsafe(32)
            if kind == "registration":
                self._reserve_registration_challenge(
                    identity,
                    ceremony,
                    expires_at,
                )
            doc = {
                "id": ceremony,
                "pk": ceremony,
                "ceremony_id": ceremony,
                "challenge": encoded_challenge,
                "kind": kind,
                "identity": identity,
                "origin": origin,
                "rp_id": rp_id,
                "proxy_id": proxy_id,
                "created_at": created,
                "expires_at": expires_at,
                "consumed_at": None,
            }
            try:
                stored = self.containers["challenges"].create_item(body=doc)
            except Exception as exc:
                if kind == "registration":
                    self._release_registration_reservation(identity, ceremony)
                if _cosmos_status(exc) == 409:
                    continue
                raise
            if kind == "registration":
                self._finish_registration_reservation(
                    identity,
                    ceremony,
                    committed=True,
                )
            return self._challenge(stored)
        raise AuthStoreError("could not generate ceremony ID")

    def consume_rate_limit(
            self, scope: str, key: str, window_start: int, limit: int
    ) -> bool:
        """Consume one fixed-window budget using Cosmos ETag compare-and-swap."""
        scope, key, window_start, limit = _validate_rate_limit_inputs(
            scope, key, window_start, limit
        )
        digest = hashlib.sha256(f"{scope}\0{key}\0{window_start}".encode()).hexdigest()
        container = self.containers["rate_limits"]
        for _ in range(_CAS_RETRIES):
            current = self._read("rate_limits", digest, digest)
            if current is None:
                try:
                    container.create_item(
                        body={
                            "id": digest,
                            "pk": digest,
                            "scope": scope,
                            "rate_key": key,
                            "window_start": window_start,
                            "count": 1,
                            "updated_at": self._clock(),
                            "ttl": 180,
                        }
                    )
                    return True
                except Exception as exc:
                    if _cosmos_status(exc) == 409:
                        continue
                    raise
            if current.get("count", 0) >= limit:
                return False
            updated = dict(
                current,
                count=current.get("count", 0) + 1,
                updated_at=self._clock(),
                ttl=180,
            )
            if self._replace("rate_limits", updated):
                return True
        raise AuthStoreError("rate-limit update conflict")

    def reclaim_orphan_reservations(
            self,
            identity: str,
            *,
            cutoff: float,
            limit: int = 100,
            confirmed_quiesced: bool = False,
            include_committed_missing: bool = False,
            apply: bool = False,
    ) -> dict[str, Any]:
        """Inspect or reclaim old missing-document reservations while quiesced.

        Every writer replica must be stopped before calling this method. Cosmos
        cannot atomically fence a paused writer across account and credential or
        challenge containers, so confirmation is mandatory even for dry runs.
        """
        if confirmed_quiesced is not True:
            raise ValueError("all Cosmos auth writers must be confirmed quiesced")
        if not isinstance(include_committed_missing, bool):
            raise ValueError("include_committed_missing must be boolean")
        if not isinstance(identity, str) or not identity:
            raise ValueError("identity is required")
        if (
                not isinstance(cutoff, (int, float))
                or isinstance(cutoff, bool)
                or not 0 <= float(cutoff) < float("inf")
        ):
            raise ValueError("cutoff must be a finite non-negative timestamp")
        if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be between 1 and 100")
        cutoff = float(cutoff)

        for _ in range(_CAS_RETRIES):
            account = self._read("accounts", identity, identity)
            if account is None:
                raise ValueError("account does not exist")
            raw_credentials = account.get("credential_reservations")
            credential_reservations = (
                list(raw_credentials) if isinstance(raw_credentials, list) else []
            )
            raw_challenges = account.get("registration_challenge_reservations")
            challenge_reservations = (
                list(raw_challenges) if isinstance(raw_challenges, list) else []
            )

            credential_orphans: set[str] = set()
            challenge_orphans: set[str] = set()
            remaining = limit
            for reservation in credential_reservations:
                if remaining == 0 or not isinstance(reservation, Mapping):
                    continue
                credential_id = reservation.get("credential_id")
                lease_end = reservation.get("expires_at")
                eligible_by_age = (
                        isinstance(lease_end, (int, float))
                        and not isinstance(lease_end, bool)
                        and lease_end <= cutoff
                )
                eligible_committed = include_committed_missing and lease_end is None
                if (
                        not isinstance(credential_id, str)
                        or not credential_id
                        or not (eligible_by_age or eligible_committed)
                ):
                    continue
                if self._read("credentials", credential_id, credential_id) is None:
                    credential_orphans.add(credential_id)
                    remaining -= 1
            for reservation in challenge_reservations:
                if remaining == 0 or not isinstance(reservation, Mapping):
                    continue
                ceremony_id = reservation.get("ceremony_id")
                lease_end = reservation.get("reserved_until")
                eligible_by_age = (
                        isinstance(lease_end, (int, float))
                        and not isinstance(lease_end, bool)
                        and lease_end <= cutoff
                )
                eligible_committed = include_committed_missing and lease_end is None
                if (
                        not isinstance(ceremony_id, str)
                        or not ceremony_id
                        or not (eligible_by_age or eligible_committed)
                ):
                    continue
                if self._read("challenges", ceremony_id, ceremony_id) is None:
                    challenge_orphans.add(ceremony_id)
                    remaining -= 1

            report = {
                "identity": identity,
                "cutoff": cutoff,
                "apply": apply,
                "include_committed_missing": include_committed_missing,
                "credential_orphans": len(credential_orphans),
                "challenge_orphans": len(challenge_orphans),
                "reclaimed": 0,
            }
            if not apply or not (credential_orphans or challenge_orphans):
                return report

            updated_credentials = [
                reservation
                for reservation in credential_reservations
                if not isinstance(reservation, Mapping)
                   or reservation.get("credential_id") not in credential_orphans
            ]
            updated_challenges = [
                reservation
                for reservation in challenge_reservations
                if not isinstance(reservation, Mapping)
                   or reservation.get("ceremony_id") not in challenge_orphans
            ]
            updated = dict(
                account,
                credential_count=len(updated_credentials),
                credential_reservations=updated_credentials,
                registration_challenge_count=len(updated_challenges),
                registration_challenge_reservations=updated_challenges,
            )
            if self._replace("accounts", updated):
                report["reclaimed"] = len(credential_orphans) + len(challenge_orphans)
                return report
        raise AuthStoreError("orphan reservation repair conflict")

    def close(self) -> None:
        """Close only the Cosmos client explicitly owned by this adapter."""
        if self._owner is not None:
            self._owner.close()
