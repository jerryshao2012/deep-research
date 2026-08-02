"""Contract tests for synced Markdown image storage."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import webapp
from conftest import TEST_API_KEY

_AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}
_PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload"
_JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-payload"
_GIF = b"GIF89a" + b"gif-payload"
_WEBP = b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 " + b"webp"


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
        [(f"chart-{i}.png", _PNG, "image/png") for i in range(6)],
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
            "message": "Image filename is invalid",
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
    _upload(client, "123456", [("one.png", _PNG, "image/png")])

    first = client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)
    second = client.delete("/markdown-threads/123456/images", headers=_AUTH_HEADERS)

    assert first.status_code == 200
    assert first.json()["deleted_count"] == 1
    assert second.status_code == 200
    assert second.json()["deleted_count"] == 0
    assert not (docs_root / "markdown-threads" / "123456" / "images").exists()
