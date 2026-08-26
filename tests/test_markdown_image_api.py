"""Contract tests for synced Markdown asset storage."""

from __future__ import annotations

import asyncio
import json
import tarfile
import threading
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import py7zr
import pytest
from conftest import TEST_API_KEY
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncByteStream, AsyncClient
from starlette import formparsers as starlette_formparsers

import webapp
from webapp import markdown_images

_AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}
_PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload"
_JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-payload"
_GIF = b"GIF89a" + b"gif-payload"
_WEBP = b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 " + b"webp"
_PDF = b"%PDF-1.4\n" + b"pdf-payload"


def _zip_bytes(filename: str = "report.txt", content: bytes = b"report") -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()


def _seven_zip_bytes() -> bytes:
    buffer = BytesIO()
    with py7zr.SevenZipFile(buffer, "w") as archive:
        archive.writestr(b"report", "report.txt")
    return buffer.getvalue()


def _tar_bytes(*, compressed: bool = False) -> bytes:
    buffer = BytesIO()
    mode = "w:gz" if compressed else "w"
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        member = tarfile.TarInfo("report.txt")
        member.size = len(b"report")
        archive.addfile(member, BytesIO(b"report"))
    return buffer.getvalue()


def _zip_with_corrupt_member() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("report.txt", b"report")
    data = bytearray(buffer.getvalue())
    with ZipFile(BytesIO(data)) as archive:
        member = archive.infolist()[0]
        payload_offset = (
            member.header_offset
            + 30
            + len(member.filename.encode("utf-8"))
            + len(member.extra)
        )
    data[payload_offset] ^= 1
    return bytes(data)


def _zip_with_corrupt_duplicate_member() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("same.txt", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("same.txt", b"second")
    data = bytearray(buffer.getvalue())
    with ZipFile(BytesIO(data)) as archive:
        member = archive.infolist()[0]
        payload_offset = (
            member.header_offset
            + 30
            + len(member.filename.encode("utf-8"))
            + len(member.extra)
        )
    data[payload_offset] ^= 1
    return bytes(data)


def _zip_with_invalid_deflate_stream() -> bytes:
    data = bytearray(_zip_bytes())
    with ZipFile(BytesIO(data)) as archive:
        member = archive.infolist()[0]
        payload_offset = (
            member.header_offset
            + 30
            + len(member.filename.encode("utf-8"))
            + len(member.extra)
        )
    data[payload_offset] = (data[payload_offset] & ~0x07) | 0x06
    return bytes(data)


_ZIP = _zip_bytes()
_SEVEN_ZIP = _seven_zip_bytes()
_TAR = _tar_bytes()
_TAR_GZ = _tar_bytes(compressed=True)
_ARCHIVE_ASSETS = (
    ("audit résumé.zip", _ZIP, "application/zip"),
    ("evidence.7z", _SEVEN_ZIP, "application/x-7z-compressed"),
    ("evidence.tar", _TAR, "application/x-tar"),
    ("evidence.tar.gz", _TAR_GZ, "application/gzip"),
    ("evidence.tgz", _TAR_GZ, "application/gzip"),
)
_OFFICE_ASSETS = (
    ("word", "report résumé.docx"),
    ("excel", "ledger.xlsx"),
    ("powerpoint", "slides.pptx"),
    ("access", "database.accdb"),
    ("visio", "diagram.vsdx"),
    ("onenote", "notes.one"),
    ("project", "schedule.mpp"),
    ("outlook", "mail.msg"),
    ("publisher", "layout.pub"),
    ("infopath", "form.xsn"),
)


def _upload(client: TestClient, markdown_id: str, files):
    return client.post(
        f"/markdown-threads/{markdown_id}/images",
        headers=_AUTH_HEADERS,
        files=[("files", file) for file in files],
    )


class _CountingAsyncStream(AsyncByteStream):
    def __init__(self, chunks) -> None:
        self._chunks = chunks
        self.consumed = 0

    async def __aiter__(self):
        for chunk in self._chunks:
            self.consumed += len(chunk)
            yield chunk


def test_upload_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = client.post(
        "/markdown-threads/123456/images",
        files=[("files", ("chart.png", _PNG, "image/png"))],
    )

    assert response.status_code == 401


def test_upload_authentication_runs_before_multipart_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = client.post(
        "/markdown-threads/123456/images",
        content=b"not-a-valid-multipart-body",
        headers={"Content-Type": "multipart/form-data; boundary=broken"},
    )

    assert response.status_code == 401


def test_upload_validates_markdown_id_and_batch_count(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    invalid_id = _upload(client, "../bad", [("chart.png", _PNG, "image/png")])
    too_many = _upload(
        client,
        "123456",
        [
            ("chart-0.png", _PNG, "image/png"),
            ("archive-0.zip", _ZIP, "application/zip"),
            ("chart-1.png", _PNG, "image/png"),
            ("archive-1.zip", _ZIP, "application/zip"),
            ("chart-2.png", _PNG, "image/png"),
            ("archive-2.zip", _ZIP, "application/zip"),
        ],
    )

    assert invalid_id.status_code in {404, 422}
    assert too_many.status_code == 400
    assert not (tmp_path / "docs" / "markdown-threads").exists()


def test_chunked_upload_without_content_length_stops_at_total_request_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    boundary = "markdown-assets-boundary"
    chunk_size = 64 * 1024
    chunks = []
    for index in range(markdown_images._MAX_IMAGE_COUNT):
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="files"; filename="{index}.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode()
        )
        chunks.extend(
            [b"x" * chunk_size] * (markdown_images._MAX_IMAGE_BYTES // chunk_size)
        )
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    chunks.extend([b"x" * chunk_size] * 20)
    stream = _CountingAsyncStream(chunks)

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return await client.post(
                "/markdown-threads/123456/images",
                headers={
                    **_AUTH_HEADERS,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                content=stream,
            )

    response = asyncio.run(scenario())

    assert response.status_code == 413
    assert response.json() == {"detail": "Asset upload request is too large"}
    assert stream.consumed <= markdown_images._MAX_REQUEST_BYTES + chunk_size
    assert not (tmp_path / "docs" / "markdown-threads").exists()


def test_multipart_parser_stops_oversized_file_before_validation_or_storage(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    validation_called = False
    storage_called = False
    spooled_files = []
    original_spooled_file = starlette_formparsers.SpooledTemporaryFile

    def fail_validation(*args, **kwargs):
        nonlocal validation_called
        validation_called = True
        raise AssertionError("oversized file reached validation")

    def fail_storage(*args, **kwargs):
        nonlocal storage_called
        storage_called = True
        raise AssertionError("oversized file reached storage")

    def tracking_spooled_file(*args, **kwargs):
        temporary = original_spooled_file(*args, **kwargs)
        spooled_files.append(temporary)
        return temporary

    monkeypatch.setattr(markdown_images, "_validate_asset", fail_validation)
    monkeypatch.setattr(markdown_images, "_validate_image", fail_validation)
    monkeypatch.setattr(markdown_images, "_store_asset", fail_storage)
    monkeypatch.setattr(
        starlette_formparsers, "SpooledTemporaryFile", tracking_spooled_file
    )
    boundary = "markdown-assets-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="large.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode()
    chunk_size = 64 * 1024
    stream = _CountingAsyncStream(
        [prefix, _PNG]
        + [b"x" * chunk_size] * ((markdown_images._MAX_IMAGE_BYTES // chunk_size) + 4)
        + [f"\r\n--{boundary}--\r\n".encode()]
    )

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return await client.post(
                "/markdown-threads/123456/images",
                headers={
                    **_AUTH_HEADERS,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                content=stream,
            )

    response = asyncio.run(scenario())

    assert response.status_code == 413
    assert response.json() == {"detail": "File exceeds 10 MiB"}
    assert not validation_called
    assert not storage_called
    assert spooled_files and all(temporary.closed for temporary in spooled_files)
    assert stream.consumed <= len(prefix) + len(_PNG) + 10 * 1024 * 1024 + chunk_size
    assert not (tmp_path / "docs" / "markdown-threads").exists()


def test_upload_accepts_supported_images_and_preserves_order(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("one.png", _PNG, "image/png"),
            ("two.jpg", _JPEG, "image/jpeg"),
            ("three.gif", _GIF, "image/gif"),
            ("four.webp", _WEBP, "image/webp"),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert [asset["filename"] for asset in data["assets"]] == [
        "one.png",
        "two.jpg",
        "three.gif",
        "four.webp",
    ]
    assert data["errors"] == []
    for asset in data["assets"]:
        asset_dir = docs_root / "markdown-threads" / "123456" / "images" / asset["id"]
        assert (asset_dir / "payload").is_file()
        metadata = json.loads((asset_dir / "metadata.json").read_text())
        assert metadata["filename"] == asset["filename"]


@pytest.mark.parametrize(
    "content_type",
    [
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
        "",
    ],
)
def test_upload_accepts_zip_browser_mime_variants(tmp_path, monkeypatch, content_type):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [("evidence.zip", _ZIP, content_type)],
    )

    assert response.status_code == 200
    assert response.json()["errors"] == []
    asset = response.json()["assets"][0]
    assert asset["filename"] == "evidence.zip"
    assert asset["content_type"] == "application/zip"
    assert asset["size"] == len(_ZIP)


@pytest.mark.parametrize(
    ("filename", "payload", "content_type"),
    [
        ("fake.zip", b"not-a-zip", "application/zip"),
        ("photo.zip", _PNG, "image/png"),
        ("archive.png", _ZIP, "application/zip"),
    ],
)
def test_upload_rejects_disguised_or_mismatched_archives(
    tmp_path, monkeypatch, filename, payload, content_type
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [(filename, payload, content_type)],
    )

    assert response.status_code == 200
    assert response.json()["assets"] == []
    assert response.json()["errors"] == [
        {
            "filename": filename,
            "code": "unsupported_or_mismatched_archive",
            "message": "Only valid ZIP, 7Z, TAR, TAR.GZ, and TGZ archives are supported",
        }
    ]


def test_upload_rejects_zip_with_corrupt_member_crc(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [("corrupt.zip", _zip_with_corrupt_member(), "application/zip")],
    )

    assert response.status_code == 200
    assert response.json()["assets"] == []
    assert response.json()["errors"] == [
        {
            "filename": "corrupt.zip",
            "code": "unsupported_or_mismatched_archive",
            "message": "Only valid ZIP, 7Z, TAR, TAR.GZ, and TGZ archives are supported",
        }
    ]


def test_upload_checks_crc_for_duplicate_named_zip_members(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            (
                "duplicate.zip",
                _zip_with_corrupt_duplicate_member(),
                "application/zip",
            )
        ],
    )

    assert response.status_code == 200
    assert response.json()["assets"] == []
    assert response.json()["errors"][0]["code"] == ("unsupported_or_mismatched_archive")


def test_upload_normalizes_malformed_deflate_as_ordered_archive_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app, raise_server_exceptions=False)

    response = _upload(
        client,
        "123456",
        [
            ("one.png", _PNG, "image/png"),
            ("malformed.zip", _zip_with_invalid_deflate_stream(), "application/zip"),
            ("two.gif", _GIF, "image/gif"),
        ],
    )

    assert response.status_code == 200
    assert [asset["filename"] for asset in response.json()["assets"]] == [
        "one.png",
        "two.gif",
    ]
    assert response.json()["errors"][0]["code"] == ("unsupported_or_mismatched_archive")


def test_upload_rejects_zip_with_excessive_member_count(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for index in range(1_001):
            archive.writestr(f"{index}.txt", b"")

    response = _upload(
        client,
        "123456",
        [("many-files.zip", buffer.getvalue(), "application/zip")],
    )

    assert response.status_code == 200
    assert response.json()["assets"] == []
    assert response.json()["errors"][0]["code"] == ("unsupported_or_mismatched_archive")


def test_upload_preserves_mixed_asset_order_and_stores_archives_opaque(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("one.png", _PNG, "image/png"),
            ("evidence.zip", _ZIP, "application/zip"),
            ("two.gif", _GIF, "image/gif"),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert [asset["filename"] for asset in data["assets"]] == [
        "one.png",
        "evidence.zip",
        "two.gif",
    ]
    archive = data["assets"][1]
    archive_dir = docs_root / "markdown-threads" / "123456" / "images" / archive["id"]
    assert (archive_dir / "payload").read_bytes() == _ZIP
    assert sorted(path.name for path in archive_dir.iterdir()) == [
        "metadata.json",
        "payload",
    ]


def test_mixed_upload_returns_partial_errors_without_reordering_successes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("one.png", _PNG, "image/png"),
            ("broken.zip", b"not-a-zip", "application/zip"),
            ("evidence.zip", _ZIP, "application/octet-stream"),
            ("script.svg", b"<svg/>", "image/svg+xml"),
            ("two.gif", _GIF, "image/gif"),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert [asset["filename"] for asset in data["assets"]] == [
        "one.png",
        "evidence.zip",
        "two.gif",
    ]
    assert [error["filename"] for error in data["errors"]] == [
        "broken.zip",
        "script.svg",
    ]


def test_upload_returns_ordered_partial_errors_without_writing_invalid_files(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("valid.png", _PNG, "image/png"),
            ("wrong.jpg", _PNG, "image/jpeg"),
            ("script.svg", b"<svg/>", "image/svg+xml"),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert [asset["filename"] for asset in data["assets"]] == ["valid.png"]
    assert [error["filename"] for error in data["errors"]] == [
        "wrong.jpg",
        "script.svg",
    ]
    assert all(
        error["code"] == "unsupported_or_mismatched_image" for error in data["errors"]
    )


def test_upload_treats_malformed_filename_as_ordered_item_error(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("valid.png", _PNG, "image/png"),
            ("..", _PNG, "image/png"),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert [asset["filename"] for asset in data["assets"]] == ["valid.png"]
    assert data["errors"] == [
        {
            "filename": "..",
            "code": "invalid_filename",
            "message": "Asset filename is invalid",
        }
    ]


def test_storage_failure_rolls_back_assets_written_by_request(tmp_path, monkeypatch):
    from webapp import markdown_images

    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    original_store = markdown_images._store_asset

    def fail_second_store(*args, filename, **kwargs):
        if filename == "two.png":
            raise OSError("disk full")
        return original_store(*args, filename=filename, **kwargs)

    monkeypatch.setattr(markdown_images, "_store_asset", fail_second_store)
    client = TestClient(webapp.app, raise_server_exceptions=False)

    response = _upload(
        client,
        "123456",
        [
            ("one.png", _PNG, "image/png"),
            ("two.png", _PNG, "image/png"),
        ],
    )

    assert response.status_code == 500
    images_root = docs_root / "markdown-threads" / "123456" / "images"
    assert not images_root.exists() or not any(images_root.iterdir())


def test_upload_rejects_oversized_file_during_multipart_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [("large.png", _PNG + b"x" * (10 * 1024 * 1024), "image/png")],
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "File exceeds 10 MiB"}


def test_upload_rejects_oversized_archive_during_multipart_parsing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [("large.zip", _ZIP + b"x" * (10 * 1024 * 1024), "application/zip")],
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "File exceeds 10 MiB"}


def test_extended_uploads_accept_mixed_max_five_batches_in_input_order(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    monkeypatch.delenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", raising=False)
    client = TestClient(webapp.app)
    office_payloads = {
        "report.docx": (b"", ""),
        "ledger.xlsx": (b"excel", "application/octet-stream"),
        "slides.pptx": (b"powerpoint", "image/png"),
        "database.accdb": (b"access", "application/vnd.ms-access"),
        "diagram.vsdx": (b"visio", "application/x-tar"),
        "notes.one": (b"onenote", "application/onenote"),
        "schedule.mpp": (b"project", "text/plain"),
        "mail.msg": (b"outlook", "application/vnd.ms-outlook"),
        "layout.pub": (b"publisher", "application/x-publisher"),
        "form.xsn": (b"infopath", "application/vnd.ms-infopath"),
    }
    items = [
        ("chart.png", _PNG, "image/png", "image/png"),
        ("evidence.zip", _ZIP, "application/x-zip-compressed", "application/zip"),
        (
            "evidence.7z",
            _SEVEN_ZIP,
            "application/vnd.7zip",
            "application/x-7z-compressed",
        ),
        *[
            (filename, payload, content_type, "application/octet-stream")
            for filename, (payload, content_type) in list(office_payloads.items())[:2]
        ],
        ("evidence.tar", _TAR, "application/tar", "application/x-tar"),
        *[
            (filename, payload, content_type, "application/octet-stream")
            for filename, (payload, content_type) in list(office_payloads.items())[2:6]
        ],
        ("evidence.tar.gz", _TAR_GZ, "application/x-gzip", "application/gzip"),
        ("evidence.tgz", _TAR_GZ, "application/x-tgz", "application/gzip"),
        *[
            (filename, payload, content_type, "application/octet-stream")
            for filename, (payload, content_type) in list(office_payloads.items())[6:9]
        ],
        *[
            (filename, payload, content_type, "application/octet-stream")
            for filename, (payload, content_type) in list(office_payloads.items())[9:]
        ],
    ]

    for batch_index, start in enumerate(range(0, len(items), 5)):
        markdown_id = f"{123450 + batch_index:06d}"
        batch = items[start : start + 5]
        response = _upload(
            client,
            markdown_id,
            [
                (filename, payload, declared_type)
                for filename, payload, declared_type, _ in batch
            ],
        )

        assert response.status_code == 200
        assert response.json()["errors"] == []
        assets = response.json()["assets"]
        assert [asset["filename"] for asset in assets] == [item[0] for item in batch]
        assert [asset["content_type"] for asset in assets] == [
            item[3] for item in batch
        ]
        for asset, (_, payload, _, _) in zip(assets, batch, strict=True):
            asset_dir = (
                docs_root / "markdown-threads" / markdown_id / "images" / asset["id"]
            )
            assert (asset_dir / "payload").read_bytes() == payload


@pytest.mark.parametrize("disabled_value", ["false", " FALSE ", "FaLsE"])
def test_feature_gate_disables_extended_and_office_items_only(
    tmp_path, monkeypatch, disabled_value
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    monkeypatch.setenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", disabled_value)
    client = TestClient(webapp.app)

    first = _upload(
        client,
        "123456",
        [
            ("chart.png", _PNG, "image/png"),
            ("evidence.zip", _ZIP, "application/zip"),
            ("evidence.7z", _SEVEN_ZIP, "application/x-7z-compressed"),
            ("evidence.tar", _TAR, "application/x-tar"),
            (
                "report.docx",
                b"office",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ],
    )
    second = _upload(
        client,
        "123457",
        [
            ("evidence.tar.gz", _TAR_GZ, "application/gzip"),
            ("evidence.tgz", _TAR_GZ, "application/x-tgz"),
            ("mail.msg", b"mail", "application/vnd.ms-outlook"),
        ],
    )

    assert [asset["filename"] for asset in first.json()["assets"]] == [
        "chart.png",
        "evidence.zip",
    ]
    assert [error["filename"] for error in first.json()["errors"]] == [
        "evidence.7z",
        "evidence.tar",
        "report.docx",
    ]
    assert [error["filename"] for error in second.json()["errors"]] == [
        "evidence.tar.gz",
        "evidence.tgz",
        "mail.msg",
    ]
    assert all(
        error["code"] == "extended_attachment_upload_disabled"
        for response in (first, second)
        for error in response.json()["errors"]
    )


@pytest.mark.parametrize("enabled_value", [None, "true", "0", "disabled", ""])
def test_feature_gate_absent_or_non_false_value_defaults_enabled(
    tmp_path, monkeypatch, enabled_value
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    if enabled_value is None:
        monkeypatch.delenv(
            "MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", raising=False
        )
    else:
        monkeypatch.setenv(
            "MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", enabled_value
        )
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("evidence.tar", _TAR, "application/x-tar"),
            ("report.docx", b"office", "application/x-tar"),
        ],
    )

    assert response.status_code == 200
    assert response.json()["errors"] == []
    assert [asset["filename"] for asset in response.json()["assets"]] == [
        "evidence.tar",
        "report.docx",
    ]


def test_archive_overload_rejects_whole_batch_before_any_write_then_retries(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", asyncio.Semaphore(0))
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_WAIT_SECONDS", 0.001)
    client = TestClient(webapp.app)
    files = [
        ("chart.png", _PNG, "image/png"),
        ("evidence.tar", _TAR, "application/x-tar"),
    ]

    overloaded = _upload(client, "123456", files)

    assert overloaded.status_code == 503
    assert overloaded.json() == {"detail": "Archive validation is busy"}
    assert overloaded.headers["retry-after"] == "2"
    assert not (docs_root / "markdown-threads" / "123456" / "images").exists()

    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", asyncio.Semaphore(2))
    retried = _upload(client, "123456", files)

    assert retried.status_code == 200
    assert retried.json()["errors"] == []
    assert [asset["filename"] for asset in retried.json()["assets"]] == [
        "chart.png",
        "evidence.tar",
    ]
    images_root = docs_root / "markdown-threads" / "123456" / "images"
    assert (
        len([path for path in images_root.iterdir() if not path.name.startswith(".")])
        == 2
    )


def test_archive_specific_mime_mismatch_participates_in_overload_limit(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", asyncio.Semaphore(0))
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_WAIT_SECONDS", 0.001)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [("chart.png", _PNG, "application/x-tar")],
    )

    assert response.status_code == 503
    assert not (docs_root / "markdown-threads" / "123456" / "images").exists()


def test_archive_limiter_supports_contention_across_two_event_loops():
    limiter = markdown_images._ARCHIVE_BATCH_LIMITER

    async def contend() -> None:
        await limiter.acquire()
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        try:
            assert not waiter.done()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        finally:
            if not waiter.done():
                waiter.cancel()
            try:
                await waiter
            except BaseException:
                pass
            limiter.release()
            limiter.release()

    asyncio.run(contend())
    asyncio.run(contend())


def test_cancelled_archive_validation_holds_slot_until_worker_finishes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    limiter = asyncio.Semaphore(1)
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", limiter)
    original_validate = markdown_images.validate_archive
    worker_started = threading.Event()
    worker_finish = threading.Event()

    def blocking_validate(filename, content_type, data):
        worker_started.set()
        if not worker_finish.wait(5):
            raise AssertionError("validation worker was not released")
        return original_validate(filename, content_type, data)

    monkeypatch.setattr(markdown_images, "validate_archive", blocking_validate)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/markdown-threads/123456/images",
                    headers=_AUTH_HEADERS,
                    files={"files": ("evidence.tar", _TAR, "application/x-tar")},
                )
            )
            assert await asyncio.to_thread(worker_started.wait, 2)
            request.cancel()
            await asyncio.sleep(0.02)
            try:
                assert not request.done()
                assert limiter.locked()
                request.cancel()
                await asyncio.sleep(0.02)
                assert not request.done()
                assert limiter.locked()
            finally:
                worker_finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 2)
            assert not limiter.locked()

    asyncio.run(scenario())


def test_cancelled_storage_waits_then_rolls_back_completed_and_current_assets(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    original_store = markdown_images._store_asset
    worker_started = threading.Event()
    worker_finish = threading.Event()

    def blocking_second_store(*args, filename, **kwargs):
        if filename == "two.png":
            worker_started.set()
            if not worker_finish.wait(5):
                raise AssertionError("storage worker was not released")
        return original_store(*args, filename=filename, **kwargs)

    monkeypatch.setattr(markdown_images, "_store_asset", blocking_second_store)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/markdown-threads/123456/images",
                    headers=_AUTH_HEADERS,
                    files=[
                        ("files", ("one.png", _PNG, "image/png")),
                        ("files", ("two.png", _PNG, "image/png")),
                    ],
                )
            )
            assert await asyncio.to_thread(worker_started.wait, 2)
            request.cancel()
            await asyncio.sleep(0.02)
            try:
                assert not request.done()
                request.cancel()
                await asyncio.sleep(0.02)
                assert not request.done()
            finally:
                worker_finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 2)

    asyncio.run(scenario())
    images_root = docs_root / "markdown-threads" / "123456" / "images"
    assert not images_root.exists() or not any(images_root.iterdir())


def test_cancelled_form_cleanup_rolls_back_assets_before_retry(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    original_close = markdown_images.UploadFile.close
    cleanup_started = threading.Event()
    block_next_cleanup = True

    async def blocking_first_close(upload):
        nonlocal block_next_cleanup
        if not block_next_cleanup:
            await original_close(upload)
            return
        block_next_cleanup = False
        cleanup_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await original_close(upload)

    monkeypatch.setattr(markdown_images.UploadFile, "close", blocking_first_close)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/markdown-threads/123456/images",
                    headers=_AUTH_HEADERS,
                    files={"files": ("chart.png", _PNG, "image/png")},
                )
            )
            assert await asyncio.to_thread(cleanup_started.wait, 2)
            images_root = docs_root / "markdown-threads" / "123456" / "images"
            assert len(list(images_root.iterdir())) == 1

            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 2)

            assert not images_root.exists() or not any(images_root.iterdir())
            retried = await client.post(
                "/markdown-threads/123456/images",
                headers=_AUTH_HEADERS,
                files={"files": ("chart.png", _PNG, "image/png")},
            )
            assert retried.status_code == 200
            assert retried.json()["errors"] == []
            assert [asset["filename"] for asset in retried.json()["assets"]] == [
                "chart.png"
            ]
            assert len(list(images_root.iterdir())) == 1

    asyncio.run(scenario())


def test_feature_gate_disabled_archive_candidates_wait_on_saturated_limiter(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    monkeypatch.setenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", "false")
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", asyncio.Semaphore(0))
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_WAIT_SECONDS", 0.001)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("evidence.7z", _SEVEN_ZIP, "application/x-7z-compressed"),
            ("evidence.tar", _TAR, "application/x-tar"),
            ("evidence.tgz", _TAR_GZ, "application/gzip"),
            ("report.docx", b"office", "application/x-tar"),
        ],
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Archive validation is busy"}
    assert response.headers["retry-after"] == "2"
    assert not (docs_root / "markdown-threads" / "123456" / "images").exists()


def test_feature_gate_disabled_office_only_batch_skips_saturated_archive_limiter(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    monkeypatch.setenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", "false")
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", asyncio.Semaphore(0))
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_WAIT_SECONDS", 0.001)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("chart.png", _PNG, "image/png"),
            ("report.docx", b"office", "application/x-tar"),
        ],
    )

    assert response.status_code == 200
    assert [asset["filename"] for asset in response.json()["assets"]] == ["chart.png"]
    assert response.json()["errors"] == [
        {
            "filename": "report.docx",
            "code": "extended_attachment_upload_disabled",
            "message": "Extended archive and Microsoft Office uploads are disabled",
        }
    ]


def test_feature_gate_disabled_batch_with_zip_still_waits_for_archive_limiter(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    monkeypatch.setenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", "false")
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", asyncio.Semaphore(0))
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_WAIT_SECONDS", 0.001)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("evidence.tar", _TAR, "application/x-tar"),
            ("evidence.zip", _ZIP, "application/zip"),
        ],
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Archive validation is busy"}
    assert not (docs_root / "markdown-threads" / "123456" / "images").exists()


def test_archive_validation_runs_off_loop_sequentially(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    original_validate = markdown_images.validate_archive
    validation_calls: list[tuple[str, bool]] = []

    def tracked_validate(filename, content_type, data):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_loop = True
        else:
            off_loop = False
        validation_calls.append((filename, off_loop))
        return original_validate(filename, content_type, data)

    monkeypatch.setattr(markdown_images, "validate_archive", tracked_validate)
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("evidence.tar", _TAR, "application/x-tar"),
            ("evidence.7z", _SEVEN_ZIP, "application/x-7z-compressed"),
        ],
    )

    assert response.status_code == 200
    assert [call[0] for call in validation_calls] == ["evidence.tar", "evidence.7z"]
    assert all(call[1] for call in validation_calls)


def test_office_only_batches_never_acquire_archive_limiter(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")

    class ExplodingLimiter:
        async def acquire(self):
            raise AssertionError("Office uploads must not acquire archive limiter")

        def release(self):
            raise AssertionError("Office uploads must not release archive limiter")

    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", ExplodingLimiter())
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("chart.png", _PNG, "image/png"),
            ("report.docx", b"office", "application/x-tar"),
        ],
    )

    assert response.status_code == 200
    assert response.json()["errors"] == []


def test_extended_invalid_errors_are_classified_and_ordered_before_later_successes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("broken.tar", b"not-a-tar", "application/x-tar"),
            ("notes.txt", b"notes", "text/plain"),
            ("icon.ico", b"not-a-png", "image/png"),
            ("report.docx", b"office", "application/x-tar"),
            ("chart.png", _PNG, "image/png"),
        ],
    )

    assert response.status_code == 200
    assert [asset["filename"] for asset in response.json()["assets"]] == [
        "report.docx",
        "chart.png",
    ]
    assert response.json()["errors"] == [
        {
            "filename": "broken.tar",
            "code": "unsupported_or_mismatched_archive",
            "message": "Only valid ZIP, 7Z, TAR, TAR.GZ, and TGZ archives are supported",
        },
        {
            "filename": "notes.txt",
            "code": "unsupported_or_mismatched_attachment",
            "message": "Only supported images, archives, and Microsoft Office files are supported",
        },
        {
            "filename": "icon.ico",
            "code": "unsupported_or_mismatched_image",
            "message": "Only valid PNG, JPEG, GIF, and WebP images are supported",
        },
    ]


def test_feature_gate_never_blocks_view_or_download_of_stored_extended_assets(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    monkeypatch.delenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", raising=False)
    client = TestClient(webapp.app)
    uploaded = _upload(
        client,
        "123456",
        [
            ("evidence.tar", _TAR, "application/x-tar"),
            ("report.docx", b"office", "application/vnd.ms-word"),
        ],
    ).json()
    monkeypatch.setenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", " false ")

    for asset, expected in zip(uploaded["assets"], (_TAR, b"office"), strict=True):
        for suffix in ("", "/download"):
            response = client.get(
                f"/markdown-threads/123456/images/{asset['id']}{suffix}",
                headers=_AUTH_HEADERS,
            )

            assert response.status_code == 200
            assert response.content == expected


def test_inline_and_download_return_exact_bytes_and_original_filename(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)
    uploaded = _upload(client, "123456", [("my chart.png", _PNG, "image/png")]).json()
    asset_id = uploaded["assets"][0]["id"]

    inline = client.get(
        f"/markdown-threads/123456/images/{asset_id}", headers=_AUTH_HEADERS
    )
    download = client.get(
        f"/markdown-threads/123456/images/{asset_id}/download",
        headers=_AUTH_HEADERS,
    )

    assert inline.status_code == 200
    assert inline.content == _PNG
    assert inline.headers["content-type"].startswith("image/png")
    assert inline.headers["content-disposition"].startswith("inline")
    assert inline.headers["cache-control"] == "private, no-store"
    assert download.content == _PNG
    assert "attachment" in download.headers["content-disposition"]
    assert "my%20chart.png" in download.headers["content-disposition"]


@pytest.mark.parametrize(
    ("filename", "payload", "normalized_content_type"), _ARCHIVE_ASSETS
)
def test_archive_routes_return_exact_opaque_bytes_as_safe_download(
    tmp_path, monkeypatch, filename, payload, normalized_content_type
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)
    uploaded = _upload(
        client,
        "123456",
        [(filename, payload, normalized_content_type)],
    ).json()
    asset_id = uploaded["assets"][0]["id"]

    view = client.get(
        f"/markdown-threads/123456/images/{asset_id}", headers=_AUTH_HEADERS
    )
    download = client.get(
        f"/markdown-threads/123456/images/{asset_id}/download",
        headers=_AUTH_HEADERS,
    )

    for response in (view, download):
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"] == normalized_content_type
        assert response.headers["content-disposition"].startswith("attachment")
        if filename == "audit résumé.zip":
            assert (
                "audit%20r%C3%A9sum%C3%A9.zip"
                in response.headers["content-disposition"]
            )
        assert "\r" not in response.headers["content-disposition"]
        assert "\n" not in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(("family", "filename"), _OFFICE_ASSETS)
def test_office_routes_serve_arbitrary_bytes_as_safe_download(
    tmp_path, monkeypatch, family, filename
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    payload = f"{family}\x00arbitrary-office-bytes".encode()
    uploaded = _upload(client, "123456", [(filename, payload, "text/plain")]).json()
    asset_id = uploaded["assets"][0]["id"]

    for suffix in ("", "/download"):
        response = client.get(
            f"/markdown-threads/123456/images/{asset_id}{suffix}",
            headers=_AUTH_HEADERS,
        )

        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "\r" not in response.headers["content-disposition"]
        assert "\n" not in response.headers["content-disposition"]
        if family == "word":
            assert (
                "report%20r%C3%A9sum%C3%A9.docx"
                in response.headers["content-disposition"]
            )

    mutated_payload = b"x" * len(payload)
    stored_payload = (
        docs_root / "markdown-threads" / "123456" / "images" / asset_id / "payload"
    )
    stored_payload.write_bytes(mutated_payload)
    for suffix in ("", "/download"):
        response = client.get(
            f"/markdown-threads/123456/images/{asset_id}{suffix}",
            headers=_AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert response.content == mutated_payload


@pytest.mark.parametrize(
    "stored_metadata",
    [
        [],
        {
            "filename": "report.txt",
            "content_type": "application/octet-stream",
            "size": 1,
        },
        {
            "filename": "report.docx",
            "content_type": "application/octet-stream",
            "size": True,
        },
        {
            "filename": "report.docx",
            "content_type": "application/octet-stream",
            "size": -1,
        },
        {
            "filename": "report.docx",
            "content_type": "application/octet-stream",
            "size": "1",
        },
        {"filename": "report.docx", "content_type": "text/plain", "size": 1},
    ],
)
def test_office_routes_reject_malformed_metadata_shape(
    tmp_path, monkeypatch, stored_metadata
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app, raise_server_exceptions=False)
    uploaded = _upload(
        client,
        "123456",
        [("report.docx", b"x", "application/octet-stream")],
    ).json()
    asset_id = uploaded["assets"][0]["id"]
    metadata_path = (
        docs_root
        / "markdown-threads"
        / "123456"
        / "images"
        / asset_id
        / "metadata.json"
    )
    metadata_path.write_text(json.dumps(stored_metadata))

    for suffix in ("", "/download"):
        response = client.get(
            f"/markdown-threads/123456/images/{asset_id}{suffix}",
            headers=_AUTH_HEADERS,
        )
        assert response.status_code == 404


@pytest.mark.parametrize(
    ("metadata_size", "file_size"),
    [
        (markdown_images._MAX_IMAGE_BYTES + 1, markdown_images._MAX_IMAGE_BYTES + 1),
        (markdown_images._MAX_IMAGE_BYTES, markdown_images._MAX_IMAGE_BYTES + 1),
    ],
)
def test_stored_attachment_oversized_metadata_or_file_returns_404_without_response_read(
    tmp_path, monkeypatch, metadata_size, file_size
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app, raise_server_exceptions=False)
    uploaded = _upload(
        client,
        "123456",
        [("report.docx", b"x", "application/octet-stream")],
    ).json()
    asset_id = uploaded["assets"][0]["id"]
    asset_root = docs_root / "markdown-threads" / "123456" / "images" / asset_id
    payload_path = asset_root / "payload"
    with payload_path.open("r+b") as payload_file:
        payload_file.truncate(file_size)
    metadata_path = asset_root / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["size"] = metadata_size
    metadata_path.write_text(json.dumps(metadata))
    monkeypatch.setattr(markdown_images, "FileResponse", lambda *args, **kwargs: None)

    for suffix in ("", "/download"):
        response = client.get(
            f"/markdown-threads/123456/images/{asset_id}{suffix}",
            headers=_AUTH_HEADERS,
        )
        assert response.status_code == 404


def test_retrieval_overload_limits_archives_but_office_bypasses_limiter(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)
    uploaded = _upload(
        client,
        "123456",
        [
            ("evidence.tar", _TAR, "application/x-tar"),
            ("report.docx", b"office", "application/octet-stream"),
        ],
    ).json()
    archive_id, office_id = (asset["id"] for asset in uploaded["assets"])
    monkeypatch.setattr(
        markdown_images,
        "_ARCHIVE_BATCH_LIMITER",
        markdown_images._LoopLocalArchiveBatchLimiter(0),
    )
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_WAIT_SECONDS", 0.001)

    for suffix in ("", "/download"):
        overloaded = client.get(
            f"/markdown-threads/123456/images/{archive_id}{suffix}",
            headers=_AUTH_HEADERS,
        )
        office = client.get(
            f"/markdown-threads/123456/images/{office_id}{suffix}",
            headers=_AUTH_HEADERS,
        )

        assert overloaded.status_code == 503
        assert overloaded.json() == {"detail": "Archive validation is busy"}
        assert overloaded.headers["retry-after"] == "2"
        assert office.status_code == 200
        assert office.content == b"office"


def test_archive_routes_revalidate_off_loop_and_hold_slot_until_cancelled_worker_finishes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)
    uploaded = _upload(
        client, "123456", [("evidence.tar", _TAR, "application/x-tar")]
    ).json()
    asset_id = uploaded["assets"][0]["id"]
    limiter = asyncio.Semaphore(1)
    monkeypatch.setattr(markdown_images, "_ARCHIVE_BATCH_LIMITER", limiter)
    original_validate = markdown_images.validate_archive
    worker_started = threading.Event()
    worker_finish = threading.Event()
    validation_threads: list[bool] = []

    def blocking_validate(filename, content_type, data):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            validation_threads.append(True)
        else:
            validation_threads.append(False)
        worker_started.set()
        if not validation_threads[-1]:
            raise AssertionError("retrieval validation ran on event loop")
        if not worker_finish.wait(5):
            raise AssertionError("retrieval validation worker was not released")
        return original_validate(filename, content_type, data)

    monkeypatch.setattr(markdown_images, "validate_archive", blocking_validate)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app), base_url="http://test"
        ) as async_client:
            request = asyncio.create_task(
                async_client.get(
                    f"/markdown-threads/123456/images/{asset_id}",
                    headers=_AUTH_HEADERS,
                )
            )
            assert await asyncio.to_thread(worker_started.wait, 2)
            assert validation_threads == [True]
            request.cancel()
            await asyncio.sleep(0.02)
            try:
                assert not request.done()
                assert limiter.locked()
                request.cancel()
                await asyncio.sleep(0.02)
                assert not request.done()
            finally:
                worker_finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 2)
            assert not limiter.locked()

    asyncio.run(scenario())


@pytest.mark.parametrize("suffix", ["", "/download"])
def test_archive_routes_serve_validated_snapshot_when_backing_file_changes(
    tmp_path, monkeypatch, suffix
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    uploaded = _upload(
        client, "123456", [("evidence.zip", _ZIP, "application/zip")]
    ).json()
    asset_id = uploaded["assets"][0]["id"]
    payload_path = (
        docs_root / "markdown-threads" / "123456" / "images" / asset_id / "payload"
    )
    replacement = b"x" * len(_ZIP)
    original_validate = markdown_images.validate_archive

    def validate_then_replace(filename, content_type, data):
        verified_type = original_validate(filename, content_type, data)
        payload_path.write_bytes(replacement)
        return verified_type

    monkeypatch.setattr(markdown_images, "validate_archive", validate_then_replace)

    response = client.get(
        f"/markdown-threads/123456/images/{asset_id}{suffix}",
        headers=_AUTH_HEADERS,
    )

    assert response.status_code == 200
    assert response.content == _ZIP
    assert payload_path.read_bytes() == replacement
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["cache-control"] == "private, no-store"


def test_missing_or_corrupt_assets_are_not_served(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    missing_id = "00000000-0000-4000-8000-000000000000"
    corrupt_dir = docs_root / "markdown-threads" / "123456" / "images" / missing_id
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "payload").write_bytes(_PNG)

    missing = client.get(
        f"/markdown-threads/123456/images/{missing_id}", headers=_AUTH_HEADERS
    )

    assert missing.status_code == 404


def test_non_string_stored_content_type_is_not_served(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app, raise_server_exceptions=False)
    uploaded = _upload(client, "123456", [("one.png", _PNG, "image/png")]).json()
    asset_id = uploaded["assets"][0]["id"]
    metadata_path = (
        docs_root
        / "markdown-threads"
        / "123456"
        / "images"
        / asset_id
        / "metadata.json"
    )
    metadata = json.loads(metadata_path.read_text())
    metadata["content_type"] = 7
    metadata_path.write_text(json.dumps(metadata))

    response = client.get(
        f"/markdown-threads/123456/images/{asset_id}", headers=_AUTH_HEADERS
    )

    assert response.status_code == 404


def test_same_size_corrupt_payload_is_not_served(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    uploaded = _upload(client, "123456", [("one.png", _PNG, "image/png")]).json()
    asset_id = uploaded["assets"][0]["id"]
    payload = (
        docs_root / "markdown-threads" / "123456" / "images" / asset_id / "payload"
    )
    payload.write_bytes(b"x" * len(_PNG))

    response = client.get(
        f"/markdown-threads/123456/images/{asset_id}", headers=_AUTH_HEADERS
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("filename", "payload", "normalized_content_type"), _ARCHIVE_ASSETS
)
def test_same_size_corrupt_archive_payload_is_not_served(
    tmp_path, monkeypatch, filename, payload, normalized_content_type
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    uploaded = _upload(
        client, "123456", [(filename, payload, normalized_content_type)]
    ).json()
    asset_id = uploaded["assets"][0]["id"]
    stored_payload = (
        docs_root / "markdown-threads" / "123456" / "images" / asset_id / "payload"
    )
    stored_payload.write_bytes(b"x" * len(payload))

    for suffix in ("", "/download"):
        response = client.get(
            f"/markdown-threads/123456/images/{asset_id}{suffix}",
            headers=_AUTH_HEADERS,
        )

        assert response.status_code == 404


@pytest.mark.parametrize("markdown_id", ["12345", "1234567", "12a456", "１２３４５６"])
def test_markdown_id_requires_exactly_six_ascii_digits(
    tmp_path, monkeypatch, markdown_id
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(client, markdown_id, [("one.png", _PNG, "image/png")])

    assert response.status_code == 422


def test_cleanup_deletes_combined_image_archive_and_all_office_families(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    assets = [
        ("one.png", _PNG, "image/png"),
        *_ARCHIVE_ASSETS,
        *[
            (filename, f"{family}-office".encode(), "application/octet-stream")
            for family, filename in _OFFICE_ASSETS
        ],
    ]
    for start in range(0, len(assets), 5):
        response = _upload(client, "123456", assets[start : start + 5])
        assert response.status_code == 200
        assert response.json()["errors"] == []

    first = client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)
    second = client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)

    assert first.status_code == 200
    assert first.json()["deleted_count"] == len(assets)
    assert second.status_code == 200
    assert second.json()["deleted_count"] == 0
    assert not (docs_root / "markdown-threads" / "123456" / "images").exists()


def test_cleanup_concurrent_deletes_claim_namespace_once_without_residue(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    uploaded = _upload(
        client,
        "123456",
        [
            ("one.png", _PNG, "image/png"),
            ("evidence.zip", _ZIP, "application/zip"),
            ("report.docx", b"office", "application/octet-stream"),
        ],
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["errors"] == []

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as async_client:
            return await asyncio.gather(
                async_client.delete(
                    "/markdown-threads/123456/images", headers=_AUTH_HEADERS
                ),
                async_client.delete(
                    "/markdown-threads/123456/images", headers=_AUTH_HEADERS
                ),
            )

    responses = asyncio.run(scenario())

    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["deleted_count"] for response in responses) == [
        0,
        3,
    ]
    namespace_root = docs_root / "markdown-threads" / "123456"
    assert not (namespace_root / "images").exists()
    assert list(namespace_root.glob(".images-delete-*")) == []


def test_cleanup_waits_for_entire_upload_batch_then_deletes_it(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    original_store = markdown_images._store_asset
    first_stored = threading.Event()
    finish_upload = threading.Event()

    def pause_after_first_store(*args, filename, **kwargs):
        original_store(*args, filename=filename, **kwargs)
        if filename == "one.png":
            first_stored.set()
            if not finish_upload.wait(5):
                raise AssertionError("upload was not released")

    monkeypatch.setattr(markdown_images, "_store_asset", pause_after_first_store)

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app), base_url="http://test"
        ) as client:
            upload = asyncio.create_task(
                client.post(
                    "/markdown-threads/123456/images",
                    headers=_AUTH_HEADERS,
                    files=[
                        ("files", ("one.png", _PNG, "image/png")),
                        ("files", ("two.png", _PNG, "image/png")),
                    ],
                )
            )
            assert await asyncio.to_thread(first_stored.wait, 2)
            delete = asyncio.create_task(
                client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)
            )
            await asyncio.sleep(0.05)
            try:
                assert not delete.done()
            finally:
                finish_upload.set()
            return await asyncio.wait_for(upload, 2), await asyncio.wait_for(delete, 2)

    uploaded, deleted = asyncio.run(scenario())

    assert uploaded.status_code == 200
    assert [asset["filename"] for asset in uploaded.json()["assets"]] == [
        "one.png",
        "two.png",
    ]
    assert deleted.status_code == 200
    assert deleted.json()["deleted_count"] == 2
    images_root = docs_root / "markdown-threads" / "123456" / "images"
    assert not images_root.exists()


def test_upload_waits_for_delete_fence_then_creates_new_namespace(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    original_delete = markdown_images._delete_images_namespace
    delete_started = threading.Event()
    finish_delete = threading.Event()

    def blocking_delete(images_root):
        delete_started.set()
        if not finish_delete.wait(5):
            raise AssertionError("delete was not released")
        return original_delete(images_root)

    monkeypatch.setattr(markdown_images, "_delete_images_namespace", blocking_delete)

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app), base_url="http://test"
        ) as client:
            delete = asyncio.create_task(
                client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)
            )
            assert await asyncio.to_thread(delete_started.wait, 2)
            upload = asyncio.create_task(
                client.post(
                    "/markdown-threads/123456/images",
                    headers=_AUTH_HEADERS,
                    files={"files": ("one.png", _PNG, "image/png")},
                )
            )
            await asyncio.sleep(0.05)
            try:
                assert not upload.done()
            finally:
                finish_delete.set()
            return await asyncio.wait_for(delete, 2), await asyncio.wait_for(upload, 2)

    deleted, uploaded = asyncio.run(scenario())

    assert deleted.status_code == 200
    assert deleted.json()["deleted_count"] == 0
    assert uploaded.status_code == 200
    assert [asset["filename"] for asset in uploaded.json()["assets"]] == ["one.png"]
    images_root = docs_root / "markdown-threads" / "123456" / "images"
    assert len([child for child in images_root.iterdir() if child.is_dir()]) == 1


def test_namespace_mutation_locks_use_at_most_64_fixed_stripes(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)

    async def scenario() -> int:
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app), base_url="http://test"
        ) as client:
            for value in range(64 * 3):
                response = await client.delete(
                    f"/markdown-threads/{value:06d}/images", headers=_AUTH_HEADERS
                )
                assert response.status_code == 200
                assert response.json()["deleted_count"] == 0
        loop_locks = markdown_images._NAMESPACE_MUTATION_LOCKS._locks[
            asyncio.get_running_loop()
        ]
        return len(loop_locks)

    allocated_lock_count = asyncio.run(scenario())
    lock_files = sorted((docs_root / ".markdown-asset-locks").glob("*.lock"))

    assert allocated_lock_count <= 64
    assert len(lock_files) == 64
    assert {path.name for path in lock_files} == {
        f"stripe-{stripe:02d}.lock" for stripe in range(64)
    }


def test_same_stripe_ids_serialize_while_unrelated_stripe_proceeds(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    held_markdown_id = "123456"
    colliding_markdown_id = "123520"
    unrelated_markdown_id = "123457"
    original_store = markdown_images._store_asset
    stored = threading.Event()
    finish_upload = threading.Event()

    def blocking_store(images_root, **kwargs):
        original_store(images_root, **kwargs)
        if images_root.parent.name == held_markdown_id:
            stored.set()
            if not finish_upload.wait(5):
                raise AssertionError("upload was not released")

    monkeypatch.setattr(markdown_images, "_store_asset", blocking_store)

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=webapp.app), base_url="http://test"
        ) as client:
            upload = asyncio.create_task(
                client.post(
                    f"/markdown-threads/{held_markdown_id}/images",
                    headers=_AUTH_HEADERS,
                    files={"files": ("one.png", _PNG, "image/png")},
                )
            )
            assert await asyncio.to_thread(stored.wait, 2)
            colliding_delete = asyncio.create_task(
                client.delete(
                    f"/markdown-threads/{colliding_markdown_id}/images",
                    headers=_AUTH_HEADERS,
                )
            )
            unrelated_delete = asyncio.create_task(
                client.delete(
                    f"/markdown-threads/{unrelated_markdown_id}/images",
                    headers=_AUTH_HEADERS,
                )
            )
            unrelated_response = await asyncio.wait_for(unrelated_delete, 1)
            try:
                assert unrelated_response.status_code == 200
                assert not colliding_delete.done()
            finally:
                finish_upload.set()
            return (
                await asyncio.wait_for(upload, 2),
                await asyncio.wait_for(colliding_delete, 2),
            )

    uploaded, colliding_deleted = asyncio.run(scenario())

    assert uploaded.status_code == 200
    assert colliding_deleted.status_code == 200
    assert colliding_deleted.json()["deleted_count"] == 0


def test_cancelled_cross_loop_file_lock_wait_does_not_leak_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    holder_ready = threading.Event()
    release_holder = threading.Event()
    holder_errors: list[BaseException] = []

    def hold_fence() -> None:
        async def scenario() -> None:
            fence = markdown_images._NamespaceMutationFence("123456")
            await fence.acquire()
            holder_ready.set()
            try:
                if not release_holder.wait(5):
                    raise AssertionError("holder was not released")
            finally:
                fence.release()

        try:
            asyncio.run(scenario())
        except BaseException as exc:
            holder_errors.append(exc)

    holder = threading.Thread(target=hold_fence)
    holder.start()
    assert holder_ready.wait(2)

    async def contend() -> None:
        contender_fence = markdown_images._NamespaceMutationFence("123456")
        contender = asyncio.create_task(contender_fence.acquire())
        await asyncio.sleep(0.05)
        assert not contender.done()
        contender.cancel()
        await asyncio.sleep(0.05)
        assert not contender.done()
        release_holder.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(contender, 2)

        subsequent_fence = markdown_images._NamespaceMutationFence("123456")
        await asyncio.wait_for(subsequent_fence.acquire(), 1)
        subsequent_fence.release()

    try:
        asyncio.run(contend())
    finally:
        release_holder.set()
        holder.join(2)

    assert not holder.is_alive()
    assert holder_errors == []


def test_cleanup_counts_only_supported_metadata_and_removes_whole_namespace(
    tmp_path, monkeypatch
):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app, raise_server_exceptions=False)
    uploaded = _upload(client, "123456", [("one.png", _PNG, "image/png")])
    assert uploaded.status_code == 200
    images_root = docs_root / "markdown-threads" / "123456" / "images"
    unsupported = images_root / "unsupported"
    unsupported.mkdir()
    (unsupported / "payload").write_bytes(b"x")
    (unsupported / "metadata.json").write_text(
        json.dumps({"filename": "bad.bin", "content_type": [], "size": 1})
    )

    response = client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["deleted_count"] == 1
    assert not images_root.exists()


def test_upload_and_download_pdf_document(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)

    uploaded = _upload(
        client,
        "123456",
        [
            ("quarterly-report.pdf", _PDF, "application/pdf"),
            ("MANUAL.PDF", _PDF, "application/octet-stream"),
        ],
    )

    assert uploaded.status_code == 200
    data = uploaded.json()
    assert len(data["assets"]) == 2
    assert data["errors"] == []
    assert data["assets"][0]["filename"] == "quarterly-report.pdf"
    assert data["assets"][0]["content_type"] == "application/pdf"
    assert data["assets"][1]["filename"] == "MANUAL.PDF"
    assert data["assets"][1]["content_type"] == "application/pdf"

    asset_id = data["assets"][0]["id"]

    view_resp = client.get(
        f"/markdown-threads/123456/images/{asset_id}",
        headers=_AUTH_HEADERS,
    )
    assert view_resp.status_code == 200
    assert view_resp.content == _PDF
    assert view_resp.headers["content-type"] == "application/pdf"
    assert "attachment" in view_resp.headers["content-disposition"]

    download_resp = client.get(
        f"/markdown-threads/123456/images/{asset_id}/download",
        headers=_AUTH_HEADERS,
    )
    assert download_resp.status_code == 200
    assert download_resp.content == _PDF
    assert download_resp.headers["content-type"] == "application/pdf"
    assert "attachment" in download_resp.headers["content-disposition"]


def test_upload_rejects_corrupt_pdf_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [("bad.pdf", b"not-a-pdf-header", "application/pdf")],
    )

    assert response.status_code == 200
    assert response.json()["assets"] == []
    assert response.json()["errors"] == [
        {
            "filename": "bad.pdf",
            "code": "unsupported_or_mismatched_pdf",
            "message": "Only valid PDF documents are supported",
        }
    ]


def test_feature_gate_disables_pdf_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    monkeypatch.setenv("MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED", "false")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [
            ("chart.png", _PNG, "image/png"),
            ("document.pdf", _PDF, "application/pdf"),
        ],
    )

    assert response.status_code == 200
    assert [asset["filename"] for asset in response.json()["assets"]] == ["chart.png"]
    assert response.json()["errors"] == [
        {
            "filename": "document.pdf",
            "code": "extended_attachment_upload_disabled",
            "message": "Extended archive and Microsoft Office uploads are disabled",
        }
    ]
