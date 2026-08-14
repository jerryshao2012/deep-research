"""Thread-independent assets for synchronized Markdown previews."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote
from weakref import WeakKeyDictionary

import filetype
from fastapi import Header, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import (
    MultiPartException,
    MultiPartParser,
    parse_options_header,
)

from webapp.auth_helpers import is_authenticated
from webapp.markdown_archive_validation import (
    ARCHIVE_CONTENT_TYPES,
    is_archive_upload,
    is_extended_archive_filename,
    is_stored_archive_content_type,
    validate_archive,
)
from webapp.markdown_office_formats import OFFICE_CONTENT_TYPE, is_office_upload
from webapp.utils import safe_filename

_MARKDOWN_ID_RE = re.compile(r"^[0-9]{6}$")
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_COUNT = 5
_MAX_REQUEST_BYTES = (_MAX_IMAGE_BYTES * _MAX_IMAGE_COUNT) + (1024 * 1024)
_ARCHIVE_BATCH_WAIT_SECONDS = 2.0
_ALLOWED_IMAGES: dict[str, tuple[str, frozenset[str]]] = {
    "image/png": ("png", frozenset({".png"})),
    "image/jpeg": ("jpg", frozenset({".jpg", ".jpeg"})),
    "image/gif": ("gif", frozenset({".gif"})),
    "image/webp": ("webp", frozenset({".webp"})),
}
_ALLOWED_IMAGE_EXTENSIONS = frozenset(
    suffix for _, suffixes in _ALLOWED_IMAGES.values() for suffix in suffixes
)
_STORED_CONTENT_TYPES = frozenset(
    (*_ALLOWED_IMAGES, *ARCHIVE_CONTENT_TYPES, OFFICE_CONTENT_TYPE)
)
_ARCHIVE_ERROR_MESSAGE = (
    "Only valid ZIP, 7Z, TAR, TAR.GZ, and TGZ archives are supported"
)


class _LoopLocalArchiveBatchLimiter:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._lock = Lock()
        self._semaphores: WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Semaphore
        ] = WeakKeyDictionary()

    def _semaphore_for_running_loop(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._lock:
            semaphore = self._semaphores.get(loop)
            if semaphore is None:
                semaphore = asyncio.Semaphore(self._capacity)
                self._semaphores[loop] = semaphore
            return semaphore

    async def acquire(self) -> None:
        await self._semaphore_for_running_loop().acquire()

    def release(self) -> None:
        self._semaphore_for_running_loop().release()


_ARCHIVE_BATCH_LIMITER = _LoopLocalArchiveBatchLimiter(2)


class _RequestBodyTooLarge(MultiPartException):
    pass


class _UploadFileTooLarge(MultiPartException):
    pass


class _BoundedUploadMultiPartParser(MultiPartParser):
    def __init__(self, *args: Any, max_file_size: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_file_size = max_file_size
        self._current_file_size = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_size += end - start
            if self._current_file_size > self._max_file_size:
                raise _UploadFileTooLarge("File exceeds 10 MiB")
        super().on_part_data(data, start, end)


class _LoopLocalNamespaceMutationLocks:
    def __init__(self) -> None:
        self._lock = Lock()
        self._locks: WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
        ] = WeakKeyDictionary()

    def lock_for_running_loop(self, key: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._lock:
            loop_locks = self._locks.setdefault(loop, {})
            mutation_lock = loop_locks.get(key)
            if mutation_lock is None:
                mutation_lock = asyncio.Lock()
                loop_locks[key] = mutation_lock
            return mutation_lock


_NAMESPACE_MUTATION_LOCKS = _LoopLocalNamespaceMutationLocks()


def _webapp_module():
    import sys

    return sys.modules["webapp"]


def _validated_markdown_id(markdown_id: str) -> str:
    if not _MARKDOWN_ID_RE.fullmatch(markdown_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="markdown_id must contain exactly six digits",
        )
    return markdown_id


def _validated_asset_id(asset_id: str) -> str:
    try:
        parsed = uuid.UUID(asset_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="asset_id must be a UUID",
        ) from exc
    if str(parsed) != asset_id.lower():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="asset_id must use canonical UUID form",
        )
    return str(parsed)


def _images_root(markdown_id: str) -> Path:
    return (
        _webapp_module().DOCS_ROOT
        / "markdown-threads"
        / _validated_markdown_id(markdown_id)
        / "images"
    )


def _namespace_lock_path(markdown_id: str) -> Path:
    return (
        _webapp_module().DOCS_ROOT
        / ".markdown-asset-locks"
        / f"{_validated_markdown_id(markdown_id)}.lock"
    )


def _acquire_namespace_file_lock(lock_path: Path, holder: list[int]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    holder.append(descriptor)


def _release_namespace_file_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class _NamespaceMutationFence:
    def __init__(self, markdown_id: str) -> None:
        self._lock_path = _namespace_lock_path(markdown_id)
        self._async_lock: asyncio.Lock | None = None
        self._descriptor: int | None = None

    async def acquire(self) -> None:
        async_lock = _NAMESPACE_MUTATION_LOCKS.lock_for_running_loop(
            str(self._lock_path)
        )
        await async_lock.acquire()
        self._async_lock = async_lock
        holder: list[int] = []
        try:
            await _run_worker_to_completion(
                _acquire_namespace_file_lock, self._lock_path, holder
            )
        except BaseException:
            try:
                if holder:
                    _release_namespace_file_lock(holder.pop())
            finally:
                self._async_lock.release()
                self._async_lock = None
            raise
        self._descriptor = holder.pop()

    def release(self) -> None:
        try:
            if self._descriptor is not None:
                _release_namespace_file_lock(self._descriptor)
                self._descriptor = None
        finally:
            if self._async_lock is not None:
                self._async_lock.release()
                self._async_lock = None


def _validate_image(filename: str, content_type: str | None, data: bytes) -> str:
    declared_type = (content_type or "").lower()
    allowed = _ALLOWED_IMAGES.get(declared_type)
    guessed = filetype.guess(data)
    suffix = Path(filename).suffix.lower()
    if (
        allowed is None
        or guessed is None
        or guessed.mime != declared_type
        or guessed.extension != allowed[0]
        or suffix not in allowed[1]
    ):
        raise ValueError("extension, MIME type, and image signature must agree")
    return declared_type


def _validate_asset(filename: str, content_type: str | None, data: bytes) -> str:
    if is_office_upload(filename):
        return OFFICE_CONTENT_TYPE
    if is_archive_upload(filename, content_type):
        return validate_archive(filename, content_type, data)
    return _validate_image(filename, content_type, data)


def _is_image_upload(filename: str, content_type: str | None) -> bool:
    declared_type = (content_type or "").lower()
    return declared_type.startswith("image/") or Path(filename).suffix.lower() in (
        _ALLOWED_IMAGE_EXTENSIONS
    )


def _extended_attachment_uploads_enabled() -> bool:
    return (
        os.getenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", "").strip().lower()
        != "false"
    )


def _is_archive_candidate(filename: str, content_type: str | None) -> bool:
    return not is_office_upload(filename) and is_archive_upload(filename, content_type)


async def _run_worker_to_completion[T](
    function: Callable[..., T], /, *args: Any, **kwargs: Any
) -> T:
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            if worker.cancelled():
                raise
            if cancellation is None:
                cancellation = exc
            continue
        except BaseException:
            if cancellation is not None:
                raise cancellation
            raise
        if cancellation is not None:
            raise cancellation
        return result


async def _bounded_request_stream(request: Request) -> AsyncIterator[bytes]:
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > _MAX_REQUEST_BYTES:
            raise _RequestBodyTooLarge("Asset upload request is too large")
        yield chunk


async def _parse_upload_form(request: Request) -> FormData:
    content_type, _ = parse_options_header(request.headers.get("content-type"))
    if content_type != b"multipart/form-data":
        return FormData()
    parser = _BoundedUploadMultiPartParser(
        request.headers,
        _bounded_request_stream(request),
        max_files=_MAX_IMAGE_COUNT,
        max_fields=0,
        max_part_size=_MAX_IMAGE_BYTES,
        max_file_size=_MAX_IMAGE_BYTES,
    )
    try:
        return await parser.parse()
    except (_RequestBodyTooLarge, _UploadFileTooLarge) as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=exc.message,
        ) from exc
    except MultiPartException as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        ) from exc


@asynccontextmanager
async def _closing_form(form: FormData) -> AsyncIterator[FormData]:
    try:
        yield form
    finally:
        await form.close()


def _store_asset(
    images_root: Path,
    *,
    asset_id: str,
    filename: str,
    content_type: str,
    data: bytes,
) -> None:
    images_root.mkdir(parents=True, exist_ok=True)
    target = images_root / asset_id
    temporary = Path(tempfile.mkdtemp(prefix=f".{asset_id}-", dir=images_root))
    try:
        with (temporary / "payload").open("wb") as payload_file:
            payload_file.write(data)
            payload_file.flush()
            os.fsync(payload_file.fileno())
        with (temporary / "metadata.json").open("w", encoding="utf-8") as metadata_file:
            metadata_file.write(
                json.dumps(
                    {
                        "filename": filename,
                        "content_type": content_type,
                        "size": len(data),
                    },
                    separators=(",", ":"),
                )
            )
            metadata_file.flush()
            os.fsync(metadata_file.fileno())
        temporary_fd = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        os.replace(temporary, target)
        images_root_fd = os.open(images_root, os.O_RDONLY)
        try:
            os.fsync(images_root_fd)
        finally:
            os.close(images_root_fd)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _read_stored_attachment(payload: Path, expected_size: int) -> bytes:
    with payload.open("rb") as payload_file:
        data = payload_file.read(min(expected_size, _MAX_IMAGE_BYTES) + 1)
    if len(data) != expected_size:
        raise ValueError("stored attachment size changed while reading")
    return data


async def _load_asset(
    markdown_id: str, asset_id: str
) -> tuple[Path, dict[str, Any], bytes | None]:
    asset_path = _images_root(markdown_id) / _validated_asset_id(asset_id)
    payload = asset_path / "payload"
    metadata_path = asset_path / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
        ) from exc
    if not isinstance(metadata, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
        )
    stored_content_type = metadata.get("content_type")
    try:
        invalid_metadata = (
            not payload.is_file()
            or not isinstance(stored_content_type, str)
            or stored_content_type not in _STORED_CONTENT_TYPES
            or not isinstance(metadata.get("filename"), str)
            or type(metadata.get("size")) is not int
            or metadata["size"] < 0
            or (
                metadata["size"] > _MAX_IMAGE_BYTES
                and (
                    stored_content_type == OFFICE_CONTENT_TYPE
                    or is_stored_archive_content_type(stored_content_type)
                )
            )
            or metadata["size"] != payload.stat().st_size
        )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
        ) from exc
    if invalid_metadata:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
        )

    filename = metadata["filename"]
    if stored_content_type == OFFICE_CONTENT_TYPE:
        if not is_office_upload(filename):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
            )
        try:
            snapshot = await _run_worker_to_completion(
                _read_stored_attachment, payload, metadata["size"]
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
            ) from exc
        return payload, metadata, snapshot

    if is_stored_archive_content_type(stored_content_type):
        archive_slot_acquired = False
        try:
            try:
                await asyncio.wait_for(
                    _ARCHIVE_BATCH_LIMITER.acquire(),
                    timeout=_ARCHIVE_BATCH_WAIT_SECONDS,
                )
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Archive validation is busy",
                    headers={"Retry-After": "2"},
                ) from exc
            archive_slot_acquired = True
            validation_data = await _run_worker_to_completion(
                _read_stored_attachment, payload, metadata["size"]
            )
            verified_type = await _run_worker_to_completion(
                validate_archive,
                filename,
                stored_content_type,
                validation_data,
            )
            if verified_type != stored_content_type:
                raise ValueError("stored content type does not match asset")
        except HTTPException:
            raise
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
            ) from exc
        finally:
            if archive_slot_acquired:
                _ARCHIVE_BATCH_LIMITER.release()
        return payload, metadata, validation_data

    try:
        with payload.open("rb") as payload_file:
            validation_data = payload_file.read(261)
            verified_type = _validate_image(
                filename,
                stored_content_type,
                validation_data,
            )
            if verified_type != stored_content_type:
                raise ValueError("stored content type does not match asset")
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
        ) from exc
    return payload, metadata, None


def _attachment_content_disposition(filename: str) -> str:
    encoded_filename = quote(filename)
    if encoded_filename != filename:
        return f"attachment; filename*=utf-8''{encoded_filename}"
    return f'attachment; filename="{filename}"'


def _count_supported_assets(images_root: Path) -> int:
    count = 0
    for child in images_root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            metadata = json.loads((child / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        content_type = (
            metadata.get("content_type") if isinstance(metadata, dict) else None
        )
        if isinstance(content_type, str) and content_type in _STORED_CONTENT_TYPES:
            count += 1
    return count


def _delete_images_namespace(images_root: Path) -> int:
    while True:
        tombstone = images_root.with_name(f".{images_root.name}-delete-{uuid.uuid4()}")
        if tombstone.exists():
            continue
        try:
            images_root.rename(tombstone)
        except FileNotFoundError:
            return 0
        except FileExistsError:
            continue
        break

    try:
        return _count_supported_assets(tombstone)
    finally:
        shutil.rmtree(tombstone)


def _rollback_request_assets(images_root: Path, asset_ids: list[str]) -> None:
    for asset_id in asset_ids:
        shutil.rmtree(images_root / asset_id, ignore_errors=True)
    try:
        images_root.rmdir()
    except OSError:
        pass


def register_markdown_image_routes(app) -> None:
    """Register synchronized Markdown image upload and retrieval endpoints."""

    @app.post("/markdown-threads/{markdown_id}/images")
    async def upload_markdown_images(
        request: Request,
        markdown_id: str,
        x_api_key: str | None = Header(None),
    ) -> dict[str, Any]:
        if not await is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )
        images_root = _images_root(markdown_id)
        content_length = request.headers.get("content-length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > _MAX_REQUEST_BYTES
        ):
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Asset upload request is too large",
            )

        assets: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        stored_asset_ids: list[str] = []
        archive_slot_acquired = False
        mutation_fence = _NamespaceMutationFence(markdown_id)
        mutation_fence_acquired = False
        try:
            async with _closing_form(await _parse_upload_form(request)) as form:
                files = form.getlist("files")
                if not files or any(
                    not isinstance(upload, UploadFile) for upload in files
                ):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="files must contain uploaded assets",
                    )
                needs_archive_slot = any(
                    _is_archive_candidate(upload.filename or "", upload.content_type)
                    for upload in files
                )
                if needs_archive_slot:
                    try:
                        await asyncio.wait_for(
                            _ARCHIVE_BATCH_LIMITER.acquire(),
                            timeout=_ARCHIVE_BATCH_WAIT_SECONDS,
                        )
                    except TimeoutError as exc:
                        raise HTTPException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Archive validation is busy",
                            headers={"Retry-After": "2"},
                        ) from exc
                    archive_slot_acquired = True
                extended_uploads_enabled = _extended_attachment_uploads_enabled()
                for upload in files:
                    raw_filename = upload.filename or ""
                    try:
                        display_name = safe_filename(raw_filename)
                    except HTTPException:
                        errors.append(
                            {
                                "filename": raw_filename,
                                "code": "invalid_filename",
                                "message": "Asset filename is invalid",
                            }
                        )
                        continue
                    data = await upload.read(_MAX_IMAGE_BYTES + 1)
                    if len(data) > _MAX_IMAGE_BYTES:
                        errors.append(
                            {
                                "filename": display_name,
                                "code": "file_too_large",
                                "message": "File exceeds 10 MiB",
                            }
                        )
                        continue
                    office_candidate = is_office_upload(display_name)
                    archive_candidate = _is_archive_candidate(
                        display_name, upload.content_type
                    )
                    if not extended_uploads_enabled and (
                        office_candidate or is_extended_archive_filename(display_name)
                    ):
                        errors.append(
                            {
                                "filename": display_name,
                                "code": "extended_attachment_upload_disabled",
                                "message": "Extended archive and Microsoft Office uploads are disabled",
                            }
                        )
                        continue
                    try:
                        if office_candidate:
                            verified_type = OFFICE_CONTENT_TYPE
                        elif archive_candidate:
                            verified_type = await _run_worker_to_completion(
                                validate_archive,
                                display_name,
                                upload.content_type,
                                data,
                            )
                        else:
                            verified_type = _validate_image(
                                display_name, upload.content_type, data
                            )
                        if not mutation_fence_acquired:
                            await mutation_fence.acquire()
                            mutation_fence_acquired = True
                        asset_id = str(uuid.uuid4())
                        stored_asset_ids.append(asset_id)
                        await _run_worker_to_completion(
                            _store_asset,
                            images_root,
                            asset_id=asset_id,
                            filename=display_name,
                            content_type=verified_type,
                            data=data,
                        )
                    except ValueError:
                        if archive_candidate:
                            errors.append(
                                {
                                    "filename": display_name,
                                    "code": "unsupported_or_mismatched_archive",
                                    "message": _ARCHIVE_ERROR_MESSAGE,
                                }
                            )
                        elif _is_image_upload(display_name, upload.content_type):
                            errors.append(
                                {
                                    "filename": display_name,
                                    "code": "unsupported_or_mismatched_image",
                                    "message": "Only valid PNG, JPEG, GIF, and WebP images are supported",
                                }
                            )
                        else:
                            errors.append(
                                {
                                    "filename": display_name,
                                    "code": "unsupported_or_mismatched_attachment",
                                    "message": "Only supported images, archives, and Microsoft Office files are supported",
                                }
                            )
                        continue
                    except OSError as exc:
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Asset storage failed",
                        ) from exc
                    assets.append(
                        {
                            "id": asset_id,
                            "filename": display_name,
                            "content_type": verified_type,
                            "size": len(data),
                        }
                    )
        except (Exception, asyncio.CancelledError):
            if stored_asset_ids:
                await _run_worker_to_completion(
                    _rollback_request_assets, images_root, stored_asset_ids
                )
            raise
        finally:
            if mutation_fence_acquired:
                mutation_fence.release()
            if archive_slot_acquired:
                _ARCHIVE_BATCH_LIMITER.release()
        return {"assets": assets, "errors": errors}

    async def image_response(markdown_id: str, asset_id: str, *, download: bool):
        payload, metadata, attachment_snapshot = await _load_asset(
            markdown_id, asset_id
        )
        is_office = metadata["content_type"] == OFFICE_CONTENT_TYPE
        headers = {"Cache-Control": "private, no-store"}
        if is_office:
            headers["X-Content-Type-Options"] = "nosniff"
        if attachment_snapshot is not None:
            headers["Content-Disposition"] = _attachment_content_disposition(
                metadata["filename"]
            )
            return Response(
                content=attachment_snapshot,
                media_type=metadata["content_type"],
                headers=headers,
            )
        return FileResponse(
            path=payload,
            filename=metadata["filename"],
            media_type=metadata["content_type"],
            content_disposition_type=(
                "attachment"
                if download
                or is_stored_archive_content_type(metadata["content_type"])
                or is_office
                else "inline"
            ),
            headers=headers,
        )

    @app.get("/markdown-threads/{markdown_id}/images/{asset_id}")
    async def view_markdown_image(
        request: Request,
        markdown_id: str,
        asset_id: str,
        x_api_key: str | None = Header(None),
    ):
        if not await is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )
        return await image_response(markdown_id, asset_id, download=False)

    @app.get("/markdown-threads/{markdown_id}/images/{asset_id}/download")
    async def download_markdown_image(
        request: Request,
        markdown_id: str,
        asset_id: str,
        x_api_key: str | None = Header(None),
    ):
        if not await is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )
        return await image_response(markdown_id, asset_id, download=True)

    @app.delete("/markdown-threads/{markdown_id}/images")
    async def delete_markdown_images(
        request: Request,
        markdown_id: str,
        x_api_key: str | None = Header(None),
    ) -> dict[str, int | str]:
        if not await is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )
        images_root = _images_root(markdown_id)
        mutation_fence = _NamespaceMutationFence(markdown_id)
        await mutation_fence.acquire()
        try:
            deleted_count = await _run_worker_to_completion(
                _delete_images_namespace, images_root
            )
        finally:
            mutation_fence.release()
        return {"markdown_id": markdown_id, "deleted_count": deleted_count}
