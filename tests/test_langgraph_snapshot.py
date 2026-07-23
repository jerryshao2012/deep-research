from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import pickle
import shutil
import sys
import threading
from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import pytest

import langgraph_snapshot as snapshot_module
from langgraph_snapshot import (
    AWS_IMAGE_RUNTIME_VERSIONS,
    GENERATION_PREFIX,
    ROOT_MANIFEST_KEY,
    SCHEMA_VERSION,
    FileMetadata,
    GenerationMetadata,
    RuntimeLease,
    SnapshotConflictError,
    SnapshotPublishError,
    SnapshotRestoreError,
    SnapshotValidationError,
    assert_candidate_is_monotonic,
    claim_writer_epoch,
    default_restore_receipt_path,
    load_pointer,
    main,
    prune_generations,
    publish_generation,
    restore_snapshot,
    run_fence_monitor,
    run_snapshot_publisher,
    start_runtime_controller,
    start_snapshot_publisher,
    validate_snapshot,
)

UPDATED_AT = "2026-07-23T10:00:00+00:00"
BUCKET = "snapshot-bucket"


class FakeS3Error(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.calls: list[dict[str, object]] = []
        self.fail_upload_suffix: str | None = None
        self.fail_download_key: str | None = None
        self.force_root_conflict: tuple[str, int] | None = None
        self.mutate_source_on_first_upload: tuple[Path, bytes] | None = None
        self.block_next_upload: tuple[threading.Event, threading.Event] | None = None
        self.block_next_root_put: tuple[threading.Event, threading.Event] | None = None
        self.on_next_generation_list: object | None = None
        self.delete_errors: list[dict[str, object]] = []
        self._etag_sequence = 0

    def seed(self, key: str, payload: bytes) -> str:
        return self._store(key, payload)

    def _store(self, key: str, payload: bytes) -> str:
        self._etag_sequence += 1
        etag = f'"etag-{self._etag_sequence}"'
        self.objects[key] = payload
        self.etags[key] = etag
        return etag

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.calls.append({"operation": "get_object", "key": Key})
        if Bucket != BUCKET:
            raise AssertionError(f"unexpected bucket: {Bucket}")
        if Key not in self.objects:
            raise FakeS3Error("NoSuchKey", 404)
        return {
            "Body": io.BytesIO(self.objects[Key]),
            "ETag": self.etags[Key],
        }

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        IfMatch: str | None = None,
        IfNoneMatch: str | None = None,
    ) -> dict[str, str]:
        self.calls.append(
            {
                "operation": "put_object",
                "key": Key,
                "if_match": IfMatch,
                "if_none_match": IfNoneMatch,
            }
        )
        if Bucket != BUCKET:
            raise AssertionError(f"unexpected bucket: {Bucket}")
        if IfMatch is not None and self.etags.get(Key) != IfMatch:
            raise FakeS3Error("PreconditionFailed", 412)
        if IfNoneMatch == "*" and Key in self.objects:
            raise FakeS3Error("PreconditionFailed", 412)
        if Key == ROOT_MANIFEST_KEY and self.force_root_conflict is not None:
            code, status = self.force_root_conflict
            self.force_root_conflict = None
            raise FakeS3Error(code, status)
        etag = self._store(Key, bytes(Body))
        if Key == ROOT_MANIFEST_KEY and self.block_next_root_put is not None:
            started, release = self.block_next_root_put
            self.block_next_root_put = None
            started.set()
            assert release.wait(timeout=2)
        return {"ETag": etag}

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:
        self.calls.append({"operation": "upload_file", "key": Key})
        if Bucket != BUCKET:
            raise AssertionError(f"unexpected bucket: {Bucket}")
        if self.fail_upload_suffix and Key.endswith(self.fail_upload_suffix):
            raise FakeS3Error("InjectedUploadFailure", 500)
        if self.mutate_source_on_first_upload is not None:
            source_path, payload = self.mutate_source_on_first_upload
            self.mutate_source_on_first_upload = None
            source_path.write_bytes(payload)
        if self.block_next_upload is not None:
            started, release = self.block_next_upload
            self.block_next_upload = None
            started.set()
            assert release.wait(timeout=2)
        self._store(Key, Path(Filename).read_bytes())

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        self.calls.append({"operation": "download_file", "key": Key})
        if Bucket != BUCKET:
            raise AssertionError(f"unexpected bucket: {Bucket}")
        if self.fail_download_key == Key:
            raise FakeS3Error("InjectedDownloadFailure", 500)
        if Key not in self.objects:
            raise FakeS3Error("NoSuchKey", 404)
        Path(Filename).write_bytes(self.objects[Key])

    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str,
        ContinuationToken: str | None = None,
    ) -> dict[str, object]:
        self.calls.append({"operation": "list_objects_v2", "key": Prefix})
        if Bucket != BUCKET:
            raise AssertionError(f"unexpected bucket: {Bucket}")
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        if (
            Prefix == f"{GENERATION_PREFIX}/"
            and self.on_next_generation_list is not None
        ):
            callback = self.on_next_generation_list
            self.on_next_generation_list = None
            assert callable(callback)
            callback()
        offset = int(ContinuationToken or "0")
        page = keys[offset : offset + 2]
        next_offset = offset + len(page)
        result: dict[str, object] = {
            "Contents": [{"Key": key, "Size": len(self.objects[key])} for key in page],
            "IsTruncated": next_offset < len(keys),
        }
        if next_offset < len(keys):
            result["NextContinuationToken"] = str(next_offset)
        return result

    def delete_objects(
        self,
        *,
        Bucket: str,
        Delete: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append({"operation": "delete_objects", "delete": Delete})
        if Bucket != BUCKET:
            raise AssertionError(f"unexpected bucket: {Bucket}")
        if self.delete_errors:
            return {"Errors": list(self.delete_errors)}
        objects = Delete.get("Objects", [])
        assert isinstance(objects, list)
        for item in objects:
            assert isinstance(item, dict)
            key = item["Key"]
            assert isinstance(key, str)
            self.objects.pop(key, None)
            self.etags.pop(key, None)
        return {"Deleted": objects}


def thread(
    thread_id: str | UUID,
    *,
    updated_at: str | datetime = UPDATED_AT,
) -> dict[str, object]:
    return {"thread_id": thread_id, "updated_at": updated_at}


def make_snapshot(
    tmp_path: Path,
    *,
    threads: list[object],
    runs: list[object] | None = None,
) -> Path:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(parents=True)
    with (snapshot / ".langgraph_ops.pckl").open("wb") as stream:
        pickle.dump({"threads": threads, "runs": runs or []}, stream)
    with (snapshot / "checkpoints.pckl").open("wb") as stream:
        pickle.dump({"checkpoint": "readable"}, stream)
    return snapshot


def seed_canonical_snapshot(client: FakeS3Client, snapshot: Path) -> None:
    for pickle_path in sorted(snapshot.glob("*.pckl")):
        client.seed(f".langgraph_api/{pickle_path.name}", pickle_path.read_bytes())


def read_json_object(client: FakeS3Client, key: str) -> dict[str, object]:
    return json.loads(client.objects[key])


def active_generation(client: FakeS3Client) -> str:
    pointer = read_json_object(client, ROOT_MANIFEST_KEY)
    generation = pointer["active_generation"]
    assert isinstance(generation, str)
    return generation


def test_boto3_runtime_dependency_is_importable() -> None:
    boto3 = importlib.import_module("boto3")

    assert callable(boto3.client)


def generation_metadata(
    thread_ids: tuple[str, ...],
    thread_versions: dict[str, str],
) -> GenerationMetadata:
    return GenerationMetadata(
        generation="generation",
        created_at=UPDATED_AT,
        thread_ids=thread_ids,
        thread_versions=thread_versions,
        runtime_versions={},
        files={},
    )


def test_metadata_models_are_deeply_immutable() -> None:
    file_metadata = FileMetadata(size=1, sha256="digest")
    thread_versions = {"t1": UPDATED_AT}
    runtime_versions = {"python": "3.12"}
    files = {"ops.pckl": file_metadata}
    generation = GenerationMetadata(
        generation="generation",
        created_at=UPDATED_AT,
        thread_ids=("t1",),
        thread_versions=thread_versions,
        runtime_versions=runtime_versions,
        files=files,
    )

    with pytest.raises(FrozenInstanceError):
        file_metadata.size = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        generation.generation = "replacement"  # type: ignore[misc]
    with pytest.raises(TypeError):
        generation.thread_versions["t2"] = UPDATED_AT  # type: ignore[index]
    with pytest.raises(TypeError):
        generation.runtime_versions["langgraph"] = "1.0"  # type: ignore[index]
    with pytest.raises(TypeError):
        generation.files["checkpoint.pckl"] = file_metadata  # type: ignore[index]

    thread_versions["t1"] = "changed"
    runtime_versions["python"] = "changed"
    files.clear()
    assert generation.thread_versions == {"t1": UPDATED_AT}
    assert generation.runtime_versions == {"python": "3.12"}
    assert generation.files == {"ops.pckl": file_metadata}


def test_validate_snapshot_returns_thread_versions_and_checksums(
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(
        tmp_path,
        threads=[thread("t1", updated_at=UPDATED_AT)],
    )

    result = validate_snapshot(snapshot, require_non_empty=True)

    assert result.thread_ids == ("t1",)
    assert result.thread_versions == {"t1": UPDATED_AT}
    assert result.created_at.endswith("+00:00")
    assert result.generation
    assert result.runtime_versions == {
        "python": sys.version.split()[0],
        "langgraph": version("langgraph"),
        "langgraph-api": version("langgraph-api"),
        "langgraph-runtime-inmem": version("langgraph-runtime-inmem"),
    }
    assert set(result.files) == {
        ".langgraph_ops.pckl",
        "checkpoints.pckl",
    }
    for filename, metadata in result.files.items():
        payload = (snapshot / filename).read_bytes()
        assert metadata == FileMetadata(
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_validate_snapshot_reads_each_pickle_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    read_counts: Counter[str] = Counter()
    original_open = Path.open

    def counting_open(
        file_path: Path,
        *args: object,
        **kwargs: object,
    ) -> object:
        mode = kwargs.get("mode", args[0] if args else "r")
        if file_path.parent == snapshot and file_path.suffix == ".pckl" and mode == "rb":
            read_counts[file_path.name] += 1
        return original_open(file_path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", counting_open)

    validate_snapshot(snapshot, require_non_empty=True)

    assert read_counts == {
        ".langgraph_ops.pckl": 1,
        "checkpoints.pckl": 1,
    }


def test_validate_snapshot_rejects_empty_catalog_when_required(
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(tmp_path, threads=[])

    with pytest.raises(SnapshotValidationError, match="empty thread catalog"):
        validate_snapshot(snapshot, require_non_empty=True)


def test_validate_snapshot_accepts_empty_catalog_when_not_required(
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(tmp_path, threads=[])

    result = validate_snapshot(snapshot, require_non_empty=False)

    assert result.thread_ids == ()
    assert result.thread_versions == {}


def test_validate_snapshot_requires_keyword_for_non_empty_policy(
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])

    with pytest.raises(TypeError):
        validate_snapshot(snapshot, True)  # type: ignore[call-arg]


def test_validate_snapshot_rejects_unreadable_pickle(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    (snapshot / "broken.pckl").write_bytes(b"not a pickle")

    with pytest.raises(SnapshotValidationError, match="unreadable pickle"):
        validate_snapshot(snapshot, require_non_empty=True)


def test_validate_snapshot_rejects_tmp_file(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    (snapshot / ".langgraph_ops.pckl.tmp").write_bytes(b"in progress")

    with pytest.raises(SnapshotValidationError, match=r"\.tmp"):
        validate_snapshot(snapshot, require_non_empty=True)


def test_validate_snapshot_rejects_missing_ops_catalog(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    with (snapshot / "checkpoints.pckl").open("wb") as stream:
        pickle.dump({}, stream)

    with pytest.raises(SnapshotValidationError, match="missing .langgraph_ops.pckl"):
        validate_snapshot(snapshot, require_non_empty=False)


@pytest.mark.parametrize("status", ["pending", "running"])
def test_validate_snapshot_rejects_active_run(
    tmp_path: Path,
    status: str,
) -> None:
    snapshot = make_snapshot(
        tmp_path,
        threads=[thread("t1")],
        runs=[{"run_id": "r1", "status": status}],
    )

    with pytest.raises(SnapshotValidationError, match="active run"):
        validate_snapshot(snapshot, require_non_empty=True)


@pytest.mark.parametrize(
    "run",
    [
        "not-a-mapping",
        {"run_id": "r1"},
        {"run_id": "r1", "status": 1},
        {"run_id": "r1", "status": "unknown"},
    ],
)
def test_validate_snapshot_rejects_malformed_run(
    tmp_path: Path,
    run: object,
) -> None:
    snapshot = make_snapshot(
        tmp_path,
        threads=[thread("t1")],
        runs=[run],
    )

    with pytest.raises(SnapshotValidationError, match="invalid run catalog entry"):
        validate_snapshot(snapshot, require_non_empty=True)


def test_validate_snapshot_normalizes_uuid_and_dict_thread_entries(
    tmp_path: Path,
) -> None:
    uuid_id = UUID("5b9ce899-6aeb-4e95-8807-5b9ddb16dd82")
    snapshot = make_snapshot(
        tmp_path,
        threads=[
            thread("z-thread", updated_at="2026-07-23T12:00:00Z"),
            thread(uuid_id, updated_at=datetime(2026, 7, 23, 10, tzinfo=UTC)),
            "a-thread",
        ],
    )

    result = validate_snapshot(snapshot, require_non_empty=True)

    assert result.thread_ids == (
        "5b9ce899-6aeb-4e95-8807-5b9ddb16dd82",
        "a-thread",
        "z-thread",
    )
    assert result.thread_versions == {
        "5b9ce899-6aeb-4e95-8807-5b9ddb16dd82": UPDATED_AT,
        "a-thread": "",
        "z-thread": "2026-07-23T12:00:00+00:00",
    }


def test_candidate_rejects_thread_shrink() -> None:
    previous = generation_metadata(
        ("t1", "t2"),
        {"t1": UPDATED_AT, "t2": UPDATED_AT},
    )
    candidate = generation_metadata(("t1",), {"t1": UPDATED_AT})

    with pytest.raises(SnapshotValidationError, match="removed published threads"):
        assert_candidate_is_monotonic(candidate, previous)


def test_candidate_rejects_timestamp_rollback() -> None:
    previous = generation_metadata(("t1",), {"t1": UPDATED_AT})
    candidate = generation_metadata(
        ("t1",),
        {"t1": "2026-07-23T09:59:59+00:00"},
    )

    with pytest.raises(SnapshotValidationError, match="older thread timestamp"):
        assert_candidate_is_monotonic(candidate, previous)


@pytest.mark.parametrize(
    ("candidate_ids", "candidate_versions"),
    [
        (("t1",), {"t1": UPDATED_AT}),
        (
            ("t1", "t2"),
            {
                "t1": "2026-07-23T10:00:01+00:00",
                "t2": "2026-07-23T10:00:00+00:00",
            },
        ),
    ],
)
def test_candidate_allows_equal_or_newer_state(
    candidate_ids: tuple[str, ...],
    candidate_versions: dict[str, str],
) -> None:
    previous = generation_metadata(("t1",), {"t1": UPDATED_AT})
    candidate = generation_metadata(candidate_ids, candidate_versions)

    assert_candidate_is_monotonic(candidate, previous)


def test_load_pointer_returns_validated_manifest_and_etag() -> None:
    client = FakeS3Client()
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "active_generation": "generation-2",
        "previous_generation": "generation-1",
        "writer_epoch": "writer-2",
        "created_at": UPDATED_AT,
    }
    expected_etag = client.seed(ROOT_MANIFEST_KEY, json.dumps(pointer).encode())

    result, etag = load_pointer(client, BUCKET)

    assert result == pointer
    assert etag == expected_etag


def test_publish_uploads_generation_before_conditional_pointer(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])

    new_etag = publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )

    pointer = read_json_object(client, ROOT_MANIFEST_KEY)
    generation = pointer["active_generation"]
    write_calls = [
        call
        for call in client.calls
        if call["operation"] in {"upload_file", "put_object"}
    ]
    pointer_index = next(
        index
        for index, call in enumerate(write_calls)
        if call["key"] == ROOT_MANIFEST_KEY
    )
    generation_manifest_key = f"{GENERATION_PREFIX}/{generation}/manifest.json"
    generation_manifest_index = next(
        index
        for index, call in enumerate(write_calls)
        if call["key"] == generation_manifest_key
    )

    assert pointer_index == len(write_calls) - 1
    assert generation_manifest_index < pointer_index
    assert all(
        call["key"] != ROOT_MANIFEST_KEY
        for call in write_calls[:generation_manifest_index]
    )
    assert pointer["previous_generation"] is None
    assert pointer["writer_epoch"] == "writer-1"
    assert new_etag == client.etags[ROOT_MANIFEST_KEY]


def test_publish_uploads_only_staged_bytes_when_source_mutates(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    checkpoint_path = snapshot / "checkpoints.pckl"
    original_checkpoint = checkpoint_path.read_bytes()
    mutated_checkpoint = pickle.dumps({"checkpoint": "mutated during upload"})
    client.mutate_source_on_first_upload = (
        checkpoint_path,
        mutated_checkpoint,
    )

    publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    restored = tmp_path / "restored"
    result = restore_snapshot(client, BUCKET, restored)

    assert result.thread_ids == ("t1",)
    assert checkpoint_path.read_bytes() == mutated_checkpoint
    assert (restored / "checkpoints.pckl").read_bytes() == original_checkpoint
    assert not list(tmp_path.glob(".snapshot.publish-*"))


def test_partial_generation_upload_does_not_move_pointer(tmp_path: Path) -> None:
    client = FakeS3Client()
    first = make_snapshot(tmp_path / "first", threads=[thread("t1")])
    current_etag = publish_generation(
        client,
        BUCKET,
        first,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    pointer_before = client.objects[ROOT_MANIFEST_KEY]
    second = make_snapshot(
        tmp_path / "second",
        threads=[thread("t1"), thread("t2")],
    )
    client.fail_upload_suffix = "checkpoints.pckl"

    with pytest.raises(SnapshotPublishError, match="upload"):
        publish_generation(
            client,
            BUCKET,
            second,
            expected_etag=current_etag,
            writer_epoch="writer-1",
        )

    assert client.objects[ROOT_MANIFEST_KEY] == pointer_before
    assert client.etags[ROOT_MANIFEST_KEY] == current_etag


def test_stale_writer_etag_cannot_move_pointer(tmp_path: Path) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    pointer_before = client.objects[ROOT_MANIFEST_KEY]
    etag_before = client.etags[ROOT_MANIFEST_KEY]
    objects_before = set(client.objects)

    with pytest.raises(SnapshotConflictError, match="stale"):
        publish_generation(
            client,
            BUCKET,
            snapshot,
            expected_etag='"stale-etag"',
            writer_epoch="writer-1",
        )

    assert client.objects[ROOT_MANIFEST_KEY] == pointer_before
    assert client.etags[ROOT_MANIFEST_KEY] == etag_before
    assert set(client.objects) == objects_before


def test_conditional_request_conflict_409_is_snapshot_conflict(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    client.force_root_conflict = ("ConditionalRequestConflict", 409)

    with pytest.raises(SnapshotConflictError, match="conditional"):
        publish_generation(
            client,
            BUCKET,
            snapshot,
            expected_etag=None,
            writer_epoch="writer-1",
        )

    assert ROOT_MANIFEST_KEY not in client.objects


def test_empty_bucket_bootstrap_uses_if_none_match_star(tmp_path: Path) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])

    publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )

    pointer_call = next(
        call
        for call in client.calls
        if call["operation"] == "put_object"
        and call["key"] == ROOT_MANIFEST_KEY
    )
    assert pointer_call["if_match"] is None
    assert pointer_call["if_none_match"] == "*"


def test_publish_contract_accepts_positional_fencing_arguments(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])

    etag = publish_generation(client, BUCKET, snapshot, None, "writer-1")

    assert etag == client.etags[ROOT_MANIFEST_KEY]


def test_publish_enforces_monotonic_state_unless_allow_shrink(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    first = make_snapshot(
        tmp_path / "first",
        threads=[thread("t1"), thread("t2")],
    )
    current_etag = publish_generation(
        client,
        BUCKET,
        first,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    second = make_snapshot(tmp_path / "second", threads=[thread("t1")])

    with pytest.raises(SnapshotPublishError, match="removed published threads"):
        publish_generation(
            client,
            BUCKET,
            second,
            expected_etag=current_etag,
            writer_epoch="writer-1",
        )

    new_etag = publish_generation(
        client,
        BUCKET,
        second,
        expected_etag=current_etag,
        writer_epoch="writer-1",
        allow_shrink=True,
    )

    assert new_etag != current_etag


def test_canonical_prefix_is_migrated_to_first_generation(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    canonical = make_snapshot(tmp_path / "canonical", threads=[thread("t1")])
    seed_canonical_snapshot(client, canonical)
    target = tmp_path / ".langgraph_api"

    result = restore_snapshot(client, BUCKET, target)

    pointer = read_json_object(client, ROOT_MANIFEST_KEY)
    assert pointer["previous_generation"] is None
    assert pointer["active_generation"] == result.generation
    assert result.thread_ids == ("t1",)
    assert (target / ".langgraph_ops.pckl").is_file()
    assert (target / "checkpoints.pckl").is_file()


def test_prior_pointer_plus_invalid_generations_fails_closed(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    canonical = make_snapshot(tmp_path / "canonical", threads=[thread("t1")])
    seed_canonical_snapshot(client, canonical)
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "active_generation": "missing-active",
        "previous_generation": "missing-previous",
        "writer_epoch": "writer-1",
        "created_at": UPDATED_AT,
    }
    root_etag = client.seed(ROOT_MANIFEST_KEY, json.dumps(pointer).encode())

    with pytest.raises(SnapshotRestoreError, match="active and previous"):
        restore_snapshot(client, BUCKET, tmp_path / "target")

    assert client.etags[ROOT_MANIFEST_KEY] == root_etag
    assert read_json_object(client, ROOT_MANIFEST_KEY) == pointer


@pytest.mark.parametrize(
    "pointer",
    [
        {
            "schema_version": 999,
            "active_generation": "generation",
            "previous_generation": None,
            "writer_epoch": "writer-1",
            "created_at": UPDATED_AT,
        },
        {
            "schema_version": True,
            "active_generation": "generation",
            "previous_generation": None,
            "writer_epoch": "writer-1",
            "created_at": UPDATED_AT,
        },
        {
            "schema_version": SCHEMA_VERSION,
            "active_generation": "generation",
            "previous_generation": None,
            "created_at": UPDATED_AT,
        },
    ],
)
def test_unknown_or_incomplete_root_schema_is_rejected(
    pointer: dict[str, object],
) -> None:
    client = FakeS3Client()
    client.seed(ROOT_MANIFEST_KEY, json.dumps(pointer).encode())

    with pytest.raises(SnapshotRestoreError, match="root manifest"):
        load_pointer(client, BUCKET)


@pytest.mark.parametrize("invalid_schema", [999, True])
def test_unknown_generation_schema_is_rejected(
    tmp_path: Path,
    invalid_schema: object,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    generation = active_generation(client)
    manifest_key = f"{GENERATION_PREFIX}/{generation}/manifest.json"
    manifest = read_json_object(client, manifest_key)
    manifest["schema_version"] = invalid_schema
    client.seed(manifest_key, json.dumps(manifest).encode())

    with pytest.raises(SnapshotRestoreError, match="generation manifest"):
        restore_snapshot(client, BUCKET, tmp_path / "target")


def test_restore_falls_back_to_previous_generation(tmp_path: Path) -> None:
    client = FakeS3Client()
    first = make_snapshot(tmp_path / "first", threads=[thread("t1")])
    first_etag = publish_generation(
        client,
        BUCKET,
        first,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    first_generation = active_generation(client)
    second = make_snapshot(
        tmp_path / "second",
        threads=[thread("t1"), thread("t2")],
    )
    publish_generation(
        client,
        BUCKET,
        second,
        expected_etag=first_etag,
        writer_epoch="writer-1",
    )
    corrupt_key = (
        f"{GENERATION_PREFIX}/{active_generation(client)}/.langgraph_ops.pckl"
    )
    client.objects[corrupt_key] = b"corrupt"

    result = restore_snapshot(client, BUCKET, tmp_path / "target")

    assert result.generation == first_generation
    assert result.thread_ids == ("t1",)


def test_restore_read_error_is_normalized_and_falls_back_to_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    first = make_snapshot(tmp_path / "first", threads=[thread("t1")])
    first_etag = publish_generation(
        client,
        BUCKET,
        first,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    first_generation = active_generation(client)
    second = make_snapshot(
        tmp_path / "second",
        threads=[thread("t1"), thread("t2")],
    )
    publish_generation(
        client,
        BUCKET,
        second,
        expected_etag=first_etag,
        writer_epoch="writer-1",
    )
    target = tmp_path / "target"
    original_read_bytes = Path.read_bytes
    failed_once = False

    def fail_first_staged_read(path: Path) -> bytes:
        nonlocal failed_once
        if (
            not failed_once
            and path.parent.name.startswith(f".{target.name}.restore-")
        ):
            failed_once = True
            raise OSError("injected staged read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_first_staged_read)

    result = restore_snapshot(client, BUCKET, target)

    assert failed_once
    assert result.generation == first_generation
    assert result.thread_ids == ("t1",)


def test_restore_rolls_back_existing_directory_on_install_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path / "source", threads=[thread("t1")])
    publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("original")
    original_rename = Path.rename

    def fail_install(source: Path, destination: Path) -> Path:
        if source.name.startswith(f".{target.name}.restore-") and destination == target:
            raise OSError(
                18,
                "Invalid cross-device link",
                "/private/secret-source",
            )
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", fail_install)
    with pytest.raises(
        SnapshotRestoreError,
        match=(
            r"snapshot install failed "
            r"\(OSError: \[Errno 18\] Invalid cross-device link\)"
        ),
    ) as error:
        restore_snapshot(client, BUCKET, target)

    assert "secret-source" not in str(error.value)
    assert marker.read_text() == "original"
    assert sorted(path.name for path in tmp_path.iterdir() if path.name != "source") == [
        "target"
    ]


def test_backup_cleanup_failure_does_not_fail_successful_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path / "source", threads=[thread("t1")])
    publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old")
    original_rmtree = shutil.rmtree

    def fail_backup_cleanup(path: str | Path, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(f".{target.name}.backup-"):
            raise OSError("injected backup cleanup failure")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.shutil, "rmtree", fail_backup_cleanup)

    result = restore_snapshot(client, BUCKET, target)

    assert result.thread_ids == ("t1",)
    assert not (target / "old.txt").exists()
    assert (target / ".langgraph_ops.pckl").is_file()


def test_runtime_version_mismatch_is_rejected_before_unpickling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    generation = active_generation(client)
    manifest_key = f"{GENERATION_PREFIX}/{generation}/manifest.json"
    manifest = read_json_object(client, manifest_key)
    runtime_versions = manifest["runtime_versions"]
    assert isinstance(runtime_versions, dict)
    runtime_versions["python"] = "0.0-incompatible"
    client.seed(manifest_key, json.dumps(manifest).encode())
    loads_calls = 0
    original_loads = pickle.loads

    def count_loads(payload: bytes) -> object:
        nonlocal loads_calls
        loads_calls += 1
        return original_loads(payload)

    monkeypatch.setattr(pickle, "loads", count_loads)

    with pytest.raises(SnapshotRestoreError, match="runtime version"):
        restore_snapshot(client, BUCKET, tmp_path / "target")

    assert loads_calls == 0


def test_aws_image_runtime_contract_matches_snapshot_runtime() -> None:
    assert AWS_IMAGE_RUNTIME_VERSIONS == {
        "python": "3.12.13",
        "langgraph": version("langgraph"),
        "langgraph-api": version("langgraph-api"),
        "langgraph-runtime-inmem": version("langgraph-runtime-inmem"),
    }


def test_publish_rejects_runtime_that_cannot_load_in_pinned_aws_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    incompatible = {
        **AWS_IMAGE_RUNTIME_VERSIONS,
        "python": "3.12.12",
    }
    monkeypatch.setattr(snapshot_module, "_runtime_versions", lambda: incompatible)

    with pytest.raises(SnapshotPublishError, match="pinned AWS image runtime"):
        publish_generation(client, BUCKET, source, None, "writer-1")

    assert client.calls == []


def test_checksum_mismatch_is_rejected_before_unpickling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    snapshot = make_snapshot(tmp_path, threads=[thread("t1")])
    publish_generation(
        client,
        BUCKET,
        snapshot,
        expected_etag=None,
        writer_epoch="writer-1",
    )
    generation = active_generation(client)
    corrupt_key = f"{GENERATION_PREFIX}/{generation}/.langgraph_ops.pckl"
    client.objects[corrupt_key] = b"corrupt"
    loads_calls = 0

    def reject_unpickle(payload: bytes) -> object:
        nonlocal loads_calls
        loads_calls += 1
        raise AssertionError(f"unpickled unchecked bytes: {payload!r}")

    monkeypatch.setattr(pickle, "loads", reject_unpickle)

    with pytest.raises(SnapshotRestoreError, match="checksum"):
        restore_snapshot(client, BUCKET, tmp_path / "target")

    assert loads_calls == 0


def test_restore_without_pointer_or_canonical_state_fails_clearly(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()

    with pytest.raises(SnapshotRestoreError, match="no prior valid state"):
        restore_snapshot(client, BUCKET, tmp_path / "target", False)


def test_restore_receipt_fences_claim_to_exact_restored_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source_a = make_snapshot(tmp_path / "a", threads=[thread("t1")])
    etag_a = publish_generation(client, BUCKET, source_a, None, "writer-a")
    target = tmp_path / ".langgraph_api"
    receipt = default_restore_receipt_path(target)

    restored = restore_snapshot(
        client,
        BUCKET,
        target,
        receipt_path=receipt,
    )
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert restored.generation == receipt_payload["restored_generation"]
    assert receipt_payload["root_etag"] == etag_a
    assert receipt_payload["bucket"] == BUCKET
    assert receipt_payload["target_dir"] == str(target.resolve())

    source_b = make_snapshot(
        tmp_path / "b",
        threads=[thread("t1"), thread("t2")],
    )
    etag_b = publish_generation(
        client,
        BUCKET,
        source_b,
        etag_a,
        "writer-a",
    )
    pointer_before = read_json_object(client, ROOT_MANIFEST_KEY)
    root_writes_before = len(
        [
            call
            for call in client.calls
            if call["operation"] == "put_object"
            and call["key"] == ROOT_MANIFEST_KEY
        ]
    )
    monkeypatch.setattr(
        snapshot_module,
        "start_snapshot_publisher",
        lambda *args, **kwargs: pytest.fail("publisher started"),
    )
    monkeypatch.setattr(
        snapshot_module,
        "start_fence_monitor",
        lambda *args, **kwargs: pytest.fail("fence monitor started"),
    )

    with pytest.raises(SnapshotConflictError, match="changed since restore"):
        start_runtime_controller(
            client,
            BUCKET,
            target,
            read_only=False,
            restore_receipt_path=receipt,
            require_restore_receipt=True,
        )

    assert client.etags[ROOT_MANIFEST_KEY] == etag_b
    assert read_json_object(client, ROOT_MANIFEST_KEY) == pointer_before
    assert len(
        [
            call
            for call in client.calls
            if call["operation"] == "put_object"
            and call["key"] == ROOT_MANIFEST_KEY
        ]
    ) == root_writes_before
    assert receipt.is_file()


def test_fallback_restore_cannot_claim_corrupt_active_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source_a = make_snapshot(tmp_path / "a", threads=[thread("t1")])
    etag_a = publish_generation(client, BUCKET, source_a, None, "writer-a")
    source_b = make_snapshot(
        tmp_path / "b",
        threads=[thread("t1"), thread("t2")],
    )
    publish_generation(client, BUCKET, source_b, etag_a, "writer-a")
    pointer_before = read_json_object(client, ROOT_MANIFEST_KEY)
    active = str(pointer_before["active_generation"])
    previous = str(pointer_before["previous_generation"])
    client.objects[
        f"{GENERATION_PREFIX}/{active}/.langgraph_ops.pckl"
    ] = b"corrupt-active"
    target = tmp_path / ".langgraph_api"
    receipt = default_restore_receipt_path(target)

    restored = restore_snapshot(
        client,
        BUCKET,
        target,
        receipt_path=receipt,
    )
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert restored.generation == previous
    assert receipt_payload["restored_generation"] == previous
    assert receipt_payload["root_pointer"]["active_generation"] == active
    root_writes_before = len(
        [
            call
            for call in client.calls
            if call["operation"] == "put_object"
            and call["key"] == ROOT_MANIFEST_KEY
        ]
    )
    monkeypatch.setattr(
        snapshot_module,
        "start_snapshot_publisher",
        lambda *args, **kwargs: pytest.fail("publisher started"),
    )
    monkeypatch.setattr(
        snapshot_module,
        "start_fence_monitor",
        lambda *args, **kwargs: pytest.fail("fence monitor started"),
    )

    with pytest.raises(SnapshotConflictError, match="fallback.*not active"):
        start_runtime_controller(
            client,
            BUCKET,
            target,
            read_only=False,
            restore_receipt_path=receipt,
            require_restore_receipt=True,
        )

    assert read_json_object(client, ROOT_MANIFEST_KEY) == pointer_before
    assert len(
        [
            call
            for call in client.calls
            if call["operation"] == "put_object"
            and call["key"] == ROOT_MANIFEST_KEY
        ]
    ) == root_writes_before
    assert receipt.is_file()


def test_unchanged_restore_receipt_claims_and_is_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path / "source", threads=[thread("t1")])
    publish_generation(client, BUCKET, source, None, "writer-a")
    target = tmp_path / ".langgraph_api"
    receipt = default_restore_receipt_path(target)
    restore_snapshot(client, BUCKET, target, receipt_path=receipt)
    started: list[str] = []
    monkeypatch.setattr(
        snapshot_module,
        "start_snapshot_publisher",
        lambda *args, **kwargs: started.append("publisher"),
    )
    monkeypatch.setattr(
        snapshot_module,
        "start_fence_monitor",
        lambda *args, **kwargs: started.append("monitor"),
    )

    lease = start_runtime_controller(
        client,
        BUCKET,
        target,
        read_only=False,
        restore_receipt_path=receipt,
        require_restore_receipt=True,
    )

    assert lease is not None
    assert started == ["publisher", "monitor"]
    assert not receipt.exists()
    assert read_json_object(client, ROOT_MANIFEST_KEY)["writer_epoch"] == (
        lease.writer_epoch
    )


@pytest.mark.parametrize("receipt_payload", [None, b"{not-json"])
def test_required_restore_receipt_missing_or_malformed_fails_before_s3(
    tmp_path: Path,
    receipt_payload: bytes | None,
) -> None:
    client = FakeS3Client()
    receipt = tmp_path / "restore-receipt.json"
    if receipt_payload is not None:
        receipt.write_bytes(receipt_payload)

    with pytest.raises(SnapshotPublishError, match="restore receipt"):
        start_runtime_controller(
            client,
            BUCKET,
            tmp_path / ".langgraph_api",
            read_only=False,
            restore_receipt_path=receipt,
            require_restore_receipt=True,
        )

    assert client.calls == []


def test_aws_read_write_infers_required_restore_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    monkeypatch.setenv("S3_BUCKET_NAME", BUCKET)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    with pytest.raises(SnapshotPublishError, match="restore receipt"):
        start_runtime_controller(
            client,
            BUCKET,
            tmp_path / ".langgraph_api",
            read_only=False,
        )

    assert client.calls == []


def test_restore_receipt_cannot_be_reused_for_another_source(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path / "source", threads=[thread("t1")])
    publish_generation(client, BUCKET, source, None, "writer-a")
    restored_target = tmp_path / "restored" / ".langgraph_api"
    receipt = default_restore_receipt_path(restored_target)
    restore_snapshot(
        client,
        BUCKET,
        restored_target,
        receipt_path=receipt,
    )
    client.calls.clear()

    with pytest.raises(SnapshotPublishError, match="target does not match"):
        start_runtime_controller(
            client,
            BUCKET,
            tmp_path / "different" / ".langgraph_api",
            read_only=False,
            restore_receipt_path=receipt,
            require_restore_receipt=True,
        )

    assert client.calls == []


def test_read_only_controller_ignores_restore_receipt_without_s3_writes(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    missing_receipt = tmp_path / "missing-receipt.json"

    lease = start_runtime_controller(
        client,
        BUCKET,
        tmp_path / ".langgraph_api",
        read_only=True,
        restore_receipt_path=missing_receipt,
        require_restore_receipt=True,
    )

    assert lease is None
    assert client.calls == []
    assert not missing_receipt.exists()


def test_read_only_startup_skips_writer_claim_and_threads(tmp_path: Path) -> None:
    client = FakeS3Client()

    lease = start_runtime_controller(
        client,
        BUCKET,
        tmp_path / ".langgraph_api",
        read_only=True,
    )

    assert lease is None
    assert client.calls == []


def test_claim_writer_epoch_preserves_generation_pointers(tmp_path: Path) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    etag = publish_generation(client, BUCKET, source, None, "old-writer")
    pointer, loaded_etag = load_pointer(client, BUCKET)
    assert pointer is not None
    assert loaded_etag == etag
    active = pointer["active_generation"]
    previous = pointer["previous_generation"]

    claimed_etag = claim_writer_epoch(
        client,
        BUCKET,
        pointer,
        etag,
        "new-writer",
    )

    claimed = read_json_object(client, ROOT_MANIFEST_KEY)
    pointer_call = client.calls[-1]
    assert claimed["active_generation"] == active
    assert claimed["previous_generation"] == previous
    assert claimed["writer_epoch"] == "new-writer"
    assert pointer_call["if_match"] == etag
    assert claimed_etag == client.etags[ROOT_MANIFEST_KEY]


def test_failed_writer_claim_aborts_runtime_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    publish_generation(client, BUCKET, source, None, "old-writer")
    client.force_root_conflict = ("PreconditionFailed", 412)
    started: list[str] = []
    monkeypatch.setattr(
        snapshot_module,
        "start_snapshot_publisher",
        lambda *args, **kwargs: started.append("publisher"),
    )
    monkeypatch.setattr(
        snapshot_module,
        "start_fence_monitor",
        lambda *args, **kwargs: started.append("monitor"),
    )

    with pytest.raises(SnapshotConflictError, match="claim"):
        start_runtime_controller(
            client,
            BUCKET,
            source,
            read_only=False,
        )

    assert started == []


def test_runtime_controller_uses_configured_logical_writer_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    publish_generation(client, BUCKET, source, None, "old-writer")
    monkeypatch.setenv("LANGGRAPH_WRITER_EPOCH", "aws-apprunner-demo")
    monkeypatch.setattr(
        snapshot_module,
        "start_snapshot_publisher",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        snapshot_module,
        "start_fence_monitor",
        lambda *args, **kwargs: None,
    )

    lease = start_runtime_controller(
        client,
        BUCKET,
        source,
        read_only=False,
        require_restore_receipt=False,
    )

    assert lease is not None
    assert lease.writer_epoch == "aws-apprunner-demo"
    assert read_json_object(client, ROOT_MANIFEST_KEY)["writer_epoch"] == (
        "aws-apprunner-demo"
    )


def test_successful_publish_advances_lease_etag_without_self_fencing(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    initial_etag = publish_generation(client, BUCKET, source, None, "writer-1")
    lease = RuntimeLease(writer_epoch="writer-1", etag=initial_etag)
    elapsed = 0.0
    publisher_stop = threading.Event()
    sleeps = 0

    def clock() -> float:
        return elapsed

    def publisher_sleep(seconds: float) -> None:
        nonlocal elapsed, sleeps
        elapsed += seconds
        sleeps += 1
        if sleeps == 3:
            publisher_stop.set()

    run_snapshot_publisher(
        client,
        BUCKET,
        source,
        lease,
        scan_interval_seconds=6.0,
        clock=clock,
        sleep=publisher_sleep,
        stop=publisher_stop.is_set,
    )
    terminated: list[int] = []
    monitor_stop = threading.Event()

    def monitor_sleep(_seconds: float) -> None:
        monitor_stop.set()

    run_fence_monitor(
        client,
        BUCKET,
        lease,
        sleep=monitor_sleep,
        stop=monitor_stop.is_set,
        terminate=terminated.append,
    )

    assert lease.etag != initial_etag
    assert lease.etag == client.etags[ROOT_MANIFEST_KEY]
    assert not lease.fenced.is_set()
    assert terminated == []


def test_monitor_runs_during_upload_but_waits_for_cas_etag_update(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    initial_etag = publish_generation(client, BUCKET, source, None, "writer-1")
    lease = RuntimeLease(writer_epoch="writer-1", etag=initial_etag)
    upload_started = threading.Event()
    release_upload = threading.Event()
    cas_stored = threading.Event()
    release_cas = threading.Event()
    client.block_next_upload = (upload_started, release_upload)
    client.block_next_root_put = (cas_stored, release_cas)
    publisher_stop = threading.Event()
    publisher_sleeps = 0

    def publisher_sleep(_seconds: float) -> None:
        nonlocal publisher_sleeps
        publisher_sleeps += 1
        if publisher_sleeps >= 2:
            publisher_stop.set()

    publisher = threading.Thread(
        target=run_snapshot_publisher,
        kwargs={
            "client": client,
            "bucket": BUCKET,
            "source_dir": source,
            "lease": lease,
            "clock": iter([0.0, 12.0]).__next__,
            "sleep": publisher_sleep,
            "stop": publisher_stop.is_set,
            "terminate": lambda code: pytest.fail(f"publisher terminated: {code}"),
        },
    )
    terminated: list[int] = []
    first_monitor_stop = threading.Event()
    first_monitor = threading.Thread(
        target=run_fence_monitor,
        kwargs={
            "client": client,
            "bucket": BUCKET,
            "lease": lease,
            "sleep": lambda _seconds: first_monitor_stop.set(),
            "stop": first_monitor_stop.is_set,
            "terminate": terminated.append,
        },
    )
    second_monitor_stop = threading.Event()
    second_monitor = threading.Thread(
        target=run_fence_monitor,
        kwargs={
            "client": client,
            "bucket": BUCKET,
            "lease": lease,
            "sleep": lambda _seconds: second_monitor_stop.set(),
            "stop": second_monitor_stop.is_set,
            "terminate": terminated.append,
        },
    )

    try:
        publisher.start()
        assert upload_started.wait(timeout=1)
        first_monitor.start()
        first_monitor.join(timeout=0.2)
        monitor_completed_during_upload = not first_monitor.is_alive()

        release_upload.set()
        assert cas_stored.wait(timeout=1)
        second_monitor.start()
        second_monitor.join(timeout=0.1)
        monitor_waited_for_cas = second_monitor.is_alive()

        release_cas.set()
        publisher.join(timeout=2)
        first_monitor.join(timeout=2)
        second_monitor.join(timeout=2)
    finally:
        release_upload.set()
        release_cas.set()

    assert monitor_completed_during_upload
    assert monitor_waited_for_cas
    assert not publisher.is_alive()
    assert not first_monitor.is_alive()
    assert not second_monitor.is_alive()
    assert lease.etag == client.etags[ROOT_MANIFEST_KEY]
    assert not lease.fenced.is_set()
    assert terminated == []


def test_publisher_requires_two_fingerprints_twelve_seconds_apart(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    initial_etag = publish_generation(client, BUCKET, source, None, "writer-1")
    lease = RuntimeLease(writer_epoch="writer-1", etag=initial_etag)
    elapsed = 0.0
    stop_event = threading.Event()
    root_writes_before = sum(
        call["operation"] == "put_object" and call["key"] == ROOT_MANIFEST_KEY
        for call in client.calls
    )
    observed_root_write_times: list[float] = []
    original_put_object = client.put_object

    def timed_put_object(**kwargs: object) -> dict[str, str]:
        if kwargs["Key"] == ROOT_MANIFEST_KEY:
            observed_root_write_times.append(elapsed)
        return original_put_object(**kwargs)

    client.put_object = timed_put_object  # type: ignore[method-assign]
    sleeps = 0

    def sleep(seconds: float) -> None:
        nonlocal elapsed, sleeps
        elapsed += seconds
        sleeps += 1
        if sleeps == 3:
            stop_event.set()

    run_snapshot_publisher(
        client,
        BUCKET,
        source,
        lease,
        stability_seconds=12.0,
        scan_interval_seconds=6.0,
        clock=lambda: elapsed,
        sleep=sleep,
        stop=stop_event.is_set,
    )

    root_writes_after = sum(
        call["operation"] == "put_object" and call["key"] == ROOT_MANIFEST_KEY
        for call in client.calls
    )
    assert root_writes_after == root_writes_before + 1
    assert observed_root_write_times == [12.0]


def test_publisher_rejects_stability_window_below_twelve_seconds(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    lease = RuntimeLease(writer_epoch="writer-1", etag='"etag-1"')

    with pytest.raises(ValueError, match="at least 12"):
        run_snapshot_publisher(
            client,
            BUCKET,
            source,
            lease,
            stability_seconds=11.99,
            stop=lambda: True,
        )

    with pytest.raises(ValueError, match="at least 12"):
        start_snapshot_publisher(
            client,
            BUCKET,
            source,
            lease,
            stability_seconds=11.99,
        )

    assert lease.threads == []


@pytest.mark.parametrize("from_environment", [False, True])
def test_runtime_controller_rejects_stability_below_twelve_before_s3_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    from_environment: bool,
) -> None:
    client = FakeS3Client()
    kwargs: dict[str, object] = {}
    if from_environment:
        monkeypatch.setenv("LANGGRAPH_SNAPSHOT_STABILITY_SECONDS", "11")
    else:
        kwargs["stability_seconds"] = 11.0

    with pytest.raises(ValueError, match="at least 12"):
        start_runtime_controller(
            client,
            BUCKET,
            tmp_path / ".langgraph_api",
            read_only=False,
            **kwargs,
        )

    assert client.calls == []


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("stability_seconds", float("nan")),
        ("stability_seconds", float("inf")),
        ("scan_interval_seconds", 0.0),
        ("scan_interval_seconds", -1.0),
        ("scan_interval_seconds", float("nan")),
        ("fence_interval_seconds", 0.0),
        ("fence_interval_seconds", -1.0),
        ("fence_interval_seconds", float("inf")),
    ],
)
def test_runtime_controller_validates_all_intervals_before_s3(
    tmp_path: Path,
    parameter: str,
    value: float,
) -> None:
    client = FakeS3Client()

    with pytest.raises(ValueError, match="finite|greater than zero|at least 12"):
        start_runtime_controller(
            client,
            BUCKET,
            tmp_path / ".langgraph_api",
            read_only=False,
            **{parameter: value},
        )

    assert client.calls == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LANGGRAPH_SNAPSHOT_STABILITY_SECONDS", "nan"),
        ("LANGGRAPH_SNAPSHOT_SCAN_INTERVAL_SECONDS", "0"),
        ("LANGGRAPH_FENCE_INTERVAL_SECONDS", "-1"),
    ],
)
def test_runtime_controller_validates_interval_environment_before_s3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    client = FakeS3Client()
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="finite|greater than zero|at least 12"):
        start_runtime_controller(
            client,
            BUCKET,
            tmp_path / ".langgraph_api",
            read_only=False,
        )

    assert client.calls == []


def test_thread_helpers_validate_intervals_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    lease = RuntimeLease(writer_epoch="writer-1", etag='"etag-1"')
    spawned: list[bool] = []

    def reject_thread(*args: object, **kwargs: object) -> None:
        spawned.append(True)
        raise AssertionError("thread must not be created")

    monkeypatch.setattr(snapshot_module.threading, "Thread", reject_thread)

    with pytest.raises(ValueError, match="greater than zero"):
        start_snapshot_publisher(
            client,
            BUCKET,
            tmp_path,
            lease,
            scan_interval_seconds=0,
        )
    with pytest.raises(ValueError, match="finite"):
        snapshot_module.start_fence_monitor(
            client,
            BUCKET,
            lease,
            interval_seconds=float("nan"),
        )

    assert spawned == []


def test_publisher_defers_while_run_is_active(tmp_path: Path) -> None:
    client = FakeS3Client()
    published = make_snapshot(tmp_path / "published", threads=[thread("t1")])
    etag = publish_generation(client, BUCKET, published, None, "writer-1")
    source = make_snapshot(
        tmp_path / "active",
        threads=[thread("t1")],
        runs=[{"run_id": "r1", "status": "running"}],
    )
    lease = RuntimeLease(writer_epoch="writer-1", etag=etag)
    stop_event = threading.Event()
    elapsed = 0.0
    sleeps = 0
    root_before = client.objects[ROOT_MANIFEST_KEY]

    def sleep(seconds: float) -> None:
        nonlocal elapsed, sleeps
        elapsed += seconds
        sleeps += 1
        if sleeps == 3:
            stop_event.set()

    run_snapshot_publisher(
        client,
        BUCKET,
        source,
        lease,
        scan_interval_seconds=6.0,
        clock=lambda: elapsed,
        sleep=sleep,
        stop=stop_event.is_set,
    )

    assert client.objects[ROOT_MANIFEST_KEY] == root_before
    assert not lease.fenced.is_set()


def test_fence_loss_terminates_process(tmp_path: Path) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    etag = publish_generation(client, BUCKET, source, None, "new-writer")
    lease = RuntimeLease(writer_epoch="old-writer", etag=etag)
    terminated: list[int] = []

    run_fence_monitor(
        client,
        BUCKET,
        lease,
        sleep=lambda _seconds: None,
        stop=lambda: False,
        terminate=terminated.append,
    )

    assert lease.fenced.is_set()
    assert terminated and terminated[0] != 0


def test_fence_monitor_adopts_etag_from_same_logical_writer(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    initial_etag = publish_generation(
        client,
        BUCKET,
        source,
        None,
        "aws-apprunner-demo",
    )
    pointer, loaded_etag = load_pointer(client, BUCKET)
    assert pointer is not None
    assert loaded_etag == initial_etag
    lease = RuntimeLease(
        writer_epoch="aws-apprunner-demo",
        etag=initial_etag,
    )
    sibling_etag = claim_writer_epoch(
        client,
        BUCKET,
        pointer,
        initial_etag,
        "aws-apprunner-demo",
    )
    stopped = threading.Event()
    terminated: list[int] = []

    run_fence_monitor(
        client,
        BUCKET,
        lease,
        sleep=lambda _seconds: stopped.set(),
        stop=stopped.is_set,
        terminate=terminated.append,
    )

    assert lease.etag == sibling_etag
    assert not lease.fenced.is_set()
    assert terminated == []


def test_publisher_retries_same_logical_writer_cas_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    initial_etag = publish_generation(
        client,
        BUCKET,
        source,
        None,
        "aws-apprunner-demo",
    )
    initial_generation = read_json_object(
        client,
        ROOT_MANIFEST_KEY,
    )["active_generation"]
    lease = RuntimeLease(
        writer_epoch="aws-apprunner-demo",
        etag=initial_etag,
    )
    original_commit = snapshot_module._commit_prepared_generation
    inject_sibling_claim = True

    def commit_with_one_sibling_race(*args, **kwargs):
        nonlocal inject_sibling_claim
        if inject_sibling_claim:
            inject_sibling_claim = False
            pointer, remote_etag = load_pointer(client, BUCKET)
            assert pointer is not None
            assert remote_etag is not None
            claim_writer_epoch(
                client,
                BUCKET,
                pointer,
                remote_etag,
                lease.writer_epoch,
            )
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(
        snapshot_module,
        "_commit_prepared_generation",
        commit_with_one_sibling_race,
    )
    elapsed = 0.0
    stopped = threading.Event()
    sleeps = 0
    terminated: list[int] = []

    def clock() -> float:
        return elapsed

    def sleep(seconds: float) -> None:
        nonlocal elapsed, sleeps
        elapsed += seconds
        sleeps += 1
        if sleeps >= 6:
            stopped.set()

    run_snapshot_publisher(
        client,
        BUCKET,
        source,
        lease,
        scan_interval_seconds=6.0,
        clock=clock,
        sleep=sleep,
        stop=stopped.is_set,
        terminate=terminated.append,
    )

    assert read_json_object(
        client,
        ROOT_MANIFEST_KEY,
    )["active_generation"] != initial_generation
    assert not lease.fenced.is_set()
    assert terminated == []


def test_same_thread_older_updated_at_is_not_published(tmp_path: Path) -> None:
    client = FakeS3Client()
    current = make_snapshot(
        tmp_path / "current",
        threads=[thread("t1", updated_at=UPDATED_AT)],
    )
    etag = publish_generation(client, BUCKET, current, None, "writer-1")
    older = make_snapshot(
        tmp_path / "older",
        threads=[thread("t1", updated_at="2026-07-23T09:59:59+00:00")],
    )
    pointer_before = client.objects[ROOT_MANIFEST_KEY]

    with pytest.raises(SnapshotPublishError, match="older thread timestamp"):
        publish_generation(client, BUCKET, older, etag, "writer-1")

    assert client.objects[ROOT_MANIFEST_KEY] == pointer_before


def test_publish_rejects_matching_etag_owned_by_different_writer_epoch(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    etag = publish_generation(client, BUCKET, source, None, "current-writer")
    pointer_before = client.objects[ROOT_MANIFEST_KEY]
    objects_before = set(client.objects)
    writes_before = len(
        [
            call
            for call in client.calls
            if call["operation"] in {"upload_file", "put_object"}
        ]
    )

    with pytest.raises(SnapshotConflictError, match="writer epoch"):
        publish_generation(
            client,
            BUCKET,
            source,
            etag,
            "stale-writer",
        )

    writes_after = len(
        [
            call
            for call in client.calls
            if call["operation"] in {"upload_file", "put_object"}
        ]
    )
    assert writes_after == writes_before
    assert client.objects[ROOT_MANIFEST_KEY] == pointer_before
    assert set(client.objects) == objects_before


def test_changing_source_during_staging_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    checkpoint = source / "checkpoints.pckl"
    original_copy = shutil.copy2
    copy_count = 0

    def mutate_between_copies(source_path: Path, target_path: Path) -> Path:
        nonlocal copy_count
        result = original_copy(source_path, target_path)
        copy_count += 1
        if copy_count == 1:
            checkpoint.write_bytes(pickle.dumps({"checkpoint": "changed"}))
        return result

    monkeypatch.setattr(snapshot_module.shutil, "copy2", mutate_between_copies)

    with pytest.raises(SnapshotPublishError, match="changed during staging"):
        publish_generation(client, BUCKET, source, None, "writer-1")

    assert client.calls == []


def test_mixed_pickle_set_during_staging_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    source = make_snapshot(tmp_path, threads=[thread("t1")])
    original_copy = shutil.copy2
    copy_count = 0

    def add_pickle_between_copies(source_path: Path, target_path: Path) -> Path:
        nonlocal copy_count
        result = original_copy(source_path, target_path)
        copy_count += 1
        if copy_count == 1:
            (source / "late.pckl").write_bytes(pickle.dumps({"late": True}))
        return result

    monkeypatch.setattr(snapshot_module.shutil, "copy2", add_pickle_between_copies)

    with pytest.raises(SnapshotPublishError, match="changed during staging"):
        publish_generation(client, BUCKET, source, None, "writer-1")

    assert client.calls == []


def _seed_valid_generation(
    client: FakeS3Client,
    generation: str,
    created_at: str,
) -> None:
    file_key = f"{GENERATION_PREFIX}/{generation}/.langgraph_ops.pckl"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generation": generation,
        "created_at": created_at,
        "thread_ids": ["t1"],
        "thread_versions": {"t1": UPDATED_AT},
        "runtime_versions": {"python": "3.12"},
        "files": {
            ".langgraph_ops.pckl": {
                "key": file_key,
                "size": 1,
                "sha256": "0" * 64,
            }
        },
    }
    client.seed(file_key, b"x")
    client.seed(
        f"{GENERATION_PREFIX}/{generation}/manifest.json",
        json.dumps(manifest).encode(),
    )


def test_retention_keeps_active_previous_and_five_recent() -> None:
    client = FakeS3Client()
    generations = ["protected-active", "protected-previous"] + [
        f"recent-{index}" for index in range(7)
    ]
    for index, generation in enumerate(generations):
        _seed_valid_generation(
            client,
            generation,
            f"2026-07-{index + 1:02d}T10:00:00+00:00",
        )
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "active_generation": "protected-active",
        "previous_generation": "protected-previous",
        "writer_epoch": "writer-1",
        "created_at": UPDATED_AT,
    }
    client.seed(ROOT_MANIFEST_KEY, json.dumps(pointer).encode())

    prune_generations(client, BUCKET, keep_recent=5)

    remaining_manifests = {
        key.split("/")[-2]
        for key in client.objects
        if key.startswith(f"{GENERATION_PREFIX}/")
        and key.endswith("/manifest.json")
    }
    assert remaining_manifests == {
        "protected-active",
        "protected-previous",
        "recent-2",
        "recent-3",
        "recent-4",
        "recent-5",
        "recent-6",
    }


def test_retention_aborts_if_pointer_changes_during_scan() -> None:
    client = FakeS3Client()
    for index, generation in enumerate(
        ["old-active", "takeover-active", "deletable"]
    ):
        _seed_valid_generation(
            client,
            generation,
            f"2026-07-{index + 1:02d}T10:00:00+00:00",
        )
    initial_pointer = {
        "schema_version": SCHEMA_VERSION,
        "active_generation": "old-active",
        "previous_generation": None,
        "writer_epoch": "writer-1",
        "created_at": UPDATED_AT,
    }
    client.seed(ROOT_MANIFEST_KEY, json.dumps(initial_pointer).encode())

    def takeover() -> None:
        pointer = {
            **initial_pointer,
            "active_generation": "takeover-active",
            "previous_generation": "old-active",
            "writer_epoch": "writer-2",
        }
        client.seed(ROOT_MANIFEST_KEY, json.dumps(pointer).encode())

    client.on_next_generation_list = takeover

    with pytest.raises(SnapshotConflictError, match="retention.*lease"):
        prune_generations(client, BUCKET, keep_recent=0)

    assert any(
        key.startswith(f"{GENERATION_PREFIX}/takeover-active/")
        for key in client.objects
    )
    assert not any(
        call["operation"] == "delete_objects" for call in client.calls
    )


def test_retention_uses_manifest_created_at_despite_generation_clock_skew() -> None:
    client = FakeS3Client()
    generations = {
        "9999-clock-ahead-but-old": "2026-07-01T10:00:00+00:00",
        "0000-clock-behind-but-new": "2026-07-20T10:00:00+00:00",
        "middle": "2026-07-10T10:00:00+00:00",
        "active": "2026-07-05T10:00:00+00:00",
    }
    for generation, created_at in generations.items():
        _seed_valid_generation(client, generation, created_at)
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "active_generation": "active",
        "previous_generation": None,
        "writer_epoch": "writer-1",
        "created_at": UPDATED_AT,
    }
    client.seed(ROOT_MANIFEST_KEY, json.dumps(pointer).encode())

    prune_generations(client, BUCKET, keep_recent=2)

    remaining_manifests = {
        key.split("/")[-2]
        for key in client.objects
        if key.endswith("/manifest.json")
        and key.startswith(f"{GENERATION_PREFIX}/")
    }
    assert remaining_manifests == {
        "active",
        "middle",
        "0000-clock-behind-but-new",
    }


def test_retention_surfaces_delete_objects_errors() -> None:
    client = FakeS3Client()
    _seed_valid_generation(client, "active", "2026-07-20T10:00:00+00:00")
    _seed_valid_generation(client, "old", "2026-07-01T10:00:00+00:00")
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "active_generation": "active",
        "previous_generation": None,
        "writer_epoch": "writer-1",
        "created_at": UPDATED_AT,
    }
    client.seed(ROOT_MANIFEST_KEY, json.dumps(pointer).encode())
    client.delete_errors = [
        {"Key": f"{GENERATION_PREFIX}/old/manifest.json", "Code": "AccessDenied"}
    ]

    with pytest.raises(SnapshotPublishError, match="delete.*error"):
        prune_generations(client, BUCKET, keep_recent=0)


def test_cli_publish_uploads_then_claims_epoch_and_allows_shrink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    current = make_snapshot(
        tmp_path / "current",
        threads=[thread("t1"), thread("t2")],
    )
    source = make_snapshot(tmp_path / "source", threads=[thread("t1")])
    old_etag = publish_generation(client, BUCKET, current, None, "old-writer")
    monkeypatch.setenv("S3_BUCKET_NAME", BUCKET)

    exit_code = main(
        ["publish", "--source", str(source), "--allow-shrink"],
        client_factory=lambda: client,
    )

    assert exit_code == 0
    pointer = read_json_object(client, ROOT_MANIFEST_KEY)
    assert pointer["writer_epoch"] != "old-writer"
    manifest = read_json_object(
        client,
        f"{GENERATION_PREFIX}/{pointer['active_generation']}/manifest.json",
    )
    assert manifest["thread_ids"] == ["t1"]
    root_writes = [
        call
        for call in client.calls
        if call["operation"] == "put_object"
        and call["key"] == ROOT_MANIFEST_KEY
    ]
    assert root_writes[-2]["if_match"] == old_etag
    assert isinstance(root_writes[-1]["if_match"], str)
    assert root_writes[-1]["if_match"] != old_etag
    claim_index = client.calls.index(root_writes[-2])
    prepared_write_indexes = [
        index
        for index, call in enumerate(client.calls)
        if index > client.calls.index(root_writes[-3])
        and call["operation"] in {"upload_file", "put_object"}
        and call["key"] != ROOT_MANIFEST_KEY
    ]
    assert prepared_write_indexes
    assert max(prepared_write_indexes) < claim_index


def test_cli_inspect_source_checks_pinned_image_runtime_without_s3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_snapshot(tmp_path / "source", threads=[thread("t1")])
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.delenv("DATABASE_URI", raising=False)
    monkeypatch.delenv("REDIS_URI", raising=False)

    exit_code = main(["inspect-source", "--source", str(source)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {
        "thread_count": 1,
        "thread_ids": ["t1"],
        "runtime_versions": AWS_IMAGE_RUNTIME_VERSIONS,
    }
    assert os.environ["DATABASE_URI"] == "postgres://unused"
    assert os.environ["REDIS_URI"] == "redis://unused"


def test_cli_returns_nonzero_on_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeS3Client()
    current = make_snapshot(tmp_path / "current", threads=[thread("t1")])
    source = make_snapshot(
        tmp_path / "source",
        threads=[thread("t1"), thread("t2")],
    )
    publish_generation(client, BUCKET, current, None, "old-writer")
    pointer_before = client.objects[ROOT_MANIFEST_KEY]
    root_writes_before = sum(
        call["operation"] == "put_object" and call["key"] == ROOT_MANIFEST_KEY
        for call in client.calls
    )
    client.fail_upload_suffix = "checkpoints.pckl"
    monkeypatch.setenv("S3_BUCKET_NAME", BUCKET)

    exit_code = main(
        ["publish", "--source", str(source)],
        client_factory=lambda: client,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "failed" in captured.err.lower()
    assert client.objects[ROOT_MANIFEST_KEY] == pointer_before
    assert (
        sum(
            call["operation"] == "put_object"
            and call["key"] == ROOT_MANIFEST_KEY
            for call in client.calls
        )
        == root_writes_before
    )


def test_cli_invalid_source_does_not_claim_writer_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    current = make_snapshot(tmp_path / "current", threads=[thread("t1")])
    publish_generation(client, BUCKET, current, None, "old-writer")
    pointer_before = client.objects[ROOT_MANIFEST_KEY]
    invalid = make_snapshot(
        tmp_path / "invalid",
        threads=[thread("t1")],
        runs=[{"run_id": "r1", "status": "pending"}],
    )
    root_writes_before = sum(
        call["operation"] == "put_object" and call["key"] == ROOT_MANIFEST_KEY
        for call in client.calls
    )
    monkeypatch.setenv("S3_BUCKET_NAME", BUCKET)

    exit_code = main(
        ["publish", "--source", str(invalid)],
        client_factory=lambda: client,
    )

    assert exit_code != 0
    assert client.objects[ROOT_MANIFEST_KEY] == pointer_before
    assert (
        sum(
            call["operation"] == "put_object"
            and call["key"] == ROOT_MANIFEST_KEY
            for call in client.calls
        )
        == root_writes_before
    )
