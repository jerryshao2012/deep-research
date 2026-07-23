from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import s3_storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _DownloadPaginator:
    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def paginate(self, **_kwargs):
        return [{"Contents": [{"Key": key} for key in self._keys]}]


class _DownloadClient:
    def __init__(self, keys: list[str]) -> None:
        self._keys = keys
        self.downloads: list[tuple[str, str, str]] = []

    def get_paginator(self, _name: str) -> _DownloadPaginator:
        return _DownloadPaginator(self._keys)

    def download_file(
        self,
        bucket: str,
        key: str,
        destination: str,
    ) -> None:
        self.downloads.append((bucket, key, destination))
        Path(destination).write_text("downloaded", encoding="utf-8")


def test_generic_s3_sync_excludes_langgraph_state(monkeypatch) -> None:
    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", "/tmp/reports")
    monkeypatch.setenv("INPUT_FOLDER", "/tmp/input")

    tracked = s3_storage._resolve_tracked_folders()

    assert [prefix for prefix, _path in tracked] == ["docs", "output", "input"]
    assert all(".langgraph_api" not in str(path) for _prefix, path in tracked)


@pytest.mark.parametrize(
    "environment_update",
    [
        {"S3_BUCKET_NAME": "demo-bucket"},
        {"AWS_REGION": "us-east-1"},
    ],
)
def test_partial_s3_configuration_disables_optional_helpers(
    monkeypatch,
    tmp_path,
    environment_update,
) -> None:
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    for name, value in environment_update.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        s3_storage,
        "_get_client",
        lambda: pytest.fail("disabled S3 helper created a client"),
    )

    assert s3_storage.is_s3_enabled() is False
    assert s3_storage.startup_sync() == 0
    assert s3_storage.download_prefix_sync("docs", tmp_path / "docs") == 0
    assert s3_storage.upload_directory_sync(tmp_path, "docs") == 0
    s3_storage.fire_and_forget_upload(tmp_path / "missing", "docs/missing")
    s3_storage.fire_and_forget_directory_upload(tmp_path, "docs")


def test_s3_storage_startup_cli_fails_closed_on_download_error(tmp_path) -> None:
    fake_modules = tmp_path / "fake-modules"
    fake_modules.mkdir()
    (fake_modules / "boto3.py").write_text(
        """
class _Paginator:
    def paginate(self, **kwargs):
        return [{"Contents": [{"Key": "docs/example.txt"}]}]

class _Client:
    def get_paginator(self, name):
        return _Paginator()

    def download_file(self, bucket, key, destination):
        raise RuntimeError("download denied")

def client(service, region_name=None):
    return _Client()
""".lstrip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "S3_BUCKET_NAME": "demo-bucket",
            "AWS_REGION": "us-east-1",
            "PYTHONPATH": os.pathsep.join(
                [str(fake_modules), str(PROJECT_ROOT)]
            ),
            "REPORTS_OUTPUT_FOLDER": str(tmp_path / "output"),
            "INPUT_FOLDER": str(tmp_path / "input"),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "s3_storage", "startup"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "S3 startup sync failed" in result.stderr


@pytest.mark.parametrize(
    ("environment_update", "missing_name"),
    [
        ({}, "S3_BUCKET_NAME"),
        ({"S3_BUCKET_NAME": "demo-bucket"}, "AWS_REGION"),
        ({"AWS_REGION": "us-east-1"}, "S3_BUCKET_NAME"),
    ],
)
def test_s3_storage_startup_cli_requires_complete_aws_configuration(
    tmp_path,
    environment_update,
    missing_name,
) -> None:
    environment = os.environ.copy()
    environment.pop("S3_BUCKET_NAME", None)
    environment.pop("AWS_REGION", None)
    environment.update(environment_update)
    environment["PYTHONPATH"] = str(PROJECT_ROOT)

    result = subprocess.run(
        [sys.executable, "-m", "s3_storage", "startup"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert missing_name in result.stderr


@pytest.mark.parametrize(
    "key",
    [
        "docs/../../.langgraph_api/file",
        "docs//tmp/absolute",
        "docs/nested/../escape",
        "docs/./dot",
    ],
)
def test_download_prefix_rejects_unsafe_object_suffixes(
    monkeypatch,
    tmp_path,
    key,
) -> None:
    client = _DownloadClient([key])
    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(s3_storage, "_get_client", lambda: client)

    with pytest.raises(ValueError, match="unsafe S3 object key"):
        s3_storage._download_prefix("docs", tmp_path / "docs")

    assert client.downloads == []
    assert not (tmp_path / ".langgraph_api").exists()


def test_download_prefix_accepts_normal_nested_object(
    monkeypatch,
    tmp_path,
) -> None:
    client = _DownloadClient(["docs/threads/thread-1/wiki/index.md"])
    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(s3_storage, "_get_client", lambda: client)

    count = s3_storage._download_prefix("docs", tmp_path / "docs")

    expected = tmp_path / "docs/threads/thread-1/wiki/index.md"
    assert count == 1
    assert expected.read_text(encoding="utf-8") == "downloaded"
    assert client.downloads == [
        ("demo-bucket", "docs/threads/thread-1/wiki/index.md", str(expected))
    ]


def test_download_prefix_rejects_destination_resolved_outside_root(
    monkeypatch,
    tmp_path,
) -> None:
    client = _DownloadClient(["docs/linked/file.txt"])
    tracked_root = tmp_path / "docs"
    outside_root = tmp_path / "outside"
    tracked_root.mkdir()
    outside_root.mkdir()
    (tracked_root / "linked").symlink_to(outside_root, target_is_directory=True)
    monkeypatch.setenv("S3_BUCKET_NAME", "demo-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(s3_storage, "_get_client", lambda: client)

    with pytest.raises(ValueError, match="unsafe S3 object key"):
        s3_storage._download_prefix("docs", tracked_root)

    assert client.downloads == []
    assert not (outside_root / "file.txt").exists()


def test_aws_entrypoint_restores_guarded_snapshot_before_exec() -> None:
    source = (PROJECT_ROOT / "entrypoint.sh").read_text(encoding="utf-8")

    generic_startup = source.index("-m s3_storage startup")
    guarded_restore = source.index("-m langgraph_snapshot restore")
    application_exec = source.index('exec "$@"')

    assert generic_startup < guarded_restore < application_exec
    assert "langgraph_snapshot restore ||" not in source
    assert "-m langgraph_snapshot restore --write-receipt" in source


def test_aws_entrypoint_restore_failure_stops_application(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$*" in
  *"langgraph_snapshot restore"*) exit 23 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_app = fake_bin / "demo-app"
    fake_app.write_text(
        """#!/bin/sh
printf 'application-started\\n' >> "$CALL_LOG"
""",
        encoding="utf-8",
    )
    fake_app.chmod(0o755)
    runtime_root = tmp_path / "runtime"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "CALL_LOG": str(call_log),
            "PROJECT_ROOT": str(runtime_root),
            "S3_BUCKET_NAME": "demo-bucket",
            "AWS_REGION": "us-east-1",
        }
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "entrypoint.sh"), "demo-app"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert result.returncode == 23
    assert calls == [
        "-m s3_storage startup",
        "-m langgraph_snapshot restore --write-receipt",
    ]


def test_entrypoint_does_not_restore_s3_snapshot_outside_aws(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/bin/sh
printf 'python %s\\n' "$*" >> "$CALL_LOG"
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_app = fake_bin / "demo-app"
    fake_app.write_text(
        """#!/bin/sh
printf 'application-started\\n' >> "$CALL_LOG"
""",
        encoding="utf-8",
    )
    fake_app.chmod(0o755)
    environment = os.environ.copy()
    environment.pop("S3_BUCKET_NAME", None)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "CALL_LOG": str(call_log),
            "PROJECT_ROOT": str(tmp_path / "runtime"),
            "MOUNT_PATH": str(tmp_path / "azure-mount"),
        }
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "entrypoint.sh"), "demo-app"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "application-started"
    ]


def test_aws_entrypoint_rejects_bucket_without_region(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/bin/sh
case "$*" in
  *"s3_storage startup"*) exec "$REAL_PYTHON" "$@" ;;
esac
exit 99
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_app = fake_bin / "demo-app"
    fake_app.write_text(
        """#!/bin/sh
printf 'application-started\\n' >> "$CALL_LOG"
""",
        encoding="utf-8",
    )
    fake_app.chmod(0o755)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    environment = os.environ.copy()
    environment.pop("AWS_REGION", None)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "PYTHONPATH": str(PROJECT_ROOT),
            "REAL_PYTHON": sys.executable,
            "CALL_LOG": str(call_log),
            "PROJECT_ROOT": str(runtime_root),
            "S3_BUCKET_NAME": "demo-bucket",
        }
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "entrypoint.sh"), "demo-app"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "AWS_REGION" in result.stderr
    assert not call_log.exists()


def test_manual_aws_sync_delegates_langgraph_state_to_guarded_cli() -> None:
    source = (PROJECT_ROOT / "sync-files-aws.sh").read_text(encoding="utf-8")

    assert "-m langgraph_snapshot restore" in source
    assert "-m langgraph_snapshot publish" in source
    assert "--source \"$PROJECT_ROOT/.langgraph_api\"" in source
    assert "--target \"$PROJECT_ROOT/.langgraph_api\"" in source
    for line in source.splitlines():
        if "aws s3 cp" in line or "aws s3 sync" in line:
            assert ".langgraph_api" not in line


def test_manual_aws_sync_uses_project_runtime_directories() -> None:
    source = (PROJECT_ROOT / "sync-files-aws.sh").read_text(encoding="utf-8")

    assert 'PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in source
    assert 'local_folder="$PROJECT_ROOT/${folder}"' in source
    assert '"docs"' in source
    assert "SYNC_ROOT=\"./sync-aws\"" not in source


def test_manual_verbose_upload_runs_without_optional_aws_args(
    tmp_path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/bin/sh
printf 'aws %s\\n' "$*" >> "$CALL_LOG"
exit 0
""",
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/bin/sh
printf 'python %s\\n' "$*" >> "$CALL_LOG"
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env_file = tmp_path / "env-aws.sh"
    env_file.write_text(
        "S3_BUCKET_NAME=demo-bucket\nAWS_REGION=us-east-1\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "AWS_ENV_FILE": str(env_file),
            "CALL_LOG": str(call_log),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "PYTHON_BIN": str(fake_python),
        }
    )

    result = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "sync-files-aws.sh"),
            "--upload",
            "--verbose",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert f"aws s3 sync {PROJECT_ROOT / 'docs'}" in calls
    assert (
        "python -m langgraph_snapshot publish "
        f"--source {PROJECT_ROOT / '.langgraph_api'}"
    ) in calls
    assert "aws s3 sync" in calls
    assert "aws s3 sync" not in "\n".join(
        line for line in calls.splitlines() if ".langgraph_api" in line
    )
    upload_calls = [
        line
        for line in calls.splitlines()
        if line.startswith("aws s3 sync")
    ]
    assert upload_calls
    assert all("--no-follow-symlinks" in line for line in upload_calls)


def test_manual_default_sync_is_read_only(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/bin/sh
printf 'aws %s\\n' "$*" >> "$CALL_LOG"
exit 0
""",
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/bin/sh
printf 'python %s\\n' "$*" >> "$CALL_LOG"
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env_file = tmp_path / "env-aws.sh"
    env_file.write_text(
        "S3_BUCKET_NAME=demo-bucket\nAWS_REGION=us-east-1\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "AWS_ENV_FILE": str(env_file),
            "CALL_LOG": str(call_log),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "PYTHON_BIN": str(fake_python),
        }
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "sync-files-aws.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "python -m langgraph_snapshot restore" in calls
    assert "langgraph_snapshot publish" not in calls
    generic_calls = [
        line
        for line in calls.splitlines()
        if line.startswith("aws s3 sync")
    ]
    assert generic_calls
    assert all("aws s3 sync s3://" in line for line in generic_calls)


def test_aws_container_keeps_langgraph_reload_disabled() -> None:
    source = (PROJECT_ROOT / "Dockerfile-aws").read_text(encoding="utf-8")

    assert '"langgraph", "dev"' in source
    assert '"--no-reload"' in source


def test_aws_container_uses_frozen_uv_runtime() -> None:
    dockerignore_lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    source = (PROJECT_ROOT / "Dockerfile-aws").read_text(encoding="utf-8")

    assert "uv.lock" not in dockerignore_lines
    assert "uv sync --frozen" in source
    assert 'ENV PATH="/deps/deep_research/.venv/bin:$PATH"' in source
    assert ".env-aws.docker" not in source
    assert "/deps/deep_research/.venv/bin/python" in source
    assert "/deps/deep_research/.venv/bin/langgraph" in source
    for module in (
        "boto3",
        "langgraph_api",
        "langgraph_runtime_inmem",
        "langgraph_snapshot",
    ):
        assert f"import {module}" in source


def test_aws_container_removes_state_created_by_langgraph_cli_smoke() -> None:
    source = (PROJECT_ROOT / "Dockerfile-aws").read_text(encoding="utf-8")

    smoke_index = source.index(
        "/deps/deep_research/.venv/bin/langgraph dev --help"
    )
    cleanup_index = source.index(
        "rm -rf /deps/deep_research/.langgraph_api",
        smoke_index,
    )

    assert cleanup_index > smoke_index
    assert "/deps/deep_research/.langgraph_api.restore-receipt.json" in source
    assert "/deps/deep_research/..langgraph_api.restore-*" in source
    assert "/deps/deep_research/..langgraph_api.backup-*" in source
    assert "/deps/deep_research/..langgraph_api.publish-*" in source
    assert "/deps/deep_research/..langgraph_api.canonical-*" in source
    assert (
        "/deps/deep_research/..langgraph_api.restore-receipt.json.*"
        in source
    )


def test_aws_container_pins_python_patch_and_linux_amd64_digest() -> None:
    source = (PROJECT_ROOT / "Dockerfile-aws").read_text(encoding="utf-8")

    assert source.startswith(
        "FROM python:3.12.13-slim-bookworm@"
        "sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
    )
    assert (
        "sha256:72d3d75f2639ab82b34b29390ad3d6e0827c775befee94edda8e9976818f488d"
        in source
    )


def test_aws_container_excludes_local_secrets_and_runtime_state() -> None:
    dockerignore_lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "uv.lock" not in dockerignore_lines
    assert ".env*" in dockerignore_lines
    assert ".env-aws.docker" in dockerignore_lines
    assert "!.env.example" in dockerignore_lines
    assert "secrets*.sh" in dockerignore_lines
    assert ".env.example" not in dockerignore_lines
    assert "secrets-aws.sh.example" not in dockerignore_lines
    assert "deep_research.db*" in dockerignore_lines
    assert "sync-aws/" in dockerignore_lines
    assert ".langgraph_api/" in dockerignore_lines
    assert ".langgraph_snapshots/" in dockerignore_lines
    assert "..langgraph_api.*" in dockerignore_lines
    assert "*.pckl" in dockerignore_lines
    assert "*.pckl.tmp" in dockerignore_lines
    assert ".deepagents/" not in dockerignore_lines
    assert "thread_wiki/" not in dockerignore_lines
    assert "current_config.json" in dockerignore_lines


def test_aws_deploy_defaults_to_read_only_snapshot_rollout() -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")

    assert 'LANGGRAPH_S3_READ_ONLY="true"' in source
    assert "--read-write" in source
    assert 'LANGGRAPH_S3_READ_ONLY="false"' in source
    expected_environment = {
        "LANGGRAPH_SNAPSHOT_PREFIX": ".langgraph_snapshots",
        "LANGGRAPH_SNAPSHOT_STABILITY_SECONDS": "12",
        "LANGGRAPH_SNAPSHOT_SCAN_INTERVAL_SECONDS": "2",
        "LANGGRAPH_FENCE_INTERVAL_SECONDS": "2",
        "LANGGRAPH_SNAPSHOT_RETENTION_COUNT": "5",
        "LANGGRAPH_WRITER_EPOCH": "${APP_NAME}",
    }
    for name, value in expected_environment.items():
        assert f'"{name}": "{value}"' in source
    assert (
        '"LANGGRAPH_S3_READ_ONLY": "${LANGGRAPH_S3_READ_ONLY}"'
        in source
    )


def test_aws_deploy_enforces_singleton_and_http_health_check() -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")

    assert "create-auto-scaling-configuration" in source
    assert (
        'AUTOSCALING_CONFIGURATION_NAME="deep-research-singleton-${SEED}"'
        in source
    )
    assert "describe-auto-scaling-configuration" in source
    assert "AutoScalingConfiguration.[Status,MinSize,MaxSize]" in source
    assert "--min-size 1" in source
    assert "--max-size 1" in source
    assert source.count(
        '--auto-scaling-configuration-arn '
        '"$AUTOSCALING_CONFIGURATION_ARN"'
    ) >= 2
    expected_health = (
        'Protocol=HTTP,Path=/ok,Interval=5,Timeout=2,'
        "HealthyThreshold=1,UnhealthyThreshold=5"
    )
    assert source.count(
        f'--health-check-configuration "{expected_health}"'
    ) == 2


@pytest.mark.parametrize(
    ("status_code", "curl_exit", "expected_returncode"),
    [
        ("200", "0", 0),
        ("503", "0", 1),
        ("", "7", 1),
    ],
)
def test_aws_deploy_readiness_check_requires_ok_2xx(
    tmp_path,
    status_code,
    curl_exit,
    expected_returncode,
) -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")
    function_start = source.index("verify_app_runner_readiness() {")
    function_end = source.index("\n}", function_start) + 2
    function_source = source[function_start:function_end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "curl.log"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" > "$CALL_LOG"
printf '%s' "$FAKE_CURL_STATUS"
exit "$FAKE_CURL_EXIT"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "CALL_LOG": str(call_log),
            "FAKE_CURL_STATUS": status_code,
            "FAKE_CURL_EXIT": curl_exit,
        }
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -e\n"
                f"{function_source}\n"
                "verify_app_runner_readiness https://example.test"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_returncode
    assert "https://example.test/ok" in call_log.read_text(encoding="utf-8")
    if expected_returncode:
        assert "readiness" in result.stderr.lower()


def test_aws_deploy_checks_readiness_before_reporting_completion() -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")

    endpoint = source.index('EXTERNAL_URL="https://$RAW_URL"')
    mandatory_check = source.index(
        'verify_app_runner_readiness "$EXTERNAL_URL"',
    )
    completion = source.index("AWS App Runner Deployment Complete!")

    assert endpoint < mandatory_check < completion


def _extract_shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}", start) + 2
    return source[start:end]


@pytest.mark.parametrize(
    ("operation_status", "expected_returncode"),
    [
        ("SUCCEEDED", 0),
        ("FAILED", 1),
        ("ROLLBACK_SUCCEEDED", 1),
        ("None", 1),
    ],
)
def test_aws_deploy_waits_for_exact_operation_terminal_status(
    tmp_path,
    operation_status,
    expected_returncode,
) -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")
    function_source = _extract_shell_function(
        source,
        "wait_for_app_runner_operation",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "aws.log"
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$*" in
  *"list-operations"*) printf '%s' "$FAKE_OPERATION_STATUS" ;;
  *"describe-service"*) printf 'RUNNING' ;;
esac
""",
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "CALL_LOG": str(call_log),
            "FAKE_OPERATION_STATUS": operation_status,
            "AWS_REGION": "us-east-1",
            "APP_RUNNER_OPERATION_MAX_POLLS": "2",
            "APP_RUNNER_OPERATION_POLL_SECONDS": "0",
        }
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -e\n"
                f"{function_source}\n"
                "wait_for_app_runner_operation "
                "arn:aws:apprunner:service operation-new"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_returncode
    calls = call_log.read_text(encoding="utf-8")
    assert "apprunner list-operations" in calls
    assert "operation-new" in calls
    assert "describe-service" not in calls
    if expected_returncode:
        assert "operation" in result.stderr.lower()


def test_aws_deploy_captures_every_operation_id() -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")

    assert "UPDATE_OUT=$(aws apprunner update-service" in source
    assert "START_OUT=$(aws apprunner start-deployment" in source
    assert "CREATE_OUT=$(aws apprunner create-service" in source
    assert source.count("data.get('OperationId', '')") >= 3
    assert 'wait_for_app_runner_operation "$SERVICE_ARN" "$OP_ID"' in source
    assert "Waiting for App Runner service deployment to finish" not in source


def test_aws_deploy_replaces_inactive_singleton_configuration(
    tmp_path,
) -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")
    function_source = _extract_shell_function(
        source,
        "resolve_singleton_autoscaling_configuration",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "aws.log"
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$*" in
  *"list-auto-scaling-configurations"*) printf 'arn:old' ;;
  *"describe-auto-scaling-configuration"*) printf 'INACTIVE\\t1\\t1' ;;
  *"create-auto-scaling-configuration"*) printf 'arn:new' ;;
esac
""",
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "CALL_LOG": str(call_log),
            "AWS_REGION": "us-east-1",
            "SEED": "0312",
        }
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -e\n"
                f"{function_source}\n"
                "resolve_singleton_autoscaling_configuration"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "arn:new"
    assert "create-auto-scaling-configuration" in call_log.read_text(
        encoding="utf-8"
    )


def test_aws_deploy_reuses_lowercase_active_singleton_configuration(
    tmp_path,
) -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")
    function_source = _extract_shell_function(
        source,
        "resolve_singleton_autoscaling_configuration",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "aws.log"
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$*" in
  *"list-auto-scaling-configurations"*) printf 'arn:existing' ;;
  *"describe-auto-scaling-configuration"*) printf 'active\\t1\\t1' ;;
  *"create-auto-scaling-configuration"*) printf 'arn:unexpected' ;;
esac
""",
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "CALL_LOG": str(call_log),
            "AWS_REGION": "us-east-1",
            "SEED": "0312",
        }
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -e\n"
                f"{function_source}\n"
                "resolve_singleton_autoscaling_configuration"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "arn:existing"
    assert "create-auto-scaling-configuration" not in call_log.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("deployed_version", "expected_version", "expected_returncode"),
    [
        ("1.2.3", "1.2.3", 0),
        ("old-revision", "1.2.3", 1),
    ],
)
def test_aws_deploy_requires_expected_health_version(
    tmp_path,
    deployed_version,
    expected_version,
    expected_returncode,
) -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")
    function_source = _extract_shell_function(
        source,
        "verify_deployed_version",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/bin/sh
printf '{"version":"%s"}' "$FAKE_DEPLOYED_VERSION"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_DEPLOYED_VERSION": deployed_version,
            "APP_RUNNER_VERSION_MAX_RETRIES": "1",
            "APP_RUNNER_VERSION_POLL_SECONDS": "0",
        }
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -e\n"
                f"{function_source}\n"
                "verify_deployed_version "
                f"https://example.test {expected_version}"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_returncode
    if expected_returncode:
        assert "version" in result.stderr.lower()


def test_aws_deploy_version_gate_precedes_success_message() -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")

    version_check = source.index(
        'verify_deployed_version "$EXTERNAL_URL" "$NEW_VERSION"',
    )
    completion = source.index("AWS App Runner Deployment Complete!")

    assert version_check < completion


def test_aws_deploy_registers_temp_files_for_exit_cleanup() -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")

    assert "trap cleanup_temp_files EXIT" in source
    for variable in (
        "TRUST_POLICY_FILE",
        "INSTANCE_TRUST_FILE",
        "INSTANCE_POLICY_FILE",
        "S3_POLICY_FILE",
        "SOURCE_CONFIG_FILE",
    ):
        creation = source.index(f"{variable}=$(mktemp)")
        registration = source.index(
            'TEMP_FILES+=("$' + variable + '")',
            creation,
        )
        assert creation < registration


def test_aws_deploy_exit_trap_removes_registered_temp_file(
    tmp_path,
) -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")
    cleanup_source = _extract_shell_function(source, "cleanup_temp_files")
    temporary_file = tmp_path / "deployment-config.json"
    temporary_file.write_text("temporary", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"{cleanup_source}\n"
                'TEMP_FILES=("$1")\n'
                "trap cleanup_temp_files EXIT\n"
                "exit 7"
            ),
            "bash",
            str(temporary_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 7
    assert not temporary_file.exists()


def test_aws_deploy_injects_google_oauth_credentials() -> None:
    source = (PROJECT_ROOT / "deploy-aws.sh").read_text(encoding="utf-8")

    assert (
        '"GOOGLE_CLIENT_ID": '
        '"${SECRET_ARN}:GOOGLE-CLIENT-ID::"'
    ) in source
    assert (
        '"GOOGLE_CLIENT_SECRET": '
        '"${SECRET_ARN}:GOOGLE-CLIENT-SECRET::"'
    ) in source


def test_aws_deploy_configures_cloudfront_frontend_origin() -> None:
    deploy_source = (PROJECT_ROOT / "deploy-aws.sh").read_text(
        encoding="utf-8"
    )
    environment_source = (PROJECT_ROOT / "env-aws.sh").read_text(
        encoding="utf-8"
    )

    assert (
        'export FRONTEND_URLS="${FRONTEND_URLS:-'
        'https://d600y3wyk0xvf.cloudfront.net}"'
    ) in environment_source
    assert '"FRONTEND_URLS": "${FRONTEND_URLS}"' in deploy_source


def test_environment_frontend_origin_is_primary_oauth_fallback() -> None:
    environment = os.environ.copy()
    environment["FRONTEND_URLS"] = (
        "https://d600y3wyk0xvf.cloudfront.net"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from webapp.config import FRONTEND_ORIGINS; "
                "print(FRONTEND_ORIGINS[0])"
            ),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == (
        "https://d600y3wyk0xvf.cloudfront.net"
    )


def test_aws_secret_sync_includes_google_oauth_credentials() -> None:
    source = (PROJECT_ROOT / "secrets-aws.sh.example").read_text(
        encoding="utf-8"
    )

    assert 'source ./.env.docker' in source
    assert '"GOOGLE-CLIENT-ID": "$GOOGLE_CLIENT_ID"' in source
    assert '"GOOGLE-CLIENT-SECRET": "$GOOGLE_CLIENT_SECRET"' in source
