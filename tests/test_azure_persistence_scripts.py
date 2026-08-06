from __future__ import annotations

import re
from pathlib import Path

import pytest

from research_agent import azure_storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_azure_build_stages_context_without_git_metadata() -> None:
    source = (PROJECT_ROOT / "build.sh").read_text(encoding="utf-8")

    assert "set -o pipefail" in source
    assert 'mktemp -d "$SCRIPT_DIR/.container-build-context.XXXXXX"' in source
    assert "git ls-files --cached --others --exclude-standard -z" in source
    assert "tar --null -T - -cf -" in source
    assert 'tar -xf - -C "$BUILD_CONTEXT_DIR"' in source
    assert 'cp .env.docker "$BUILD_CONTEXT_DIR/.env.docker"' in source
    assert (
            'container build --platform linux/amd64 -t $FULL_IMAGE_NAME "$BUILD_CONTEXT_DIR"'
            in source
    )
    assert "trap cleanup_build_context EXIT" in source


def test_azure_deploy_uses_sqlite_without_cosmos() -> None:
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert re.search(
        r"(?m)^\s*-\s+name:\s+DB_TYPE\s*\n\s+value:\s+sqlite\s*$",
        source,
    )
    for forbidden in ("az cosmosdb", "COSMOSDB_", "cosmosdb-", "value: cosmosdb"):
        assert forbidden not in source


def test_passkey_sqlite_deployment_is_single_replica_on_persistent_azure_file():
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert "az storage share-rm create" in source
    assert "az containerapp env storage set" in source
    assert "mountPath: /mnt/auth" in source
    assert "storageType: AzureFile" in source
    assert re.search(
        r"name:\s+SQLITE_DB_PATH\s*\n\s+value:\s+/mnt/auth/auth.db",
        source,
    )
    assert re.search(
        r"name:\s+AUTH_SQLITE_JOURNAL_MODE\s*\n\s+value:\s+DELETE",
        source,
    )
    assert "maxReplicas: 1" in source


def test_passkey_demo_configuration_documents_safe_azure_sqlite_contract():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert re.search(r"(?m)^PASSKEY_ENABLED=false$", env_example)
    for setting in (
            "PASSKEY_RP_ID",
            "PASSKEY_RP_NAME",
            "PASSKEY_ORIGINS",
            "PASSKEY_PROXY_ID",
            "PASSKEY_PROXY_SECRET",
            "OAUTH_SECRET_KEY",
            "SQLITE_DB_PATH",
            "AUTH_SQLITE_JOURNAL_MODE",
    ):
        assert setting in env_example
        assert setting in readme

    assert "Azure File" in readme
    assert "one replica" in readme.lower()
    assert "DELETE" in readme
    assert "OAuth recovery" in readme
    assert "PASSKEY_PROXY_SECRET" in readme and "Key Vault" in readme


def test_passkey_demo_documents_requested_multi_domain_rp_configuration():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    expected = (
        'PASSKEY_RP_IDS="bmo-deepagent-ui-0312.azurewebsites.net,'
        'bmo-deepagent-ui.vercel.app"'
    )
    expected_origins = (
        'PASSKEY_ORIGINS="https://bmo-deepagent-ui-0312.azurewebsites.net,'
        'https://bmo-deepagent-ui.vercel.app"'
    )

    assert expected in env_example
    assert expected_origins in env_example
    assert expected in readme
    assert expected_origins in readme


def test_passkey_documentation_never_presents_an_accepted_oauth_secret_placeholder():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assignments = re.findall(r"(?m)^OAUTH_SECRET_KEY=(.*)$", readme)

    assert assignments
    assert all(value.strip() in {"", '""', "''"} for value in assignments)
    assert "inject `OAUTH_SECRET_KEY` at runtime" in readme


def test_generic_azure_sync_includes_langgraph_state(monkeypatch) -> None:
    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", "/tmp/reports")
    monkeypatch.setenv("INPUT_FOLDER", "/tmp/input")

    tracked = azure_storage._resolve_tracked_folders()

    assert [prefix for prefix, _path in tracked] == [
        "docs",
        "output",
        "input",
        ".langgraph_api",
    ]


def test_azure_entrypoint_uses_packaged_storage_cli() -> None:
    source = (PROJECT_ROOT / "entrypoint.sh").read_text(encoding="utf-8")

    assert "python3 -m research_agent.azure_storage startup" in source
    assert "python3 -m azure_storage startup" not in source


@pytest.mark.parametrize(
    "environment_update",
    [
        {"AZURE_STORAGE_CONTAINER_NAME": "demo-container"},
        {"STORAGE_ACCOUNT_NAME": "demoaccount"},
        {"STORAGE_ACCOUNT_KEY": "demokey"},
    ],
)
def test_partial_azure_configuration_disables_optional_helpers(
        monkeypatch,
        tmp_path,
        environment_update,
) -> None:
    monkeypatch.delenv("AZURE_STORAGE_CONTAINER_NAME", raising=False)
    monkeypatch.delenv("STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("STORAGE_ACCOUNT_KEY", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)

    for name, value in environment_update.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(
        azure_storage,
        "_get_client",
        lambda: pytest.fail("disabled Azure helper created a client"),
    )

    assert azure_storage.is_azure_storage_enabled() is False
    assert azure_storage.startup_sync() == 0
    assert azure_storage.download_prefix_sync("docs", tmp_path / "docs") == 0
    assert azure_storage.upload_directory_sync(tmp_path, "docs") == 0
    azure_storage.fire_and_forget_upload(tmp_path / "missing", "docs/missing")
    azure_storage.fire_and_forget_directory_upload(tmp_path, "docs")


class _MockBlob:
    def __init__(self, name: str) -> None:
        self.name = name


class _MockContainerClient:
    def __init__(self, blobs: list[str]) -> None:
        self._blobs = blobs
        self.downloads: list[tuple[str, str]] = []

    def list_blobs(self, name_starts_with: str = ""):
        return [
            _MockBlob(name) for name in self._blobs if name.startswith(name_starts_with)
        ]

    def get_blob_client(self, blob_name: str):
        class _MockBlobClient:
            def __init__(self, name: str, parent: _MockContainerClient) -> None:
                self.name = name
                self.parent = parent

            def download_blob(self):
                class _DownloadStream:
                    def readall(self) -> bytes:
                        return b"downloaded"

                return _DownloadStream()

            def upload_blob(self, data, overwrite: bool = True) -> None:
                pass

        return _MockBlobClient(blob_name, self)


def test_azure_download_prefix_sync(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AZURE_STORAGE_CONTAINER_NAME", "demo-container")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

    mock_container = _MockContainerClient(["docs/file1.txt", "docs/sub/file2.txt"])
    monkeypatch.setattr(azure_storage, "_get_container_client", lambda: mock_container)

    dest_dir = tmp_path / "downloaded"
    count = azure_storage.download_prefix_sync("docs", dest_dir)

    assert count == 2
    assert (dest_dir / "file1.txt").is_file()
    assert (dest_dir / "sub" / "file2.txt").is_file()
    assert (dest_dir / "file1.txt").read_bytes() == b"downloaded"
