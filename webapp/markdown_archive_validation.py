"""Archive classification and validation for synchronized Markdown assets."""

from __future__ import annotations

import tarfile
from dataclasses import dataclass
from gzip import BadGzipFile, GzipFile
from io import BytesIO
from tarfile import TarError
from zipfile import BadZipFile, LargeZipFile, ZipFile
from zlib import error as ZlibError

from py7zr import SevenZipFile, UnsupportedCompressionMethodError
from py7zr.exceptions import PasswordRequired
from py7zr.io import Py7zIO, WriterFactory


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

ARCHIVE_CONTENT_TYPES = frozenset(spec.normalized_content_type for spec in _FORMATS)

_MAX_ZIP_MEMBER_COUNT = 1_000
_MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_ZIP_VALIDATION_CHUNK_BYTES = 1024 * 1024

MAX_MEMBER_COUNT = 1_000
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_TAR_METADATA_RECORDS = (2 * MAX_MEMBER_COUNT) + 1
MAX_TAR_METADATA_BYTES = 1024 * 1024
MAX_TAR_ZERO_BLOCKS = 20
MAX_TAR_STREAM_BYTES = (
    MAX_EXPANDED_BYTES
    + MAX_TAR_METADATA_BYTES
    + ((MAX_MEMBER_COUNT + MAX_TAR_METADATA_RECORDS) * 512)
    + ((MAX_MEMBER_COUNT + MAX_TAR_METADATA_RECORDS) * 511)
    + (MAX_TAR_ZERO_BLOCKS * 512)
)

_TAR_BLOCK_BYTES = 512
_TAR_METADATA_TYPE_FLAGS = frozenset({b"x", b"X", b"g", b"L", b"K"})
_TAR_VALIDATION_CHUNK_BYTES = 1024 * 1024


@dataclass
class _SevenZipDecodedBudget:
    decoded_bytes: int = 0

    def consume(self, size: int) -> None:
        self.decoded_bytes += size
        if self.decoded_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ArchiveValidationError("7z decoded output exceeds validation limit")


class _SevenZipDiscardIO(Py7zIO):
    def __init__(self, budget: _SevenZipDecodedBudget) -> None:
        self._budget = budget
        self._position = 0
        self._size = 0

    def write(self, data: bytes | bytearray) -> int:
        size = len(data)
        self._budget.consume(size)
        self._position += size
        self._size = max(self._size, self._position)
        return size

    def read(self, size: int | None = None) -> bytes:
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            position = offset
        elif whence == 1:
            position = self._position + offset
        elif whence == 2:
            position = self._size + offset
        else:
            raise ValueError("invalid whence")
        if position < 0:
            raise ValueError("negative seek position")
        self._position = position
        return position

    def flush(self) -> None:
        return None

    def size(self) -> int:
        return self._size


class _SevenZipDiscardFactory(WriterFactory):
    def __init__(self, budget: _SevenZipDecodedBudget) -> None:
        self._budget = budget

    def create(self, filename: str) -> Py7zIO:
        return _SevenZipDiscardIO(self._budget)


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


def _validate_7z_coder_chains(archive: SevenZipFile) -> None:
    main_streams = archive.header.main_streams
    if main_streams is None:
        return
    for folder in main_streams.unpackinfo.folders:
        folder.get_decompressor(0, reset=True)


def _validate_7z(data: bytes) -> None:
    try:
        with SevenZipFile(BytesIO(data), mode="r") as archive:
            if archive.needs_password():
                raise ArchiveValidationError(
                    "encrypted 7z archives cannot be validated"
                )
            _validate_7z_coder_chains(archive)
            members = [member for member in archive.list() if not member.is_directory]
            if len(members) > MAX_MEMBER_COUNT:
                raise ArchiveValidationError("7z contains too many members")

            declared_bytes = 0
            for member in members:
                if member.uncompressed < 0:
                    raise ArchiveValidationError(
                        "7z structure or member integrity is invalid"
                    )
                declared_bytes += member.uncompressed
                if declared_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise ArchiveValidationError(
                        "7z uncompressed size exceeds validation limit"
                    )

            if members and archive.test() is False:
                raise ArchiveValidationError(
                    "7z structure or member integrity is invalid"
                )

        if not members:
            return

        budget = _SevenZipDecodedBudget()
        with SevenZipFile(BytesIO(data), mode="r") as archive:
            if archive.needs_password():
                raise ArchiveValidationError(
                    "encrypted 7z archives cannot be validated"
                )
            archive.extractall(factory=_SevenZipDiscardFactory(budget))
    except ArchiveValidationError:
        raise
    except PasswordRequired:
        raise ArchiveValidationError(
            "encrypted 7z archives cannot be validated"
        ) from None
    except UnsupportedCompressionMethodError:
        raise ArchiveValidationError("7z compression method is unsupported") from None
    except Exception:
        raise ArchiveValidationError(
            "7z structure or member integrity is invalid"
        ) from None


def _parse_tar_number(field: bytes) -> int:
    if field[0] & 0x80:
        if field[0] & 0x40:
            raise ArchiveValidationError("TAR structure or member integrity is invalid")
        return int.from_bytes(bytes((field[0] & 0x7F,)) + field[1:], "big")

    stripped = field.rstrip(b"\0 ").lstrip(b" ")
    if not stripped:
        return 0
    if any(byte < ord("0") or byte > ord("7") for byte in stripped):
        raise ArchiveValidationError("TAR structure or member integrity is invalid")
    return int(stripped, 8)


def _validate_tar_header_checksum(header: bytes) -> None:
    stored_checksum = _parse_tar_number(header[148:156])
    checksum_header = header[:148] + (b" " * 8) + header[156:]
    unsigned_checksum = sum(checksum_header)
    signed_checksum = sum(
        byte if byte < 128 else byte - 256 for byte in checksum_header
    )
    if stored_checksum not in {unsigned_checksum, signed_checksum}:
        raise ArchiveValidationError("TAR structure or member integrity is invalid")


def _parse_pax_size_override(payload: bytes) -> int | None:
    offset = 0
    size_override = None
    while offset < len(payload):
        if payload[offset] == 0:
            if any(payload[offset:]):
                raise ArchiveValidationError(
                    "TAR structure or member integrity is invalid"
                )
            break

        prefix_end = payload.find(b" ", offset)
        length_field = payload[offset:prefix_end]
        if (
            prefix_end < 0
            or not length_field
            or len(length_field) > 20
            or not length_field.isdigit()
        ):
            raise ArchiveValidationError("TAR structure or member integrity is invalid")
        record_length = int(length_field)
        record_end = offset + record_length
        if (
            record_length < 5
            or prefix_end >= record_end
            or record_end > len(payload)
            or payload[record_end - 1] != ord("\n")
        ):
            raise ArchiveValidationError("TAR structure or member integrity is invalid")

        keyword, separator, value = payload[prefix_end + 1 : record_end - 1].partition(
            b"="
        )
        if not keyword or not separator:
            raise ArchiveValidationError("TAR structure or member integrity is invalid")
        if keyword == b"size":
            if not value or len(value) > 20 or not value.isdigit():
                raise ArchiveValidationError(
                    "TAR structure or member integrity is invalid"
                )
            size_override = int(value)
        offset = record_end

    return size_override


def _rewrite_tar_header_size(data: bytearray, offset: int, size: int) -> None:
    data[offset + 124 : offset + 136] = f"{size:011o}\0".encode("ascii")
    data[offset + 148 : offset + 156] = b"        "
    checksum = sum(data[offset : offset + _TAR_BLOCK_BYTES])
    data[offset + 148 : offset + 156] = f"{checksum:06o}\0 ".encode("ascii")


def _scan_tar_framing(data: bytes) -> bytes | bytearray:
    offset = 0
    member_count = 0
    declared_payload_bytes = 0
    metadata_count = 0
    metadata_payload_bytes = 0
    global_pax_size: int | None = None
    local_pax_size: int | None = None
    local_pax_active = False
    semantic_data: bytearray | None = None

    while offset < len(data):
        header_end = offset + _TAR_BLOCK_BYTES
        if header_end > len(data):
            raise ArchiveValidationError("TAR structure or member integrity is invalid")
        header = data[offset:header_end]
        if not any(header):
            break

        _validate_tar_header_checksum(header)
        raw_size = _parse_tar_number(header[124:136])
        type_flag = header[156:157]
        is_metadata = type_flag in _TAR_METADATA_TYPE_FLAGS
        if is_metadata:
            size = raw_size
        elif local_pax_size is not None:
            size = local_pax_size
        elif global_pax_size is not None:
            size = global_pax_size
        else:
            size = raw_size

        payload_end = header_end + size
        record_end = payload_end + (-size % _TAR_BLOCK_BYTES)
        if payload_end > len(data) or record_end > len(data):
            raise ArchiveValidationError("TAR structure or member integrity is invalid")
        if any(data[payload_end:record_end]):
            raise ArchiveValidationError("TAR record padding is invalid")

        if is_metadata:
            metadata_count += 1
            metadata_payload_bytes += size
            if metadata_count > MAX_TAR_METADATA_RECORDS:
                raise ArchiveValidationError("TAR contains too many metadata records")
            if metadata_payload_bytes > MAX_TAR_METADATA_BYTES:
                raise ArchiveValidationError(
                    "TAR metadata payload exceeds validation limit"
                )
            if type_flag in {b"x", b"X", b"g"}:
                pax_size = _parse_pax_size_override(data[header_end:payload_end])
                if type_flag in {b"x", b"X"}:
                    if not local_pax_active:
                        local_pax_active = True
                        local_pax_size = (
                            pax_size if pax_size is not None else global_pax_size
                        )
                    elif local_pax_size is None and pax_size is not None:
                        local_pax_size = pax_size
                elif type_flag == b"g" and pax_size is not None:
                    global_pax_size = pax_size
        else:
            member_count += 1
            declared_payload_bytes += size
            local_pax_active = False
            local_pax_size = None
            if member_count > MAX_MEMBER_COUNT:
                raise ArchiveValidationError("TAR contains too many members")
            if declared_payload_bytes > MAX_EXPANDED_BYTES:
                raise ArchiveValidationError(
                    "TAR declared payload exceeds validation limit"
                )
            if size != raw_size:
                if semantic_data is None:
                    semantic_data = bytearray(data)
                _rewrite_tar_header_size(semantic_data, offset, size)

        offset = record_end

    if offset == len(data):
        raise ArchiveValidationError("TAR end marker is invalid")

    trailing = data[offset:]
    full_blocks, partial_bytes = divmod(len(trailing), _TAR_BLOCK_BYTES)
    if partial_bytes:
        raise ArchiveValidationError("TAR trailing data is invalid")
    if any(trailing):
        raise ArchiveValidationError("TAR trailing data is invalid")
    if full_blocks < 2 or full_blocks > MAX_TAR_ZERO_BLOCKS:
        raise ArchiveValidationError("TAR end marker is invalid")

    return semantic_data if semantic_data is not None else data


def _validate_raw_tar(data: bytes) -> None:
    semantic_data = _scan_tar_framing(data)
    try:
        expanded_bytes = 0
        semantic_member_count = 0
        with tarfile.open(fileobj=BytesIO(semantic_data), mode="r:") as archive:
            for member in archive:
                semantic_member_count += 1
                if semantic_member_count > MAX_MEMBER_COUNT:
                    raise ArchiveValidationError("TAR contains too many members")
                if not member.isreg():
                    continue
                member_file = archive.extractfile(member)
                if member_file is None:
                    raise ArchiveValidationError(
                        "TAR structure or member integrity is invalid"
                    )
                with member_file:
                    while chunk := member_file.read(_TAR_VALIDATION_CHUNK_BYTES):
                        expanded_bytes += len(chunk)
                        if expanded_bytes > MAX_EXPANDED_BYTES:
                            raise ArchiveValidationError(
                                "TAR expanded payload exceeds validation limit"
                            )
    except ArchiveValidationError:
        raise
    except (
        TarError,
        EOFError,
        OSError,
        ValueError,
        UnicodeError,
        ZlibError,
        RuntimeError,
    ):
        raise ArchiveValidationError(
            "TAR structure or member integrity is invalid"
        ) from None


def _decompress_gzip_tar(data: bytes) -> bytes:
    try:
        output = BytesIO()
        with GzipFile(fileobj=BytesIO(data), mode="rb") as compressed:
            while True:
                remaining = MAX_TAR_STREAM_BYTES - output.tell()
                chunk = compressed.read(min(_TAR_VALIDATION_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ArchiveValidationError(
                        "gzip TAR stream exceeds validation limit"
                    )
                output.write(chunk)
        return output.getvalue()
    except ArchiveValidationError:
        raise
    except (
        BadGzipFile,
        EOFError,
        OSError,
        ValueError,
        UnicodeError,
        ZlibError,
    ):
        raise ArchiveValidationError(
            "gzip TAR structure or integrity is invalid"
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

    if archive_format == ".7z":
        _validate_7z(data)
        return spec.normalized_content_type

    if archive_format == ".tar":
        _validate_raw_tar(data)
        return spec.normalized_content_type

    if archive_format in {".tar.gz", ".tgz"}:
        _validate_raw_tar(_decompress_gzip_tar(data))
        return spec.normalized_content_type

    raise ArchiveValidationError("archive format validation is not yet supported")
