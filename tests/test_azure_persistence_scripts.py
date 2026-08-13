from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from research_agent import azure_storage
from scripts import sanitize_passkey_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOLVER = PROJECT_ROOT / "scripts/resolve_azure_endpoints.sh"
SANITIZER = PROJECT_ROOT / "scripts/sanitize_passkey_dotenv.py"
CONFIG_MERGER = PROJECT_ROOT / "scripts/merge_azure_containerapp_config.py"
CONFIG_RENDERER = PROJECT_ROOT / "scripts/render_azure_containerapp_config.py"
DOCKER_CREDENTIALS = PROJECT_ROOT / "scripts/load_docker_credentials.py"
KEY_VAULT_RBAC = PROJECT_ROOT / "scripts/evaluate_keyvault_rbac.py"
AZURE_METADATA_SNAPSHOT = PROJECT_ROOT / "scripts/snapshot_azure_passkey_metadata.py"
BUILD_CONFIG_PUBLISHER = PROJECT_ROOT / "scripts/publish_build_config.py"
KEY_VAULT_SECRET_VALIDATOR = (
    PROJECT_ROOT / "scripts/validate_keyvault_secret_versions.py"
)
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


def _parse_resolver_assignments(stdout: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line in stdout.splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)='((?:[^']|'\"'\"')*)'", line)
        if match is None:
            raise ValueError(f"unsafe resolver assignment: {line!r}")
        key, encoded = match.groups()
        if key in assignments:
            raise ValueError(f"duplicate resolver assignment: {key}")
        assignments[key] = encoded.replace("'\"'\"'", "'")
    return assignments


def _run_sanitizer(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SANITIZER), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_passkey_dotenv_check_accepts_only_files_without_protected_keys(tmp_path):
    dotenv = tmp_path / "backend.env"
    dotenv.write_bytes(b"OTHER=kept\r\n# comment\r\nEMPTY=\r\n")

    result = _run_sanitizer("--input", str(dotenv), "--check")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert dotenv.read_bytes() == b"OTHER=kept\r\n# comment\r\nEMPTY=\r\n"


@pytest.mark.parametrize(
    "key",
    [
        "PASSKEY_PROXY_SECRET",
        "FRONTEND_URLS",
        "PASSKEY_ORIGINS",
        "PASSKEY_RP_ID",
        "PASSKEY_RP_IDS",
        "PASSKEY_DERIVE_FROM_FRONTEND_URLS",
        "PASSKEY_ENABLED",
        "PASSKEY_PROXY_ID",
    ],
)
def test_passkey_dotenv_check_rejects_protected_keys_without_leaking_values(
    tmp_path, key
):
    dotenv = tmp_path / "backend.env"
    canary = "never-print-this-canary"
    dotenv.write_text(f'{key}="{canary}"\nOTHER=ok\n', encoding="utf-8")

    result = _run_sanitizer("--input", str(dotenv), "--check")

    assert result.returncode == 2
    assert key in result.stderr
    assert canary not in result.stdout + result.stderr
    assert dotenv.read_text(encoding="utf-8") == f'{key}="{canary}"\nOTHER=ok\n'


def test_passkey_dotenv_sanitize_preserves_unrelated_bytes_newlines_and_mode(tmp_path):
    dotenv = tmp_path / "backend.env"
    original = (
        b"# private configuration\r\n"
        b"UNCHANGED = ' spaced # value ' # keep this comment\r\n"
        b"PASSKEY_ORIGINS=https://old.example.test\r\n"
        b"PASSKEY_RP_IDS=old.example.test\r\n"
        b"LAST=kept"
    )
    expected = (
        b"# private configuration\r\n"
        b"UNCHANGED = ' spaced # value ' # keep this comment\r\n"
        b"LAST=kept"
    )
    dotenv.write_bytes(original)
    dotenv.chmod(0o640)

    result = _run_sanitizer("--input", str(dotenv), "--sanitize")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert dotenv.read_bytes() == expected
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o640
    assert not list(tmp_path.glob(f".{dotenv.name}.sanitize.*"))


def test_passkey_dotenv_sanitize_captures_proxy_secret_securely(tmp_path):
    dotenv = tmp_path / "backend.env"
    capture = tmp_path / "passkey-secret"
    canary = "old-secret-canary"
    dotenv.write_text(
        f"PASSKEY_PROXY_SECRET='{canary}'\nOTHER=value\n", encoding="utf-8"
    )

    result = _run_sanitizer(
        "--input",
        str(dotenv),
        "--sanitize",
        "--capture-secret-to",
        str(capture),
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert dotenv.read_bytes() == b"OTHER=value\n"
    assert capture.read_bytes() == canary.encode()
    assert stat.S_IMODE(capture.stat().st_mode) == 0o600
    assert canary not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "content",
    [
        b"PASSKEY_PROXY_SECRET=one\nPASSKEY_PROXY_SECRET=two\n",
        b"PASSKEY_PROXY_SECRET\nOTHER=kept\n",
        b"PASSKEY_PROXY_SECRET=$(unsafe-command)\nOTHER=kept\n",
        b"OTHER=$(unsafe-command)\nPASSKEY_ORIGINS=https://old.test\n",
        b"PASSKEY_ORIGINS=one\\\ntwo\n",
        b"OTHER=one\nOTHER=two\n",
    ],
)
def test_passkey_dotenv_sanitize_rejects_duplicate_or_ambiguous_syntax_atomically(
    tmp_path, content
):
    dotenv = tmp_path / "backend.env"
    dotenv.write_bytes(content)
    dotenv.chmod(0o640)

    result = _run_sanitizer("--input", str(dotenv), "--sanitize")

    assert result.returncode == 2
    assert result.stdout == ""
    assert dotenv.read_bytes() == content
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o640
    assert not list(tmp_path.glob(f".{dotenv.name}.sanitize.*"))


def test_passkey_dotenv_sanitize_capture_failure_leaves_input_unchanged(tmp_path):
    dotenv = tmp_path / "backend.env"
    capture = tmp_path / "already-exists"
    original = b"PASSKEY_PROXY_SECRET=secret-canary\nOTHER=value\n"
    dotenv.write_bytes(original)
    capture.write_bytes(b"do-not-overwrite")

    result = _run_sanitizer(
        "--input",
        str(dotenv),
        "--sanitize",
        "--capture-secret-to",
        str(capture),
    )

    assert result.returncode != 0
    assert "secret-canary" not in result.stdout + result.stderr
    assert dotenv.read_bytes() == original
    assert capture.read_bytes() == b"do-not-overwrite"


def test_passkey_dotenv_rejects_missing_symlink_and_unsafe_capture_paths(tmp_path):
    missing = tmp_path / "missing.env"
    target = tmp_path / "target.env"
    target.write_bytes(b"PASSKEY_ORIGINS=https://old.test\n")
    symlink = tmp_path / "linked.env"
    symlink.symlink_to(target)

    missing_result = _run_sanitizer("--input", str(missing), "--check")
    symlink_result = _run_sanitizer("--input", str(symlink), "--sanitize")

    assert missing_result.returncode != 0
    assert "does not exist" in missing_result.stderr.lower()
    assert symlink_result.returncode != 0
    assert target.read_bytes() == b"PASSKEY_ORIGINS=https://old.test\n"


@pytest.mark.parametrize("failing_operation", ["write", "fsync", "fchmod", "replace"])
def test_passkey_dotenv_sanitize_io_failures_are_atomic_and_clean(
    tmp_path, monkeypatch, failing_operation
):
    dotenv = tmp_path / "backend.env"
    capture = tmp_path / "capture"
    original = b"PASSKEY_PROXY_SECRET=private-canary\nOTHER=value\n"
    dotenv.write_bytes(original)
    real_write = sanitize_passkey_dotenv.os.write
    real_fsync = sanitize_passkey_dotenv.os.fsync
    real_fchmod = sanitize_passkey_dotenv.os.fchmod
    real_replace = sanitize_passkey_dotenv.os.replace

    if failing_operation == "write":
        monkeypatch.setattr(
            sanitize_passkey_dotenv.os,
            "write",
            lambda *_args: (_ for _ in ()).throw(OSError("injected write failure")),
        )
    elif failing_operation == "fsync":
        monkeypatch.setattr(
            sanitize_passkey_dotenv.os,
            "fsync",
            lambda *_args: (_ for _ in ()).throw(OSError("injected fsync failure")),
        )
    elif failing_operation == "fchmod":
        monkeypatch.setattr(
            sanitize_passkey_dotenv.os,
            "fchmod",
            lambda *_args: (_ for _ in ()).throw(OSError("injected chmod failure")),
        )
    else:
        monkeypatch.setattr(
            sanitize_passkey_dotenv.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected replace failure")),
        )

    result = sanitize_passkey_dotenv.main(
        [
            "--input",
            str(dotenv),
            "--sanitize",
            "--capture-secret-to",
            str(capture),
        ]
    )

    monkeypatch.setattr(sanitize_passkey_dotenv.os, "write", real_write)
    monkeypatch.setattr(sanitize_passkey_dotenv.os, "fsync", real_fsync)
    monkeypatch.setattr(sanitize_passkey_dotenv.os, "fchmod", real_fchmod)
    monkeypatch.setattr(sanitize_passkey_dotenv.os, "replace", real_replace)
    assert result != 0
    assert dotenv.read_bytes() == original
    assert not capture.exists()
    assert not list(tmp_path.glob(f".{dotenv.name}.sanitize.*"))


def test_passkey_dotenv_capture_fsync_failure_removes_partial_capture(
    tmp_path, monkeypatch
):
    dotenv = tmp_path / "backend.env"
    capture = tmp_path / "capture"
    original = b"PASSKEY_PROXY_SECRET=private-canary\nOTHER=value\n"
    dotenv.write_bytes(original)
    real_fsync = sanitize_passkey_dotenv.os.fsync
    calls = 0

    def fail_second_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected capture fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(sanitize_passkey_dotenv.os, "fsync", fail_second_fsync)

    result = sanitize_passkey_dotenv.main(
        [
            "--input",
            str(dotenv),
            "--sanitize",
            "--capture-secret-to",
            str(capture),
        ]
    )

    assert result != 0
    assert dotenv.read_bytes() == original
    assert not capture.exists()
    assert not list(tmp_path.glob(f".{dotenv.name}.sanitize.*"))


def test_passkey_dotenv_concurrent_replacement_is_never_overwritten(
    tmp_path, monkeypatch
):
    dotenv = tmp_path / "backend.env"
    capture = tmp_path / "capture"
    original = b"PASSKEY_PROXY_SECRET=old-private-canary\nOTHER=old\n"
    replacement = b"OTHER=new-concurrent-value\n"
    dotenv.write_bytes(original)
    real_assert = sanitize_passkey_dotenv._assert_input_unchanged

    def replace_then_validate(path, expected_stat, expected_content):
        concurrent = tmp_path / "concurrent.env"
        concurrent.write_bytes(replacement)
        os.replace(concurrent, path)
        real_assert(path, expected_stat, expected_content)

    monkeypatch.setattr(
        sanitize_passkey_dotenv, "_assert_input_unchanged", replace_then_validate
    )

    result = sanitize_passkey_dotenv.main(
        [
            "--input",
            str(dotenv),
            "--sanitize",
            "--capture-secret-to",
            str(capture),
        ]
    )

    assert result != 0
    assert dotenv.read_bytes() == replacement
    assert not capture.exists()
    assert not list(tmp_path.glob(f".{dotenv.name}.sanitize.*"))


def test_azure_endpoint_resolver_queries_environment_once_and_emits_schema(tmp_path):
    fake_environment, argv_log = _install_fake_az(tmp_path)

    result = _run_resolver(tmp_path, fake_environment)

    assert result.returncode == 0
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    assert result.stdout.splitlines() == [
        f"AZURE_ENVIRONMENT_ID='{ENVIRONMENT_ID}'",
        f"AZURE_ENVIRONMENT_DEFAULT_DOMAIN='{DEFAULT_DOMAIN}'",
        "BACKEND_APP_NAME='demo-api'",
        "UI_APP_NAME='demo-ui'",
        f"BACKEND_URL='{BACKEND_URL}'",
        f"AZURE_UI_URL='{UI_URL}'",
        f"FRONTEND_URLS='{UI_URL},https://bmo-deepagent-ui.vercel.app'",
        f"GOOGLE_CALLBACK_URL='{BACKEND_URL}/auth/callback/google'",
        f"GITHUB_CALLBACK_URL='{BACKEND_URL}/auth/callback/github'",
        f"GITHUB_HOMEPAGE_URL='{UI_URL}'",
        "CHANGED='true'",
    ]
    assert result.stderr.splitlines() == [
        "ACTION REQUIRED: update and verify Google/GitHub OAuth provider settings before deployment.",
        f"Google authorized redirect URI: {BACKEND_URL}/auth/callback/google",
        f"GitHub authorization callback URL: {BACKEND_URL}/auth/callback/github",
        f"GitHub homepage / frontend origin: {UI_URL}",
    ]
    assert not (tmp_path / METADATA_NAME).exists()


def test_azure_endpoint_resolver_accepts_current_cli_multiline_tsv(tmp_path):
    fake_environment, argv_log = _install_fake_az(
        tmp_path,
        output=f"{ENVIRONMENT_ID}\n{DEFAULT_DOMAIN}\nSucceeded\n",
    )

    result = _run_resolver(tmp_path, fake_environment)

    assert result.returncode == 0
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    parsed = _parse_resolver_assignments(result.stdout)
    assert parsed["AZURE_ENVIRONMENT_ID"] == ENVIRONMENT_ID
    assert parsed["AZURE_ENVIRONMENT_DEFAULT_DOMAIN"] == DEFAULT_DOMAIN


def test_azure_endpoint_resolver_shell_quotes_parenthesized_resource_group(tmp_path):
    resource_group = "demo(rg)"
    environment_id = (
        "/subscriptions/00000000-1111-2222-3333-444444444444/"
        f"resourceGroups/{resource_group}/providers/Microsoft.App/"
        "managedEnvironments/demo-env"
    )
    fake_environment, _ = _install_fake_az(
        tmp_path,
        output=f"{environment_id}\t{DEFAULT_DOMAIN}\tSucceeded\n",
    )

    result = _run_resolver(
        tmp_path,
        fake_environment,
        environment_update={"RESOURCE_GROUP": resource_group},
    )

    assert result.returncode == 0
    parsed = _parse_resolver_assignments(result.stdout)
    assert parsed["AZURE_ENVIRONMENT_ID"] == environment_id
    assert parsed["BACKEND_URL"] == BACKEND_URL
    assert parsed["CHANGED"] == "true"


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
    assert _parse_resolver_assignments(unchanged.stdout)["CHANGED"] == "false"
    assert unchanged.stderr.splitlines() == [
        "OAuth provider reminder: verify the following URLs remain configured.",
        f"Google authorized redirect URI: {BACKEND_URL}/auth/callback/google",
        f"GitHub authorization callback URL: {BACKEND_URL}/auth/callback/github",
        f"GitHub homepage / frontend origin: {UI_URL}",
    ]
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV, EXPECTED_AZ_ARGV]


def test_azure_endpoint_resolver_guarded_record_requires_exact_current_assignments(
    tmp_path,
):
    fake_environment, _ = _install_fake_az(tmp_path)
    expected_path = tmp_path / "expected-assignments"
    initial = _run_resolver(tmp_path, fake_environment)
    assert initial.returncode == 0
    expected_path.write_text(initial.stdout, encoding="utf-8")

    recorded = _run_resolver(
        tmp_path,
        fake_environment,
        "--record-if-current",
        str(expected_path),
    )

    assert recorded.returncode == 0
    metadata_path = tmp_path / METADATA_NAME
    prior = metadata_path.read_bytes()
    expected_path.write_text(
        initial.stdout.replace(DEFAULT_DOMAIN, "drift.example.test"),
        encoding="utf-8",
    )
    rejected = _run_resolver(
        tmp_path,
        fake_environment,
        "--record-if-current",
        str(expected_path),
    )
    assert rejected.returncode == 2
    assert "current endpoints" in rejected.stderr.lower()
    assert rejected.stdout == ""
    assert metadata_path.read_bytes() == prior
    assert not list(tmp_path.glob(f"{METADATA_NAME}.tmp.*"))


def test_azure_endpoint_resolver_reports_metadata_changes_without_writing(tmp_path):
    metadata_path = tmp_path / METADATA_NAME
    metadata_path.write_text(
        json.dumps(_expected_metadata(domain="old.example.test")), encoding="utf-8"
    )
    before = metadata_path.read_bytes()
    fake_environment, _ = _install_fake_az(tmp_path)

    result = _run_resolver(tmp_path, fake_environment)

    assert result.returncode == 0
    assert _parse_resolver_assignments(result.stdout)["CHANGED"] == "true"
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
    parsed = _parse_resolver_assignments(result.stdout)
    assert parsed["BACKEND_URL"] == f"https://{backend_name}.{DEFAULT_DOMAIN}"
    assert parsed["AZURE_UI_URL"] == f"https://{ui_name}.{DEFAULT_DOMAIN}"


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
    assert _parse_resolver_assignments(result.stdout)["CHANGED"] == "true"


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
    assert _parse_resolver_assignments(result.stdout)["CHANGED"] == "false"
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


def test_azure_env_exports_canonical_backend_and_ui_app_names() -> None:
    source = (PROJECT_ROOT / "env.sh").read_text(encoding="utf-8")

    assert 'export BACKEND_APP_NAME="$AGENT_NAME"' in source
    assert 'export UI_APP_NAME="bmo-deepagent-ui-$SEED"' in source
    assert source.index("export AGENT_NAME=") < source.index("export BACKEND_APP_NAME=")


@pytest.mark.parametrize("script_name", ["build.sh", "deploy.sh"])
def test_azure_scripts_resolve_canonical_endpoints_without_eval(script_name: str):
    source = (PROJECT_ROOT / script_name).read_text(encoding="utf-8")

    assert '"$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh"' in source
    assert 'BACKEND_APP_NAME="$BACKEND_APP_NAME"' in source
    assert 'UI_APP_NAME="$UI_APP_NAME"' in source
    assert "eval " not in source


def test_azure_build_checks_private_dotenv_before_azure_or_runtime_access():
    source = (PROJECT_ROOT / "build.sh").read_text(encoding="utf-8")
    check = (
        'python3 "$SCRIPT_DIR/scripts/sanitize_passkey_dotenv.py" '
        '--input "$SCRIPT_DIR/.env.docker" --check'
    )
    resolver = '"$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh"'

    assert check in source
    assert source.index(check) < source.index(resolver)
    assert source.index(check) < source.index("select_container_runtime")
    assert source.index(check) < source.index("az account set")
    assert 'cp .env.docker "$BUILD_CONTEXT_DIR/.env.docker"' in source


def _install_azure_script_fixture(
    tmp_path: Path, script_name: str
) -> tuple[Path, Path]:
    fixture = tmp_path / "fixture"
    scripts_dir = fixture / "scripts"
    bin_dir = fixture / "bin"
    scripts_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(PROJECT_ROOT / script_name, fixture / script_name)
    shutil.copy2(RESOLVER, scripts_dir / RESOLVER.name)
    shutil.copy2(SANITIZER, scripts_dir / SANITIZER.name)
    shutil.copy2(CONFIG_MERGER, scripts_dir / CONFIG_MERGER.name)
    shutil.copy2(CONFIG_RENDERER, scripts_dir / CONFIG_RENDERER.name)
    shutil.copy2(DOCKER_CREDENTIALS, scripts_dir / DOCKER_CREDENTIALS.name)
    if KEY_VAULT_RBAC.exists():
        shutil.copy2(KEY_VAULT_RBAC, scripts_dir / KEY_VAULT_RBAC.name)
    if KEY_VAULT_SECRET_VALIDATOR.exists():
        shutil.copy2(
            KEY_VAULT_SECRET_VALIDATOR, scripts_dir / KEY_VAULT_SECRET_VALIDATOR.name
        )
    (fixture / "env.sh").write_text(
        "\n".join(
            [
                'export SEED="0312"',
                'export AZURE_SUBSCRIPTION_ID="00000000-1111-2222-3333-444444444444"',
                'export RESOURCE_GROUP="demo-rg"',
                'export LOCATION="canadacentral"',
                'export ENV_NAME="demo-env"',
                'export AGENT_NAME="demo-api"',
                'export BACKEND_APP_NAME="$AGENT_NAME"',
                'export UI_APP_NAME="demo-ui"',
                'export KV_NAME="demo-vault"',
                'export STORAGE_ACCOUNT_NAME="demostorage"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    argv_log = fixture / "az.jsonl"
    fake_az = bin_dir / "az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:4] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
    raise SystemExit(0)
raise SystemExit(91)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    return fixture, argv_log


def _run_docker_credentials(
    dotenv: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DOCKER_CREDENTIALS), "--input", str(dotenv), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_docker_credentials_strictly_load_only_requested_values(tmp_path):
    dotenv = tmp_path / ".env"
    pat_file = tmp_path / "pat"
    dotenv.write_text(
        "OTHER_CONTROL=$(touch never)\n"
        "DOCKER_HUB_USERNAME=demo-user\n"
        "DOCKER_HUB_PAT='private-pat-canary'\n",
        encoding="utf-8",
    )

    result = _run_docker_credentials(dotenv, "--username", "--pat-file", str(pat_file))

    assert result.returncode == 2
    assert "private-pat-canary" not in result.stdout + result.stderr
    assert not pat_file.exists()


def test_docker_credentials_normal_dotenv_fallback_and_no_pat_output(tmp_path):
    dotenv = tmp_path / ".env"
    pat_file = tmp_path / "pat"
    dotenv.write_text(
        "# local Docker credentials\n"
        "UNRELATED=value\n"
        "DOCKER_HUB_USERNAME=demo-user\n"
        "DOCKER_HUB_PAT='private-pat-canary'\n",
        encoding="utf-8",
    )

    result = _run_docker_credentials(dotenv, "--username", "--pat-file", str(pat_file))

    assert result.returncode == 0
    assert result.stdout == "demo-user\n"
    assert result.stderr == ""
    assert pat_file.read_text(encoding="utf-8") == "private-pat-canary"
    assert stat.S_IMODE(pat_file.stat().st_mode) == 0o600
    assert "private-pat-canary" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "content",
    [
        "DOCKER_HUB_USERNAME=one\nDOCKER_HUB_USERNAME=two\n",
        "DOCKER_HUB_PAT=$(unsafe)\n",
        "if true; then DOCKER_HUB_USERNAME=bad; fi\n",
    ],
)
def test_docker_credentials_reject_malformed_duplicates_and_control_syntax(
    tmp_path, content
):
    dotenv = tmp_path / ".env"
    pat_file = tmp_path / "pat"
    dotenv.write_text(content, encoding="utf-8")

    result = _run_docker_credentials(dotenv, "--username", "--pat-file", str(pat_file))

    assert result.returncode == 2
    assert result.stdout == ""
    assert not pat_file.exists()


def test_azure_build_rejects_protected_private_config_before_az_or_runtime(tmp_path):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "build.sh")
    runtime_marker = fixture / "runtime-called"
    (fixture / "scripts/container_runtime.sh").write_text(
        f"select_container_runtime() {{ touch '{runtime_marker}'; }}\n"
        "ensure_container_runtime_ready() { :; }\n",
        encoding="utf-8",
    )
    canary = "private-build-secret-canary"
    (fixture / ".env.docker").write_text(
        f"PASSKEY_PROXY_SECRET={canary}\nOTHER=value\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
        }
    )

    result = subprocess.run(
        ["bash", "build.sh"],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PASSKEY_PROXY_SECRET" in result.stderr
    assert canary not in result.stdout + result.stderr
    assert not argv_log.exists()
    assert not runtime_marker.exists()


@pytest.mark.parametrize("key", ["PASSKEY_ENABLED", "PASSKEY_PROXY_ID"])
def test_azure_build_rejects_all_deployment_owned_passkey_keys_before_az(tmp_path, key):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "build.sh")
    (fixture / "scripts/container_runtime.sh").write_text(
        "select_container_runtime() { exit 99; }\n",
        encoding="utf-8",
    )
    (fixture / ".env.docker").write_text(f"{key}=private-value\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
        }
    )

    result = subprocess.run(
        ["bash", "-x", "build.sh"],
        cwd=fixture,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert key in result.stderr
    assert not argv_log.exists()


def test_azure_build_strictly_decodes_quoted_resolver_assignments(tmp_path):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "build.sh")
    runtime_marker = fixture / "runtime-selected"
    (fixture / "scripts/container_runtime.sh").write_text(
        f"select_container_runtime() {{ printf selected > '{runtime_marker}'; }}\n"
        "ensure_container_runtime_ready() { :; }\n",
        encoding="utf-8",
    )
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    fake_resolver = fixture / "scripts/resolve_azure_endpoints.sh"
    fake_resolver.write_text("#!/bin/sh\nprintf 'CHANGED=true\\n'\n", encoding="utf-8")
    fake_resolver.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
        }
    )

    result = subprocess.run(
        ["bash", "build.sh"],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 65
    assert "malformed resolver output assignment" in result.stderr
    assert not runtime_marker.exists()
    assert not argv_log.exists()


def test_azure_build_uses_root_dotenv_docker_credentials_without_leaking_pat(tmp_path):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "build.sh")
    login_capture = fixture / "login-pat"
    (fixture / "scripts/container_runtime.sh").write_text(
        'select_container_runtime() { CONTAINER_RUNTIME="fake"; }\n'
        "ensure_container_runtime_ready() { :; }\n"
        f"container_runtime_login() {{ cat > '{login_capture}'; return 73; }}\n",
        encoding="utf-8",
    )
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    pat_canary = "private-build-pat-canary"
    (fixture / ".env").write_text(
        "DOCKER_HUB_USERNAME=dotenv-user\n"
        f"DOCKER_HUB_PAT='{pat_canary}'\n"
        "UNRELATED=value\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("DOCKER_HUB_USERNAME", None)
    env.pop("DOCKER_HUB_PAT", None)
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
        }
    )

    result = subprocess.run(
        ["bash", "-x", "build.sh"],
        cwd=fixture,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 73
    assert login_capture.read_text(encoding="utf-8") == pat_canary
    assert "dotenv-user" in result.stdout
    assert pat_canary not in result.stdout + result.stderr


def test_azure_build_process_docker_credentials_override_dotenv_fallback(tmp_path):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "build.sh")
    login_capture = fixture / "login-pat"
    (fixture / "scripts/container_runtime.sh").write_text(
        'select_container_runtime() { CONTAINER_RUNTIME="fake"; }\n'
        "ensure_container_runtime_ready() { :; }\n"
        f"container_runtime_login() {{ cat > '{login_capture}'; return 73; }}\n",
        encoding="utf-8",
    )
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    (fixture / ".env").write_text(
        "DOCKER_HUB_USERNAME=fallback-user\nDOCKER_HUB_PAT=fallback-pat\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "DOCKER_HUB_USERNAME": "exported-user",
            "DOCKER_HUB_PAT": "exported-pat",
        }
    )

    result = subprocess.run(
        ["bash", "-x", "build.sh"],
        cwd=fixture,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 73
    assert login_capture.read_text(encoding="utf-8") == "exported-pat"
    assert "exported-user" in result.stdout
    assert "fallback-user" not in result.stdout + result.stderr
    for canary in ("exported-pat", "fallback-pat"):
        assert canary not in result.stdout + result.stderr


def test_azure_deploy_first_resolution_blocks_before_any_mutation(tmp_path):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "deploy.sh")
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "DOCKER_HUB_USERNAME": "demo-user",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 3
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "ACTION REQUIRED: update and verify Google/GitHub OAuth provider settings before deployment.",
        f"Google authorized redirect URI: {BACKEND_URL}/auth/callback/google",
        f"GitHub authorization callback URL: {BACKEND_URL}/auth/callback/github",
        f"GitHub homepage / frontend origin: {UI_URL}",
        "Deployment blocked: set OAUTH_REDIRECTS_CONFIRMED=true for this process "
        "after updating the exact OAuth URLs above.",
    ]
    assert _read_az_calls(argv_log) == [EXPECTED_AZ_ARGV]
    assert not (fixture / METADATA_NAME).exists()


@pytest.mark.parametrize(
    ("args", "expected_message"),
    [
        (
            ("--oauth-redirects-confirmed", "--oauth-redirects-confirmed"),
            "may be supplied only once",
        ),
        (("--oauth-redirects-confirmed=false",), "unknown argument"),
        (("--unknown",), "unknown argument"),
        (("--help", "--oauth-redirects-confirmed"), "--help must be used alone"),
        (("-h", "--oauth-redirects-confirmed"), "--help must be used alone"),
    ],
)
def test_azure_deploy_oauth_confirmation_argument_rejects_invalid_input_before_side_effects(
    tmp_path, args, expected_message
):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "deploy.sh")
    env_marker = fixture / "env-sourced"
    sanitizer_marker = fixture / "sanitizer-called"
    resolver_marker = fixture / "resolver-called"
    (fixture / "env.sh").write_text(
        f"touch '{env_marker}'\n", encoding="utf-8"
    )
    (fixture / "scripts/sanitize_passkey_dotenv.py").write_text(
        f"#!/bin/sh\ntouch '{sanitizer_marker}'\n", encoding="utf-8"
    )
    (fixture / "scripts/resolve_azure_endpoints.sh").write_text(
        f"#!/bin/sh\ntouch '{resolver_marker}'\n", encoding="utf-8"
    )

    result = subprocess.run(
        ["bash", "deploy.sh", *args],
        cwd=fixture,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert expected_message in result.stderr
    assert not env_marker.exists()
    assert not sanitizer_marker.exists()
    assert not resolver_marker.exists()
    assert _read_az_calls(argv_log) == []


@pytest.mark.parametrize("help_arg", ["--help", "-h"])
def test_azure_deploy_help_is_side_effect_free(tmp_path, help_arg):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "deploy.sh")
    env_marker = fixture / "env-sourced"
    (fixture / "env.sh").write_text(
        f"touch '{env_marker}'\n", encoding="utf-8"
    )

    result = subprocess.run(
        ["bash", "deploy.sh", help_arg],
        cwd=fixture,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "Usage: ./deploy.sh [--oauth-redirects-confirmed] [--help]"
        in result.stdout
    )
    assert not env_marker.exists()
    assert _read_az_calls(argv_log) == []


def _run_deploy_confirmation_probe(
    tmp_path: Path,
    *,
    args: tuple[str, ...] = (),
    confirmation: str | None = None,
    changed: bool = True,
    env_sh_tail: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "deploy.sh")
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    if not changed:
        (fixture / METADATA_NAME).write_text(
            json.dumps(_expected_metadata()), encoding="utf-8"
        )
    if env_sh_tail:
        with (fixture / "env.sh").open("a", encoding="utf-8") as stream:
            stream.write(env_sh_tail)
    env = os.environ.copy()
    env.pop("OAUTH_REDIRECTS_CONFIRMED", None)
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "DOCKER_HUB_USERNAME": "demo-user",
        }
    )
    if confirmation is not None:
        env["OAUTH_REDIRECTS_CONFIRMED"] = confirmation
    result = subprocess.run(
        ["bash", "deploy.sh", *args],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, _read_az_calls(argv_log)


@pytest.mark.parametrize(
    ("args", "confirmation", "changed"),
    [
        (("--oauth-redirects-confirmed",), None, True),
        (("--oauth-redirects-confirmed",), "false", True),
        ((), "true", True),
        ((), None, False),
        (("--oauth-redirects-confirmed",), None, False),
    ],
)
def test_azure_deploy_oauth_confirmation_argument_reaches_read_only_preflight(
    tmp_path, args, confirmation, changed
):
    result, calls = _run_deploy_confirmation_probe(
        tmp_path, args=args, confirmation=confirmation, changed=changed
    )

    assert result.returncode == 91
    assert [call[:2] for call in calls] == [
        ["containerapp", "env"],
        ["group", "show"],
    ]


def test_azure_deploy_env_sh_cannot_forge_oauth_confirmation(tmp_path):
    result, calls = _run_deploy_confirmation_probe(
        tmp_path,
        env_sh_tail=(
            "export OAUTH_REDIRECTS_CONFIRMED=true\n"
            "CLI_OAUTH_REDIRECTS_CONFIRMED=true\n"
            "CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN=true\n"
        ),
    )

    assert result.returncode == 3
    assert "Deployment blocked" in result.stderr
    assert [call[:2] for call in calls] == [["containerapp", "env"]]


@pytest.mark.parametrize(
    ("args", "confirmation"),
    [
        ((), "true"),
        (("--oauth-redirects-confirmed",), "false"),
    ],
)
def test_azure_deploy_env_sh_cannot_clear_oauth_confirmation(
    tmp_path, args, confirmation
):
    result, calls = _run_deploy_confirmation_probe(
        tmp_path,
        args=args,
        confirmation=confirmation,
        env_sh_tail=(
            "unset OAUTH_REDIRECTS_CONFIRMED\n"
            "CLI_OAUTH_REDIRECTS_CONFIRMED=false\n"
            "CLI_OAUTH_REDIRECTS_CONFIRMED_SEEN=false\n"
        ),
    )

    assert result.returncode == 91
    assert [call[:2] for call in calls] == [
        ["containerapp", "env"],
        ["group", "show"],
    ]


@pytest.mark.parametrize(
    ("resolver_output", "message"),
    [
        ("CHANGED=true\n", "malformed resolver output assignment"),
        ("CHANGED='true'junk\n", "malformed resolver output assignment"),
        ("CHANGED='true'\nCHANGED='false'\n", "duplicate resolver output key"),
        ("UNKNOWN='value'\n", "unexpected resolver output key"),
    ],
)
def test_azure_deploy_strictly_decodes_quoted_resolver_assignments_before_az(
    tmp_path, resolver_output, message
):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "deploy.sh")
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    fake_resolver = fixture / "scripts/resolve_azure_endpoints.sh"
    fake_resolver.write_text(
        "#!/usr/bin/python3\n"
        "import os\n"
        "print(os.environ['FAKE_RESOLVER_OUTPUT'], end='')\n",
        encoding="utf-8",
    )
    fake_resolver.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_RESOLVER_OUTPUT": resolver_output,
            "DOCKER_HUB_USERNAME": "demo-user",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 65
    assert message in result.stderr
    assert not argv_log.exists()


def test_azure_deploy_preserves_resolver_failure_status_and_bytes(tmp_path):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "deploy.sh")
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        "#!/bin/sh\nprintf 'resolver stdout bytes\\n'\n"
        "printf 'resolver stderr bytes\\n' >&2\nexit 43\n",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env["PATH"] = f"{fixture / 'bin'}:{env['PATH']}"
    env["FAKE_AZ_ARGV_LOG"] = str(argv_log)

    result = subprocess.run(
        ["bash", "deploy.sh"],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 43
    assert result.stdout == "resolver stdout bytes\n"
    assert result.stderr == "resolver stderr bytes\n"


def _prepare_deploy_preflight_fixture(tmp_path: Path) -> tuple[Path, Path]:
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "deploy.sh")
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    (fixture / ".build_version").write_text("20260812010101\n", encoding="utf-8")
    (fixture / "webapp").mkdir()
    (fixture / "webapp/config.py").write_text(
        'API_VERSION = "9.8.7"\n', encoding="utf-8"
    )
    fake_sleep = fixture / "bin/sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o700)
    fake_date = fixture / "bin/date"
    fake_date.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y%m%d%H%M%S" ]; then printf "20260812010101\\n"; '
        'else printf "1786500061\\n"; fi\n',
        encoding="utf-8",
    )
    fake_date.chmod(0o700)
    return fixture, argv_log


@pytest.mark.parametrize(
    ("missing", "expected_message", "expected_calls"),
    [
        (
            "identity",
            "existing user-assigned managed identity",
            [["containerapp", "env"], ["group", "show"], ["identity", "show"]],
        ),
        (
            "app",
            "existing Container App",
            [
                ["containerapp", "env"],
                ["group", "show"],
                ["identity", "show"],
                ["containerapp", "show"],
            ],
        ),
        (
            "vault",
            "existing Key Vault",
            [
                ["containerapp", "env"],
                ["group", "show"],
                ["identity", "show"],
                ["containerapp", "show"],
                ["keyvault", "show"],
            ],
        ),
    ],
)
def test_azure_deploy_missing_managed_prerequisites_fail_before_mutation(
    tmp_path, missing, expected_message, expected_calls
):
    fixture, argv_log = _prepare_deploy_preflight_fixture(tmp_path)
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
missing = os.environ["FAKE_MISSING"]
if args[:3] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    if missing == "identity":
        raise SystemExit(3)
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args:
    if missing == "app":
        raise SystemExit(3)
    sys.stdout.write(json.dumps({"identity": {"userAssignedIdentities": {"/subscriptions/demo/identity": {}}}, "properties": {"configuration": {}, "template": {"containers": [{"name": "deep-research-agent"}]}}}))
elif args[:2] == ["keyvault", "show"]:
    if missing == "vault":
        raise SystemExit(3)
    sys.stdout.write("/subscriptions/demo/vaults/demo-vault|false|1\\n")
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "FAKE_MISSING": missing,
            "DOCKER_HUB_USERNAME": "demo-user",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert expected_message in result.stderr
    assert [call[:2] for call in _read_az_calls(argv_log)] == expected_calls


@pytest.mark.parametrize(
    "secret_versions",
    [
        [],
        [
            {
                "id": "https://demo-vault.vault.azure.net/secrets/PASSKEY-PROXY-SECRET/v1",
                "name": "PASSKEY-PROXY-SECRET",
                "version": None,
                "enabled": False,
            }
        ],
    ],
)
def test_azure_deploy_rejects_missing_enabled_passkey_secret_before_mutation(
    tmp_path, secret_versions
):
    fixture, argv_log = _prepare_deploy_preflight_fixture(tmp_path)
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:3] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args:
    sys.stdout.write(json.dumps({"identity": {"userAssignedIdentities": {"/subscriptions/demo/identity": {}}}, "properties": {"configuration": {}, "template": {"containers": [{"name": "deep-research-agent"}]}}}))
elif args[:2] == ["keyvault", "show"]:
    sys.stdout.write("/subscriptions/demo/vaults/demo-vault|false|1\\n")
elif args[:3] == ["keyvault", "secret", "list-versions"]:
    name = args[args.index("--name") + 1]
    if name == "PASSKEY-PROXY-SECRET":
        payload = json.loads(os.environ["FAKE_SECRET_VERSIONS"])
    else:
        payload = [{"id": f"https://demo-vault.vault.azure.net/secrets/{name}/v1", "name": name, "version": None, "enabled": True}]
    json.dump(payload, sys.stdout)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "FAKE_SECRET_VERSIONS": json.dumps(secret_versions),
            "DOCKER_HUB_USERNAME": "demo-user",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert result.returncode == 65
    assert "PASSKEY-PROXY-SECRET" in result.stderr
    assert "enabled" in result.stderr
    calls = _read_az_calls(argv_log)
    assert not any(call[:2] == ["account", "set"] for call in calls)
    assert not any(call[:2] == ["keyvault", "set-policy"] for call in calls)
    assert not any(call[:2] == ["containerapp", "update"] for call in calls)


def test_azure_deploy_preserves_passkey_secret_query_status_without_bytes(tmp_path):
    fixture, argv_log = _prepare_deploy_preflight_fixture(tmp_path)
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:3] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args:
    sys.stdout.write(json.dumps({"identity": {"userAssignedIdentities": {"/subscriptions/demo/identity": {}}}, "properties": {"configuration": {}, "template": {"containers": [{"name": "deep-research-agent"}]}}}))
elif args[:2] == ["keyvault", "show"]:
    sys.stdout.write("/subscriptions/demo/vaults/demo-vault|false|1\\n")
elif args[:3] == ["keyvault", "secret", "list-versions"]:
    name = args[args.index("--name") + 1]
    if name == "PASSKEY-PROXY-SECRET":
        sys.stdout.write("secret query stdout bytes\\n")
        sys.stderr.write("secret query stderr bytes\\n")
        raise SystemExit(47)
    json.dump([{"id": f"https://demo-vault.vault.azure.net/secrets/{name}/v1", "name": name, "version": None, "enabled": True}], sys.stdout)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "DOCKER_HUB_USERNAME": "demo-user",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode == 47
    assert "secret query stdout bytes" not in result.stdout + result.stderr
    assert "secret query stderr bytes" not in result.stdout + result.stderr
    assert "missing or unreadable" in result.stderr
    assert not any(call[:2] == ["account", "set"] for call in _read_az_calls(argv_log))


def test_azure_deploy_malformed_existing_config_fails_before_mutation(tmp_path):
    fixture, argv_log = _prepare_deploy_preflight_fixture(tmp_path)
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:3] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args:
    sys.stdout.write("{malformed config bytes\\n")
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "DOCKER_HUB_USERNAME": "demo-user",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    calls = _read_az_calls(argv_log)
    assert [call[:2] for call in calls] == [
        ["containerapp", "env"],
        ["group", "show"],
        ["identity", "show"],
        ["containerapp", "show"],
    ]


def test_azure_deploy_containerapp_show_failure_never_leaks_response_bytes(tmp_path):
    fixture, argv_log = _prepare_deploy_preflight_fixture(tmp_path)
    fake_az = fixture / "bin/az"
    stdout_canary = "partial-config-secret-canary"
    stderr_canary = "azure-error-secret-canary"
    fake_az.write_text(
        f"""#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:3] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args:
    sys.stdout.write('{stdout_canary}\\n')
    sys.stderr.write('{stderr_canary}\\n')
    raise SystemExit(47)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "DOCKER_HUB_USERNAME": "demo-user",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode == 47
    assert "Container App configuration query failed" in result.stderr
    assert stdout_canary not in result.stdout + result.stderr
    assert stderr_canary not in result.stdout + result.stderr
    assert [call[:2] for call in _read_az_calls(argv_log)] == [
        ["containerapp", "env"],
        ["group", "show"],
        ["identity", "show"],
        ["containerapp", "show"],
    ]


@pytest.mark.parametrize(
    ("docker_username", "build_version", "expected_message"),
    [
        ("bad:user\nsecret: injected", "20260812010101", "Docker Hub username"),
        ("demo-user", "20260812010101\nsecret: injected", "build version"),
    ],
)
def test_azure_deploy_hostile_render_inputs_fail_before_any_mutation(
    tmp_path, docker_username, build_version, expected_message
):
    fixture, argv_log = _prepare_deploy_preflight_fixture(tmp_path)
    (fixture / ".build_version").write_text(build_version, encoding="utf-8")
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:3] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args:
    sys.stdout.write(json.dumps({"identity": {"userAssignedIdentities": {"/subscriptions/demo/identity": {}}}, "properties": {"configuration": {}, "template": {"containers": [{"name": "deep-research-agent"}]}}}))
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "DOCKER_HUB_USERNAME": docker_username,
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert expected_message in result.stderr
    calls = _read_az_calls(argv_log)
    assert [call[:2] for call in calls] == [
        ["containerapp", "env"],
        ["group", "show"],
        ["identity", "show"],
        ["containerapp", "show"],
    ]


def test_azure_deploy_secret_immutable_rest_patch_and_multiline_tsv(tmp_path):
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "deploy.sh")
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    deploy_pat_canary = "deploy-must-not-consume-this-pat"
    (fixture / ".env").write_text(
        f"DOCKER_HUB_USERNAME=demo-user\nDOCKER_HUB_PAT='{deploy_pat_canary}'\n",
        encoding="utf-8",
    )
    (fixture / ".build_version").write_text("20260812010101\n", encoding="utf-8")
    (fixture / "webapp").mkdir()
    (fixture / "webapp/config.py").write_text(
        'API_VERSION = "9.8.7"\n', encoding="utf-8"
    )
    update_patch = fixture / "captured-update.json"
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import pathlib
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:3] == ["containerapp", "env", "show"] and "--subscription" in args:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    sys.stdout.write("/subscriptions/demo/identity\\nprincipal-123\\n")
elif args[:2] == ["keyvault", "show"] and "accessPolicies" in " ".join(args):
    sys.stdout.write("/subscriptions/demo/vaults/demo-vault|false|1\\n")
elif args[:3] == ["keyvault", "secret", "list-versions"]:
    name = args[args.index("--name") + 1]
    json.dump([{"id": f"https://demo-vault.vault.azure.net/secrets/{name}/v1", "name": name, "version": None, "enabled": True}], sys.stdout)
elif args[:3] == ["storage", "account", "show"]:
    sys.stdout.write("/subscriptions/demo/storageAccounts/demostorage\\n")
elif args[:3] == ["storage", "container-rm", "show"]:
    sys.stdout.write("deep-research-blobs\\n")
elif args[:3] == ["storage", "share-rm", "show"]:
    sys.stdout.write("deep-research-auth\\n")
elif args[:4] == ["containerapp", "env", "storage", "show"]:
    sys.stdout.write("authsqlite\\ndemostorage\\ndeep-research-auth\\nReadWrite\\n")
elif args[:2] == ["containerapp", "show"] and "provisioningState" in " ".join(args):
    sys.stdout.write("Succeeded\\n")
elif args[:2] == ["containerapp", "show"] and "--query" in args and args[args.index("--query") + 1] == "properties.template":
    if os.environ.get("FAKE_FINAL_TEMPLATE_FAILURE"):
        sys.stdout.write("final-template-secret-canary\\n")
        sys.stderr.write("final-template-error-canary\\n")
        raise SystemExit(int(os.environ["FAKE_FINAL_TEMPLATE_FAILURE"]))
    if os.environ.get("FAKE_FINAL_TEMPLATE_MALFORMED"):
        sys.stdout.write('{"secret":"final-template-secret-canary"')
        raise SystemExit(0)
    template = {"containers": [{
        "name": "renamed-main",
        "env": [{"name": "UNRELATED_ENV", "value": "kept"}],
        "volumeMounts": [{"volumeName": "unrelated-volume", "mountPath": "/kept"}],
    }]}
    if os.environ.get("FAKE_FINAL_TEMPLATE_DRIFT"):
        template["containers"][0]["image"] = "concurrent/image:changed"
    json.dump(template, sys.stdout)
elif args[:2] == ["containerapp", "show"] and "--output" in args and args[args.index("--output") + 1] == "json":
    required = [
        ("tavily-api-key", "TAVILY-API-KEY"),
        ("langchain-api-key", "LANGCHAIN-API-KEY"),
        ("upload-api-key", "UPLOAD-API-KEY"),
        ("storage-account-name", "STORAGE-ACCOUNT-NAME"),
        ("storage-account-key", "STORAGE-ACCOUNT-KEY"),
        ("azure-storage-container-name", "AZURE-STORAGE-CONTAINER-NAME"),
        ("google-api-key", "GOOGLE-API-KEY"),
        ("docker-hub-pat", "DOCKER-HUB-PAT"),
        ("passkey-proxy-secret", "PASSKEY-PROXY-SECRET"),
    ]
    json.dump({
        "id": "/subscriptions/demo/resourceGroups/demo-rg/providers/Microsoft.App/containerApps/demo-api",
        "etag": None,
        "name": "demo-api",
        "location": "canadacentral",
        "identity": {"userAssignedIdentities": {"/subscriptions/demo/identity": {}}},
        "properties": {
            "configuration": {
                "secrets": [
                    {"name": name, "keyVaultUrl": f"https://demo-vault.vault.azure.net/secrets/{secret}", "identity": "/subscriptions/demo/identity"}
                    for name, secret in required
                ],
                "registries": [{"server": "docker.io", "username": "wrong-user" if os.environ.get("FAKE_REGISTRY_DRIFT") else "demo-user", "passwordSecretRef": "docker-hub-pat"}],
            },
            "template": {"containers": [{
                "name": "renamed-main",
                "env": [{"name": "UNRELATED_ENV", "value": "kept"}],
                "volumeMounts": [{"volumeName": "unrelated-volume", "mountPath": "/kept"}],
            }]},
        },
    }, sys.stdout)
elif args[:3] == ["containerapp", "secret", "list"]:
    required = [
        ("tavily-api-key", "TAVILY-API-KEY"),
        ("langchain-api-key", "LANGCHAIN-API-KEY"),
        ("upload-api-key", "UPLOAD-API-KEY"),
        ("storage-account-name", "STORAGE-ACCOUNT-NAME"),
        ("storage-account-key", "STORAGE-ACCOUNT-KEY"),
        ("azure-storage-container-name", "AZURE-STORAGE-CONTAINER-NAME"),
        ("google-api-key", "GOOGLE-API-KEY"),
        ("docker-hub-pat", "DOCKER-HUB-PAT"),
        ("passkey-proxy-secret", "PASSKEY-PROXY-SECRET"),
    ]
    json.dump([
        {"name": name, "keyVaultUrl": f"https://demo-vault.vault.azure.net/secrets/{secret}", "identity": "/subscriptions/demo/wrong" if os.environ.get("FAKE_SECRET_DRIFT") else "/subscriptions/demo/identity"}
        for name, secret in required
    ], sys.stdout)
elif args[:2] == ["containerapp", "show"] and "ingress.fqdn" in " ".join(args):
    sys.stdout.write("demo-api.calmpond-123.eastus.azurecontainerapps.io\\n")
elif args[:3] == ["containerapp", "revision", "list"]:
    expected_query = (
        f"[?name=='{os.environ['FAKE_EXPECTED_REVISION']}'] | "
        "[0].[properties.runningState,properties.healthState]"
    )
    if "--query" not in args or args[args.index("--query") + 1] != expected_query:
        sys.stderr.write("revision query did not target exact expected revision\\n")
        raise SystemExit(89)
    sys.stdout.write("RunningAtMaxScale\\nHealthy\\n")
elif args[:1] == ["rest"]:
    source_argument = args[args.index("--body") + 1]
    if not source_argument.startswith("@"):
        raise SystemExit(88)
    source = pathlib.Path(source_argument[1:])
    pathlib.Path(os.environ["FAKE_UPDATE_PATCH"]).write_bytes(source.read_bytes())
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    fake_curl = fixture / "bin/curl"
    fake_curl.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(["curl", *sys.argv[1:]]) + "\\n")
sys.stdout.write('{"version":"9.8.7","status":"ok"}\\n')
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    fake_sleep = fixture / "bin/sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o700)
    fake_date = fixture / "bin/date"
    fake_date.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y%m%d%H%M%S" ]; then printf "20260812010101\\n"; '
        'else printf "1786500061\\n"; fi\n',
        encoding="utf-8",
    )
    fake_date.chmod(0o700)
    env = os.environ.copy()
    env.pop("DOCKER_HUB_USERNAME", None)
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "FAKE_UPDATE_PATCH": str(update_patch),
            "FAKE_EXPECTED_REVISION": "demo-api--passkeys-20260812010101",
            "DOCKER_HUB_PAT": "",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert deploy_pat_canary not in result.stdout + result.stderr
    rendered = json.loads(update_patch.read_text(encoding="utf-8"))
    assert set(rendered) == {"location", "properties"}
    assert set(rendered["properties"]) == {"template"}
    assert rendered["location"] == "canadacentral"
    revision_suffix = rendered["properties"]["template"]["revisionSuffix"]
    assert revision_suffix == "passkeys-20260812010101"
    container = rendered["properties"]["template"]["containers"][0]
    assert container["name"] == "renamed-main"
    runtime_env = {item["name"]: item for item in container["env"]}
    assert runtime_env["UNRELATED_ENV"]["value"] == "kept"
    assert runtime_env["FRONTEND_URLS"]["value"] == (
        f"{UI_URL},https://bmo-deepagent-ui.vercel.app"
    )
    assert runtime_env["PASSKEY_PROXY_SECRET"]["secretRef"] == ("passkey-proxy-secret")
    volume_mounts = {item["volumeName"]: item for item in container["volumeMounts"]}
    assert volume_mounts["unrelated-volume"]["mountPath"] == "/kept"
    assert volume_mounts["auth-sqlite"]["mountPath"] == "/mnt/auth"
    calls = _read_az_calls(argv_log)
    assert deploy_pat_canary not in json.dumps(calls)
    secret_calls = [
        call for call in calls if call[:3] == ["keyvault", "secret", "list-versions"]
    ]
    assert {call[call.index("--name") + 1] for call in secret_calls} == {
        "TAVILY-API-KEY",
        "LANGCHAIN-API-KEY",
        "UPLOAD-API-KEY",
        "STORAGE-ACCOUNT-NAME",
        "STORAGE-ACCOUNT-KEY",
        "AZURE-STORAGE-CONTAINER-NAME",
        "GOOGLE-API-KEY",
        "DOCKER-HUB-PAT",
        "PASSKEY-PROXY-SECRET",
    }
    assert all(
        "value" not in call[call.index("--query") + 1].casefold()
        for call in secret_calls
    )
    update_call = next(call for call in calls if call[:1] == ["rest"])
    assert update_call[:3] == ["rest", "--method", "patch"]
    assert update_call[update_call.index("--uri") + 1] == (
        "/subscriptions/demo/resourceGroups/demo-rg/providers/Microsoft.App/"
        "containerApps/demo-api?api-version=2025-07-01"
    )
    assert update_call[update_call.index("--headers") + 1] == (
        "Content-Type=application/merge-patch+json"
    )
    assert not any(argument.startswith("If-Match=") for argument in update_call)
    assert "containerapp update" not in json.dumps(calls)
    assert "--show-values" not in json.dumps(calls)
    account_set = next(
        index for index, call in enumerate(calls) if call[:2] == ["account", "set"]
    )
    assert [call[:2] for call in calls[:account_set]] == [
        ["containerapp", "env"],
        ["group", "show"],
        ["identity", "show"],
        ["containerapp", "show"],
        ["keyvault", "show"],
        *(["keyvault", "secret"] for _ in range(9)),
        ["storage", "account"],
        ["storage", "container-rm"],
        ["storage", "share-rm"],
        ["containerapp", "env"],
        ["containerapp", "secret"],
    ]
    curl_index = next(index for index, call in enumerate(calls) if call[0] == "curl")
    assert calls[-2:] == [EXPECTED_AZ_ARGV, EXPECTED_AZ_ARGV]
    assert curl_index < len(calls) - 2
    assert json.loads((fixture / METADATA_NAME).read_text()) == _expected_metadata()

    for drift_variable in ("FAKE_SECRET_DRIFT", "FAKE_REGISTRY_DRIFT"):
        argv_log.unlink()
        update_patch.unlink(missing_ok=True)
        drift_environment = env | {drift_variable: "true"}
        drifted = subprocess.run(
            ["bash", "deploy.sh"],
            cwd=fixture,
            env=drift_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert drifted.returncode == 4
        assert "immutable configuration" in drifted.stderr
        drift_calls = _read_az_calls(argv_log)
        assert not any(call[:1] == ["rest"] for call in drift_calls)
        assert not any(call[:2] == ["account", "set"] for call in drift_calls)
        assert not update_patch.exists()

    argv_log.unlink()
    update_patch.unlink(missing_ok=True)
    concurrent_environment = env | {"FAKE_FINAL_TEMPLATE_DRIFT": "true"}
    concurrent = subprocess.run(
        ["bash", "deploy.sh"],
        cwd=fixture,
        env=concurrent_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert concurrent.returncode == 70
    assert "concurrent" in concurrent.stderr.lower()
    assert not any(call[:1] == ["rest"] for call in _read_az_calls(argv_log))
    assert not update_patch.exists()

    for final_environment, expected_status in (
        ({"FAKE_FINAL_TEMPLATE_FAILURE": "47"}, 47),
        ({"FAKE_FINAL_TEMPLATE_MALFORMED": "true"}, 65),
    ):
        argv_log.unlink()
        failed = subprocess.run(
            ["bash", "deploy.sh"],
            cwd=fixture,
            env=env | final_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert failed.returncode == expected_status
        assert "secret-canary" not in failed.stdout + failed.stderr
        assert "error-canary" not in failed.stdout + failed.stderr
        assert not any(call[:1] == ["rest"] for call in _read_az_calls(argv_log))


def test_azure_deploy_gates_mutation_on_oauth_confirmation_and_strict_output():
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    resolver = '"$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh"'
    confirmation = "OAUTH_REDIRECTS_CONFIRMED:-"

    assert resolver in source
    assert confirmation in source
    assert 'SEEN_RESOLVER_KEYS="|"' in source
    assert "unexpected resolver output key" in source
    assert source.index(resolver) < source.index("az account set")
    assert source.index(confirmation) < source.index("az account set")
    assert ".resolved-azure-endpoints.json" not in source


def test_azure_deploy_uses_final_template_drift_guard_not_etag() -> None:
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert "APP_ETAG" not in source
    assert "If-Match" not in source
    assert "properties.template" in source


def _render_desired_config(tmp_path: Path) -> dict:
    output = tmp_path / "desired.yaml"
    result = subprocess.run(
        [
            sys.executable,
            str(CONFIG_RENDERER),
            "--docker-username",
            "demo-user",
            "--build-version",
            "20260812010101",
            "--identity-id",
            "/subscriptions/demo/identity",
            "--container-name",
            "deep-research-agent",
            "--key-vault-name",
            "demo-vault",
            "--frontend-urls",
            f"{UI_URL},https://bmo-deepagent-ui.vercel.app",
            "--storage-name",
            "authsqlite",
            "--restart-trigger",
            "1234567890",
            "--revision-suffix",
            "passkeys-20260812010101",
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(output.read_text(encoding="utf-8"))


def test_azure_config_renderer_rejects_invalid_revision_suffix(tmp_path):
    output = tmp_path / "desired.yaml"
    result = subprocess.run(
        [
            sys.executable,
            str(CONFIG_RENDERER),
            "--docker-username",
            "demo-user",
            "--build-version",
            "20260812010101",
            "--identity-id",
            "/subscriptions/demo/identity",
            "--container-name",
            "deep-research-agent",
            "--key-vault-name",
            "demo-vault",
            "--frontend-urls",
            UI_URL,
            "--storage-name",
            "authsqlite",
            "--restart-trigger",
            "1234567890",
            "--revision-suffix",
            "Bad_Suffix: injected",
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "revision suffix" in result.stderr
    assert not output.exists()


def test_azure_deploy_uses_managed_passkey_runtime_configuration(tmp_path):
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    rendered = _render_desired_config(tmp_path)
    assert "configuration" not in rendered["properties"]
    container = rendered["properties"]["template"]["containers"][0]
    environment = {item["name"]: item for item in container["env"]}

    assert environment["PASSKEY_PROXY_SECRET"]["secretRef"] == "passkey-proxy-secret"
    expected_values = {
        "FRONTEND_URLS": f"{UI_URL},https://bmo-deepagent-ui.vercel.app",
        "PASSKEY_DERIVE_FROM_FRONTEND_URLS": "true",
        "PASSKEY_ENABLED": "true",
        "PASSKEY_PROXY_ID": "web-bff",
    }
    for name, value in expected_values.items():
        assert environment[name]["value"] == value
    assert "--revision-suffix" in source
    assert "revision list" in source
    assert source.index("revision list") < source.index(
        'resolve_azure_endpoints.sh" --record-if-current'
    )
    assert source.index("VERSION_MATCHED=true") < source.index(
        'resolve_azure_endpoints.sh" --record-if-current'
    )
    assert source.count('"$SCRIPT_DIR/scripts/resolve_azure_endpoints.sh"') >= 3
    assert "az keyvault secret list-versions" in source
    assert "az keyvault secret show" not in source
    assert "--query value" not in source


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "enabled"),
        (
            [
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/TAVILY-API-KEY/v1",
                    "name": "TAVILY-API-KEY",
                    "version": None,
                    "enabled": False,
                }
            ],
            "enabled",
        ),
        ({"value": "secret-canary"}, "array"),
        (
            [
                {
                    "id": "https://wrong.vault.azure.net/secrets/TAVILY-API-KEY/v1",
                    "name": "TAVILY-API-KEY",
                    "version": None,
                    "enabled": True,
                }
            ],
            "vault",
        ),
        (
            [
                {
                    "id": "https://demo-vault.vault.azure.net:bad/secrets/TAVILY-API-KEY/v1",
                    "name": "TAVILY-API-KEY",
                    "version": None,
                    "enabled": True,
                }
            ],
            "binding",
        ),
        (
            [
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/TAVILY-API-KEY/v1",
                    "name": "TAVILY-API-KEY",
                    "version": None,
                    "enabled": True,
                },
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/TAVILY-API-KEY/v1",
                    "name": "TAVILY-API-KEY",
                    "version": "v1",
                    "enabled": True,
                },
            ],
            "duplicate",
        ),
        (
            [
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/TAVILY-API-KEY/v1",
                    "name": "TAVILY-API-KEY",
                    "version": None,
                    "enabled": True,
                    "value": "secret-canary",
                }
            ],
            "schema",
        ),
    ],
)
def test_key_vault_secret_version_validator_fails_closed(tmp_path, payload, message):
    source = tmp_path / "versions.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(KEY_VAULT_SECRET_VALIDATOR),
            str(source),
            "demo-vault",
            "TAVILY-API-KEY",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 65
    assert message in result.stderr.lower()
    assert result.stdout == ""
    assert "secret-canary" not in result.stderr


def test_key_vault_secret_version_validator_accepts_real_null_version_shape(tmp_path):
    source = tmp_path / "versions.json"
    source.write_text(
        json.dumps(
            [
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/TAVILY-API-KEY/v1",
                    "name": "TAVILY-API-KEY",
                    "version": None,
                    "enabled": False,
                },
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/TAVILY-API-KEY/v2",
                    "name": "TAVILY-API-KEY",
                    "version": None,
                    "enabled": True,
                },
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(KEY_VAULT_SECRET_VALIDATOR),
            str(source),
            "demo-vault",
            "TAVILY-API-KEY",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_azure_deploy_runs_yaml_helpers_in_project_environment():
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")

    for helper in (
        "render_azure_containerapp_config.py",
        "merge_azure_containerapp_config.py",
    ):
        assert f'uv run python "$SCRIPT_DIR/scripts/{helper}"' in source


def test_azure_deploy_uses_configured_global_resource_names() -> None:
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert ': "${KV_NAME:?Set KV_NAME in env.sh}"' in source
    assert ': "${STORAGE_ACCOUNT_NAME:?Set STORAGE_ACCOUNT_NAME in env.sh}"' in source
    assert 'STORAGE_ACCOUNT_NAME="stdeepagents"' not in source


def test_azure_deploy_help_describes_update_only_managed_passkey_cutover():
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "deploy.sh"), "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "update existing deployment" in result.stdout.lower()
    assert "managed passkey cutover" in result.stdout.lower()
    for prerequisite in (
        "resource group",
        "Container Apps environment",
        "backend Container App",
        "user-assigned managed identity",
        "Key Vault",
        "secret get access",
        "PASSKEY-PROXY-SECRET",
        "storage account",
        "Blob container",
        "Azure Files share",
        "Container Apps environment storage",
    ):
        assert prerequisite in result.stdout
    assert "Full deployment" not in result.stdout
    assert "--skip-kv-access" not in result.stdout


def test_azure_deploy_contains_no_bootstrap_permission_or_secret_mutations():
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")

    for forbidden in (
        "--skip-kv-access",
        "az group create",
        "az containerapp env create",
        "az containerapp create",
        "az containerapp update",
        "az containerapp secret set",
        "az keyvault update",
        "az keyvault create",
        "az keyvault set-policy",
        "az keyvault delete-policy",
        "az keyvault secret set",
        "az identity create",
        "az containerapp identity assign",
        "az containerapp identity remove",
        "az role assignment create",
        "az role assignment delete",
        "az storage account create",
        "az storage container create",
        "az storage container show",
        "az storage share-rm create",
        "az containerapp env storage set",
        "./secrets.sh",
    ):
        assert forbidden not in source
    assert "az storage container-rm show" in source
    assert "--show-values" not in source


@pytest.mark.parametrize(
    ("missing", "expected_message"),
    [
        ("environment", "existing Container Apps environment"),
        ("resource_group", "existing resource group"),
        ("identity_assignment", "assigned to the existing Container App"),
        ("vault_access", "lacks Key Vault secret get access"),
        ("required_secret", "TAVILY-API-KEY"),
        ("storage_account", "existing storage account"),
        ("blob_container", "existing Blob container"),
        ("file_share", "existing Azure Files share"),
        ("environment_storage", "existing Container Apps environment storage"),
        ("environment_storage_account", "does not match required Azure Files binding"),
        ("environment_storage_share", "does not match required Azure Files binding"),
        ("environment_storage_access", "does not match required Azure Files binding"),
    ],
)
def test_azure_deploy_all_update_only_prerequisites_preflight_before_mutation(
    tmp_path, missing, expected_message
):
    fixture, argv_log = _prepare_deploy_preflight_fixture(tmp_path)
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
missing = os.environ["FAKE_MISSING"]
if args[:3] == ["containerapp", "env", "show"] and "--query" in args and "defaultDomain" in args[args.index("--query") + 1]:
    if missing == "environment":
        raise SystemExit(3)
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    if missing == "resource_group":
        raise SystemExit(3)
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args:
    identity = {} if missing == "identity_assignment" else {"/subscriptions/demo/identity": {}}
    json.dump({"identity": {"userAssignedIdentities": identity}, "properties": {"configuration": {}, "template": {"containers": [{"name": "deep-research-agent"}]}}}, sys.stdout)
elif args[:2] == ["keyvault", "show"]:
    policy_matches = "0" if missing == "vault_access" else "1"
    sys.stdout.write(f"/subscriptions/demo/vaults/demo-vault|false|{policy_matches}\\n")
elif args[:3] == ["keyvault", "secret", "list-versions"]:
    name = args[args.index("--name") + 1]
    if missing == "required_secret" and name == "TAVILY-API-KEY":
        raise SystemExit(3)
    json.dump([{"id": f"https://demo-vault.vault.azure.net/secrets/{name}/v1", "name": name, "version": None, "enabled": True}], sys.stdout)
elif args[:3] == ["storage", "account", "show"]:
    if missing == "storage_account":
        raise SystemExit(3)
    sys.stdout.write("/subscriptions/demo/storageAccounts/demostorage\\n")
elif args[:3] == ["storage", "container-rm", "show"]:
    if missing == "blob_container":
        raise SystemExit(3)
    sys.stdout.write("deep-research-blobs\\n")
elif args[:3] == ["storage", "share-rm", "show"]:
    if missing == "file_share":
        raise SystemExit(3)
    sys.stdout.write("deep-research-auth\\n")
elif args[:4] == ["containerapp", "env", "storage", "show"]:
    if missing == "environment_storage":
        raise SystemExit(3)
    account = "wrongstorage" if missing == "environment_storage_account" else "demostorage"
    share = "wrong-share" if missing == "environment_storage_share" else "deep-research-auth"
    access = "ReadOnly" if missing == "environment_storage_access" else "ReadWrite"
    query = args[args.index("--query") + 1]
    if query == "name":
        sys.stdout.write("authsqlite\\n")
    else:
        sys.stdout.write(f"authsqlite\\t{account}\\t{share}\\t{access}\\n")
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "FAKE_MISSING": missing,
            "DOCKER_HUB_USERNAME": "demo-user",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert expected_message in result.stderr
    calls = _read_az_calls(argv_log)
    assert not any(call[:2] == ["account", "set"] for call in calls)
    assert not any(call[:2] == ["containerapp", "update"] for call in calls)


def test_azure_deploy_arm_blob_container_failure_is_exact_and_never_leaks(tmp_path):
    fixture, argv_log = _prepare_deploy_preflight_fixture(tmp_path)
    stdout_canary = "arm-container-stdout-canary"
    stderr_canary = "arm-container-stderr-canary"
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        f"""#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:3] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] == ["group", "show"]:
    sys.stdout.write("/subscriptions/demo/resourceGroups/demo-rg\\n")
elif args[:2] == ["identity", "show"]:
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args:
    sys.stdout.write(json.dumps({{"identity": {{"userAssignedIdentities": {{"/subscriptions/demo/identity": {{}}}}}}, "properties": {{"configuration": {{}}, "template": {{"containers": [{{"name": "deep-research-agent"}}]}}}}}}))
elif args[:2] == ["keyvault", "show"]:
    sys.stdout.write("/subscriptions/demo/vaults/demo-vault|false|1\\n")
elif args[:3] == ["keyvault", "secret", "list-versions"]:
    name = args[args.index("--name") + 1]
    json.dump([{{"id": f"https://demo-vault.vault.azure.net/secrets/{{name}}/v1", "name": name, "version": None, "enabled": True}}], sys.stdout)
elif args[:3] == ["storage", "account", "show"]:
    sys.stdout.write("/subscriptions/demo/storageAccounts/demostorage\\n")
elif args[:3] == ["storage", "container-rm", "show"]:
    sys.stdout.write("{stdout_canary}\\n")
    sys.stderr.write("{stderr_canary}\\n")
    raise SystemExit(47)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "DOCKER_HUB_USERNAME": "demo-user",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode == 47
    assert stdout_canary not in result.stdout + result.stderr
    assert stderr_canary not in result.stdout + result.stderr
    assert [
        "storage",
        "container-rm",
        "show",
        "--subscription",
        "00000000-1111-2222-3333-444444444444",
        "--resource-group",
        "demo-rg",
        "--storage-account",
        "demostorage",
        "--name",
        "deep-research-blobs",
        "--query",
        "name",
        "-o",
        "tsv",
    ] in _read_az_calls(argv_log)


def test_azure_deployment_guide_matches_update_only_cutover_workflow():
    guide = (PROJECT_ROOT / "documents/deployment/azure/README.md").read_text(
        encoding="utf-8"
    )

    for stale in (
        "`deploy.sh` creates or updates one externally accessible Container App",
        "`deploy.sh` creates Key Vault when needed",
        "### 3. Create or update Azure resources",
        "`deploy.sh` writes the resolved HTTPS endpoint back to `env.sh`",
        "It creates or reuses the resource group, Container Apps environment, Key Vault",
        "rights to update Container Apps, Key Vault access policies, and Storage",
        "rights for its existing storage-management steps",
    ):
        assert stale not in guide
    for required in (
        "update-only managed passkey cutover",
        "PASSKEY-PROXY-SECRET",
        "user-assigned managed identity",
        "secret `get` access",
        ".resolved-azure-endpoints.json",
        'curl --fail --silent --show-error "$BACKEND_URL/health"',
        "OAUTH_REDIRECTS_CONFIRMED=true ./deploy.sh",
        "read-only access to each prerequisite",
        "permission to update the existing backend Container App",
    ):
        assert required in guide
    resolver = "./scripts/resolve_azure_endpoints.sh"
    assert guide.index(resolver) < guide.index("./build.sh")
    assert guide.index("./build.sh") < guide.index(
        "OAUTH_REDIRECTS_CONFIRMED=true ./deploy.sh"
    )


def test_azure_guides_document_read_only_preflight_and_no_bootstrap_mutations():
    guides = {
        name: (PROJECT_ROOT / f"documents/deployment/azure/{name}.md").read_text(
            encoding="utf-8"
        )
        for name in ("README", "operations", "storage", "troubleshooting", "security")
    }
    maintained = "\n".join(guides.values())

    for stale in (
        "./deploy.sh --skip-kv-access",
        "az keyvault set-policy",
        "az containerapp env create` in `deploy.sh",
        "`deploy.sh` performs the supported setup",
        "rerun `./deploy.sh` to reconcile the account",
        "rerun `./deploy.sh` to restore the `authsqlite`",
        "creates a user-assigned managed identity",
        "grants that identity `get` and `list`",
        "stores the Docker Hub PAT in Key Vault",
    ):
        assert stale not in maintained

    for required in (
        "read-only preflight",
        "contact the Azure administrator",
        "does not grant roles or access policies",
        "does not create identities",
        "does not create storage resources",
        "existing Container Apps environment storage",
    ):
        assert required in maintained


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
    assert "trap finish_build EXIT" in source
    assert source.index("finish_build()") < source.index("trap finish_build EXIT")
    assert source.index(
        "cleanup_build_context", source.index("finish_build()")
    ) < source.index("trap - EXIT")


def test_azure_deploy_uses_sqlite_without_cosmos(tmp_path) -> None:
    source = "\n".join(
        (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for name in ("deploy.sh", "scripts/render_azure_containerapp_config.py")
    )
    rendered = _render_desired_config(tmp_path)
    environment = {
        item["name"]: item
        for item in rendered["properties"]["template"]["containers"][0]["env"]
    }

    assert environment["DB_TYPE"]["value"] == "sqlite"
    for forbidden in ("az cosmosdb", "COSMOSDB_", "cosmosdb-", "value: cosmosdb"):
        assert forbidden not in source


def test_passkey_sqlite_deployment_is_single_replica_on_persistent_azure_file(tmp_path):
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    rendered = _render_desired_config(tmp_path)
    template = rendered["properties"]["template"]
    container = template["containers"][0]
    environment = {item["name"]: item for item in container["env"]}

    assert "az storage share-rm show" in source
    assert "az containerapp env storage show" in source
    assert container["volumeMounts"] == [
        {"volumeName": "auth-sqlite", "mountPath": "/mnt/auth"}
    ]
    assert template["volumes"] == [
        {
            "name": "auth-sqlite",
            "storageType": "AzureFile",
            "storageName": "authsqlite",
        }
    ]
    assert environment["SQLITE_DB_PATH"]["value"] == "/mnt/auth/auth.db"
    assert environment["AUTH_SQLITE_JOURNAL_MODE"]["value"] == "DELETE"
    assert template["scale"]["maxReplicas"] == 1


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
        "PASSKEY_DERIVE_FROM_FRONTEND_URLS",
        "PASSKEY_RP_NAME",
        "PASSKEY_PROXY_ID",
        "PASSKEY_PROXY_SECRET",
        "OAUTH_SECRET_KEY",
    ):
        assert setting in env_example
        assert setting in auth_guide

    for legacy_setting in ("PASSKEY_RP_ID", "PASSKEY_RP_IDS", "PASSKEY_ORIGINS"):
        assert legacy_setting in auth_guide
        assert not re.search(rf"(?m)^{legacy_setting}=", env_example)

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


def test_backend_build_keeps_docker_credentials_outside_repository():
    source = (PROJECT_ROOT / "build.sh").read_text(encoding="utf-8")
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert 'mktemp -d "$SCRIPT_DIR/.docker-credentials.' not in source
    assert "/tmp/deep-research-docker-credentials." in source
    assert ".docker-credentials.*" in ignore


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        (
            {
                "permissions": [
                    {
                        "actions": ["*"],
                        "notActions": [],
                        "dataActions": [],
                        "notDataActions": [],
                    }
                ]
            },
            1,
        ),
        (
            {
                "permissions": [
                    {
                        "actions": [],
                        "notActions": [],
                        "dataActions": ["Microsoft.KeyVault/vaults/secrets/read"],
                        "notDataActions": [],
                    }
                ]
            },
            1,
        ),
        (
            {
                "permissions": [
                    {
                        "actions": [],
                        "notActions": [],
                        "dataActions": [
                            "Microsoft.KeyVault/vaults/*/read",
                            "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
                        ],
                        "notDataActions": [],
                    }
                ]
            },
            1,
        ),
        (
            {
                "permissions": [
                    {
                        "actions": [],
                        "notActions": [],
                        "dataActions": [
                            "Microsoft.KeyVault/vaults/secrets/getSecret/action",
                            "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
                        ],
                        "notDataActions": [],
                    }
                ]
            },
            0,
        ),
        (
            {
                "permissions": [
                    {
                        "actions": [],
                        "notActions": [],
                        "dataActions": ["*"],
                        "notDataActions": ["Microsoft.KeyVault/vaults/secrets/*"],
                    }
                ]
            },
            1,
        ),
    ],
)
def test_backend_keyvault_rbac_uses_effective_data_actions(
    tmp_path, definition, expected
):
    result = subprocess.run(
        [sys.executable, str(KEY_VAULT_RBAC)],
        input=json.dumps([definition]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected


def test_backend_config_renderer_targets_existing_container_name(tmp_path):
    output = tmp_path / "desired.yaml"
    command = [
        sys.executable,
        str(CONFIG_RENDERER),
        "--docker-username",
        "demo-user",
        "--build-version",
        "20260812010101",
        "--identity-id",
        "/subscriptions/demo/identity",
        "--container-name",
        "renamed-main",
        "--key-vault-name",
        "demo-vault",
        "--frontend-urls",
        f"{UI_URL},https://bmo-deepagent-ui.vercel.app",
        "--storage-name",
        "authsqlite",
        "--restart-trigger",
        "1234567890",
        "--revision-suffix",
        "passkeys-20260812010101",
        "--output",
        str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert [
        item["name"] for item in rendered["properties"]["template"]["containers"]
    ] == ["renamed-main"]


def test_backend_rbac_role_definition_lookup_uses_target_subscription():
    source = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert (
        'az role definition list --subscription "$AZURE_SUBSCRIPTION_ID" '
        '--name "$ROLE_DEFINITION_ID" -o json' in source
    )


def test_backend_config_merge_refuses_to_append_missing_container(tmp_path):
    existing = tmp_path / "existing.json"
    desired = tmp_path / "desired.yaml"
    output = tmp_path / "output.yaml"
    existing.write_text(
        json.dumps(
            {"properties": {"template": {"containers": [{"name": "renamed-main"}]}}}
        ),
        encoding="utf-8",
    )
    desired.write_text(
        "properties:\n  template:\n    containers:\n      - name: deep-research-agent\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(CONFIG_MERGER),
            "--existing-json",
            str(existing),
            "--desired-yaml",
            str(desired),
            "--output-yaml",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not output.exists()


def test_passkey_demo_documents_requested_multi_domain_rp_configuration():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    auth_guide = (PROJECT_ROOT / "documents/guides/authentication.md").read_text(
        encoding="utf-8"
    )
    assert re.search(r"(?m)^FRONTEND_URLS=http://localhost:3000$", env_example)
    assert re.search(r"(?m)^PASSKEY_DERIVE_FROM_FRONTEND_URLS=true$", env_example)
    assert not re.search(r"(?m)^PASSKEY_(?:RP_ID|RP_IDS|ORIGINS)=", env_example)

    for required in (
        "FRONTEND_URLS",
        "PASSKEY_DERIVE_FROM_FRONTEND_URLS=true",
        "PASSKEY_ENABLED=true",
        '"https://bmo-deepagent-ui.vercel.app", "bmo-deepagent-ui.vercel.app"',
        "Legacy explicit mode",
    ):
        assert required in auth_guide


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


def _prepare_build_rollback_fixture(tmp_path: Path) -> tuple[Path, Path]:
    fixture, argv_log = _install_azure_script_fixture(tmp_path, "build.sh")
    shutil.copy2(
        PROJECT_ROOT / "increment_version.py", fixture / "increment_version.py"
    )
    shutil.copy2(BUILD_CONFIG_PUBLISHER, fixture / "scripts/publish_build_config.py")
    (fixture / "webapp").mkdir()
    (fixture / "webapp/config.py").write_text(
        'API_VERSION = "1.8.126"\n', encoding="utf-8"
    )
    (fixture / ".env.docker").write_text("OTHER=value\n", encoding="utf-8")
    (fixture / ".env").write_text(
        "DOCKER_HUB_USERNAME=demo-user\nDOCKER_HUB_PAT=private-pat\n",
        encoding="utf-8",
    )
    (fixture / ".gitignore").write_text(
        ".container-build-context.*\n", encoding="utf-8"
    )
    runtime = fixture / "scripts/container_runtime.sh"
    runtime.write_text(
        """select_container_runtime() {
  printf '%s\n' "${CONTAINER_RUNTIME:-automatic}" > "$FAKE_SELECTED_RUNTIME_FILE"
  CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-fake}"
}
ensure_container_runtime_ready() { :; }
container_runtime_login() {
  cat >/dev/null
  [ "${FAKE_BUILD_FAILURE:-}" != login ] || return 41
}
container_runtime_build() {
  if [ "${FAKE_BUILD_FAILURE:-}" = concurrent ]; then
    printf '# concurrent edit\\n' >> webapp/config.py
    return 42
  fi
  [ "${FAKE_BUILD_FAILURE:-}" != build ] || return 42
}
container_runtime_push() {
  count_file="${FAKE_PUSH_COUNT_FILE:?}"
  count=0
  [ ! -f "$count_file" ] || count=$(cat "$count_file")
  count=$((count + 1))
  printf '%s\\n' "$count" >"$count_file"
  if [ "${FAKE_BUILD_FAILURE:-}" = latest_push ] && [ "$count" -eq 1 ]; then
    return 43
  fi
  if [ "${FAKE_BUILD_FAILURE:-}" = versioned_push ] && [ "$count" -eq 2 ]; then
    return 44
  fi
  if [ "${FAKE_BUILD_FAILURE:-}" = success_concurrent_config ] && [ "$count" -eq 2 ]; then
    printf '# concurrent success edit\\n' >> webapp/config.py
  fi
  if [ "${FAKE_BUILD_FAILURE:-}" = success_concurrent_marker ] && [ "$count" -eq 2 ]; then
    printf 'concurrent marker\\n' > .build_version
  fi
}
container_runtime_tag() { :; }
""",
        encoding="utf-8",
    )
    fake_az = fixture / "bin/az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:3] == ["containerapp", "env", "show"]:
    sys.stdout.write(os.environ["FAKE_ENV_ROW"])
elif args[:2] in (["account", "set"], ["group", "show"]):
    pass
else:
    raise SystemExit(91)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    fake_date = fixture / "bin/date"
    fake_date.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y%m%d%H%M%S" ]; then printf "20260812010101\\n"; '
        'else printf "1786500061\\n"; fi\n',
        encoding="utf-8",
    )
    fake_date.chmod(0o700)
    subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=fixture,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=fixture, check=True)
    subprocess.run(["git", "add", "."], cwd=fixture, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=fixture, check=True)
    return fixture, argv_log


def _run_build_fixture(
    fixture: Path,
    argv_log: Path,
    *,
    failure: str = "",
    extra_path: str | None = None,
    args: tuple[str, ...] = (),
    environment_update: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    path = f"{fixture / 'bin'}:{env['PATH']}"
    if extra_path:
        path = f"{extra_path}:{path}"
    env.update(
        {
            "PATH": path,
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
            "FAKE_BUILD_FAILURE": failure,
            "FAKE_PUSH_COUNT_FILE": str(fixture / "push-count"),
            "FAKE_SELECTED_RUNTIME_FILE": str(fixture / "selected-runtime"),
        }
    )
    env.pop("CONTAINER_CLI", None)
    env.pop("CONTAINER_RUNTIME", None)
    if environment_update:
        env.update(environment_update)
    return subprocess.run(
        ["bash", "build.sh", *args],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _install_build_fixture_runtime(fixture: Path, name: str) -> None:
    runtime = fixture / "bin" / name
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o700)


@pytest.mark.parametrize(
    "args",
    [
        ("--container-cli", "podman"),
        ("--container-cli=podman",),
        ("-c", "podman"),
    ],
    ids=("long-separate", "long-equals", "short"),
)
def test_backend_build_container_cli_argument_forms_select_runtime(tmp_path, args):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    _install_build_fixture_runtime(fixture, "podman")

    result = _run_build_fixture(fixture, argv_log, args=args)

    assert result.returncode == 0, result.stderr
    assert (fixture / "selected-runtime").read_text(encoding="utf-8") == "podman\n"


@pytest.mark.parametrize(
    ("environment_update", "expected"),
    [
        ({"CONTAINER_CLI": "container"}, "container"),
        ({"CONTAINER_RUNTIME": "podman"}, "podman"),
        (
            {"CONTAINER_CLI": "docker", "CONTAINER_RUNTIME": "docker"},
            "docker",
        ),
    ],
    ids=("new-alias", "legacy-alias", "matching-aliases"),
)
def test_backend_build_runtime_alias_precedence_without_argument(
    tmp_path, environment_update, expected
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    _install_build_fixture_runtime(fixture, expected)

    result = _run_build_fixture(
        fixture, argv_log, environment_update=environment_update
    )

    assert result.returncode == 0, result.stderr
    assert (fixture / "selected-runtime").read_text(encoding="utf-8") == f"{expected}\n"


def test_backend_build_container_cli_argument_overrides_conflicting_runtime_aliases(
    tmp_path,
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    _install_build_fixture_runtime(fixture, "podman")

    result = _run_build_fixture(
        fixture,
        argv_log,
        args=("--container-cli", "podman"),
        environment_update={
            "CONTAINER_CLI": "container",
            "CONTAINER_RUNTIME": "docker",
        },
    )

    assert result.returncode == 0, result.stderr
    assert (fixture / "selected-runtime").read_text(encoding="utf-8") == "podman\n"


def test_backend_build_runtime_alias_from_sourced_config_is_ignored(tmp_path):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    with (fixture / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write(
            "export CONTAINER_CLI=container\nexport CONTAINER_RUNTIME=docker\n"
        )

    result = _run_build_fixture(fixture, argv_log)

    assert result.returncode == 0, result.stderr
    assert (fixture / "selected-runtime").read_text(encoding="utf-8") == "automatic\n"


def test_backend_build_runtime_alias_parser_state_cannot_be_poisoned_by_env_sh(
    tmp_path,
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    _install_build_fixture_runtime(fixture, "podman")
    with (fixture / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write(
            "CLI_CONTAINER_CLI=docker\n"
            "CLI_CONTAINER_CLI_SEEN=false\n"
            "CALLER_CONTAINER_CLI=container\n"
            "CALLER_CONTAINER_CLI_WAS_SET=true\n"
            "CALLER_CONTAINER_RUNTIME=docker\n"
            "CALLER_CONTAINER_RUNTIME_WAS_SET=true\n"
            "RESOLVED_CONTAINER_RUNTIME=docker\n"
        )
    config_before = (fixture / "webapp/config.py").read_bytes()

    result = _run_build_fixture(
        fixture, argv_log, args=("--container-cli", "podman")
    )

    assert result.returncode != 0
    assert "readonly" in result.stderr.lower()
    assert (fixture / "webapp/config.py").read_bytes() == config_before
    assert not (fixture / ".build_version").exists()
    assert not (fixture / "selected-runtime").exists()
    assert not (fixture / "push-count").exists()
    assert not argv_log.exists()


def _assert_build_argument_failure_has_no_side_effects(
    fixture: Path,
    argv_log: Path,
    config_before: bytes,
) -> None:
    assert (fixture / "webapp/config.py").read_bytes() == config_before
    assert not (fixture / ".build_version").exists()
    assert not (fixture / "env-source-marker").exists()
    assert not (fixture / "selected-runtime").exists()
    assert not (fixture / "push-count").exists()
    assert not argv_log.exists()
    assert not list(fixture.glob(".container-build-context.*"))


@pytest.mark.parametrize(
    "args",
    [
        ("--container-cli",),
        ("-c",),
        ("--container-cli", ""),
        ("--container-cli=",),
        ("--container-cli", "nerdctl"),
        ("--container-cli", "docker", "-c", "docker"),
        ("--container-cli", "docker", "--container-cli=podman"),
        ("-cpodman",),
        ("--unknown",),
        ("--help", "--container-cli", "docker"),
        ("--container-cli", "docker", "--help"),
    ],
    ids=(
        "missing-long",
        "missing-short",
        "empty-separate",
        "empty-equals",
        "unsupported",
        "duplicate-same",
        "duplicate-conflict",
        "combined-short",
        "unknown",
        "help-mixed-first",
        "help-mixed-last",
    ),
)
def test_backend_build_container_cli_argument_rejects_invalid_before_side_effects(
    tmp_path, args
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    with (fixture / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write('touch "$SCRIPT_DIR/env-source-marker"\n')
    config_before = (fixture / "webapp/config.py").read_bytes()

    result = _run_build_fixture(fixture, argv_log, args=args)

    assert result.returncode == 64, result.stderr
    _assert_build_argument_failure_has_no_side_effects(
        fixture, argv_log, config_before
    )


def test_backend_build_runtime_alias_conflict_rejected_before_side_effects(tmp_path):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    with (fixture / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write('touch "$SCRIPT_DIR/env-source-marker"\n')
    config_before = (fixture / "webapp/config.py").read_bytes()

    result = _run_build_fixture(
        fixture,
        argv_log,
        environment_update={
            "CONTAINER_CLI": "podman",
            "CONTAINER_RUNTIME": "docker",
        },
    )

    assert result.returncode == 64, result.stderr
    assert "CONTAINER_CLI and CONTAINER_RUNTIME disagree" in result.stderr
    _assert_build_argument_failure_has_no_side_effects(
        fixture, argv_log, config_before
    )


@pytest.mark.parametrize(
    "environment_update",
    [
        {"CONTAINER_CLI": ""},
        {"CONTAINER_RUNTIME": "nerdctl"},
    ],
    ids=("empty-new-alias", "unsupported-legacy-alias"),
)
def test_backend_build_runtime_alias_invalid_rejected_before_side_effects(
    tmp_path, environment_update
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    with (fixture / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write('touch "$SCRIPT_DIR/env-source-marker"\n')
    config_before = (fixture / "webapp/config.py").read_bytes()

    result = _run_build_fixture(
        fixture, argv_log, environment_update=environment_update
    )

    assert result.returncode == 64, result.stderr
    _assert_build_argument_failure_has_no_side_effects(
        fixture, argv_log, config_before
    )


def test_backend_build_container_runtime_argument_unavailable_before_side_effects(
    tmp_path,
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    with (fixture / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write('touch "$SCRIPT_DIR/env-source-marker"\n')
    config_before = (fixture / "webapp/config.py").read_bytes()
    restricted_path = f"{fixture / 'bin'}:/usr/bin:/bin"

    result = _run_build_fixture(
        fixture,
        argv_log,
        args=("--container-cli", "podman"),
        environment_update={"PATH": restricted_path},
    )

    assert result.returncode == 64, result.stderr
    assert "podman" in result.stderr
    assert "PATH" in result.stderr
    _assert_build_argument_failure_has_no_side_effects(
        fixture, argv_log, config_before
    )


@pytest.mark.parametrize("help_arg", ["--help", "-h"])
def test_backend_build_help_is_side_effect_free(tmp_path, help_arg):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    with (fixture / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write('touch "$SCRIPT_DIR/env-source-marker"\n')
    config_before = (fixture / "webapp/config.py").read_bytes()

    result = _run_build_fixture(
        fixture,
        argv_log,
        args=(help_arg,),
        environment_update={
            "CONTAINER_CLI": "podman",
            "CONTAINER_RUNTIME": "docker",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Usage: ./build.sh" in result.stdout
    assert "container, podman, or docker" in result.stdout
    _assert_build_argument_failure_has_no_side_effects(
        fixture, argv_log, config_before
    )


@pytest.mark.parametrize(
    ("failure", "status"),
    [("build", 42), ("latest_push", 43), ("versioned_push", 44)],
)
@pytest.mark.parametrize("marker_exists", [False, True])
def test_backend_build_rollback_restores_owned_files_exactly(
    tmp_path, failure, status, marker_exists
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    config = fixture / "webapp/config.py"
    marker = fixture / ".build_version"
    config.chmod(0o640)
    if marker_exists:
        marker.write_bytes(b"old-build-marker\r\n")
        marker.chmod(0o604)
    config_before = config.read_bytes()
    config_mode = stat.S_IMODE(config.stat().st_mode)
    marker_before = marker.read_bytes() if marker_exists else None
    marker_mode = stat.S_IMODE(marker.stat().st_mode) if marker_exists else None
    unrelated = fixture / "unrelated.txt"
    unrelated.write_text("operator edit\n", encoding="utf-8")

    result = _run_build_fixture(fixture, argv_log, failure=failure)

    assert result.returncode == status, result.stderr
    assert config.read_bytes() == config_before
    assert stat.S_IMODE(config.stat().st_mode) == config_mode
    assert marker.exists() is marker_exists
    if marker_exists:
        assert marker.read_bytes() == marker_before
        assert stat.S_IMODE(marker.stat().st_mode) == marker_mode
    assert unrelated.read_text(encoding="utf-8") == "operator edit\n"


def test_backend_build_rollback_covers_cleanup_failure(tmp_path):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    config = fixture / "webapp/config.py"
    before = config.read_bytes()
    fake_bin = tmp_path / "cleanup-bin"
    fake_bin.mkdir()
    fake_rm = fake_bin / "rm"
    fake_rm.write_text(
        "#!/bin/sh\n"
        'case "$*" in *container-build-context*) exit 45;; esac\n'
        'exec /bin/rm "$@"\n',
        encoding="utf-8",
    )
    fake_rm.chmod(0o700)

    result = _run_build_fixture(
        fixture, argv_log, failure="cleanup", extra_path=str(fake_bin)
    )

    assert result.returncode == 45
    assert config.read_bytes() == before
    assert not (fixture / ".build_version").exists()


@pytest.mark.parametrize(("exit_kind", "status"), [("exit", 46), ("signal", 143)])
def test_backend_build_rollback_restores_partial_increment_failure_status(
    tmp_path, exit_kind, status
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    config = fixture / "webapp/config.py"
    marker = fixture / ".build_version"
    marker.write_bytes(b"old-marker\r\n")
    marker.chmod(0o604)
    config.chmod(0o640)
    before_config = config.read_bytes()
    before_marker = marker.read_bytes()
    fake_increment = fixture / "increment_version.py"
    if exit_kind == "exit":
        fake_increment.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text('API_VERSION = \\\"1.8.')\n"
            "raise SystemExit(46)\n",
            encoding="utf-8",
        )
    else:
        fake_increment.write_text(
            "import os, signal, sys\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text('API_VERSION = \\\"1.8.127\\\"\\n')\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n",
            encoding="utf-8",
        )

    result = _run_build_fixture(fixture, argv_log)

    assert result.returncode == status
    assert config.read_bytes() == before_config
    assert marker.read_bytes() == before_marker
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert stat.S_IMODE(marker.stat().st_mode) == 0o604


@pytest.mark.parametrize(
    ("failure", "status"),
    [("write", 46), ("fsync", 47), ("rename", 48), ("signal_after_rename", 143)],
)
def test_backend_build_atomic_publication_failure_restores_exact_state(
    tmp_path, failure, status
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    config = fixture / "webapp/config.py"
    marker = fixture / ".build_version"
    config.chmod(0o640)
    marker.write_bytes(b"old-marker\r\n")
    marker.chmod(0o604)
    before_config = config.read_bytes()
    before_marker = marker.read_bytes()
    fake_publisher = fixture / "scripts/publish_build_config.py"
    fake_publisher.write_text(
        "import os, shutil, signal, sys, tempfile\n"
        "from pathlib import Path\n"
        "failure = os.environ['FAKE_PUBLICATION_FAILURE']\n"
        "if failure == 'signal_after_rename':\n"
        "    descriptor, temporary = tempfile.mkstemp(dir=Path(sys.argv[2]).parent)\n"
        "    os.close(descriptor)\n"
        "    shutil.copy2(sys.argv[1], temporary)\n"
        "    os.replace(temporary, sys.argv[2])\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "Path(sys.argv[2]).parent.joinpath('.publication-partial').write_text(failure)\n"
        "raise SystemExit({'write': 46, 'fsync': 47, 'rename': 48}[failure])\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["FAKE_PUBLICATION_FAILURE"] = failure

    result = subprocess.run(
        ["bash", "build.sh"],
        cwd=fixture,
        env=env
        | {
            "PATH": f"{fixture / 'bin'}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_ENV_ROW": f"{ENVIRONMENT_ID}\t{DEFAULT_DOMAIN}\tSucceeded\n",
                "FAKE_BUILD_FAILURE": "",
                "FAKE_PUSH_COUNT_FILE": str(fixture / "push-count"),
                "FAKE_SELECTED_RUNTIME_FILE": str(fixture / "selected-runtime"),
            },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == status
    assert config.read_bytes() == before_config
    assert marker.read_bytes() == before_marker
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert stat.S_IMODE(marker.stat().st_mode) == 0o604


def test_build_config_publisher_replaces_atomically_with_exact_mode(tmp_path):
    source = tmp_path / "expected.py"
    target = tmp_path / "config.py"
    source.write_bytes(b'API_VERSION = "1.8.127"\n')
    source.chmod(0o640)
    target.write_bytes(b'API_VERSION = "1.8.126"\n')
    target.chmod(0o600)

    result = subprocess.run(
        [sys.executable, str(BUILD_CONFIG_PUBLISHER), str(source), str(target)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not list(tmp_path.glob(".config.py.publish.*"))


def test_backend_build_version_refuses_dirty_owned_config_before_mutation(tmp_path):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)
    config = fixture / "webapp/config.py"
    config.write_text('API_VERSION = "7.7.7"\n# operator edit\n', encoding="utf-8")
    before = config.read_bytes()

    result = _run_build_fixture(fixture, argv_log)

    assert result.returncode != 0
    assert "webapp/config.py" in result.stderr
    assert "dirty" in result.stderr.lower()
    assert config.read_bytes() == before
    assert not (fixture / ".build_version").exists()
    assert not (fixture / "push-count").exists()


def test_backend_build_version_concurrent_edit_fails_closed_without_overwrite(tmp_path):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)

    result = _run_build_fixture(fixture, argv_log, failure="concurrent")

    assert result.returncode == 70
    assert "concurrent" in result.stderr.lower()
    assert "# concurrent edit\n" in (fixture / "webapp/config.py").read_text()
    assert not (fixture / ".build_version").exists()


@pytest.mark.parametrize(
    ("failure", "owned_path", "expected"),
    [
        (
            "success_concurrent_config",
            "webapp/config.py",
            "# concurrent success edit\n",
        ),
        ("success_concurrent_marker", ".build_version", "concurrent marker\n"),
    ],
)
def test_backend_build_version_detects_concurrent_edit_before_success(
    tmp_path, failure, owned_path, expected
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)

    result = _run_build_fixture(fixture, argv_log, failure=failure)

    assert result.returncode == 70
    assert "concurrent" in result.stderr.lower()
    assert (fixture / owned_path).read_text(encoding="utf-8").endswith(expected)


def test_backend_build_version_retry_advances_once_and_publishes_marker_after_push(
    tmp_path,
):
    fixture, argv_log = _prepare_build_rollback_fixture(tmp_path)

    failed = _run_build_fixture(fixture, argv_log, failure="latest_push")
    assert failed.returncode == 43
    assert (fixture / "webapp/config.py").read_text() == 'API_VERSION = "1.8.126"\n'
    assert not (fixture / ".build_version").exists()
    (fixture / "push-count").unlink()

    succeeded = _run_build_fixture(fixture, argv_log)

    assert succeeded.returncode == 0, succeeded.stderr
    assert (fixture / "webapp/config.py").read_text() == 'API_VERSION = "1.8.127"\n'
    assert (fixture / ".build_version").read_text() == "20260812010101\n"
    assert stat.S_IMODE((fixture / ".build_version").stat().st_mode) == 0o600
    assert (
        subprocess.run(
            ["git", "diff", "--quiet", "--", "webapp/config.py"], cwd=fixture
        ).returncode
        == 1
    )


def _snapshot_fake_app(name: str, principal: str) -> dict:
    return {
        "id": f"/subscriptions/demo/resourceGroups/demo-rg/providers/Microsoft.App/containerApps/{name}",
        "location": "canadacentral",
        "identity": {
            "type": "SystemAssigned",
            "principalId": principal,
            "tenantId": "tenant-1",
            "userAssignedIdentities": {},
        },
        "environmentId": "/subscriptions/demo/resourceGroups/demo-rg/providers/Microsoft.App/managedEnvironments/demo-env",
        "secretMetadata": [
            {
                "name": "passkey-proxy-secret",
                "keyVaultUrl": "https://demo-vault.vault.azure.net/secrets/PASSKEY-PROXY-SECRET",
                "identity": "system",
            }
        ],
        "registries": [
            {
                "server": "docker.io",
                "username": "demo-user",
                "passwordSecretRef": "docker-hub-pat",
            }
        ],
        "revisionSuffix": "passkeys-before",
        "latestRevisionName": f"{name}--passkeys-before",
        "containers": [
            {
                "name": name,
                "image": f"demo-user/{name}:before",
                "volumeMounts": [],
            }
        ],
        "volumes": [],
    }


def _install_snapshot_fake_az(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "snapshot-az.jsonl"
    fake_az = bin_dir / "az"
    fake_az.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_ARGV_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:2] == ["containerapp", "show"]:
    name = args[args.index("--name") + 1]
    payload = json.loads(os.environ[f"FAKE_APP_{name.replace('-', '_').upper()}"])
elif args[:2] == ["keyvault", "show"]:
    payload = {
        "id": "/subscriptions/demo/resourceGroups/demo-rg/providers/Microsoft.KeyVault/vaults/demo-vault",
        "location": "canadacentral",
        "enableRbacAuthorization": True,
        "accessPolicies": [],
    }
elif args[:3] == ["keyvault", "secret", "list-versions"]:
    name = args[args.index("--name") + 1]
    payload = json.loads(os.environ["FAKE_SECRET_VERSIONS"]) if os.environ.get("FAKE_SECRET_VERSIONS") else [{
        "id": f"https://demo-vault.vault.azure.net/secrets/{name}/version-1",
        "name": name,
        "version": None,
        "enabled": True,
        "created": "2026-01-01",
        "updated": "2026-01-02",
    }]
    for item in payload:
        if isinstance(item, dict):
            if isinstance(item.get("id"), str):
                item["id"] = item["id"].replace("{name}", name)
            if item.get("name") == "{name}":
                item["name"] = name
elif args[:3] == ["role", "assignment", "list"]:
    principal = args[args.index("--assignee-object-id") + 1]
    payload = [{"id": f"/roles/{principal}", "principalId": principal, "roleDefinitionId": "/definitions/reader", "scope": "/subscriptions/demo", "condition": None, "conditionVersion": None}]
elif args[:3] == ["containerapp", "env", "show"]:
    payload = {"id": "/subscriptions/demo/resourceGroups/demo-rg/providers/Microsoft.App/managedEnvironments/demo-env", "location": "canadacentral"}
elif args[:4] == ["containerapp", "env", "storage", "list"]:
    payload = [{"name": "authsqlite", "azureFile": {"accountName": "demostorage", "shareName": "deep-research-auth", "accessMode": "ReadWrite"}}]
else:
    raise SystemExit(91)
json.dump(payload, sys.stdout)
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o700)
    return bin_dir, argv_log


def _run_snapshot_capture(
    tmp_path: Path,
    output: Path,
    *,
    backend_app: dict | None = None,
    secret_versions: list[dict] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, argv_log = _install_snapshot_fake_az(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
            "FAKE_APP_DEMO_API": json.dumps(
                backend_app
                if backend_app is not None
                else _snapshot_fake_app("demo-api", "backend-principal")
            ),
            "FAKE_APP_DEMO_UI": json.dumps(
                _snapshot_fake_app("demo-ui", "ui-principal")
            ),
            "FAKE_SECRET_VERSIONS": (
                json.dumps(secret_versions) if secret_versions is not None else ""
            ),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            str(AZURE_METADATA_SNAPSHOT),
            "capture",
            "--subscription",
            "demo-subscription",
            "--resource-group",
            "demo-rg",
            "--vault-name",
            "demo-vault",
            "--backend-app",
            "demo-api",
            "--ui-app",
            "demo-ui",
            "--output",
            str(output),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, argv_log


def test_azure_metadata_snapshot_capture_is_canonical_metadata_only_and_mode_0600(
    tmp_path,
):
    output = tmp_path / "before.json"

    result, argv_log = _run_snapshot_capture(tmp_path, output)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    raw = output.read_text(encoding="utf-8")
    assert raw == json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"
    calls = _read_az_calls(argv_log)
    assert (
        len(
            [
                call
                for call in calls
                if call[:3] == ["keyvault", "secret", "list-versions"]
            ]
        )
        == 9
    )
    assert not any(call[:3] == ["keyvault", "secret", "show"] for call in calls)
    assert all("--query" in call for call in calls)
    query_text = "\n".join(
        call[call.index("--query") + 1] for call in calls if "--query" in call
    )
    assert "value" not in query_text.casefold()
    assert "--show-values" not in json.dumps(calls)


def test_azure_metadata_snapshot_captures_all_versions_without_current_inference(
    tmp_path,
):
    versions = [
        {
            "id": "https://demo-vault.vault.azure.net/secrets/{name}/v2",
            "name": "{name}",
            "version": None,
            "enabled": False,
            "created": "2026-01-02",
            "updated": "2025-01-01",
        },
        {
            "id": "https://demo-vault.vault.azure.net/secrets/{name}/v1",
            "name": "{name}",
            "version": None,
            "enabled": True,
            "created": "2026-01-01",
            "updated": "2027-01-01",
        },
    ]
    output = tmp_path / "before.json"

    result, argv_log = _run_snapshot_capture(tmp_path, output, secret_versions=versions)

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert all(
        [version["version"] for version in secret["versions"]] == ["v1", "v2"]
        for secret in payload["key_vault"]["secrets"]
    )
    assert all(
        secret["versions"][1]["enabled"] is False
        for secret in payload["key_vault"]["secrets"]
    )
    assert not any(
        call[:3] == ["keyvault", "secret", "show"] for call in _read_az_calls(argv_log)
    )


@pytest.mark.parametrize(
    ("versions", "message"),
    [
        ([], "version"),
        (
            [
                {
                    "id": "https://wrong.vault.azure.net/secrets/{name}/v1",
                    "name": "{name}",
                    "version": None,
                    "enabled": True,
                    "created": "2026-01-01",
                    "updated": "2026-01-02",
                }
            ],
            "vault",
        ),
        (
            [
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/{name}",
                    "name": "{name}",
                    "version": None,
                    "enabled": True,
                    "created": "2026-01-01",
                    "updated": "2026-01-02",
                }
            ],
            "versioned",
        ),
        (
            [
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/{name}/v1",
                    "name": "{name}",
                    "version": None,
                    "enabled": True,
                    "created": "2026-01-01",
                    "updated": "2026-01-02",
                },
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/{name}/v1",
                    "name": "{name}",
                    "version": None,
                    "enabled": False,
                    "created": "2026-01-01",
                    "updated": "2026-01-03",
                },
            ],
            "duplicate",
        ),
        (
            [
                {
                    "id": "https://demo-vault.vault.azure.net/secrets/{name}/v1",
                    "name": "{name}",
                    "version": None,
                    "enabled": True,
                    "updated": "2026-01-02",
                    "value": "secret-canary",
                }
            ],
            "schema",
        ),
    ],
)
def test_azure_metadata_snapshot_secret_versions_fail_closed(
    tmp_path, versions, message
):
    output = tmp_path / "before.json"

    result, argv_log = _run_snapshot_capture(tmp_path, output, secret_versions=versions)

    assert result.returncode == 2
    assert message in result.stderr.lower()
    assert "secret-canary" not in result.stdout + result.stderr
    assert not output.exists()
    assert not any(
        call[:3] == ["keyvault", "secret", "show"] for call in _read_az_calls(argv_log)
    )


def test_azure_metadata_snapshot_capture_rejects_malformed_query_output(tmp_path):
    output = tmp_path / "before.json"
    bin_dir, argv_log = _install_snapshot_fake_az(tmp_path)
    fake_az = bin_dir / "az"
    fake_az.write_text("#!/bin/sh\nprintf '{bad json'\n", encoding="utf-8")
    fake_az.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_AZ_ARGV_LOG": str(argv_log),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            str(AZURE_METADATA_SNAPSHOT),
            "capture",
            "--subscription",
            "demo-subscription",
            "--resource-group",
            "demo-rg",
            "--vault-name",
            "demo-vault",
            "--backend-app",
            "demo-api",
            "--ui-app",
            "demo-ui",
            "--output",
            str(output),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert not output.exists()
    assert "{bad json" not in result.stdout + result.stderr


def test_azure_metadata_snapshot_capture_rejects_unapproved_nested_identity_key(
    tmp_path,
):
    output = tmp_path / "before.json"
    backend = _snapshot_fake_app("demo-api", "backend-principal")
    backend["identity"]["value"] = "secret-canary"

    result, _ = _run_snapshot_capture(tmp_path, output, backend_app=backend)

    assert result.returncode == 2
    assert not output.exists()
    assert "secret-canary" not in result.stdout + result.stderr


def test_azure_metadata_snapshot_compare_ignores_only_revision_and_image(tmp_path):
    before = tmp_path / "before.json"
    capture, _ = _run_snapshot_capture(tmp_path, before)
    assert capture.returncode == 0, capture.stderr
    after = tmp_path / "after.json"
    payload = json.loads(before.read_text(encoding="utf-8"))
    payload["apps"]["backend"]["deployment"]["revision_suffix"] = "passkeys-after"
    payload["apps"]["backend"]["deployment"]["latest_revision_name"] = (
        "demo-api--passkeys-after"
    )
    payload["apps"]["backend"]["deployment"]["images"] = ["demo-user/demo-api:after"]
    after.write_text(json.dumps(payload), encoding="utf-8")

    allowed = subprocess.run(
        [
            sys.executable,
            str(AZURE_METADATA_SNAPSHOT),
            "compare",
            "--before",
            str(before),
            "--after",
            str(after),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr

    payload = json.loads(before.read_text(encoding="utf-8"))
    payload["key_vault"]["secrets"][0]["versions"].append(
        {
            "id": "https://demo-vault.vault.azure.net/secrets/AZURE-STORAGE-CONTAINER-NAME/version-2",
            "version": "version-2",
            "enabled": True,
            "created": "2026-02-01",
            "updated": "2026-02-01",
        }
    )
    after.write_text(json.dumps(payload), encoding="utf-8")
    version_drift = subprocess.run(
        [
            sys.executable,
            str(AZURE_METADATA_SNAPSHOT),
            "compare",
            "--before",
            str(before),
            "--after",
            str(after),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert version_drift.returncode == 2
    assert "key_vault.secrets" in version_drift.stderr

    payload = json.loads(before.read_text(encoding="utf-8"))
    payload["apps"]["backend"]["secret_references"][0]["identity"] = "drifted"
    after.write_text(json.dumps(payload), encoding="utf-8")
    protected = subprocess.run(
        [
            sys.executable,
            str(AZURE_METADATA_SNAPSHOT),
            "compare",
            "--before",
            str(before),
            "--after",
            str(after),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert protected.returncode != 0
    assert "secret_references" in protected.stderr
    assert "value" not in protected.stdout.casefold()


def test_azure_metadata_snapshot_compare_rejects_malformed_protected_schema(tmp_path):
    before = tmp_path / "before.json"
    capture, _ = _run_snapshot_capture(tmp_path, before)
    assert capture.returncode == 0, capture.stderr
    after = tmp_path / "after.json"
    payload = json.loads(before.read_text(encoding="utf-8"))
    payload["apps"]["ui"].pop("secret_references")
    after.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(AZURE_METADATA_SNAPSHOT),
            "compare",
            "--before",
            str(before),
            "--after",
            str(after),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "schema" in result.stderr.lower()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["apps"]["backend"].__setitem__(
            "identity", {"value": "secret-canary"}
        ),
        lambda payload: payload["azure_environment"]["storage"][0].__setitem__(
            "accountKey", "secret-canary"
        ),
        lambda payload: payload["key_vault"]["access_policies"].append(
            {"value": "secret-canary"}
        ),
        lambda payload: payload["key_vault"]["secrets"][0].__setitem__(
            "value", "secret-canary"
        ),
        lambda payload: payload["role_assignments"].append(
            payload["role_assignments"][0].copy()
        ),
    ],
    ids=(
        "identity-value",
        "storage-extra",
        "policy-malformed",
        "secret-value",
        "duplicate-role",
    ),
)
def test_azure_metadata_snapshot_compare_rejects_identically_malformed_nested_schema(
    tmp_path, mutation
):
    captured = tmp_path / "captured.json"
    result, _ = _run_snapshot_capture(tmp_path, captured)
    assert result.returncode == 0, result.stderr
    payload = json.loads(captured.read_text(encoding="utf-8"))
    mutation(payload)
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(json.dumps(payload), encoding="utf-8")
    after.write_text(json.dumps(payload), encoding="utf-8")

    compared = subprocess.run(
        [
            sys.executable,
            str(AZURE_METADATA_SNAPSHOT),
            "compare",
            "--before",
            str(before),
            "--after",
            str(after),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert compared.returncode == 2
    assert "schema" in compared.stderr.lower() or "duplicate" in compared.stderr.lower()
    assert "secret-canary" not in compared.stdout + compared.stderr
