"""Archive classification and validation for synchronized Markdown assets."""

from __future__ import annotations

import bz2
import lzma
import tarfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from gzip import BadGzipFile, GzipFile
from io import BytesIO
from tarfile import TarError
from zipfile import (
    ZIP_BZIP2,
    ZIP_DEFLATED,
    ZIP_LZMA,
    ZIP_STORED,
    BadZipFile,
    LargeZipFile,
    ZipFile,
    ZipInfo,
)
from zlib import error as ZlibError

from py7zr import SevenZipFile, UnsupportedCompressionMethodError
from py7zr.exceptions import PasswordRequired
from py7zr.io import Py7zIO, WriterFactory
from py7zr.properties import COMPRESSION_METHOD


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
_ZIP_LOCAL_HEADER_BYTES = 30

MAX_MEMBER_COUNT = 1_000
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_7Z_DECODER_MEMORY_BYTES = 100 * 1024 * 1024
_MAX_7Z_ENCODED_HEADER_BYTES = 100 * 1024 * 1024
_MAX_7Z_HEADER_FOLDERS = 1_000
_MAX_7Z_HEADER_CODERS = 1_000
_MAX_7Z_HEADER_STREAMS = 1_000
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


@dataclass
class _SevenZipEncodedFolder:
    coders: list[dict[str, object]]
    input_streams: int
    output_streams: int
    unpacksizes: list[int]
    digestdefined: bool = False


@dataclass
class _SevenZipHeaderReader:
    data: memoryview
    offset: int = 0
    furthest_offset: int = 0

    def read(self, size: int) -> memoryview:
        end = self.offset + size
        if size < 0 or end < self.offset or end > len(self.data):
            raise ValueError("truncated 7z encoded header")
        value = self.data[self.offset : end]
        self.offset = end
        self.furthest_offset = max(self.furthest_offset, end)
        return value

    def read_byte(self) -> int:
        return self.read(1)[0]

    def read_uint64(self) -> int:
        first = self.read_byte()
        mask = 0x80
        extra_bytes = 0
        while extra_bytes < 8 and first & mask:
            extra_bytes += 1
            mask >>= 1
        if extra_bytes == 8:
            return int.from_bytes(self.read(8), "little")
        low = int.from_bytes(self.read(extra_bytes), "little")
        return low + ((first & (mask - 1)) << (8 * extra_bytes))


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


def _zip_compressed_payload(data: bytes, member: ZipInfo) -> memoryview:
    header_offset = member.header_offset
    if header_offset < 0 or header_offset + _ZIP_LOCAL_HEADER_BYTES > len(data):
        raise BadZipFile("truncated local header")
    if data[header_offset : header_offset + 4] != b"PK\x03\x04":
        raise BadZipFile("invalid local header")

    name_bytes = int.from_bytes(data[header_offset + 26 : header_offset + 28], "little")
    extra_bytes = int.from_bytes(
        data[header_offset + 28 : header_offset + 30], "little"
    )
    payload_start = header_offset + _ZIP_LOCAL_HEADER_BYTES + name_bytes + extra_bytes
    payload_end = payload_start + member.compress_size
    if payload_end < payload_start or payload_end > len(data):
        raise BadZipFile("truncated compressed payload")
    return memoryview(data)[payload_start:payload_end]


def _zip_lzma_decompressor(payload: memoryview) -> tuple[lzma.LZMADecompressor, int]:
    if len(payload) < 4:
        raise BadZipFile("truncated LZMA properties")
    properties_size = int.from_bytes(payload[2:4], "little")
    properties_end = 4 + properties_size
    if properties_size != 5 or properties_end > len(payload):
        raise BadZipFile("invalid LZMA properties")

    properties = payload[4:properties_end]
    property_byte = properties[0]
    if property_byte >= 9 * 5 * 5:
        raise BadZipFile("invalid LZMA properties")
    lc = property_byte % 9
    remainder = property_byte // 9
    lp = remainder % 5
    pb = remainder // 5
    dictionary_size = int.from_bytes(properties[1:5], "little")
    if dictionary_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise BadZipFile("LZMA dictionary exceeds validation limit")
    return (
        lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[
                {
                    "id": lzma.FILTER_LZMA1,
                    "dict_size": dictionary_size,
                    "lc": lc,
                    "lp": lp,
                    "pb": pb,
                }
            ],
        ),
        properties_end,
    )


def _validate_zip_member_payload(
    data: bytes, member: ZipInfo, expanded_bytes: int
) -> int:
    payload = _zip_compressed_payload(data, member)
    member_bytes = 0
    checksum = 0

    def consume(output: bytes) -> None:
        nonlocal checksum, expanded_bytes, member_bytes
        member_bytes += len(output)
        expanded_bytes += len(output)
        if expanded_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ArchiveValidationError(
                "ZIP expanded payload exceeds validation limit"
            )
        checksum = zlib.crc32(output, checksum)

    if member.compress_type == ZIP_STORED:
        for offset in range(0, len(payload), _ZIP_VALIDATION_CHUNK_BYTES):
            consume(bytes(payload[offset : offset + _ZIP_VALIDATION_CHUNK_BYTES]))
    elif member.compress_type == ZIP_DEFLATED:
        decompressor = zlib.decompressobj(-15)
        for offset in range(0, len(payload), _ZIP_VALIDATION_CHUNK_BYTES):
            pending = payload[offset : offset + _ZIP_VALIDATION_CHUNK_BYTES]
            while pending:
                output = decompressor.decompress(
                    pending, MAX_TOTAL_UNCOMPRESSED_BYTES - expanded_bytes + 1
                )
                consume(output)
                pending = decompressor.unconsumed_tail
        if not decompressor.eof or decompressor.unused_data:
            raise BadZipFile("incomplete or trailing DEFLATE stream")
    elif member.compress_type == ZIP_BZIP2:
        decompressor = bz2.BZ2Decompressor()
        for offset in range(0, len(payload), _ZIP_VALIDATION_CHUNK_BYTES):
            chunk_end = min(offset + _ZIP_VALIDATION_CHUNK_BYTES, len(payload))
            pending = payload[offset:chunk_end]
            while not decompressor.eof and (pending or not decompressor.needs_input):
                output = decompressor.decompress(
                    pending, MAX_TOTAL_UNCOMPRESSED_BYTES - expanded_bytes + 1
                )
                consume(output)
                pending = b""
            if decompressor.eof and chunk_end != len(payload):
                raise BadZipFile("trailing BZIP2 stream data")
        if not decompressor.eof or decompressor.unused_data:
            raise BadZipFile("incomplete or trailing BZIP2 stream")
    elif member.compress_type == ZIP_LZMA:
        decompressor, payload_offset = _zip_lzma_decompressor(payload)
        for offset in range(payload_offset, len(payload), _ZIP_VALIDATION_CHUNK_BYTES):
            chunk_end = min(offset + _ZIP_VALIDATION_CHUNK_BYTES, len(payload))
            pending = payload[offset:chunk_end]
            while not decompressor.eof and (pending or not decompressor.needs_input):
                output = decompressor.decompress(
                    pending, MAX_TOTAL_UNCOMPRESSED_BYTES - expanded_bytes + 1
                )
                consume(output)
                pending = b""
            if decompressor.eof and chunk_end != len(payload):
                raise BadZipFile("trailing LZMA stream data")
        if not decompressor.eof or decompressor.unused_data:
            raise BadZipFile("incomplete or trailing LZMA stream")
    else:
        raise NotImplementedError("unsupported ZIP compression method")

    if member_bytes != member.file_size or checksum != member.CRC:
        raise BadZipFile("ZIP member metadata does not match decoded payload")
    return expanded_bytes


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
            expanded_bytes = 0
            for member in members:
                expanded_bytes = _validate_zip_member_payload(
                    data, member, expanded_bytes
                )
                with archive.open(member):
                    pass
    except ArchiveValidationError:
        raise
    except (
        BadZipFile,
        lzma.LZMAError,
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


def _read_seven_zip_defined_flags(
    reader: _SevenZipHeaderReader, count: int, *, check_all: bool
) -> list[bool]:
    if check_all and reader.read_byte() != 0:
        return [True] * count
    bits = reader.read((count + 7) // 8)
    return [bool(bits[index // 8] & (0x80 >> (index % 8))) for index in range(count)]


def _skip_seven_zip_crcs(
    reader: _SevenZipHeaderReader, count: int, *, check_all: bool
) -> list[bool]:
    defined = _read_seven_zip_defined_flags(reader, count, check_all=check_all)
    reader.read(4 * sum(defined))
    return defined


def _read_seven_zip_encoded_folder(
    reader: _SevenZipHeaderReader,
    *,
    coder_budget: int,
    input_stream_budget: int,
    output_stream_budget: int,
) -> _SevenZipEncodedFolder:
    coder_count = reader.read_uint64()
    if coder_count == 0 or coder_count > coder_budget:
        raise ValueError("invalid 7z encoded header coder count")

    coders: list[dict[str, object]] = []
    total_input_streams = 0
    total_output_streams = 0
    for _ in range(coder_count):
        flags = reader.read_byte()
        if flags & 0xC0:
            raise ValueError("invalid 7z encoded header coder flags")
        method_size = flags & 0x0F
        if method_size == 0:
            method = b"\x00"
        else:
            method = bytes(reader.read(method_size))

        if flags & 0x10:
            input_streams = reader.read_uint64()
            output_streams = reader.read_uint64()
        else:
            input_streams = 1
            output_streams = 1
        if input_streams == 0 or output_streams == 0:
            raise ValueError("invalid 7z encoded header stream count")
        total_input_streams += input_streams
        total_output_streams += output_streams
        if (
            total_input_streams > input_stream_budget
            or total_output_streams > output_stream_budget
        ):
            raise ValueError("7z encoded header has too many streams")

        properties: bytes | None = None
        if flags & 0x20:
            property_size = reader.read_uint64()
            if property_size > len(reader.data) - reader.offset:
                raise ValueError("invalid 7z encoded header coder properties")
            properties = bytes(reader.read(property_size))
        coders.append(
            {
                "method": method,
                "numinstreams": input_streams,
                "numoutstreams": output_streams,
                "properties": properties,
            }
        )

    bind_pair_count = total_output_streams - 1
    for _ in range(bind_pair_count):
        reader.read_uint64()
        reader.read_uint64()
    packed_stream_count = total_input_streams - bind_pair_count
    if packed_stream_count <= 0 or packed_stream_count > _MAX_7Z_HEADER_STREAMS:
        raise ValueError("invalid 7z encoded header packed stream count")
    if packed_stream_count != 1:
        for _ in range(packed_stream_count):
            reader.read_uint64()

    return _SevenZipEncodedFolder(
        coders=coders,
        input_streams=total_input_streams,
        output_streams=total_output_streams,
        unpacksizes=[],
    )


def _read_seven_zip_encoded_streams(
    encoded_header: memoryview,
) -> tuple[int, list[int], list[_SevenZipEncodedFolder]]:
    reader = _SevenZipHeaderReader(encoded_header)
    pack_position = 0
    packed_sizes: list[int] = []
    folders: list[_SevenZipEncodedFolder] = []

    property_id = reader.read_byte()
    if property_id == 0x06:
        pack_position = reader.read_uint64()
        packed_stream_count = reader.read_uint64()
        if packed_stream_count > _MAX_7Z_HEADER_STREAMS:
            raise ValueError("7z encoded header has too many packed streams")
        if reader.read_byte() != 0x09:
            raise ValueError("missing 7z encoded header packed sizes")
        packed_sizes = [reader.read_uint64() for _ in range(packed_stream_count)]
        property_id = reader.read_byte()
        if property_id == 0x0A:
            _skip_seven_zip_crcs(reader, packed_stream_count, check_all=True)
            property_id = reader.read_byte()
        if property_id != 0x00:
            raise ValueError("invalid 7z encoded header pack info")
        property_id = reader.read_byte()

    if property_id != 0x07 or reader.read_byte() != 0x0B:
        raise ValueError("missing 7z encoded header unpack info")
    folder_count = reader.read_uint64()
    if folder_count == 0 or folder_count > _MAX_7Z_HEADER_FOLDERS:
        raise ValueError("invalid 7z encoded header folder count")
    external = reader.read_byte()
    coder_budget = _MAX_7Z_HEADER_CODERS
    input_stream_budget = _MAX_7Z_HEADER_STREAMS
    output_stream_budget = _MAX_7Z_HEADER_STREAMS

    def read_folders() -> list[_SevenZipEncodedFolder]:
        nonlocal coder_budget, input_stream_budget, output_stream_budget
        parsed: list[_SevenZipEncodedFolder] = []
        for _ in range(folder_count):
            folder = _read_seven_zip_encoded_folder(
                reader,
                coder_budget=coder_budget,
                input_stream_budget=input_stream_budget,
                output_stream_budget=output_stream_budget,
            )
            coder_budget -= len(folder.coders)
            input_stream_budget -= folder.input_streams
            output_stream_budget -= folder.output_streams
            parsed.append(folder)
        return parsed

    if external == 0:
        folders = read_folders()
    else:
        external_offset = reader.read_uint64()
        current_offset = reader.offset
        if external_offset > len(reader.data):
            raise ValueError("invalid external 7z encoded header folder offset")
        reader.offset = external_offset
        folders = read_folders()
        reader.offset = current_offset

    if reader.read_byte() != 0x0C:
        raise ValueError("missing 7z encoded header unpack sizes")
    total_output_streams = sum(folder.output_streams for folder in folders)
    if total_output_streams > _MAX_7Z_HEADER_STREAMS:
        raise ValueError("7z encoded header has too many output streams")
    for folder in folders:
        folder.unpacksizes = [
            reader.read_uint64() for _ in range(folder.output_streams)
        ]

    property_id = reader.read_byte()
    if property_id == 0x0A:
        defined = _skip_seven_zip_crcs(reader, folder_count, check_all=True)
        for folder, digestdefined in zip(folders, defined, strict=True):
            folder.digestdefined = digestdefined
        property_id = reader.read_byte()
    if property_id != 0x00:
        raise ValueError("invalid 7z encoded header unpack info")

    property_id = reader.read_byte()
    if property_id == 0x08:
        substream_counts = [1] * folder_count
        property_id = reader.read_byte()
        if property_id == 0x0D:
            substream_counts = [reader.read_uint64() for _ in range(folder_count)]
            if sum(substream_counts) > _MAX_7Z_HEADER_STREAMS:
                raise ValueError("7z encoded header has too many substreams")
            property_id = reader.read_byte()
        if property_id == 0x09:
            for substream_count in substream_counts:
                for _ in range(max(0, substream_count - 1)):
                    reader.read_uint64()
            property_id = reader.read_byte()
        digest_count = sum(
            count if count != 1 or not folder.digestdefined else 0
            for count, folder in zip(substream_counts, folders, strict=True)
        )
        if property_id == 0x0A:
            _skip_seven_zip_crcs(reader, digest_count, check_all=True)
            property_id = reader.read_byte()
        if property_id != 0x00:
            raise ValueError("invalid 7z encoded header substreams")
        property_id = reader.read_byte()

    if property_id != 0x00 or reader.furthest_offset != len(reader.data):
        raise ValueError("invalid trailing 7z encoded header data")
    return pack_position, packed_sizes, folders


def _seven_zip_decoder_memory(coder: dict[str, object]) -> int:
    method = coder.get("method")
    properties = coder.get("properties")
    if method == COMPRESSION_METHOD.LZMA:
        if not isinstance(properties, bytes) or len(properties) != 5:
            raise ArchiveValidationError("7z structure or member integrity is invalid")
        return int.from_bytes(properties[1:5], "little")
    if method == COMPRESSION_METHOD.LZMA2:
        if not isinstance(properties, bytes) or len(properties) != 1:
            raise ArchiveValidationError("7z structure or member integrity is invalid")
        property_byte = properties[0]
        if property_byte > 40:
            raise ArchiveValidationError("7z structure or member integrity is invalid")
        return (
            0xFFFFFFFF
            if property_byte == 40
            else (2 | (property_byte & 1)) << (property_byte // 2 + 11)
        )
    if method == COMPRESSION_METHOD.PPMD:
        if not isinstance(properties, bytes) or len(properties) not in {5, 7}:
            raise ArchiveValidationError("7z structure or member integrity is invalid")
        return int.from_bytes(properties[1:5], "little")
    return 0


def _validate_7z_folder_resources(folders: Iterable[object]) -> None:
    decoder_memory = 0
    for folder in folders:
        for coder in folder.coders:
            decoder_memory += _seven_zip_decoder_memory(coder)
            if decoder_memory > _MAX_7Z_DECODER_MEMORY_BYTES:
                raise ArchiveValidationError(
                    "7z decoder memory exceeds validation limit"
                )


def _preflight_7z_encoded_header(data: bytes) -> None:
    if len(data) < 32 or zlib.crc32(data[12:32]) != int.from_bytes(
        data[8:12], "little"
    ):
        raise ValueError("invalid 7z signature header")
    next_header_start = 32 + int.from_bytes(data[12:20], "little")
    next_header_size = int.from_bytes(data[20:28], "little")
    next_header_end = next_header_start + next_header_size
    if (
        next_header_start < 32
        or next_header_end < next_header_start
        or next_header_end > len(data)
    ):
        raise ValueError("invalid 7z next header bounds")
    next_header = memoryview(data)[next_header_start:next_header_end]
    if zlib.crc32(next_header) != int.from_bytes(data[28:32], "little"):
        raise ValueError("invalid 7z next header checksum")
    if not next_header or next_header[0] != 0x17:
        return

    pack_position, packed_sizes, folders = _read_seven_zip_encoded_streams(
        next_header[1:]
    )
    packed_start = 32 + pack_position
    packed_end = packed_start + sum(packed_sizes)
    if packed_start < 32 or packed_end < packed_start or packed_end > next_header_start:
        raise ValueError("invalid 7z encoded header stream bounds")

    _validate_7z_folder_resources(folders)
    declared_output = sum(
        unpack_size for folder in folders for unpack_size in folder.unpacksizes
    )
    if declared_output > _MAX_7Z_ENCODED_HEADER_BYTES:
        raise ArchiveValidationError(
            "7z encoded header expansion exceeds validation limit"
        )


def _validate_7z_coder_chains(
    archive: SevenZipFile, *, construct_decoders: bool = True
) -> None:
    main_streams = archive.header.main_streams
    if main_streams is None:
        return
    folders = main_streams.unpackinfo.folders
    _validate_7z_folder_resources(folders)
    if construct_decoders:
        for folder in folders:
            folder.get_decompressor(0, reset=True)


def _validate_7z(data: bytes) -> None:
    try:
        _preflight_7z_encoded_header(data)
        with SevenZipFile(BytesIO(data), mode="r") as archive:
            if archive.needs_password():
                raise ArchiveValidationError(
                    "encrypted 7z archives cannot be validated"
                )
            entries = archive.list()
            if len(entries) > MAX_MEMBER_COUNT:
                raise ArchiveValidationError("7z contains too many members")
            members = [member for member in entries if not member.is_directory]

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

            _validate_7z_coder_chains(archive)
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
            _validate_7z_coder_chains(archive, construct_decoders=False)
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
