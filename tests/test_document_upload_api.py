"""Tests for document upload API endpoints."""

from fastapi.testclient import TestClient

import webapp
from conftest import TEST_API_KEY

_AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


def test_upload_documents_to_requested_folder(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)

    client = TestClient(webapp.app)
    response = client.post(
        "/documents/upload",
        data={"folder": "policy"},
        headers=_AUTH_HEADERS,
        files=[
            ("files", ("policy.md", b"# Policy\n", "text/markdown")),
            ("files", ("rules.txt", b"Rules\n", "text/plain")),
        ],
    )

    assert response.status_code == 201
    data = response.json()
    assert data["folder"] == "policy"
    assert data["count"] == 2
    assert data["saved"] == [
        {
            "filename": "policy.md",
            "path": "docs/policy/policy.md",
            "size": 9,
        },
        {
            "filename": "rules.txt",
            "path": "docs/policy/rules.txt",
            "size": 6,
        },
    ]
    assert (docs_root / "policy" / "policy.md").read_bytes() == b"# Policy\n"
    assert (docs_root / "policy" / "rules.txt").read_bytes() == b"Rules\n"


def test_upload_documents_allows_nested_folders(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)

    client = TestClient(webapp.app)
    response = client.post(
        "/documents/upload",
        data={"folder": "policy/auto"},
        headers=_AUTH_HEADERS,
        files=[("files", ("coverage.pdf", b"pdf bytes", "application/pdf"))],
    )

    assert response.status_code == 201
    assert response.json()["saved"][0]["path"] == "docs/policy/auto/coverage.pdf"
    assert (docs_root / "policy" / "auto" / "coverage.pdf").read_bytes() == b"pdf bytes"


def test_upload_documents_rejects_path_traversal_folder(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)

    client = TestClient(webapp.app)
    response = client.post(
        "/documents/upload",
        data={"folder": "../outside"},
        headers=_AUTH_HEADERS,
        files=[("files", ("policy.md", b"content", "text/markdown"))],
    )

    assert response.status_code == 400
    assert not (tmp_path / "outside").exists()


def test_upload_documents_sanitizes_uploaded_filename(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)

    client = TestClient(webapp.app)
    response = client.post(
        "/documents/upload",
        data={"folder": "policy"},
        headers=_AUTH_HEADERS,
        files=[("files", ("../policy.md", b"content", "text/markdown"))],
    )

    assert response.status_code == 201
    assert response.json()["saved"][0]["filename"] == "policy.md"
    assert (docs_root / "policy" / "policy.md").read_bytes() == b"content"
    assert not (tmp_path / "policy.md").exists()


def test_upload_documents_rejects_large_files(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(webapp, "DOCS_ROOT", docs_root)
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")

    client = TestClient(webapp.app)

    # 1.1 MB content
    large_content = b"a" * (1100 * 1024)
    response = client.post(
        "/documents/upload",
        data={"folder": "policy"},
        headers=_AUTH_HEADERS,
        files=[("files", ("large.md", large_content, "text/markdown"))],
    )

    assert response.status_code == 413
    assert "exceeds limit" in response.json()["detail"]
