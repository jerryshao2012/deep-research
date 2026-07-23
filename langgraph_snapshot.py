"""Validation primitives for immutable LangGraph snapshot generations."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import pickle
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

ROOT_MANIFEST_KEY = ".langgraph_snapshots/manifest.json"
GENERATION_PREFIX = ".langgraph_snapshots/generations"
SCHEMA_VERSION = 1
RESTORE_RECEIPT_SCHEMA_VERSION = 1
AWS_IMAGE_RUNTIME_VERSIONS: Mapping[str, str] = MappingProxyType(
    {
        "python": "3.12.13",
        "langgraph": "1.2.6",
        "langgraph-api": "0.10.0",
        "langgraph-runtime-inmem": "0.30.0",
    }
)

_VALID_RUN_STATUSES = frozenset(
    {"pending", "running", "error", "success", "timeout", "interrupted"}
)


class SnapshotValidationError(RuntimeError):
    """Raised when a LangGraph snapshot is unsafe to publish or restore."""


class SnapshotPublishError(RuntimeError):
    """Raised when a snapshot generation cannot be safely published."""


class SnapshotRestoreError(RuntimeError):
    """Raised when no committed snapshot generation can be safely restored."""


class SnapshotConflictError(SnapshotPublishError):
    """Raised when a conditional root-pointer update loses an S3 race."""


@dataclass
class RuntimeLease:
    """Mutable process-local view of the currently claimed S3 writer lease."""

    writer_epoch: str
    etag: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    fenced: threading.Event = field(default_factory=threading.Event)
    stop_event: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class RestoreReceipt:
    """Local proof that startup installed one exact committed S3 generation."""

    bucket: str
    target_dir: str
    restored_generation: str
    root_etag: str
    root_pointer: Mapping[str, object]

    def __post_init__(self) -> None:
        """Freeze a defensive copy of the restored root pointer."""
        object.__setattr__(
            self,
            "root_pointer",
            MappingProxyType(dict(self.root_pointer)),
        )


@dataclass(frozen=True)
class FileMetadata:
    """Integrity metadata for one snapshot pickle."""

    size: int
    sha256: str


@dataclass(frozen=True)
class GenerationMetadata:
    """Validated metadata describing one immutable snapshot generation."""

    generation: str
    created_at: str
    thread_ids: tuple[str, ...]
    thread_versions: Mapping[str, str]
    runtime_versions: Mapping[str, str]
    files: Mapping[str, FileMetadata]

    def __post_init__(self) -> None:
        """Freeze defensive copies of all mapping fields."""
        object.__setattr__(
            self,
            "thread_versions",
            MappingProxyType(dict(self.thread_versions)),
        )
        object.__setattr__(
            self,
            "runtime_versions",
            MappingProxyType(dict(self.runtime_versions)),
        )
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


def _normalize_timestamp(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        if not value:
            return ""
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SnapshotValidationError(
                f"invalid thread updated_at timestamp: {value!r}"
            ) from exc
    else:
        raise SnapshotValidationError(
            f"invalid thread updated_at value: {value!r}"
        )

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


def _normalize_thread(entry: object) -> tuple[str, str]:
    if isinstance(entry, dict):
        if "thread_id" not in entry:
            raise SnapshotValidationError("thread catalog entry is missing thread_id")
        thread_id = entry["thread_id"]
        updated_at = _normalize_timestamp(entry.get("updated_at"))
    else:
        thread_id = entry
        updated_at = ""

    if not isinstance(thread_id, (str, UUID)):
        raise SnapshotValidationError(
            f"invalid thread catalog ID: {thread_id!r}"
        )
    normalized_id = str(thread_id)
    if not normalized_id:
        raise SnapshotValidationError("thread catalog contains an empty thread ID")
    return normalized_id, updated_at


def _runtime_versions() -> dict[str, str]:
    package_versions: dict[str, str] = {"python": sys.version.split()[0]}
    for package_name in (
        "langgraph",
        "langgraph-api",
        "langgraph-runtime-inmem",
    ):
        try:
            package_versions[package_name] = version(package_name)
        except PackageNotFoundError as exc:
            raise SnapshotValidationError(
                f"required runtime package is not installed: {package_name}"
            ) from exc
    return package_versions


def _assert_aws_image_runtime_compatible(
    runtime_versions: Mapping[str, str],
) -> None:
    actual = dict(runtime_versions)
    if actual != AWS_IMAGE_RUNTIME_VERSIONS:
        raise SnapshotValidationError(
            "snapshot runtime does not match pinned AWS image runtime: "
            f"expected {AWS_IMAGE_RUNTIME_VERSIONS!r}, got {actual!r}"
        )


def _file_metadata(payload: bytes) -> FileMetadata:
    return FileMetadata(
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def validate_snapshot(
    path: Path,
    *,
    require_non_empty: bool,
) -> GenerationMetadata:
    """Load and validate every trusted internal persistence pickle in ``path``.

    Pickle loading can execute code. Callers restoring remote snapshots must verify
    each file's declared size and SHA-256 before invoking this function.
    """
    snapshot_path = Path(path)
    if not snapshot_path.is_dir():
        raise SnapshotValidationError(
            f"snapshot directory does not exist: {snapshot_path}"
        )

    temporary_files = sorted(
        item
        for item in snapshot_path.rglob("*")
        if item.is_file() and item.suffix == ".tmp"
    )
    if temporary_files:
        raise SnapshotValidationError(
            f"snapshot contains .tmp file: {temporary_files[0].name}"
        )

    pickle_files = sorted(snapshot_path.glob("*.pckl"))
    ops_path = snapshot_path / ".langgraph_ops.pckl"
    if ops_path not in pickle_files:
        raise SnapshotValidationError("missing .langgraph_ops.pckl")

    ops: object | None = None
    files: dict[str, FileMetadata] = {}
    for pickle_path in pickle_files:
        try:
            payload = pickle_path.read_bytes()
            if pickle_path == ops_path:
                ops = pickle.loads(payload)
            else:
                pickle.loads(payload)
        except Exception as exc:
            raise SnapshotValidationError(
                f"unreadable pickle: {pickle_path.name}"
            ) from exc
        files[pickle_path.name] = _file_metadata(payload)

    if not isinstance(ops, dict):
        raise SnapshotValidationError(".langgraph_ops.pckl must contain a mapping")

    threads = ops.get("threads")
    if not isinstance(threads, (list, tuple, set)):
        raise SnapshotValidationError("invalid thread catalog")

    runs = ops.get("runs", [])
    if not isinstance(runs, (list, tuple)):
        raise SnapshotValidationError("invalid run catalog")
    for run in runs:
        if not isinstance(run, Mapping):
            raise SnapshotValidationError("invalid run catalog entry")
        status = run.get("status")
        if not isinstance(status, str) or status not in _VALID_RUN_STATUSES:
            raise SnapshotValidationError("invalid run catalog entry status")
        if status in {"pending", "running"}:
            raise SnapshotValidationError(
                f"snapshot contains active run: {status}"
            )

    thread_versions: dict[str, str] = {}
    for entry in threads:
        thread_id, updated_at = _normalize_thread(entry)
        if thread_id in thread_versions:
            raise SnapshotValidationError(
                f"duplicate thread catalog ID: {thread_id}"
            )
        thread_versions[thread_id] = updated_at

    thread_ids = tuple(sorted(thread_versions))
    if require_non_empty and not thread_ids:
        raise SnapshotValidationError("empty thread catalog")

    now = datetime.now(UTC)
    return GenerationMetadata(
        generation=f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid4()}",
        created_at=now.isoformat(),
        thread_ids=thread_ids,
        thread_versions={
            thread_id: thread_versions[thread_id] for thread_id in thread_ids
        },
        runtime_versions=_runtime_versions(),
        files=files,
    )


def assert_candidate_is_monotonic(
    candidate: GenerationMetadata,
    previous: GenerationMetadata,
) -> None:
    """Reject thread removal or per-thread timestamp rollback."""
    removed = sorted(set(previous.thread_ids) - set(candidate.thread_ids))
    if removed:
        raise SnapshotValidationError(
            f"candidate removed published threads: {', '.join(removed)}"
        )

    for thread_id in previous.thread_ids:
        previous_version = previous.thread_versions.get(thread_id, "")
        candidate_version = candidate.thread_versions.get(thread_id, "")
        if not previous_version:
            continue
        if not candidate_version:
            raise SnapshotValidationError(
                f"candidate has older thread timestamp for {thread_id}"
            )
        if _normalize_timestamp(candidate_version) < _normalize_timestamp(
            previous_version
        ):
            raise SnapshotValidationError(
                f"candidate has older thread timestamp for {thread_id}"
            )


@dataclass(frozen=True)
class _GenerationRecord:
    metadata: GenerationMetadata
    object_keys: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "object_keys",
            MappingProxyType(dict(self.object_keys)),
        )


@dataclass(frozen=True)
class _PreparedGeneration:
    metadata: GenerationMetadata
    previous_generation: str | None


def _s3_error_details(exc: Exception) -> tuple[str, int | None]:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return "", None
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = error.get("Code", "") if isinstance(error, Mapping) else ""
    status = (
        metadata.get("HTTPStatusCode")
        if isinstance(metadata, Mapping)
        else None
    )
    return str(code), status if isinstance(status, int) else None


def _is_missing_object(exc: Exception) -> bool:
    code, status = _s3_error_details(exc)
    return status == 404 or code in {"404", "NoSuchKey", "NotFound"}


def _is_precondition_failure(exc: Exception) -> bool:
    code, status = _s3_error_details(exc)
    return status in {409, 412} or code in {
        "409",
        "412",
        "ConditionalRequestConflict",
        "PreconditionFailed",
    }


def _read_s3_bytes(response: object) -> bytes:
    if not isinstance(response, Mapping) or "Body" not in response:
        raise SnapshotRestoreError("S3 object response is missing Body")
    body = response["Body"]
    payload = body.read() if hasattr(body, "read") else body
    if not isinstance(payload, bytes):
        raise SnapshotRestoreError("S3 object body is not bytes")
    return payload


def _parse_json_object(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotRestoreError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SnapshotRestoreError(f"{label} must be a JSON object")
    return value


def _validate_timestamp(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SnapshotRestoreError(f"{label} must be a non-empty timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotRestoreError(f"{label} has an invalid timestamp") from exc
    return value


def _validate_root_manifest(value: dict[str, object]) -> dict[str, object]:
    required = {
        "schema_version",
        "active_generation",
        "previous_generation",
        "writer_epoch",
        "created_at",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise SnapshotRestoreError(
            f"root manifest is missing required field: {missing[0]}"
        )
    schema_version = value["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise SnapshotRestoreError(
            f"root manifest has unknown schema: {schema_version!r}"
        )
    active = value["active_generation"]
    previous = value["previous_generation"]
    writer_epoch = value["writer_epoch"]
    if not isinstance(active, str) or not active:
        raise SnapshotRestoreError(
            "root manifest active_generation must be a non-empty string"
        )
    if previous is not None and (
        not isinstance(previous, str) or not previous
    ):
        raise SnapshotRestoreError(
            "root manifest previous_generation must be a string or null"
        )
    if not isinstance(writer_epoch, str) or not writer_epoch:
        raise SnapshotRestoreError(
            "root manifest writer_epoch must be a non-empty string"
        )
    _validate_timestamp(value["created_at"], label="root manifest created_at")
    return dict(value)


def load_pointer(
    client: object,
    bucket: str,
) -> tuple[dict[str, object] | None, str | None]:
    """Load and validate the committed root pointer and its S3 ETag."""
    try:
        response = client.get_object(Bucket=bucket, Key=ROOT_MANIFEST_KEY)
    except Exception as exc:
        if _is_missing_object(exc):
            return None, None
        raise SnapshotRestoreError("failed to load root manifest") from exc

    pointer = _validate_root_manifest(
        _parse_json_object(_read_s3_bytes(response), label="root manifest")
    )
    etag = response.get("ETag") if isinstance(response, Mapping) else None
    if not isinstance(etag, str) or not etag:
        raise SnapshotRestoreError("root manifest response is missing ETag")
    return pointer, etag


def claim_writer_epoch(
    client: object,
    bucket: str,
    pointer: Mapping[str, object],
    etag: str,
    epoch: str,
) -> str:
    """Claim an existing root pointer using its current ETag."""
    if not etag or not epoch:
        raise SnapshotConflictError(
            "writer claim requires a root ETag and non-empty epoch"
        )
    try:
        validated = _validate_root_manifest(dict(pointer))
    except SnapshotRestoreError as exc:
        raise SnapshotPublishError(str(exc)) from exc

    try:
        response = client.put_object(
            Bucket=bucket,
            Key=ROOT_MANIFEST_KEY,
            Body=_root_manifest_payload(
                active_generation=str(validated["active_generation"]),
                previous_generation=(
                    str(validated["previous_generation"])
                    if validated["previous_generation"] is not None
                    else None
                ),
                writer_epoch=epoch,
                created_at=datetime.now(UTC).isoformat(),
            ),
            IfMatch=etag,
        )
    except Exception as exc:
        if _is_precondition_failure(exc):
            raise SnapshotConflictError(
                "conditional writer claim failed"
            ) from exc
        raise SnapshotPublishError("writer claim failed") from exc

    claimed_etag = (
        response.get("ETag") if isinstance(response, Mapping) else None
    )
    if not isinstance(claimed_etag, str) or not claimed_etag:
        raise SnapshotPublishError("writer claim returned no ETag")
    return claimed_etag


def _generation_manifest_key(generation: str) -> str:
    return f"{GENERATION_PREFIX}/{generation}/manifest.json"


def _generation_file_key(generation: str, filename: str) -> str:
    return f"{GENERATION_PREFIX}/{generation}/{filename}"


def _validate_generation_manifest(
    value: dict[str, object],
    *,
    expected_generation: str,
) -> _GenerationRecord:
    required = {
        "schema_version",
        "generation",
        "created_at",
        "thread_ids",
        "thread_versions",
        "runtime_versions",
        "files",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise SnapshotRestoreError(
            f"generation manifest is missing required field: {missing[0]}"
        )
    schema_version = value["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise SnapshotRestoreError(
            "generation manifest has unknown schema: "
            f"{schema_version!r}"
        )
    generation = value["generation"]
    if generation != expected_generation:
        raise SnapshotRestoreError(
            "generation manifest does not match its immutable prefix"
        )
    _validate_timestamp(
        value["created_at"],
        label="generation manifest created_at",
    )

    raw_thread_ids = value["thread_ids"]
    if (
        not isinstance(raw_thread_ids, list)
        or not raw_thread_ids
        or any(not isinstance(item, str) or not item for item in raw_thread_ids)
        or len(set(raw_thread_ids)) != len(raw_thread_ids)
    ):
        raise SnapshotRestoreError(
            "generation manifest thread_ids must be unique and non-empty"
        )
    thread_ids = tuple(raw_thread_ids)
    if thread_ids != tuple(sorted(thread_ids)):
        raise SnapshotRestoreError(
            "generation manifest thread_ids must be sorted"
        )

    raw_thread_versions = value["thread_versions"]
    if not isinstance(raw_thread_versions, dict) or set(
        raw_thread_versions
    ) != set(thread_ids):
        raise SnapshotRestoreError(
            "generation manifest thread_versions do not match thread_ids"
        )
    if any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in raw_thread_versions.items()
    ):
        raise SnapshotRestoreError(
            "generation manifest thread_versions must contain strings"
        )

    raw_runtime_versions = value["runtime_versions"]
    if (
        not isinstance(raw_runtime_versions, dict)
        or not raw_runtime_versions
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(item, str)
            or not item
            for key, item in raw_runtime_versions.items()
        )
    ):
        raise SnapshotRestoreError(
            "generation manifest runtime_versions must contain strings"
        )

    raw_files = value["files"]
    if not isinstance(raw_files, dict) or not raw_files:
        raise SnapshotRestoreError(
            "generation manifest files must be non-empty"
        )
    files: dict[str, FileMetadata] = {}
    object_keys: dict[str, str] = {}
    for filename, raw_metadata in raw_files.items():
        if (
            not isinstance(filename, str)
            or not filename.endswith(".pckl")
            or Path(filename).name != filename
            or not isinstance(raw_metadata, dict)
        ):
            raise SnapshotRestoreError(
                "generation manifest contains an invalid file entry"
            )
        expected_key = _generation_file_key(expected_generation, filename)
        key = raw_metadata.get("key")
        size = raw_metadata.get("size")
        sha256 = raw_metadata.get("sha256")
        if key != expected_key:
            raise SnapshotRestoreError(
                "generation manifest file key escapes immutable prefix"
            )
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise SnapshotRestoreError(
                "generation manifest contains an invalid file size"
            )
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise SnapshotRestoreError(
                "generation manifest contains an invalid checksum"
            )
        files[filename] = FileMetadata(size=size, sha256=sha256)
        object_keys[filename] = key

    return _GenerationRecord(
        metadata=GenerationMetadata(
            generation=expected_generation,
            created_at=str(value["created_at"]),
            thread_ids=thread_ids,
            thread_versions=dict(raw_thread_versions),
            runtime_versions=dict(raw_runtime_versions),
            files=files,
        ),
        object_keys=object_keys,
    )


def _load_generation(
    client: object,
    bucket: str,
    generation: str,
) -> _GenerationRecord:
    try:
        response = client.get_object(
            Bucket=bucket,
            Key=_generation_manifest_key(generation),
        )
    except Exception as exc:
        raise SnapshotRestoreError(
            f"failed to load generation manifest for {generation}"
        ) from exc
    value = _parse_json_object(
        _read_s3_bytes(response),
        label="generation manifest",
    )
    return _validate_generation_manifest(
        value,
        expected_generation=generation,
    )


def _generation_manifest_payload(metadata: GenerationMetadata) -> bytes:
    files = {
        filename: {
            "key": _generation_file_key(metadata.generation, filename),
            "size": file_metadata.size,
            "sha256": file_metadata.sha256,
        }
        for filename, file_metadata in metadata.files.items()
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generation": metadata.generation,
        "created_at": metadata.created_at,
        "thread_ids": list(metadata.thread_ids),
        "thread_versions": dict(metadata.thread_versions),
        "runtime_versions": dict(metadata.runtime_versions),
        "files": files,
    }
    return json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _root_manifest_payload(
    *,
    active_generation: str,
    previous_generation: str | None,
    writer_epoch: str,
    created_at: str,
) -> bytes:
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "active_generation": active_generation,
        "previous_generation": previous_generation,
        "writer_epoch": writer_epoch,
        "created_at": created_at,
    }
    return json.dumps(
        pointer,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


_SnapshotFingerprint = tuple[tuple[str, int, str], ...]


def _snapshot_fingerprint(source_dir: Path) -> _SnapshotFingerprint:
    source = Path(source_dir)
    if not source.is_dir():
        raise SnapshotPublishError(
            f"snapshot directory does not exist: {source}"
        )
    temporary_files = sorted(
        item
        for item in source.rglob("*")
        if item.is_file() and item.suffix == ".tmp"
    )
    if temporary_files:
        raise SnapshotPublishError(
            f"snapshot contains .tmp file: {temporary_files[0].name}"
        )

    fingerprint: list[tuple[str, int, str]] = []
    for pickle_path in sorted(source.glob("*.pckl")):
        try:
            payload = pickle_path.read_bytes()
        except OSError as exc:
            raise SnapshotPublishError(
                f"failed to fingerprint snapshot file: {pickle_path.name}"
            ) from exc
        fingerprint.append(
            (
                pickle_path.name,
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
        )
    return tuple(fingerprint)


def _stage_snapshot_for_publish(source_dir: Path) -> Path:
    source = Path(source_dir)
    before = _snapshot_fingerprint(source)
    try:
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{source.name}.publish-",
                dir=source.parent,
            )
        )
        for filename, _size, _digest in before:
            shutil.copy2(source / filename, staging / filename)
    except OSError as exc:
        if "staging" in locals():
            shutil.rmtree(staging, ignore_errors=True)
        raise SnapshotPublishError(
            "failed to create stable snapshot staging"
        ) from exc
    try:
        after = _snapshot_fingerprint(source)
        staged = _snapshot_fingerprint(staging)
        if before != after or before != staged:
            raise SnapshotPublishError(
                "snapshot changed during staging"
            )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _prepare_generation(
    client: object,
    bucket: str,
    source_dir: Path,
    expected_etag: str | None,
    writer_epoch: str,
    allow_shrink: bool = False,
) -> _PreparedGeneration:
    """Validate and upload an immutable generation without moving the pointer."""
    if not writer_epoch:
        raise SnapshotPublishError("writer_epoch must be non-empty")
    staging = _stage_snapshot_for_publish(Path(source_dir))
    try:
        try:
            candidate = validate_snapshot(
                staging,
                require_non_empty=True,
            )
            _assert_aws_image_runtime_compatible(candidate.runtime_versions)
        except SnapshotValidationError as exc:
            raise SnapshotPublishError(str(exc)) from exc

        try:
            pointer, current_etag = load_pointer(client, bucket)
        except SnapshotRestoreError as exc:
            raise SnapshotPublishError(str(exc)) from exc

        previous_generation: str | None = None
        if pointer is not None:
            if expected_etag is None:
                raise SnapshotConflictError(
                    "existing root manifest requires an expected ETag"
                )
            if current_etag != expected_etag:
                raise SnapshotConflictError(
                    "stale expected ETag for root manifest"
                )
            if pointer["writer_epoch"] != writer_epoch:
                raise SnapshotConflictError(
                    "root manifest writer epoch does not match publisher"
                )
            previous_generation = str(pointer["active_generation"])
            try:
                previous = _load_generation(
                    client,
                    bucket,
                    previous_generation,
                ).metadata
                if not allow_shrink:
                    assert_candidate_is_monotonic(candidate, previous)
            except (SnapshotRestoreError, SnapshotValidationError) as exc:
                raise SnapshotPublishError(str(exc)) from exc
        elif expected_etag is not None:
            raise SnapshotConflictError(
                "expected ETag supplied for missing root manifest"
            )

        for filename in sorted(candidate.files):
            source_path = staging / filename
            key = _generation_file_key(candidate.generation, filename)
            try:
                client.upload_file(str(source_path), bucket, key)
            except Exception as exc:
                raise SnapshotPublishError(
                    f"generation file upload failed: {filename}"
                ) from exc

        try:
            client.put_object(
                Bucket=bucket,
                Key=_generation_manifest_key(candidate.generation),
                Body=_generation_manifest_payload(candidate),
                IfNoneMatch="*",
            )
        except Exception as exc:
            if _is_precondition_failure(exc):
                raise SnapshotConflictError(
                    "immutable generation manifest already exists"
                ) from exc
            raise SnapshotPublishError("generation manifest upload failed") from exc

        return _PreparedGeneration(
            metadata=candidate,
            previous_generation=previous_generation,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _commit_prepared_generation(
    client: object,
    bucket: str,
    prepared: _PreparedGeneration,
    expected_etag: str | None,
    writer_epoch: str,
) -> str:
    """Verify ownership and conditionally commit one uploaded generation."""
    if not writer_epoch:
        raise SnapshotPublishError("writer_epoch must be non-empty")
    try:
        pointer, current_etag = load_pointer(client, bucket)
    except SnapshotRestoreError as exc:
        raise SnapshotPublishError(str(exc)) from exc

    if pointer is None:
        if expected_etag is not None or prepared.previous_generation is not None:
            raise SnapshotConflictError(
                "root manifest disappeared before generation commit"
            )
    else:
        if expected_etag is None or current_etag != expected_etag:
            raise SnapshotConflictError(
                "stale expected ETag for root manifest"
            )
        if pointer["writer_epoch"] != writer_epoch:
            raise SnapshotConflictError(
                "root manifest writer epoch does not match publisher"
            )
        if pointer["active_generation"] != prepared.previous_generation:
            raise SnapshotConflictError(
                "active generation changed before generation commit"
            )

    pointer_kwargs: dict[str, object] = {
        "Bucket": bucket,
        "Key": ROOT_MANIFEST_KEY,
        "Body": _root_manifest_payload(
            active_generation=prepared.metadata.generation,
            previous_generation=prepared.previous_generation,
            writer_epoch=writer_epoch,
            created_at=prepared.metadata.created_at,
        ),
    }
    if expected_etag is None:
        pointer_kwargs["IfNoneMatch"] = "*"
    else:
        pointer_kwargs["IfMatch"] = expected_etag
    try:
        response = client.put_object(**pointer_kwargs)
    except Exception as exc:
        if _is_precondition_failure(exc):
            raise SnapshotConflictError(
                "conditional root manifest update failed"
            ) from exc
        raise SnapshotPublishError("root manifest upload failed") from exc

    etag = response.get("ETag") if isinstance(response, Mapping) else None
    if not isinstance(etag, str) or not etag:
        raise SnapshotPublishError("root manifest update returned no ETag")
    return etag


def publish_generation(
    client: object,
    bucket: str,
    source_dir: Path,
    expected_etag: str | None,
    writer_epoch: str,
    allow_shrink: bool = False,
) -> str:
    """Publish one immutable generation, committing its root pointer last."""
    prepared = _prepare_generation(
        client,
        bucket,
        Path(source_dir),
        expected_etag,
        writer_epoch,
        allow_shrink,
    )
    return _commit_prepared_generation(
        client,
        bucket,
        prepared,
        expected_etag,
        writer_epoch,
    )


def _fence_lease(
    lease: RuntimeLease,
    terminate: Callable[[int], object],
) -> None:
    lease.fenced.set()
    lease.stop_event.set()
    terminate(2)


def _validate_stability_seconds(stability_seconds: float) -> float:
    if (
        isinstance(stability_seconds, bool)
        or not isinstance(stability_seconds, (int, float))
        or not math.isfinite(stability_seconds)
    ):
        raise ValueError("stability_seconds must be a finite number")
    if stability_seconds < 12.0:
        raise ValueError("stability_seconds must be at least 12 seconds")
    return float(stability_seconds)


def _validate_positive_interval(value: float, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be a finite number")
    if value <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return float(value)


def _loop_wait(
    lease: RuntimeLease,
    interval_seconds: float,
    *,
    sleep: Callable[[float], object],
    should_stop: Callable[[], bool],
    custom_stop: bool,
) -> bool:
    """Wait interruptibly in production while preserving deterministic test clocks."""
    if sleep is time.sleep and not custom_stop:
        lease.stop_event.wait(interval_seconds)
    else:
        sleep(interval_seconds)
    return should_stop()


def run_snapshot_publisher(
    client: object,
    bucket: str,
    source_dir: Path,
    lease: RuntimeLease,
    *,
    stability_seconds: float = 12.0,
    scan_interval_seconds: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    stop: Callable[[], bool] | None = None,
    terminate: Callable[[int], object] = os._exit,
) -> None:
    """Run the guarded publisher loop using injectable timing primitives."""
    stability_seconds = _validate_stability_seconds(stability_seconds)
    scan_interval_seconds = _validate_positive_interval(
        scan_interval_seconds,
        label="scan_interval_seconds",
    )
    should_stop = stop or (
        lambda: lease.stop_event.is_set() or lease.fenced.is_set()
    )

    def wait_for_next_scan() -> bool:
        return _loop_wait(
            lease,
            scan_interval_seconds,
            sleep=sleep,
            should_stop=should_stop,
            custom_stop=stop is not None,
        )

    observed: _SnapshotFingerprint | None = None
    observed_at = 0.0
    published: _SnapshotFingerprint | None = None

    while not should_stop():
        try:
            current = _snapshot_fingerprint(Path(source_dir))
        except SnapshotPublishError as exc:
            logging.warning("snapshot fingerprint deferred: %s", exc)
            observed = None
            if wait_for_next_scan():
                return
            continue

        now = clock()
        if current != observed:
            observed = current
            observed_at = now
            if wait_for_next_scan():
                return
            continue
        if now - observed_at < stability_seconds or current == published:
            if wait_for_next_scan():
                return
            continue

        try:
            prepared = _prepare_generation(
                client,
                bucket,
                Path(source_dir),
                lease.etag,
                lease.writer_epoch,
            )
        except SnapshotConflictError as exc:
            logging.error("snapshot publisher lost preparation fence: %s", exc)
            _fence_lease(lease, terminate)
            return
        except SnapshotPublishError as exc:
            logging.warning("snapshot publication deferred: %s", exc)
            if wait_for_next_scan():
                return
            continue

        publish_succeeded = False
        committed_etag: str | None = None
        with lease.lock:
            if lease.fenced.is_set() or should_stop():
                return
            try:
                new_etag = _commit_prepared_generation(
                    client,
                    bucket,
                    prepared,
                    lease.etag,
                    lease.writer_epoch,
                )
            except SnapshotConflictError as exc:
                logging.error("snapshot publisher lost root CAS: %s", exc)
                _fence_lease(lease, terminate)
                return
            except SnapshotPublishError as exc:
                logging.warning("snapshot publication deferred: %s", exc)
            else:
                lease.etag = new_etag
                committed_etag = new_etag
                publish_succeeded = True

        if committed_etag is not None:
            try:
                prune_generations(
                    client,
                    bucket,
                    expected_etag=committed_etag,
                    writer_epoch=lease.writer_epoch,
                )
            except SnapshotPublishError as exc:
                logging.warning(
                    "snapshot retention deferred after commit: %s",
                    exc,
                )

        if publish_succeeded:
            published = current
        if wait_for_next_scan():
            return


def start_snapshot_publisher(
    client: object,
    bucket: str,
    source_dir: Path,
    lease: RuntimeLease,
    *,
    stability_seconds: float = 12.0,
    scan_interval_seconds: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    terminate: Callable[[int], object] = os._exit,
) -> threading.Thread:
    """Start the guarded publisher as a daemon thread."""
    stability_seconds = _validate_stability_seconds(stability_seconds)
    scan_interval_seconds = _validate_positive_interval(
        scan_interval_seconds,
        label="scan_interval_seconds",
    )
    publisher = threading.Thread(
        target=run_snapshot_publisher,
        kwargs={
            "client": client,
            "bucket": bucket,
            "source_dir": Path(source_dir),
            "lease": lease,
            "stability_seconds": stability_seconds,
            "scan_interval_seconds": scan_interval_seconds,
            "clock": clock,
            "sleep": sleep,
            "terminate": terminate,
        },
        name="langgraph-snapshot-publisher",
        daemon=True,
    )
    publisher.start()
    lease.threads.append(publisher)
    return publisher


def run_fence_monitor(
    client: object,
    bucket: str,
    lease: RuntimeLease,
    *,
    interval_seconds: float = 2.0,
    sleep: Callable[[float], object] = time.sleep,
    stop: Callable[[], bool] | None = None,
    terminate: Callable[[int], object] = os._exit,
) -> None:
    """Monitor the root pointer until stopped or the writer loses its fence."""
    interval_seconds = _validate_positive_interval(
        interval_seconds,
        label="fence_interval_seconds",
    )
    should_stop = stop or (
        lambda: lease.stop_event.is_set() or lease.fenced.is_set()
    )

    def wait_for_next_check() -> bool:
        return _loop_wait(
            lease,
            interval_seconds,
            sleep=sleep,
            should_stop=should_stop,
            custom_stop=stop is not None,
        )

    while not should_stop():
        with lease.lock:
            if should_stop():
                return
            try:
                pointer, remote_etag = load_pointer(client, bucket)
            except SnapshotRestoreError as exc:
                logging.warning("snapshot fence check deferred: %s", exc)
            else:
                if (
                    pointer is None
                    or remote_etag != lease.etag
                    or pointer.get("writer_epoch") != lease.writer_epoch
                ):
                    logging.error("snapshot fence monitor detected lease loss")
                    _fence_lease(lease, terminate)
                    return
        if wait_for_next_check():
            return


def start_fence_monitor(
    client: object,
    bucket: str,
    lease: RuntimeLease,
    *,
    interval_seconds: float = 2.0,
    sleep: Callable[[float], object] = time.sleep,
    terminate: Callable[[int], object] = os._exit,
) -> threading.Thread:
    """Start the root-pointer fence monitor as a daemon thread."""
    interval_seconds = _validate_positive_interval(
        interval_seconds,
        label="fence_interval_seconds",
    )
    monitor = threading.Thread(
        target=run_fence_monitor,
        kwargs={
            "client": client,
            "bucket": bucket,
            "lease": lease,
            "interval_seconds": interval_seconds,
            "sleep": sleep,
            "terminate": terminate,
        },
        name="langgraph-snapshot-fence-monitor",
        daemon=True,
    )
    monitor.start()
    lease.threads.append(monitor)
    return monitor


def default_restore_receipt_path(target_dir: Path) -> Path:
    """Return receipt path paired with one local LangGraph state directory."""
    target = Path(target_dir)
    return target.parent / f"{target.name}.restore-receipt.json"


def _load_restore_receipt(
    path: Path,
    *,
    bucket: str,
    source_dir: Path,
) -> RestoreReceipt:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SnapshotPublishError(
            f"required restore receipt is missing: {path}"
        ) from exc
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotPublishError("restore receipt is malformed") from exc
    if not isinstance(raw, dict):
        raise SnapshotPublishError("restore receipt is malformed")
    required = {
        "schema_version",
        "bucket",
        "target_dir",
        "restored_generation",
        "root_manifest_key",
        "root_etag",
        "root_pointer",
    }
    if required - raw.keys():
        raise SnapshotPublishError("restore receipt is malformed")
    if (
        raw["schema_version"] != RESTORE_RECEIPT_SCHEMA_VERSION
        or isinstance(raw["schema_version"], bool)
        or raw["root_manifest_key"] != ROOT_MANIFEST_KEY
        or not isinstance(raw["bucket"], str)
        or not isinstance(raw["target_dir"], str)
        or not isinstance(raw["restored_generation"], str)
        or not raw["restored_generation"]
        or not isinstance(raw["root_etag"], str)
        or not raw["root_etag"]
        or not isinstance(raw["root_pointer"], dict)
    ):
        raise SnapshotPublishError("restore receipt is malformed")
    if raw["bucket"] != bucket:
        raise SnapshotPublishError(
            "restore receipt bucket does not match runtime"
        )
    if raw["target_dir"] != str(Path(source_dir).resolve()):
        raise SnapshotPublishError(
            "restore receipt target does not match runtime source"
        )
    if (
        not Path(source_dir).is_dir()
        or not (Path(source_dir) / ".langgraph_ops.pckl").is_file()
    ):
        raise SnapshotPublishError(
            "restore receipt target is not an installed LangGraph snapshot"
        )
    try:
        pointer = _validate_root_manifest(raw["root_pointer"])
    except SnapshotRestoreError as exc:
        raise SnapshotPublishError(
            f"restore receipt contains an invalid root pointer: {exc}"
        ) from exc
    if raw["restored_generation"] not in {
        pointer["active_generation"],
        pointer["previous_generation"],
    }:
        raise SnapshotPublishError(
            "restore receipt generation is not referenced by its root pointer"
        )
    return RestoreReceipt(
        bucket=raw["bucket"],
        target_dir=raw["target_dir"],
        restored_generation=raw["restored_generation"],
        root_etag=raw["root_etag"],
        root_pointer=pointer,
    )


def _read_only_enabled(read_only: bool | None) -> bool:
    if read_only is not None:
        return read_only
    return os.getenv("LANGGRAPH_S3_READ_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _environment_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise SnapshotPublishError(
            f"{name} must be a number"
        ) from exc


def start_runtime_controller(
    client: object | None = None,
    bucket: str | None = None,
    source_dir: Path = Path(".langgraph_api"),
    *,
    read_only: bool | None = None,
    stability_seconds: float | None = None,
    fence_interval_seconds: float | None = None,
    scan_interval_seconds: float | None = None,
    restore_receipt_path: Path | None = None,
    require_restore_receipt: bool | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    terminate: Callable[[int], object] = os._exit,
) -> RuntimeLease | None:
    """Claim the writer epoch before starting publisher and fence threads."""
    if _read_only_enabled(read_only):
        return None

    resolved_stability_seconds = _validate_stability_seconds(
        stability_seconds
        if stability_seconds is not None
        else _environment_float(
            "LANGGRAPH_SNAPSHOT_STABILITY_SECONDS",
            12.0,
        )
    )
    resolved_scan_interval_seconds = _validate_positive_interval(
        scan_interval_seconds
        if scan_interval_seconds is not None
        else _environment_float(
            "LANGGRAPH_SNAPSHOT_SCAN_INTERVAL_SECONDS",
            2.0,
        ),
        label="scan_interval_seconds",
    )
    resolved_fence_interval_seconds = _validate_positive_interval(
        fence_interval_seconds
        if fence_interval_seconds is not None
        else _environment_float("LANGGRAPH_FENCE_INTERVAL_SECONDS", 2.0),
        label="fence_interval_seconds",
    )
    resolved_bucket = bucket or os.getenv("S3_BUCKET_NAME")
    if not resolved_bucket:
        raise SnapshotPublishError("S3_BUCKET_NAME is required")
    resolved_source_dir = Path(source_dir)
    receipt_required = (
        require_restore_receipt
        if require_restore_receipt is not None
        else bool(
            os.getenv("S3_BUCKET_NAME")
            and os.getenv("AWS_REGION")
        )
    )
    receipt: RestoreReceipt | None = None
    receipt_path: Path | None = None
    if receipt_required:
        receipt_path = (
            Path(restore_receipt_path)
            if restore_receipt_path is not None
            else default_restore_receipt_path(resolved_source_dir)
        )
        receipt = _load_restore_receipt(
            receipt_path,
            bucket=resolved_bucket,
            source_dir=resolved_source_dir,
        )
    resolved_client = client if client is not None else _build_s3_client()
    try:
        pointer, etag = load_pointer(resolved_client, resolved_bucket)
    except SnapshotRestoreError as exc:
        raise SnapshotPublishError(str(exc)) from exc
    if pointer is None or etag is None:
        raise SnapshotPublishError(
            "root manifest must exist before claiming a runtime writer"
        )
    if receipt is not None and (
        etag != receipt.root_etag
        or pointer != dict(receipt.root_pointer)
    ):
        raise SnapshotConflictError(
            "root pointer changed since restore; refusing writer claim"
        )
    if (
        receipt is not None
        and receipt.restored_generation != pointer["active_generation"]
    ):
        raise SnapshotConflictError(
            "fallback restored generation is not active; refusing writer claim"
        )

    writer_epoch = str(uuid4())
    claimed_etag = claim_writer_epoch(
        resolved_client,
        resolved_bucket,
        pointer,
        etag,
        writer_epoch,
    )
    if receipt_path is not None:
        try:
            receipt_path.unlink()
        except OSError as exc:
            raise SnapshotPublishError(
                "failed to consume restore receipt after writer claim"
            ) from exc
    lease = RuntimeLease(writer_epoch=writer_epoch, etag=claimed_etag)
    try:
        start_snapshot_publisher(
            resolved_client,
            resolved_bucket,
            resolved_source_dir,
            lease,
            stability_seconds=resolved_stability_seconds,
            scan_interval_seconds=resolved_scan_interval_seconds,
            clock=clock,
            sleep=sleep,
            terminate=terminate,
        )
        start_fence_monitor(
            resolved_client,
            resolved_bucket,
            lease,
            interval_seconds=resolved_fence_interval_seconds,
            sleep=sleep,
            terminate=terminate,
        )
    except Exception:
        lease.stop_event.set()
        for thread in lease.threads:
            thread.join(timeout=0.1)
        raise
    return lease


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _safe_os_error_detail(error: Exception) -> str:
    """Return useful local filesystem diagnostics without exposing path values."""
    error_type = type(error).__name__
    if not isinstance(error, OSError):
        return error_type
    if error.errno is not None and error.strerror:
        return f"{error_type}: [Errno {error.errno}] {error.strerror}"
    return f"{error_type}: operating system error"


def _install_staging(staging: Path, target: Path) -> None:
    backup = target.parent / f".{target.name}.backup-{uuid4()}"
    moved_existing = False
    try:
        if target.exists() or target.is_symlink():
            target.rename(backup)
            moved_existing = True
        staging.rename(target)
    except Exception as exc:
        rollback_error: Exception | None = None
        if moved_existing and not target.exists():
            try:
                backup.rename(target)
            except Exception as rollback_exc:
                rollback_error = rollback_exc
        if rollback_error is not None:
            raise SnapshotRestoreError(
                "snapshot install failed "
                f"({_safe_os_error_detail(exc)}); rollback failed "
                f"({_safe_os_error_detail(rollback_error)})"
            ) from rollback_error
        raise SnapshotRestoreError(
            f"snapshot install failed ({_safe_os_error_detail(exc)})"
        ) from exc
    else:
        if moved_existing:
            try:
                _remove_path(backup)
            except OSError:
                pass


def _verify_staged_files(staging: Path, record: _GenerationRecord) -> None:
    for filename, expected in record.metadata.files.items():
        try:
            payload = (staging / filename).read_bytes()
        except OSError as exc:
            raise SnapshotRestoreError(
                f"failed to read staged snapshot file: {filename}"
            ) from exc
        if len(payload) != expected.size:
            raise SnapshotRestoreError(
                f"snapshot checksum verification failed for {filename}: size mismatch"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected.sha256:
            raise SnapshotRestoreError(
                f"snapshot checksum verification failed for {filename}"
            )


def _restore_generation(
    client: object,
    bucket: str,
    generation: str,
    target: Path,
) -> GenerationMetadata:
    record = _load_generation(client, bucket, generation)
    try:
        current_runtime = _runtime_versions()
    except SnapshotValidationError as exc:
        raise SnapshotRestoreError(str(exc)) from exc
    if dict(record.metadata.runtime_versions) != current_runtime:
        raise SnapshotRestoreError(
            f"runtime version mismatch for generation {generation}"
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{target.name}.restore-",
                dir=target.parent,
            )
        )
    except OSError as exc:
        raise SnapshotRestoreError(
            "failed to create restore staging directory"
        ) from exc
    try:
        for filename, key in record.object_keys.items():
            try:
                client.download_file(
                    bucket,
                    key,
                    str(staging / filename),
                )
            except Exception as exc:
                raise SnapshotRestoreError(
                    f"failed to download generation file: {filename}"
                ) from exc
        _verify_staged_files(staging, record)
        try:
            validated = validate_snapshot(
                staging,
                require_non_empty=True,
            )
        except SnapshotValidationError as exc:
            raise SnapshotRestoreError(
                f"staged snapshot validation failed: {exc}"
            ) from exc
        if (
            validated.thread_ids != record.metadata.thread_ids
            or dict(validated.thread_versions)
            != dict(record.metadata.thread_versions)
            or dict(validated.files) != dict(record.metadata.files)
        ):
            raise SnapshotRestoreError(
                "staged snapshot does not match generation manifest"
            )
        _install_staging(staging, target)
        return record.metadata
    finally:
        try:
            _remove_path(staging)
        except OSError:
            pass


def _list_object_keys(
    client: object,
    bucket: str,
    prefix: str,
) -> list[str]:
    keys: list[str] = []
    continuation: str | None = None
    while True:
        request: dict[str, object] = {
            "Bucket": bucket,
            "Prefix": prefix,
        }
        if continuation is not None:
            request["ContinuationToken"] = continuation
        try:
            response = client.list_objects_v2(**request)
        except Exception as exc:
            raise SnapshotRestoreError(
                f"failed to list canonical snapshot prefix: {prefix}"
            ) from exc
        if not isinstance(response, Mapping):
            raise SnapshotRestoreError(
                "invalid canonical snapshot listing response"
            )
        contents = response.get("Contents", [])
        if not isinstance(contents, list):
            raise SnapshotRestoreError(
                "invalid canonical snapshot listing contents"
            )
        for item in contents:
            if isinstance(item, Mapping) and isinstance(item.get("Key"), str):
                keys.append(item["Key"])
        if not response.get("IsTruncated"):
            return keys
        next_token = response.get("NextContinuationToken")
        if not isinstance(next_token, str) or not next_token:
            raise SnapshotRestoreError(
                "canonical snapshot listing omitted continuation token"
            )
        continuation = next_token


def prune_generations(
    client: object,
    bucket: str,
    *,
    keep_recent: int = 5,
    expected_etag: str | None = None,
    writer_epoch: str | None = None,
) -> None:
    """Delete unprotected generations after a successful pointer commit."""
    if keep_recent < 0:
        raise ValueError("keep_recent must be non-negative")

    def load_retention_pointer(
        required_etag: str | None,
        required_epoch: str | None,
    ) -> tuple[dict[str, object], str]:
        try:
            current_pointer, current_etag = load_pointer(client, bucket)
        except SnapshotRestoreError as exc:
            raise SnapshotPublishError(str(exc)) from exc
        if current_pointer is None or current_etag is None:
            raise SnapshotConflictError(
                "snapshot retention lease disappeared"
            )
        if (
            required_etag is not None
            and current_etag != required_etag
        ) or (
            required_epoch is not None
            and current_pointer["writer_epoch"] != required_epoch
        ):
            raise SnapshotConflictError(
                "snapshot retention lease changed"
            )
        return current_pointer, current_etag

    pointer, pointer_etag = load_retention_pointer(
        expected_etag,
        writer_epoch,
    )
    pointer_epoch = str(pointer["writer_epoch"])
    try:
        keys = _list_object_keys(
            client,
            bucket,
            f"{GENERATION_PREFIX}/",
        )
    except SnapshotRestoreError as exc:
        raise SnapshotPublishError(str(exc)) from exc

    manifests: list[GenerationMetadata] = []
    manifest_suffix = "/manifest.json"
    for key in keys:
        if not key.endswith(manifest_suffix):
            continue
        relative = key.removeprefix(f"{GENERATION_PREFIX}/")
        generation = relative.removesuffix(manifest_suffix)
        if not generation or "/" in generation:
            continue
        try:
            manifests.append(
                _load_generation(client, bucket, generation).metadata
            )
        except SnapshotRestoreError:
            continue

    def created_at_sort_key(metadata: GenerationMetadata) -> datetime:
        timestamp = datetime.fromisoformat(
            metadata.created_at.replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)

    manifests.sort(key=created_at_sort_key)
    pointer, _fresh_etag = load_retention_pointer(
        pointer_etag,
        pointer_epoch,
    )
    protected = {
        str(pointer["active_generation"]),
        *(
            [str(pointer["previous_generation"])]
            if pointer["previous_generation"] is not None
            else []
        ),
    }
    if keep_recent:
        protected.update(
            metadata.generation for metadata in manifests[-keep_recent:]
        )

    delete_keys: list[str] = []
    for key in keys:
        relative = key.removeprefix(f"{GENERATION_PREFIX}/")
        generation = relative.split("/", 1)[0]
        if generation and generation not in protected:
            delete_keys.append(key)

    for offset in range(0, len(delete_keys), 1000):
        batch = delete_keys[offset : offset + 1000]
        fresh_pointer, _fresh_etag = load_retention_pointer(
            pointer_etag,
            pointer_epoch,
        )
        fresh_protected = {
            str(fresh_pointer["active_generation"]),
            *(
                [str(fresh_pointer["previous_generation"])]
                if fresh_pointer["previous_generation"] is not None
                else []
            ),
        }
        batch = [
            key
            for key in batch
            if key.removeprefix(f"{GENERATION_PREFIX}/").split("/", 1)[0]
            not in fresh_protected
        ]
        if not batch:
            continue
        try:
            response = client.delete_objects(
                Bucket=bucket,
                Delete={
                    "Objects": [{"Key": key} for key in batch],
                    "Quiet": True,
                },
            )
        except Exception as exc:
            raise SnapshotPublishError(
                "failed to prune snapshot generations"
            ) from exc
        if not isinstance(response, Mapping):
            raise SnapshotPublishError(
                "snapshot generation delete returned errors"
            )
        errors = response.get("Errors", [])
        if not isinstance(errors, list) or errors:
            raise SnapshotPublishError(
                "snapshot generation delete returned errors"
            )


def _write_restore_receipt(
    path: Path,
    *,
    bucket: str,
    target: Path,
    metadata: GenerationMetadata,
    pointer: Mapping[str, object],
    etag: str,
) -> None:
    payload = {
        "schema_version": RESTORE_RECEIPT_SCHEMA_VERSION,
        "bucket": bucket,
        "target_dir": str(target.resolve()),
        "restored_generation": metadata.generation,
        "root_manifest_key": ROOT_MANIFEST_KEY,
        "root_etag": etag,
        "root_pointer": dict(pointer),
    }
    receipt_path = Path(path)
    temporary_path: Path | None = None
    try:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{receipt_path.name}.",
            dir=receipt_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as receipt_file:
            json.dump(payload, receipt_file, sort_keys=True, separators=(",", ":"))
            receipt_file.flush()
            os.fsync(receipt_file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, receipt_path)
        directory_descriptor = os.open(receipt_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise SnapshotRestoreError(
            f"failed to write restore receipt: {receipt_path}"
        ) from exc


def _migrate_canonical_snapshot(
    client: object,
    bucket: str,
    target: Path,
) -> tuple[GenerationMetadata, dict[str, object], str]:
    prefix = ".langgraph_api/"
    canonical_keys = [
        key
        for key in _list_object_keys(client, bucket, prefix)
        if key.endswith(".pckl")
        and Path(key.removeprefix(prefix)).name == key.removeprefix(prefix)
    ]
    if not canonical_keys:
        raise SnapshotRestoreError(
            "no prior valid state exists: root manifest and canonical snapshot are absent"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    canonical_staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.canonical-",
            dir=target.parent,
        )
    )
    try:
        for key in canonical_keys:
            filename = key.removeprefix(prefix)
            try:
                client.download_file(
                    bucket,
                    key,
                    str(canonical_staging / filename),
                )
            except Exception as exc:
                raise SnapshotRestoreError(
                    f"failed to download canonical snapshot file: {filename}"
                ) from exc
        try:
            new_etag = publish_generation(
                client,
                bucket,
                canonical_staging,
                expected_etag=None,
                writer_epoch="canonical-bootstrap",
            )
        except SnapshotPublishError as exc:
            raise SnapshotRestoreError(
                f"canonical snapshot migration failed: {exc}"
            ) from exc
        pointer, pointer_etag = load_pointer(client, bucket)
        if pointer is None or pointer_etag != new_etag:
            raise SnapshotRestoreError(
                "canonical snapshot migration did not commit a root manifest"
            )
        metadata = _restore_generation(
            client,
            bucket,
            str(pointer["active_generation"]),
            target,
        )
        return metadata, pointer, pointer_etag
    finally:
        _remove_path(canonical_staging)


def _restore_snapshot_with_basis(
    client: object,
    bucket: str,
    target_dir: Path,
    allow_canonical_bootstrap: bool = True,
) -> tuple[GenerationMetadata, dict[str, object], str]:
    """Restore active or previous committed generation and install atomically."""
    pointer, etag = load_pointer(client, bucket)
    target = Path(target_dir)
    if pointer is None:
        if not allow_canonical_bootstrap:
            raise SnapshotRestoreError(
                "no prior valid state exists: root manifest is absent"
            )
        return _migrate_canonical_snapshot(client, bucket, target)
    if etag is None:
        raise SnapshotRestoreError("root manifest response is missing ETag")

    generations: list[str] = [str(pointer["active_generation"])]
    previous = pointer["previous_generation"]
    if isinstance(previous, str) and previous not in generations:
        generations.append(previous)

    failures: list[str] = []
    for generation in generations:
        try:
            metadata = _restore_generation(
                client,
                bucket,
                generation,
                target,
            )
            return metadata, pointer, etag
        except SnapshotRestoreError as exc:
            failures.append(f"{generation}: {exc}")
    raise SnapshotRestoreError(
        "active and previous generations are invalid: " + "; ".join(failures)
    )


def restore_snapshot(
    client: object,
    bucket: str,
    target_dir: Path,
    allow_canonical_bootstrap: bool = True,
    *,
    receipt_path: Path | None = None,
) -> GenerationMetadata:
    """Restore committed state and optionally persist its exact pointer basis."""
    target = Path(target_dir)
    metadata, pointer, etag = _restore_snapshot_with_basis(
        client,
        bucket,
        target,
        allow_canonical_bootstrap,
    )
    if receipt_path is not None:
        _write_restore_receipt(
            Path(receipt_path),
            bucket=bucket,
            target=target,
            metadata=metadata,
            pointer=pointer,
            etag=etag,
        )
    return metadata


def _build_s3_client() -> object:
    try:
        import boto3
    except ImportError as exc:
        raise SnapshotPublishError("boto3 is required for S3 snapshots") from exc
    region = os.getenv("AWS_REGION")
    if region:
        return boto3.client("s3", region_name=region)
    return boto3.client("s3")


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m langgraph_snapshot",
        description="Restore or publish guarded LangGraph S3 snapshots.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument(
        "--target",
        type=Path,
        default=Path(".langgraph_api"),
    )
    restore.add_argument(
        "--write-receipt",
        action="store_true",
        help="write local restore basis for AWS runtime writer claim",
    )
    restore.add_argument(
        "--receipt",
        type=Path,
        help="override local restore receipt path",
    )
    publish = commands.add_parser("publish")
    publish.add_argument("--source", type=Path, required=True)
    publish.add_argument("--allow-shrink", action="store_true")
    inspect_source = commands.add_parser("inspect-source")
    inspect_source.add_argument("--source", type=Path, required=True)
    return parser


def _configure_cli_unpickle_environment() -> None:
    """Allow trusted in-memory LangGraph records to import in maintenance CLI."""
    os.environ.setdefault("DATABASE_URI", "postgres://unused")
    os.environ.setdefault("REDIS_URI", "redis://unused")


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[], object] | None = None,
) -> int:
    """Run the snapshot maintenance CLI and return a process exit code."""
    args = _cli_parser().parse_args(argv)
    _configure_cli_unpickle_environment()
    if args.command == "inspect-source":
        try:
            metadata = validate_snapshot(
                args.source,
                require_non_empty=True,
            )
            _assert_aws_image_runtime_compatible(metadata.runtime_versions)
            sys.stdout.write(
                json.dumps(
                    {
                        "thread_count": len(metadata.thread_ids),
                        "thread_ids": list(metadata.thread_ids),
                        "runtime_versions": dict(metadata.runtime_versions),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            return 0
        except SnapshotValidationError as exc:
            sys.stderr.write(f"snapshot command failed: {exc}\n")
            return 1

    bucket = os.getenv("S3_BUCKET_NAME")
    if not bucket:
        sys.stderr.write(
            "snapshot command failed: S3_BUCKET_NAME is required\n"
        )
        return 2
    try:
        client = (
            client_factory() if client_factory is not None else _build_s3_client()
        )
        if args.command == "restore":
            receipt_path = None
            if args.write_receipt or args.receipt is not None:
                receipt_path = (
                    args.receipt
                    if args.receipt is not None
                    else default_restore_receipt_path(args.target)
                )
            restore_snapshot(
                client,
                bucket,
                args.target,
                receipt_path=receipt_path,
            )
            sys.stdout.write("LangGraph snapshot restored\n")
            return 0

        writer_epoch = str(uuid4())
        pointer, etag = load_pointer(client, bucket)
        preparation_epoch = (
            str(pointer["writer_epoch"])
            if pointer is not None
            else writer_epoch
        )
        prepared = _prepare_generation(
            client,
            bucket,
            args.source,
            etag,
            preparation_epoch,
            allow_shrink=args.allow_shrink,
        )
        if pointer is not None:
            if etag is None:
                raise SnapshotPublishError(
                    "root manifest response is missing ETag"
                )
            etag = claim_writer_epoch(
                client,
                bucket,
                pointer,
                etag,
                writer_epoch,
            )
        _commit_prepared_generation(
            client,
            bucket,
            prepared,
            etag,
            writer_epoch,
        )
        sys.stdout.write("LangGraph snapshot published\n")
        return 0
    except (
        SnapshotPublishError,
        SnapshotRestoreError,
        SnapshotValidationError,
    ) as exc:
        sys.stderr.write(f"snapshot command failed: {exc}\n")
        return 1
    except Exception as exc:
        logging.error(
            "snapshot command failed with unexpected error: %s",
            type(exc).__name__,
        )
        sys.stderr.write("snapshot command failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
