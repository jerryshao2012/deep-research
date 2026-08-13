"""Contract tests for synced Markdown asset storage."""

from __future__ import annotations

import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from conftest import TEST_API_KEY
from fastapi.testclient import TestClient

import webapp

_AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}
_PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload"
_JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-payload"
_GIF = b"GIF89a" + b"gif-payload"
_WEBP = b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 " + b"webp"


def _zip_bytes(filename: str = "report.txt", content: bytes = b"report") -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
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


def _upload(client: TestClient, markdown_id: str, files):
    return client.post(
        f"/markdown-threads/{markdown_id}/images",
        headers=_AUTH_HEADERS,
        files=[("files", file) for file in files],
    )


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
def test_upload_accepts_zip_browser_mime_variants(
    tmp_path, monkeypatch, content_type
):
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
            "message": "Only valid ZIP archives are supported",
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
            "message": "Only valid ZIP archives are supported",
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
    assert response.json()["errors"][0]["code"] == (
        "unsupported_or_mismatched_archive"
    )


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
    assert response.json()["errors"][0]["code"] == (
        "unsupported_or_mismatched_archive"
    )


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
    assert response.json()["errors"][0]["code"] == (
        "unsupported_or_mismatched_archive"
    )


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
    archive_dir = (
        docs_root / "markdown-threads" / "123456" / "images" / archive["id"]
    )
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


def test_upload_rejects_oversized_file_per_item(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [("large.png", _PNG + b"x" * (10 * 1024 * 1024), "image/png")],
    )

    assert response.status_code == 200
    assert response.json()["assets"] == []
    assert response.json()["errors"][0]["code"] == "file_too_large"


def test_upload_rejects_oversized_archive_per_item(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)

    response = _upload(
        client,
        "123456",
        [("large.zip", _ZIP + b"x" * (10 * 1024 * 1024), "application/zip")],
    )

    assert response.status_code == 200
    assert response.json()["assets"] == []
    assert response.json()["errors"][0] == {
        "filename": "large.zip",
        "code": "file_too_large",
        "message": "File exceeds 10 MiB",
    }


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


def test_archive_routes_return_exact_opaque_bytes_as_safe_download(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "DOCS_ROOT", tmp_path / "docs")
    client = TestClient(webapp.app)
    uploaded = _upload(
        client,
        "123456",
        [("audit résumé.zip", _ZIP, "application/zip")],
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
        assert response.content == _ZIP
        assert response.headers["content-type"].startswith("application/zip")
        assert response.headers["content-disposition"].startswith("attachment")
        assert (
            "audit%20r%C3%A9sum%C3%A9.zip"
            in response.headers["content-disposition"]
        )
        assert "\r" not in response.headers["content-disposition"]
        assert "\n" not in response.headers["content-disposition"]


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


def test_same_size_corrupt_archive_payload_is_not_served(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    uploaded = _upload(
        client, "123456", [("evidence.zip", _ZIP, "application/zip")]
    ).json()
    asset_id = uploaded["assets"][0]["id"]
    payload = (
        docs_root / "markdown-threads" / "123456" / "images" / asset_id / "payload"
    )
    payload.write_bytes(b"x" * len(_ZIP))

    response = client.get(
        f"/markdown-threads/123456/images/{asset_id}/download",
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


def test_delete_namespace_is_idempotent(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    client = TestClient(webapp.app)
    _upload(
        client,
        "123456",
        [
            ("one.png", _PNG, "image/png"),
            ("evidence.zip", _ZIP, "application/zip"),
        ],
    )

    first = client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)
    second = client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)

    assert first.status_code == 200
    assert first.json()["deleted_count"] == 2
    assert second.status_code == 200
    assert second.json()["deleted_count"] == 0
    assert not (docs_root / "markdown-threads" / "123456" / "images").exists()
