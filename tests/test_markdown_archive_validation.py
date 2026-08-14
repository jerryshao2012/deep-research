"""Focused tests for Markdown archive classification and validation."""

from __future__ import annotations

import gzip
import tarfile
from contextlib import nullcontext
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import webapp.markdown_archive_validation as archive_validation
from webapp.markdown_archive_validation import (
    ARCHIVE_CONTENT_TYPES,
    ArchiveValidationError,
    archive_format_for_filename,
    is_archive_upload,
    is_extended_archive_filename,
    is_stored_archive_content_type,
    normalized_archive_content_type,
    validate_archive,
)

_TAR_BLOCK_BYTES = 512


def _tar_size_field(size: int, *, base_256: bool = False) -> bytes:
    if base_256:
        return ((1 << 95) | size).to_bytes(12, "big")
    return f"{size:011o}\0".encode("ascii")


def _tar_header(
    filename: str,
    size: int,
    *,
    type_flag: bytes = b"0",
    base_256_size: bool = False,
) -> bytes:
    header = bytearray(_TAR_BLOCK_BYTES)
    header[0:100] = filename.encode("ascii").ljust(100, b"\0")
    header[100:108] = b"0000644\0"
    header[108:116] = b"0000000\0"
    header[116:124] = b"0000000\0"
    header[124:136] = _tar_size_field(size, base_256=base_256_size)
    header[136:148] = b"00000000000\0"
    header[148:156] = b"        "
    header[156:157] = type_flag
    header[257:263] = b"ustar\0"
    header[263:265] = b"00"
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}\0 ".encode("ascii")
    return bytes(header)


def _tar_record(
    filename: str,
    content: bytes = b"",
    *,
    type_flag: bytes = b"0",
    declared_size: int | None = None,
    base_256_size: bool = False,
    padding_byte: bytes = b"\0",
) -> bytes:
    size = len(content) if declared_size is None else declared_size
    padding_size = (-len(content)) % _TAR_BLOCK_BYTES
    return (
        _tar_header(
            filename,
            size,
            type_flag=type_flag,
            base_256_size=base_256_size,
        )
        + content
        + (padding_byte * padding_size)
    )


def _raw_tar(*records: bytes, zero_blocks: int = 2, trailing: bytes = b"") -> bytes:
    return b"".join(records) + (b"\0" * _TAR_BLOCK_BYTES * zero_blocks) + trailing


def _pax_record(key: str, value: str) -> bytes:
    body = f"{key}={value}\n".encode("ascii")
    length = len(body) + 2
    while True:
        candidate = len(body) + len(str(length)) + 1
        if candidate == length:
            return f"{length} ".encode("ascii") + body
        length = candidate


def _tar_with_local_pax_size(
    raw_size: int,
    content: bytes,
    *,
    type_flag: bytes = b"x",
) -> bytes:
    return _raw_tar(
        _tar_record(
            "local-pax",
            _pax_record("size", str(len(content))),
            type_flag=type_flag,
        ),
        _tar_record("pax-sized.txt", content, declared_size=raw_size),
        _tar_record("tail.txt", b"tail"),
    )


def _tar_with_global_pax_size(raw_size: int, content: bytes) -> bytes:
    return _raw_tar(
        _tar_record(
            "global-pax",
            _pax_record("size", str(len(content))),
            type_flag=b"g",
        ),
        _tar_record("global-one.txt", content, declared_size=raw_size),
        _tar_record("global-two.txt", content, declared_size=raw_size),
    )


def _zip_bytes(filename: str = "report.txt", content: bytes = b"report") -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()


def _zip_with_invalid_utf8_filename() -> bytes:
    data = bytearray(_zip_bytes(filename="a.txt"))
    central_header = data.index(b"PK\x01\x02")
    flag_bits = int.from_bytes(data[central_header + 8 : central_header + 10], "little")
    data[central_header + 8 : central_header + 10] = (flag_bits | 0x800).to_bytes(
        2, "little"
    )
    data[central_header + 46] = 0xFF
    return bytes(data)


def _zip_with_negative_member_offset() -> bytes:
    data = bytearray(_zip_bytes())
    end_header = data.index(b"PK\x05\x06")
    central_offset = int.from_bytes(data[end_header + 16 : end_header + 20], "little")
    data[end_header + 16 : end_header + 20] = (central_offset + 1).to_bytes(4, "little")
    return bytes(data)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("evidence.zip", ".zip"),
        ("evidence.7Z", ".7z"),
        ("evidence.tar", ".tar"),
        ("evidence.TAR.GZ", ".tar.gz"),
        ("evidence.tgz", ".tgz"),
        ("evidence.tar.gz.zip", ".zip"),
        ("evidence.txt", None),
    ],
)
def test_archive_format_for_filename_uses_longest_case_insensitive_suffix(
    filename: str, expected: str | None
) -> None:
    assert archive_format_for_filename(filename) == expected


def test_archive_content_types_are_exact_normalized_storage_types() -> None:
    assert ARCHIVE_CONTENT_TYPES == frozenset(
        {
            "application/zip",
            "application/x-7z-compressed",
            "application/x-tar",
            "application/gzip",
        }
    )
    assert normalized_archive_content_type("a.zip") == "application/zip"
    assert normalized_archive_content_type("a.7z") == "application/x-7z-compressed"
    assert normalized_archive_content_type("a.tar") == "application/x-tar"
    assert normalized_archive_content_type("a.tar.gz") == "application/gzip"
    assert normalized_archive_content_type("a.tgz") == "application/gzip"
    assert normalized_archive_content_type("a.txt") is None


@pytest.mark.parametrize(
    "content_type",
    [
        "application/zip",
        "application/x-zip-compressed",
        "application/x-7z-compressed",
        "application/7z",
        "application/vnd.7zip",
        "application/x-tar",
        "application/tar",
        "application/gzip",
        "application/x-gzip",
        "application/x-compressed-tar",
        "application/x-gtar",
        "application/x-tgz",
    ],
)
def test_specific_archive_mime_classifies_mismatched_filename_as_candidate(
    content_type: str,
) -> None:
    assert is_archive_upload("image.png", content_type)


@pytest.mark.parametrize("content_type", [None, "", "application/octet-stream"])
def test_generic_mime_requires_archive_extension(content_type: str | None) -> None:
    assert not is_archive_upload("image.png", content_type)
    assert is_archive_upload("evidence.tar.gz", content_type)


def test_stored_and_extended_archive_classification() -> None:
    assert all(
        is_stored_archive_content_type(content_type)
        for content_type in ARCHIVE_CONTENT_TYPES
    )
    assert not is_stored_archive_content_type("application/x-zip-compressed")
    assert not is_extended_archive_filename("evidence.zip")
    assert is_extended_archive_filename("evidence.7z")
    assert is_extended_archive_filename("evidence.tar")
    assert is_extended_archive_filename("evidence.tar.gz")
    assert is_extended_archive_filename("evidence.tgz")


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "application/octet-stream",
        "application/zip",
        "application/x-zip-compressed",
    ],
)
def test_validate_archive_accepts_zip_mime_variants(content_type: str | None) -> None:
    assert validate_archive("evidence.zip", content_type, _zip_bytes()) == (
        "application/zip"
    )


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        ("evidence.zip", "image/png", _zip_bytes()),
        ("evidence.zip", "application/zip", b"not-a-zip"),
        ("evidence.png", "application/zip", _zip_bytes()),
    ],
)
def test_validate_archive_rejects_zip_extension_mime_or_signature_mismatch(
    filename: str, content_type: str, data: bytes
) -> None:
    with pytest.raises(ArchiveValidationError):
        validate_archive(filename, content_type, data)


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        ("evidence.7z", "application/vnd.7zip", b"7z\xbc\xaf'\x1cstub"),
        ("evidence.tar", "application/tar", b"tar structure comes later"),
        ("evidence.tar.gz", "application/x-gzip", b"\x1f\x8bstub"),
        ("evidence.tgz", "application/x-tgz", b"\x1f\x8bstub"),
    ],
)
def test_validate_archive_fails_closed_for_recognized_extended_archives(
    filename: str, content_type: str, data: bytes
) -> None:
    with pytest.raises(ArchiveValidationError):
        validate_archive(filename, content_type, data)


def test_validate_archive_rejects_zip_declared_expansion_over_limit() -> None:
    data = bytearray(_zip_bytes(content=b""))
    central_header = data.index(b"PK\x01\x02")
    data[central_header + 24 : central_header + 28] = (100 * 1024 * 1024 + 1).to_bytes(
        4, "little"
    )

    with pytest.raises(
        ArchiveValidationError,
        match="^ZIP uncompressed size exceeds validation limit$",
    ):
        validate_archive("oversized.zip", "application/zip", bytes(data))


@pytest.mark.parametrize(
    "data",
    [
        _zip_with_negative_member_offset(),
        _zip_with_invalid_utf8_filename(),
    ],
    ids=("value-error", "unicode-decode-error"),
)
def test_validate_archive_normalizes_zip_parser_errors(data: bytes) -> None:
    with pytest.raises(ArchiveValidationError) as caught:
        validate_archive("broken.zip", "application/zip", data)

    assert str(caught.value) == "ZIP structure or member integrity is invalid"


def test_validate_archive_normalizes_zip_library_error_text() -> None:
    with pytest.raises(ArchiveValidationError) as caught:
        validate_archive("broken.zip", "application/zip", b"PK\x03\x04broken")

    assert str(caught.value) == "ZIP structure or member integrity is invalid"


@pytest.mark.parametrize(
    ("filename", "content_type", "compress"),
    [
        ("evidence.tar", "application/x-tar", False),
        ("evidence.tar.gz", "application/gzip", True),
        ("evidence.tgz", "application/x-tgz", True),
    ],
)
def test_validate_archive_accepts_valid_tar_variants(
    filename: str, content_type: str, compress: bool
) -> None:
    raw_tar = _raw_tar(
        _tar_record("first.txt", b"first"),
        _tar_record("second.txt", b"second"),
    )
    data = gzip.compress(raw_tar) if compress else raw_tar

    assert validate_archive(filename, content_type, data) == (
        "application/gzip" if compress else "application/x-tar"
    )


def test_validate_archive_accepts_nonnegative_base_256_tar_size() -> None:
    data = _raw_tar(_tar_record("report.txt", b"report", base_256_size=True))

    assert validate_archive("evidence.tar", "application/tar", data) == (
        "application/x-tar"
    )


def test_validate_archive_rejects_tar_with_bad_header_checksum() -> None:
    data = bytearray(_raw_tar(_tar_record("report.txt", b"report")))
    data[0] ^= 0x01

    with pytest.raises(ArchiveValidationError) as caught:
        validate_archive("broken.tar", "application/x-tar", bytes(data))

    assert str(caught.value) == "TAR structure or member integrity is invalid"


def test_validate_archive_rejects_bad_checksum_in_middle_tar_header() -> None:
    first = _tar_record("first.txt", b"first")
    second = _tar_record("second.txt", b"second")
    third = _tar_record("third.txt", b"third")
    data = bytearray(_raw_tar(first, second, third))
    data[len(first)] ^= 0x01

    with pytest.raises(ArchiveValidationError) as caught:
        validate_archive("broken.tar", "application/x-tar", bytes(data))

    assert str(caught.value) == "TAR structure or member integrity is invalid"


def test_validate_archive_rejects_tar_declared_expansion_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_validation, "MAX_EXPANDED_BYTES", 1)
    data = _raw_tar(_tar_record("oversized.txt", b"ab"))

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR declared payload exceeds validation limit$",
    ):
        validate_archive("oversized.tar", "application/x-tar", data)


def test_validate_archive_drains_every_regular_tar_member_with_actual_byte_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_validation, "MAX_EXPANDED_BYTES", 5)
    monkeypatch.setattr(archive_validation, "_scan_tar_framing", lambda data: data)
    data = _raw_tar(
        _tar_record("first.txt", b"aaa"),
        _tar_record("second.txt", b"bbb"),
    )

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR expanded payload exceeds validation limit$",
    ):
        validate_archive("oversized.tar", "application/x-tar", data)


@pytest.mark.parametrize(
    ("raw_size", "content"),
    [
        (1, b"a" * 513),
        (513, b"a"),
    ],
    ids=("larger-pax-boundary", "smaller-pax-boundary"),
)
def test_validate_archive_applies_local_pax_size_to_physical_record_boundary(
    raw_size: int, content: bytes
) -> None:
    data = _tar_with_local_pax_size(raw_size, content)

    assert validate_archive("pax.tar", "application/x-tar", data) == (
        "application/x-tar"
    )


@pytest.mark.parametrize(
    ("raw_size", "content"),
    [
        (1, b"a" * 513),
        (513, b"a"),
    ],
    ids=("larger-solaris-pax-boundary", "smaller-solaris-pax-boundary"),
)
def test_validate_archive_applies_solaris_pax_size_to_physical_record_boundary(
    raw_size: int, content: bytes
) -> None:
    data = _tar_with_local_pax_size(raw_size, content, type_flag=b"X")

    assert validate_archive("pax.tar", "application/x-tar", data) == (
        "application/x-tar"
    )


@pytest.mark.parametrize(
    ("raw_size", "content"),
    [
        (1, b"a" * 513),
        (513, b"a"),
    ],
    ids=("larger-global-pax-boundary", "smaller-global-pax-boundary"),
)
def test_validate_archive_applies_global_pax_size_to_multiple_members(
    raw_size: int,
    content: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extracted: list[tuple[str, int]] = []
    original_extractfile = tarfile.TarFile.extractfile

    def record_extractfile(
        archive: tarfile.TarFile, member: tarfile.TarInfo
    ) -> BytesIO | tarfile.ExFileObject | None:
        extracted.append((member.name, member.size))
        return original_extractfile(archive, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", record_extractfile)
    data = _tar_with_global_pax_size(raw_size, content)

    assert validate_archive("pax.tar", "application/x-tar", data) == (
        "application/x-tar"
    )
    assert extracted == [
        ("global-one.txt", len(content)),
        ("global-two.txt", len(content)),
    ]


def test_validate_archive_updates_global_pax_size_between_members() -> None:
    data = _raw_tar(
        _tar_record("global-large", _pax_record("size", "513"), type_flag=b"g"),
        _tar_record("large.txt", b"a" * 513, declared_size=1),
        _tar_record("global-small", _pax_record("size", "1"), type_flag=b"g"),
        _tar_record("small.txt", b"b", declared_size=513),
    )

    assert validate_archive("pax.tar", "application/x-tar", data) == (
        "application/x-tar"
    )


def test_validate_archive_clears_local_pax_size_after_global_override() -> None:
    data = _raw_tar(
        _tar_record("global-pax", _pax_record("size", "513"), type_flag=b"g"),
        _tar_record("local-pax", _pax_record("size", "1"), type_flag=b"x"),
        _tar_record("local.txt", b"a", declared_size=513),
        _tar_record("global.txt", b"b" * 513, declared_size=1),
    )

    assert validate_archive("pax.tar", "application/x-tar", data) == (
        "application/x-tar"
    )


def test_validate_archive_caps_semantic_tar_member_iteration_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        tarfile.TarInfo(f"dir-{index}")
        for index in range(archive_validation.MAX_MEMBER_COUNT + 1)
    ]
    for member in members:
        member.type = tarfile.DIRTYPE
    monkeypatch.setattr(archive_validation, "_scan_tar_framing", lambda data: data)
    monkeypatch.setattr(
        archive_validation.tarfile,
        "open",
        lambda **kwargs: nullcontext(members),
    )

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR contains too many members$",
    ):
        validate_archive("members.tar", "application/x-tar", b"unused")


def test_validate_archive_rejects_nonzero_tar_record_padding() -> None:
    data = _raw_tar(
        _tar_record("report.txt", b"report", padding_byte=b"x"),
    )

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR record padding is invalid$",
    ):
        validate_archive("broken.tar", "application/x-tar", data)


@pytest.mark.parametrize("zero_blocks", [0, 1])
def test_validate_archive_requires_two_tar_end_blocks(zero_blocks: int) -> None:
    data = _raw_tar(_tar_record("report.txt", b"report"), zero_blocks=zero_blocks)

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR end marker is invalid$",
    ):
        validate_archive("broken.tar", "application/x-tar", data)


@pytest.mark.parametrize("trailing", [b"x", b"\0"])
def test_validate_archive_rejects_nonzero_or_partial_tar_trailing_junk(
    trailing: bytes,
) -> None:
    data = _raw_tar(_tar_record("report.txt", b"report"), trailing=trailing)

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR trailing data is invalid$",
    ):
        validate_archive("broken.tar", "application/x-tar", data)


def test_validate_archive_rejects_excess_tar_zero_blocks() -> None:
    data = _raw_tar(_tar_record("report.txt", b"report"), zero_blocks=21)

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR end marker is invalid$",
    ):
        validate_archive("broken.tar", "application/x-tar", data)


@pytest.mark.parametrize("type_flag", [b"x", b"X"], ids=("pax", "solaris-pax"))
def test_validate_archive_rejects_excess_tar_metadata_records(
    type_flag: bytes,
) -> None:
    metadata = _tar_header("metadata", 0, type_flag=type_flag)
    member = _tar_header("empty.txt", 0)
    records: list[bytes] = []
    for _ in range(archive_validation.MAX_MEMBER_COUNT):
        records.extend((metadata, metadata, member))
    records.extend((metadata, metadata))
    data = _raw_tar(*records)

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR contains too many metadata records$",
    ):
        validate_archive("metadata.tar", "application/x-tar", data)


@pytest.mark.parametrize("type_flag", [b"x", b"X"], ids=("pax", "solaris-pax"))
def test_validate_archive_accepts_33_valid_local_pax_records(
    type_flag: bytes,
) -> None:
    data = _raw_tar(
        *(
            _tar_record(
                f"metadata-{index}",
                _pax_record("comment", str(index)),
                type_flag=type_flag,
            )
            for index in range(33)
        ),
        _tar_record("report.txt", b"report"),
    )

    assert validate_archive("metadata.tar", "application/x-tar", data) == (
        "application/x-tar"
    )


@pytest.mark.parametrize("type_flag", [b"x", b"X"], ids=("pax", "solaris-pax"))
def test_validate_archive_normalizes_recursive_tar_metadata_chain(
    type_flag: bytes,
) -> None:
    metadata = _tar_header("metadata", 0, type_flag=type_flag)
    data = _raw_tar(
        *(metadata for _ in range(1_100)),
        _tar_record("report.txt", b"report"),
    )

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR structure or member integrity is invalid$",
    ):
        validate_archive("metadata.tar", "application/x-tar", data)


@pytest.mark.parametrize(
    "parser_error",
    [
        RuntimeError("private runtime detail"),
        RecursionError("private recursion detail"),
    ],
)
def test_validate_archive_normalizes_tar_parser_runtime_errors(
    parser_error: RuntimeError,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_validation, "_scan_tar_framing", lambda data: None)

    def raise_parser_error(**kwargs: object) -> None:
        raise parser_error

    monkeypatch.setattr(archive_validation.tarfile, "open", raise_parser_error)

    with pytest.raises(ArchiveValidationError) as caught:
        validate_archive("broken.tar", "application/x-tar", b"unused")

    assert str(caught.value) == "TAR structure or member integrity is invalid"


def test_validate_archive_rejects_excess_tar_metadata_payload() -> None:
    payload = b"x" * (archive_validation.MAX_TAR_METADATA_BYTES + 1)
    data = _raw_tar(_tar_record("metadata", payload, type_flag=b"g"))

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR metadata payload exceeds validation limit$",
    ):
        validate_archive("metadata.tar", "application/x-tar", data)


def test_validate_archive_rejects_excess_tar_logical_members() -> None:
    member = _tar_header("empty.txt", 0)
    data = _raw_tar(*(member for _ in range(archive_validation.MAX_MEMBER_COUNT + 1)))

    with pytest.raises(
        ArchiveValidationError,
        match="^TAR contains too many members$",
    ):
        validate_archive("members.tar", "application/x-tar", data)


def test_validate_archive_rejects_gzip_tar_crc_corruption() -> None:
    data = bytearray(gzip.compress(_raw_tar(_tar_record("report.txt", b"report"))))
    data[-8] ^= 0x01

    with pytest.raises(ArchiveValidationError) as caught:
        validate_archive("broken.tar.gz", "application/gzip", bytes(data))

    assert str(caught.value) == "gzip TAR structure or integrity is invalid"


def test_validate_archive_rejects_gzip_tar_truncated_in_valid_stream_trailer() -> None:
    valid_gzip = gzip.compress(_raw_tar(_tar_record("report.txt", b"report")))

    with pytest.raises(ArchiveValidationError) as caught:
        validate_archive("broken.tar.gz", "application/gzip", valid_gzip[:-4])

    assert str(caught.value) == "gzip TAR structure or integrity is invalid"


@pytest.mark.parametrize(
    ("filename", "content_type", "compress"),
    [
        ("evidence.tar", "application/x-tar", False),
        ("evidence.tgz", "application/gzip", True),
    ],
)
def test_validate_archive_never_uses_filesystem_tar_extraction_methods(
    filename: str,
    content_type: str,
    compress: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> None:
        pytest.fail("filesystem TAR extraction method called")

    monkeypatch.setattr(tarfile.TarFile, "extract", fail_if_called)
    monkeypatch.setattr(tarfile.TarFile, "extractall", fail_if_called)
    raw_tar = _raw_tar(_tar_record("report.txt", b"report"))
    data = gzip.compress(raw_tar) if compress else raw_tar

    validate_archive(filename, content_type, data)


def test_validate_archive_caps_actual_gzip_decompressed_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_validation, "MAX_TAR_STREAM_BYTES", 1024)
    data = gzip.compress(b"x" * 1025)

    with pytest.raises(
        ArchiveValidationError,
        match="^gzip TAR stream exceeds validation limit$",
    ):
        validate_archive("oversized.tgz", "application/gzip", data)


@pytest.mark.parametrize(
    ("filename", "content_type", "data", "message"),
    [
        (
            "broken.tar",
            "application/x-tar",
            b"not a tar stream",
            "TAR structure or member integrity is invalid",
        ),
        (
            "broken.tgz",
            "application/gzip",
            b"\x1f\x8bnot a gzip stream",
            "gzip TAR structure or integrity is invalid",
        ),
    ],
)
def test_validate_archive_normalizes_tar_parser_errors(
    filename: str, content_type: str, data: bytes, message: str
) -> None:
    with pytest.raises(ArchiveValidationError) as caught:
        validate_archive(filename, content_type, data)

    assert str(caught.value) == message
