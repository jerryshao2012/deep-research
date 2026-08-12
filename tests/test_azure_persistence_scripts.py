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
    sys.stdout.write('{"identity":{"userAssignedIdentities":{"/subscriptions/demo/identity":{}}},"properties":{"configuration":{},"template":{}}}')
elif args[:2] == ["keyvault", "show"]:
    if missing == "vault":
        raise SystemExit(3)
    sys.stdout.write("principal-123\\n")
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


@pytest.mark.parametrize("secret_id", ["", "   \n"])
def test_azure_deploy_rejects_empty_passkey_secret_id_before_mutation(
    tmp_path, secret_id
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
    sys.stdout.write('{"identity":{"userAssignedIdentities":{"/subscriptions/demo/identity":{}}},"properties":{"configuration":{},"template":{}}}')
elif args[:2] == ["keyvault", "show"]:
    sys.stdout.write("principal-123\\n")
elif args[:3] == ["keyvault", "secret", "show"]:
    name = args[args.index("--name") + 1]
    if name == "PASSKEY-PROXY-SECRET":
        sys.stdout.write(os.environ["FAKE_SECRET_ID"])
    else:
        sys.stdout.write(f"https://demo-vault.vault.azure.net/secrets/{name}\\n")
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
            "FAKE_SECRET_ID": secret_id,
            "DOCKER_HUB_USERNAME": "demo-user",
            "OAUTH_REDIRECTS_CONFIRMED": "true",
        }
    )

    result = subprocess.run(
        ["bash", "deploy.sh"], cwd=fixture, env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "PASSKEY-PROXY-SECRET id" in result.stderr
    calls = _read_az_calls(argv_log)
    assert not any(call[:2] == ["account", "set"] for call in calls)
    assert not any(call[:2] == ["keyvault", "set-policy"] for call in calls)
    assert not any(call[:2] == ["containerapp", "update"] for call in calls)


def test_azure_deploy_preserves_passkey_secret_query_failure_bytes_and_status(tmp_path):
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
    sys.stdout.write('{"identity":{"userAssignedIdentities":{"/subscriptions/demo/identity":{}}},"properties":{"configuration":{},"template":{}}}')
elif args[:2] == ["keyvault", "show"]:
    sys.stdout.write("principal-123\\n")
elif args[:3] == ["keyvault", "secret", "show"]:
    name = args[args.index("--name") + 1]
    if name == "PASSKEY-PROXY-SECRET":
        sys.stdout.write("secret query stdout bytes\\n")
        sys.stderr.write("secret query stderr bytes\\n")
        raise SystemExit(47)
    sys.stdout.write(f"https://demo-vault.vault.azure.net/secrets/{name}\\n")
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
    assert result.stdout == "secret query stdout bytes\n"
    assert result.stderr.endswith("secret query stderr bytes\n")
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
    sys.stdout.write('{"identity":{"userAssignedIdentities":{"/subscriptions/demo/identity":{}}},"properties":{"configuration":{},"template":{}}}')
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


def test_azure_deploy_merges_passkey_config_and_records_only_after_health(tmp_path):
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
    update_yaml = fixture / "captured-update.yaml"
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
    sys.stdout.write("/subscriptions/demo/identity\\tprincipal-123\\n")
elif args[:2] == ["keyvault", "show"] and "accessPolicies" in " ".join(args):
    sys.stdout.write("principal-123\\n")
elif args[:3] == ["keyvault", "secret", "show"]:
    name = args[args.index("--name") + 1]
    sys.stdout.write(f"https://demo-vault.vault.azure.net/secrets/{name}\\n")
elif args[:3] == ["storage", "account", "show"]:
    sys.stdout.write("/subscriptions/demo/storageAccounts/demostorage\\n")
elif args[:3] == ["storage", "container-rm", "show"]:
    sys.stdout.write("deep-research-blobs\\n")
elif args[:3] == ["storage", "share-rm", "show"]:
    sys.stdout.write("deep-research-auth\\n")
elif args[:4] == ["containerapp", "env", "storage", "show"]:
    sys.stdout.write("authsqlite\\tdemostorage\\tdeep-research-auth\\tReadWrite\\n")
elif args[:2] == ["containerapp", "show"] and "provisioningState" in " ".join(args):
    sys.stdout.write("Succeeded\\n")
elif args[:2] == ["containerapp", "show"] and "--output" in args and args[args.index("--output") + 1] == "json":
    json.dump({
        "name": "demo-api",
        "tags": {"unrelated": "preserved"},
        "identity": {"userAssignedIdentities": {"/subscriptions/demo/identity": {}}},
        "properties": {
            "configuration": {
                "secrets": [{"name": "unrelated-secret", "value": "opaque"}],
                "registries": [{"server": "private.example.test", "username": "kept-user", "passwordSecretRef": "unrelated-secret"}],
            },
            "template": {"containers": [{
                "name": "deep-research-agent",
                "env": [{"name": "UNRELATED_ENV", "value": "kept"}],
                "volumeMounts": [{"volumeName": "unrelated-volume", "mountPath": "/kept"}],
            }]},
        },
    }, sys.stdout)
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
    sys.stdout.write("Running\\tHealthy\\n")
elif args[:2] == ["containerapp", "update"] and "--yaml" in args:
    if "--revision-suffix" in args:
        sys.stderr.write("obsolete revision suffix CLI flag used\\n")
        raise SystemExit(88)
    source = pathlib.Path(args[args.index("--yaml") + 1])
    pathlib.Path(os.environ["FAKE_UPDATE_YAML"]).write_bytes(source.read_bytes())
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
            "FAKE_UPDATE_YAML": str(update_yaml),
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
    rendered = yaml.safe_load(update_yaml.read_text(encoding="utf-8"))
    revision_suffix = rendered["properties"]["template"]["revisionSuffix"]
    assert revision_suffix == "passkeys-20260812010101"
    assert rendered["tags"] == {"unrelated": "preserved"}
    secrets = {
        item["name"]: item
        for item in rendered["properties"]["configuration"]["secrets"]
    }
    assert secrets["unrelated-secret"]["value"] == "opaque"
    assert secrets["passkey-proxy-secret"] == {
        "name": "passkey-proxy-secret",
        "keyVaultUrl": "https://demo-vault.vault.azure.net/secrets/PASSKEY-PROXY-SECRET",
        "identity": "/subscriptions/demo/identity",
    }
    registries = rendered["properties"]["configuration"]["registries"]
    assert {item["server"]: item for item in registries}["private.example.test"] == {
        "server": "private.example.test",
        "username": "kept-user",
        "passwordSecretRef": "unrelated-secret",
    }
    container = rendered["properties"]["template"]["containers"][0]
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
        call for call in calls if call[:3] == ["keyvault", "secret", "show"]
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
    assert all(call[call.index("--query") + 1] == "id" for call in secret_calls)
    update_call = next(call for call in calls if call[:2] == ["containerapp", "update"])
    assert "--revision-suffix" not in update_call
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
    ]
    curl_index = next(index for index, call in enumerate(calls) if call[0] == "curl")
    assert calls[-2:] == [EXPECTED_AZ_ARGV, EXPECTED_AZ_ARGV]
    assert curl_index < len(calls) - 2
    assert json.loads((fixture / METADATA_NAME).read_text()) == _expected_metadata()


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
    configuration = rendered["properties"]["configuration"]
    container = rendered["properties"]["template"]["containers"][0]
    secrets = {item["name"]: item for item in configuration["secrets"]}
    environment = {item["name"]: item for item in container["env"]}

    assert secrets["passkey-proxy-secret"] == {
        "name": "passkey-proxy-secret",
        "keyVaultUrl": "https://demo-vault.vault.azure.net/secrets/PASSKEY-PROXY-SECRET",
        "identity": "/subscriptions/demo/identity",
    }
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
    assert "az keyvault secret show" in source
    assert "--query value" not in source


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
    json.dump({"identity": {"userAssignedIdentities": identity}, "properties": {"configuration": {}, "template": {}}}, sys.stdout)
elif args[:2] == ["keyvault", "show"]:
    if missing != "vault_access":
        sys.stdout.write("principal-123\\n")
elif args[:3] == ["keyvault", "secret", "show"]:
    name = args[args.index("--name") + 1]
    if missing == "required_secret" and name == "TAVILY-API-KEY":
        raise SystemExit(3)
    sys.stdout.write(f"https://demo-vault.vault.azure.net/secrets/{name}\\n")
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
    sys.stdout.write('{{"identity":{{"userAssignedIdentities":{{"/subscriptions/demo/identity":{{}}}}}},"properties":{{"configuration":{{}},"template":{{}}}}}}')
elif args[:2] == ["keyvault", "show"]:
    sys.stdout.write("principal-123\\n")
elif args[:3] == ["keyvault", "secret", "show"]:
    name = args[args.index("--name") + 1]
    sys.stdout.write(f"https://demo-vault.vault.azure.net/secrets/{{name}}\\n")
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
