"""Archive classification and validation for synchronized Markdown assets."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from zipfile import BadZipFile, LargeZipFile, ZipFile
from zlib import error as ZlibError


class ArchiveValidationError(ValueError):
    """Raised when an uploaded archive cannot be safely validated."""


@dataclass(frozen=True)
class _ArchiveFormatSpec:
    suffix: str
    accepted_content_types: frozenset[str]
    normalized_content_type: str
    signatures: tuple[bytes, ...]


_GENERIC_CONTENT_TYPES = frozenset({"", "application/octet-stream"})
_FORMATS = (
    _ArchiveFormatSpec(
        suffix=".tar.gz",
        accepted_content_types=frozenset(
            {
                *_GENERIC_CONTENT_TYPES,
                "application/gzip",
                "application/x-gzip",
                "application/x-compressed-tar",
                "application/x-gtar",
                "application/x-tgz",
            }
        ),
        normalized_content_type="application/gzip",
        signatures=(b"\x1f\x8b",),
    ),
    _ArchiveFormatSpec(
        suffix=".tgz",
        accepted_content_types=frozenset(
            {
                *_GENERIC_CONTENT_TYPES,
                "application/gzip",
                "application/x-gzip",
                "application/x-compressed-tar",
                "application/x-gtar",
                "application/x-tgz",
            }
        ),
        normalized_content_type="application/gzip",
        signatures=(b"\x1f\x8b",),
    ),
    _ArchiveFormatSpec(
        suffix=".zip",
        accepted_content_types=frozenset(
            {
                *_GENERIC_CONTENT_TYPES,
                "application/zip",
                "application/x-zip-compressed",
            }
        ),
        normalized_content_type="application/zip",
        signatures=(b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ),
    _ArchiveFormatSpec(
        suffix=".7z",
        accepted_content_types=frozenset(
            {
                *_GENERIC_CONTENT_TYPES,
                "application/x-7z-compressed",
                "application/7z",
                "application/vnd.7zip",
            }
        ),
        normalized_content_type="application/x-7z-compressed",
        signatures=(b"7z\xbc\xaf'\x1c",),
    ),
    _ArchiveFormatSpec(
        suffix=".tar",
        accepted_content_types=frozenset(
            {
                *_GENERIC_CONTENT_TYPES,
                "application/x-tar",
                "application/tar",
            }
        ),
        normalized_content_type="application/x-tar",
        signatures=(),
    ),
)
_FORMATS_BY_SUFFIX = {spec.suffix: spec for spec in _FORMATS}
_SPECIFIC_ARCHIVE_CONTENT_TYPES = frozenset(
    content_type
    for spec in _FORMATS
    for content_type in spec.accepted_content_types
    if content_type not in _GENERIC_CONTENT_TYPES
)

ARCHIVE_CONTENT_TYPES = frozenset(
    spec.normalized_content_type for spec in _FORMATS
)

_MAX_ZIP_MEMBER_COUNT = 1_000
_MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_ZIP_VALIDATION_CHUNK_BYTES = 1024 * 1024


def archive_format_for_filename(filename: str) -> str | None:
    """Return canonical archive suffix for filename, longest suffix first."""
    lowered = filename.lower()
    for spec in _FORMATS:
        if lowered.endswith(spec.suffix):
            return spec.suffix
    return None


def normalized_archive_content_type(filename: str) -> str | None:
    """Return storage MIME for a recognized archive filename."""
    archive_format = archive_format_for_filename(filename)
    if archive_format is None:
        return None
    return _FORMATS_BY_SUFFIX[archive_format].normalized_content_type


def is_archive_upload(filename: str, content_type: str | None) -> bool:
    """Return whether filename or specific MIME identifies an archive candidate."""
    declared_type = (content_type or "").lower()
    return (
        archive_format_for_filename(filename) is not None
        or declared_type in _SPECIFIC_ARCHIVE_CONTENT_TYPES
    )


def is_stored_archive_content_type(content_type: str) -> bool:
    """Return whether MIME is one of the normalized stored archive types."""
    return content_type.lower() in ARCHIVE_CONTENT_TYPES


def is_extended_archive_filename(filename: str) -> bool:
    """Return whether filename uses a recognized non-ZIP archive suffix."""
    archive_format = archive_format_for_filename(filename)
    return archive_format is not None and archive_format != ".zip"


def _validate_zip(data: bytes) -> None:
    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ZIP_MEMBER_COUNT:
                raise ArchiveValidationError("ZIP contains too many members")
            if (
                sum(member.file_size for member in members)
                > _MAX_ZIP_UNCOMPRESSED_BYTES
            ):
                raise ArchiveValidationError(
                    "ZIP uncompressed size exceeds validation limit"
                )
            if any(member.flag_bits & 0x1 for member in members):
                raise ArchiveValidationError(
                    "encrypted ZIP members cannot be validated"
                )
            for member in members:
                with archive.open(member) as member_file:
                    while member_file.read(_ZIP_VALIDATION_CHUNK_BYTES):
                        pass
    except ArchiveValidationError:
        raise
    except (
        BadZipFile,
        LargeZipFile,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        ZlibError,
    ):
        raise ArchiveValidationError(
            "ZIP structure or member integrity is invalid"
        ) from None


def validate_archive(filename: str, content_type: str | None, data: bytes) -> str:
    """Validate archive agreement and return its normalized storage MIME."""
    archive_format = archive_format_for_filename(filename)
    if archive_format is None:
        raise ArchiveValidationError("archive filename extension is unsupported")

    spec = _FORMATS_BY_SUFFIX[archive_format]
    declared_type = (content_type or "").lower()
    if declared_type not in spec.accepted_content_types or (
        spec.signatures and not data.startswith(spec.signatures)
    ):
        raise ArchiveValidationError(
            "archive extension, MIME type, and signature must agree"
        )

    if archive_format == ".zip":
        _validate_zip(data)
        return spec.normalized_content_type

    raise ArchiveValidationError("archive format validation is not yet supported")
