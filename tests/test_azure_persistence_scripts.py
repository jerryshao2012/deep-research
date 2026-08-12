from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from research_agent import azure_storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOLVER = PROJECT_ROOT / "scripts/resolve_azure_endpoints.sh"
METADATA_NAME = ".resolved-azure-endpoints.json"
RESOLVER_ENV = {
    "AZURE_SUBSCRIPTION_ID": "00000000-1111-2222-3333-444444444444",
    "RESOURCE_GROUP": "demo-rg",
    "ENV_NAME": "demo-env",
    "BACKEND_APP_NAME": "demo-api",
    "UI_APP_NAME": "demo-ui",
}
ENVIRONMENT_ID = (
    "/subscriptions/00000000-1111-2222-3333-444444444444/"
    "resourceGroups/demo-rg/providers/Microsoft.App/managedEnvironments/demo-env"
)
DEFAULT_DOMAIN = "calmpond-123.eastus.azurecontainerapps.io"
BACKEND_URL = f"https://demo-api.{DEFAULT_DOMAIN}"
UI_URL = f"https://demo-ui.{DEFAULT_DOMAIN}"
EXPECTED_AZ_ARGV = [
    "containerapp",
    "env",
    "show",
    "--subscription",
    RESOLVER_ENV["AZURE_SUBSCRIPTION_ID"],
    "--resource-group",
    RESOLVER_ENV["RESOURCE_GROUP"],
    "--name",
    RESOLVER_ENV["ENV_NAME"],
    "--query",
    "[id,properties.defaultDomain,properties.provisioningState]",
    "--output",
    "tsv",
]


def _expected_metadata(*, domain: str = DEFAULT_DOMAIN) -> dict[str, str]:
    backend_url = f"https://demo-api.{domain}"
    ui_url = f"https://demo-ui.{domain}"
    return {
        "azure_environment_id": ENVIRONMENT_ID,
        "azure_environment_default_domain": domain,
        "backend_app_name": "demo-api",
        "ui_app_name": "demo-ui",
        "backend_url": backend_url,
        "azure_ui_url": ui_url,
        "frontend_urls": f"{ui_url},https://bmo-deepagent-ui.vercel.app",
        "google_callback_url": f"{backend_url}/auth/callback/google",
        "github_callback_url": f"{backend_url}/auth/callback/github",
        "github_homepage_url": ui_url,
    }


def _install_fake_az(
    tmp_path: Path,
    *,
    output: str | None = None,
    stdout: str = "",
    stderr: str = "",
    status: int = 0,
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    argv_log = tmp_path / "az-argv.jsonl"
    fake_az = bin_dir / "az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stdout.write(os.environ.get("FAKE_AZ_STDOUT", ""))
sys.stderr.write(os.environ.get("FAKE_AZ_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_AZ_STATUS", "0")))
""",
        encoding="utf-8",
    )
    fake_az.chmod(fake_az.stat().st_mode | stat.S_IXUSR)
    if output is None:
        output = f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n"
    environment = tmp_path / "fake-az.env"
    environment.write_text(
        json.dumps(
            {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "FAKE_AZ_ARGV_LOG": str(argv_log),
                "FAKE_AZ_STDOUT": stdout or output,
                "FAKE_AZ_STDERR": stderr,
                "FAKE_AZ_STATUS": str(status),
            }
        ),
        encoding="utf-8",
    )
    return environment, argv_log


def _run_resolver(
    tmp_path: Path,
    fake_environment: Path,
    *args: str,
    environment_update: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(RESOLVER_ENV)
    env.update(json.loads(fake_environment.read_text(encoding="utf-8")))
    if environment_update:
        env.update(environment_update)
    return subprocess.run(
        ["bash", str(RESOLVER), *args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _read_az_calls(argv_log: Path) -> list[list[str]]:
    if not argv_log.exists():
        return []
    return [json.loads(line) for line in argv_log.read_text().splitlines()]


def test_azure_endpoint_resolver_queries_environment_once_and_emits_schema(tmp_path):
    fake_environment, argv_log = _install_fake_az(tmp_path)

    result = _run_resolver(tmp_path, fake_environment)

    assert result.returncode == 0
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    assert result.stdout.splitlines() == [
        f"AZURE_ENVIRONMENT_ID={ENVIRONMENT_ID}",
        f"AZURE_ENVIRONMENT_DEFAULT_DOMAIN={DEFAULT_DOMAIN}",
        "BACKEND_APP_NAME=demo-api",
        "UI_APP_NAME=demo-ui",
        f"BACKEND_URL={BACKEND_URL}",
        f"AZURE_UI_URL={UI_URL}",
        f"FRONTEND_URLS={UI_URL},https://bmo-deepagent-ui.vercel.app",
        f"GOOGLE_CALLBACK_URL={BACKEND_URL}/auth/callback/google",
        f"GITHUB_CALLBACK_URL={BACKEND_URL}/auth/callback/github",
        f"GITHUB_HOMEPAGE_URL={UI_URL}",
        "CHANGED=true",
    ]
    assert result.stderr.splitlines() == [
        "ACTION REQUIRED: update and verify Google/GitHub OAuth provider settings before deployment.",
        f"Google authorized redirect URI: {BACKEND_URL}/auth/callback/google",
        f"GitHub authorization callback URL: {BACKEND_URL}/auth/callback/github",
        f"GitHub homepage / frontend origin: {UI_URL}",
    ]
    assert not (tmp_path / METADATA_NAME).exists()


def test_azure_endpoint_resolver_record_is_atomic_and_unchanged_is_reminder(tmp_path):
    fake_environment, argv_log = _install_fake_az(tmp_path)

    recorded = _run_resolver(tmp_path, fake_environment, "--record")
    metadata_path = tmp_path / METADATA_NAME

    assert recorded.returncode == 0
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == _expected_metadata()
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(f"{METADATA_NAME}.tmp.*"))

    unchanged = _run_resolver(tmp_path, fake_environment)
    assert unchanged.returncode == 0
    assert unchanged.stdout.splitlines()[-1] == "CHANGED=false"
    assert unchanged.stderr.splitlines() == [
        "OAuth provider reminder: verify the following URLs remain configured.",
        f"Google authorized redirect URI: {BACKEND_URL}/auth/callback/google",
        f"GitHub authorization callback URL: {BACKEND_URL}/auth/callback/github",
        f"GitHub homepage / frontend origin: {UI_URL}",
    ]
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV, EXPECTED_AZ_ARGV]


def test_azure_endpoint_resolver_reports_metadata_changes_without_writing(tmp_path):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_text(
        json.dumps(_expected_metadata(domain="old.example.test")), encoding="utf-8"
    )
    before = metadata_path.read_bytes()
    fake_environment, _ = _install_fake_az(tmp_path)

    result = _run_resolver(tmp_path, fake_environment)

    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == "CHANGED=true"
    assert result.stderr.startswith("ACTION REQUIRED:")
    assert metadata_path.read_bytes() == before


@pytest.mark.parametrize(
    ("environment_update", "message"),
    [
        ({"AZURE_SUBSCRIPTION_ID": ""}, "AZURE_SUBSCRIPTION_ID"),
        ({"AZURE_SUBSCRIPTION_ID": "not-a-uuid"}, "AZURE_SUBSCRIPTION_ID"),
        ({"RESOURCE_GROUP": ""}, "RESOURCE_GROUP"),
        ({"RESOURCE_GROUP": "bad group"}, "RESOURCE_GROUP"),
        ({"RESOURCE_GROUP": "bad."}, "RESOURCE_GROUP"),
        ({"ENV_NAME": ""}, "ENV_NAME"),
        ({"ENV_NAME": "Bad_Env"}, "ENV_NAME"),
        ({"ENV_NAME": "a"}, "ENV_NAME"),
        ({"BACKEND_APP_NAME": "Bad_App"}, "BACKEND_APP_NAME"),
        ({"BACKEND_APP_NAME": "1api"}, "BACKEND_APP_NAME"),
        ({"BACKEND_APP_NAME": "a"}, "BACKEND_APP_NAME"),
        ({"BACKEND_APP_NAME": "bad--api"}, "BACKEND_APP_NAME"),
        ({"BACKEND_APP_NAME": "a" * 33}, "BACKEND_APP_NAME"),
        ({"BACKEND_APP_NAME": "Badapi"}, "BACKEND_APP_NAME"),
        ({"UI_APP_NAME": "-bad"}, "UI_APP_NAME"),
        ({"UI_APP_NAME": "1ui"}, "UI_APP_NAME"),
        ({"UI_APP_NAME": "u"}, "UI_APP_NAME"),
        ({"UI_APP_NAME": "bad--ui"}, "UI_APP_NAME"),
        ({"UI_APP_NAME": "u" * 33}, "UI_APP_NAME"),
        ({"UI_APP_NAME": "Badui"}, "UI_APP_NAME"),
    ],
)
def test_azure_endpoint_resolver_rejects_invalid_inputs_before_az(
    tmp_path, environment_update, message
):
    fake_environment, argv_log = _install_fake_az(tmp_path)

    result = _run_resolver(
        tmp_path, fake_environment, environment_update=environment_update
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert result.stdout == ""
    assert _read_az_calls(argv_log) == []


def test_azure_endpoint_resolver_accepts_aca_app_name_length_boundaries(tmp_path):
    fake_environment, argv_log = _install_fake_az(tmp_path)
    backend_name = "ab"
    ui_name = f"{'u' * 31}1"

    result = _run_resolver(
        tmp_path,
        fake_environment,
        environment_update={
            "BACKEND_APP_NAME": backend_name,
            "UI_APP_NAME": ui_name,
        },
    )

    assert result.returncode == 0
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    assert f"BACKEND_URL=https://{backend_name}.{DEFAULT_DOMAIN}" in result.stdout
    assert f"AZURE_UI_URL=https://{ui_name}.{DEFAULT_DOMAIN}" in result.stdout


@pytest.mark.parametrize(
    ("az_output", "message"),
    [
        (f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tFailed\n", "Succeeded"),
        (f"not-an-azure-id\t{DEFAULT_DOMAIN}\tSucceeded\n", "resource ID"),
        (f"{ENVIRONMENT_ID}\tBad_Domain\tSucceeded\n", "default domain"),
        (f"{ENVIRONMENT_ID}\t\tSucceeded\n", "default domain"),
    ],
)
def test_azure_endpoint_resolver_rejects_invalid_environment_response(
    tmp_path, az_output, message
):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_bytes(b"prior validation bytes\n")
    fake_environment, argv_log = _install_fake_az(tmp_path, output=az_output)

    result = _run_resolver(tmp_path, fake_environment)

    assert result.returncode != 0
    assert message in result.stderr
    assert result.stdout == ""
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    assert metadata_path.read_bytes() == b"prior validation bytes\n"


@pytest.mark.parametrize(
    "environment_id",
    [
        (
            "/subscriptions/11111111-1111-2222-3333-444444444444/"
            "resourceGroups/demo-rg/providers/Microsoft.App/"
            "managedEnvironments/demo-env"
        ),
        (
            "/subscriptions/00000000-1111-2222-3333-444444444444/"
            "resourceGroups/other-rg/providers/Microsoft.App/"
            "managedEnvironments/demo-env"
        ),
        (
            "/subscriptions/00000000-1111-2222-3333-444444444444/"
            "resourceGroups/demo-rg/providers/Microsoft.App/"
            "managedEnvironments/other-env"
        ),
    ],
)
def test_azure_endpoint_resolver_rejects_environment_id_for_other_resource(
    tmp_path, environment_id
):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_bytes(b"prior mismatched-id bytes\n")
    fake_environment, argv_log = _install_fake_az(
        tmp_path,
        output=f"{environment_id}\t{DEFAULT_DOMAIN}\tSucceeded\n",
    )

    result = _run_resolver(tmp_path, fake_environment, "--record")

    assert result.returncode != 0
    assert "does not match requested" in result.stderr
    assert result.stdout == ""
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    assert metadata_path.read_bytes() == b"prior mismatched-id bytes\n"


def test_azure_endpoint_resolver_matches_environment_resource_id_case_insensitively(
    tmp_path,
):
    fake_environment, argv_log = _install_fake_az(
        tmp_path,
        output=f"{ENVIRONMENT_ID.upper()}\t{DEFAULT_DOMAIN}\tSucceeded\n",
    )

    result = _run_resolver(tmp_path, fake_environment)

    assert result.returncode == 0
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    assert result.stdout.splitlines()[-1] == "CHANGED=true"


def test_azure_endpoint_resolver_compare_only_never_creates_temp_files(tmp_path):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_text(json.dumps(_expected_metadata()), encoding="utf-8")
    fake_environment, argv_log = _install_fake_az(tmp_path)
    bin_dir = Path(json.loads(fake_environment.read_text())["PATH"].split(":", 1)[0])
    write_sentinel = tmp_path / "mktemp-was-called"
    fake_mktemp = bin_dir / "mktemp"
    fake_mktemp.write_text(
        f"#!/bin/sh\nprintf called > '{write_sentinel}'\nexit 72\n",
        encoding="utf-8",
    )
    fake_mktemp.chmod(fake_mktemp.stat().st_mode | stat.S_IXUSR)

    result = _run_resolver(
        tmp_path,
        fake_environment,
        environment_update={"TMPDIR": str(tmp_path)},
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == "CHANGED=false"
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    assert not write_sentinel.exists()
    assert not list(tmp_path.glob(f"{METADATA_NAME}.tmp.*"))
    assert not list(tmp_path.glob("resolve-azure-endpoints.*"))


def test_azure_endpoint_resolver_preserves_az_failure_bytes_and_status(tmp_path):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_bytes(b"prior metadata bytes\n")
    fake_environment, argv_log = _install_fake_az(
        tmp_path, stdout="az stdout bytes\n", stderr="az stderr bytes\n", status=43
    )

    result = _run_resolver(tmp_path, fake_environment, "--record")

    assert result.returncode == 43
    assert result.stdout == "az stdout bytes\n"
    assert result.stderr == "az stderr bytes\n"
    assert metadata_path.read_bytes() == b"prior metadata bytes\n"
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]


@pytest.mark.parametrize(
    "malformed_bytes",
    [b"{malformed existing bytes\n", b"[]\n", b'{"unexpected": "schema"}\n'],
)
def test_azure_endpoint_resolver_fails_closed_for_malformed_metadata(
    tmp_path, malformed_bytes
):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_bytes(malformed_bytes)
    fake_environment, _ = _install_fake_az(tmp_path)

    result = _run_resolver(tmp_path, fake_environment, "--record")

    assert result.returncode != 0
    assert "malformed" in result.stderr.lower()
    assert result.stdout == ""
    assert metadata_path.read_bytes() == malformed_bytes


def test_azure_endpoint_resolver_preserves_metadata_when_atomic_rename_fails(tmp_path):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_text(json.dumps(_expected_metadata(domain="old.test")))
    before = metadata_path.read_bytes()
    fake_environment, _ = _install_fake_az(tmp_path)
    bin_dir = Path(json.loads(fake_environment.read_text())["PATH"].split(":", 1)[0])
    fake_mv = bin_dir / "mv"
    fake_mv.write_text(
        "#!/bin/sh\nprintf 'rename failed bytes\\n' >&2\nexit 47\n", encoding="utf-8"
    )
    fake_mv.chmod(fake_mv.stat().st_mode | stat.S_IXUSR)

    result = _run_resolver(tmp_path, fake_environment, "--record")

    assert result.returncode == 47
    assert result.stderr == "rename failed bytes\n"
    assert metadata_path.read_bytes() == before
    assert not list(tmp_path.glob(f"{METADATA_NAME}.tmp.*"))


@pytest.mark.parametrize(
    ("command", "failure_bytes", "status"),
    [
        ("python3", "serialization failed bytes\n", 46),
        ("chmod", "write failed bytes\n", 48),
    ],
)
def test_azure_endpoint_resolver_preserves_serialization_and_write_failures(
    tmp_path, command, failure_bytes, status
):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_text(json.dumps(_expected_metadata(domain="old.test")))
    before = metadata_path.read_bytes()
    fake_environment, _ = _install_fake_az(tmp_path)
    bin_dir = Path(json.loads(fake_environment.read_text())["PATH"].split(":", 1)[0])
    failing_command = bin_dir / command
    failing_command.write_text(
        f"#!/bin/sh\nprintf '{failure_bytes}' >&2\nexit {status}\n", encoding="utf-8"
    )
    failing_command.chmod(failing_command.stat().st_mode | stat.S_IXUSR)

    result = _run_resolver(tmp_path, fake_environment, "--record")

    assert result.returncode == status
    assert result.stderr == failure_bytes
    assert metadata_path.read_bytes() == before
    assert not list(tmp_path.glob(f"{METADATA_NAME}.tmp.*"))


def test_azure_endpoint_resolver_rejects_unknown_arguments_without_az(tmp_path):
    fake_environment, argv_log = _install_fake_az(tmp_path)

    result = _run_resolver(tmp_path, fake_environment, "--write-dotenv")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "unknown argument: --write-dotenv" in result.stderr
    assert _read_az_calls(argv_log) == []


@pytest.mark.parametrize("script_name", ["build.sh", "deploy.sh"])
def test_azure_script_uses_configured_subscription_id(script_name: str) -> None:
    source = (PROJECT_ROOT / script_name).read_text(encoding="utf-8")

    assert ': "${AZURE_SUBSCRIPTION_ID:?Set AZURE_SUBSCRIPTION_ID in env.sh}"' in source
    assert 'az account set --subscription "$AZURE_SUBSCRIPTION_ID"' in source
    assert 'AZURE_SUBSCRIPTION_ID="66fadccd-d26d-4dd0-b108-46b3c581cdb3"' not in source


def test_azure_deploy_uses_configured_global_resource_names() -> None:
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert ': "${KV_NAME:?Set KV_NAME in env.sh}"' in source
    assert ': "${STORAGE_ACCOUNT_NAME:?Set STORAGE_ACCOUNT_NAME in env.sh}"' in source
    assert 'STORAGE_ACCOUNT_NAME="stdeepagents"' not in source


def test_azure_build_stages_context_without_git_metadata() -> None:
    source = (PROJECT_ROOT / "build.sh").read_text(encoding="utf-8")

    assert "set -o pipefail" in source
    assert 'mktemp -d "$SCRIPT_DIR/.container-build-context.XXXXXX"' in source
    assert "git ls-files --cached --others --exclude-standard -z" in source
    assert "tar --null -T - -cf -" in source
    assert 'tar -xf - -C "$BUILD_CONTEXT_DIR"' in source
    assert 'cp .env.docker "$BUILD_CONTEXT_DIR/.env.docker"' in source
    assert (
        'container_runtime_build --platform linux/amd64 -t "$FULL_IMAGE_NAME" '
        '"$BUILD_CONTEXT_DIR"' in source
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
    auth_guide = (PROJECT_ROOT / "documents/guides/authentication.md").read_text(
        encoding="utf-8"
    )
    storage_guide = (PROJECT_ROOT / "documents/deployment/azure/storage.md").read_text(
        encoding="utf-8"
    )
    security_guide = (
        PROJECT_ROOT / "documents/deployment/azure/security.md"
    ).read_text(encoding="utf-8")
    assert re.search(r"(?m)^PASSKEY_ENABLED=false$", env_example)
    for setting in (
        "PASSKEY_RP_ID",
        "PASSKEY_RP_NAME",
        "PASSKEY_ORIGINS",
        "PASSKEY_PROXY_ID",
        "PASSKEY_PROXY_SECRET",
        "OAUTH_SECRET_KEY",
    ):
        assert setting in env_example
        assert setting in auth_guide

    for setting in ("SQLITE_DB_PATH", "AUTH_SQLITE_JOURNAL_MODE"):
        assert setting in env_example
        assert setting in storage_guide

    assert "Azure Files" in storage_guide
    assert "one replica" in storage_guide.lower()
    assert "DELETE" in storage_guide
    assert "OAuth recovery" in auth_guide
    assert re.search(
        r"(?m)^- Store .*`OAUTH_SECRET_KEY`.*`PASSKEY_PROXY_SECRET` "
        r"in a secret manager\.",
        auth_guide,
    )
    assert "Key Vault" in security_guide


def test_passkey_demo_documents_requested_multi_domain_rp_configuration():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    auth_guide = (PROJECT_ROOT / "documents/guides/authentication.md").read_text(
        encoding="utf-8"
    )
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
    assert (
        "`PASSKEY_ORIGINS` and either `PASSKEY_RP_IDS` or `PASSKEY_RP_ID`" in auth_guide
    )


def test_passkey_documentation_never_presents_an_accepted_oauth_secret_placeholder():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    auth_guide = (PROJECT_ROOT / "documents/guides/authentication.md").read_text(
        encoding="utf-8"
    )
    maintained_docs = "\n".join((env_example, auth_guide))
    assignments = re.findall(r"(?m)^OAUTH_SECRET_KEY=(.*)$", maintained_docs)

    assert assignments
    assert all(value.strip() in {"", '""', "''"} for value in assignments)
    assert re.search(
        r"(?m)^- Store .*`OAUTH_SECRET_KEY`.*`PASSKEY_PROXY_SECRET` "
        r"in a secret manager\.",
        auth_guide,
    )


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
