"""Focused tests for Markdown archive classification and validation."""

from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

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
    data[end_header + 16 : end_header + 20] = (central_offset + 1).to_bytes(
        4, "little"
    )
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
