from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADAPTER = PROJECT_ROOT / "scripts" / "container_runtime.sh"
_UNSET = object()


def _install_runtime(tmp_path: Path, name: str, body: str = "exit 0") -> Path:
    runtime = tmp_path / name
    runtime.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    runtime.chmod(0o755)
    return runtime


def _run_adapter(
    tmp_path: Path,
    command: str,
    *,
    override: str | object = _UNSET,
    stdin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("CONTAINER_RUNTIME", None)
    env["PATH"] = str(tmp_path)
    env["ADAPTER"] = str(ADAPTER)
    if extra_env:
        env.update(extra_env)
    if override is not _UNSET:
        env["CONTAINER_RUNTIME"] = str(override)

    return subprocess.run(
        ["/bin/bash", "-c", f'source "$ADAPTER"; {command}'],
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _select_runtime(tmp_path: Path, *, override: str | object = _UNSET):
    return _run_adapter(
        tmp_path,
        'select_container_runtime && printf "%s\\n" "$CONTAINER_RUNTIME"',
        override=override,
    )


def test_selection_prefers_apple_container(tmp_path: Path) -> None:
    for runtime in ("container", "podman", "docker"):
        _install_runtime(tmp_path, runtime)

    result = _select_runtime(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "container\n"


def test_selection_prefers_podman_over_docker(tmp_path: Path) -> None:
    for runtime in ("podman", "docker"):
        _install_runtime(tmp_path, runtime)

    result = _select_runtime(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "podman\n"


def test_selection_uses_docker_when_it_is_only_supported_runtime(
    tmp_path: Path,
) -> None:
    _install_runtime(tmp_path, "docker")

    result = _select_runtime(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "docker\n"


def test_explicit_valid_override_wins_over_automatic_selection(tmp_path: Path) -> None:
    _install_runtime(tmp_path, "container")
    _install_runtime(tmp_path, "podman")

    result = _select_runtime(tmp_path, override="podman")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "podman\n"


@pytest.mark.parametrize(
    ("override", "installed"),
    [
        ("", ("container",)),
        ("buildah", ("buildah", "container")),
        ("docker", ("container",)),
    ],
    ids=("empty", "unsupported", "unavailable"),
)
def test_invalid_explicit_override_fails_without_fallback(
    tmp_path: Path,
    override: str,
    installed: tuple[str, ...],
) -> None:
    for runtime in installed:
        _install_runtime(tmp_path, runtime)

    result = _select_runtime(tmp_path, override=override)

    assert result.returncode != 0
    assert "container, podman, or docker" in result.stderr
    assert result.stdout == ""


def test_selection_fails_when_no_supported_runtime_exists(tmp_path: Path) -> None:
    result = _select_runtime(tmp_path)

    assert result.returncode != 0
    assert "No supported container runtime found" in result.stderr
    assert result.stdout == ""


def test_podman_readiness_invokes_only_info(tmp_path: Path) -> None:
    invocation_log = tmp_path / "invocations.log"
    _install_runtime(
        tmp_path,
        "podman",
        'printf "podman %s\\n" "$*" >> "$INVOCATION_LOG"\nexit 0',
    )

    result = _run_adapter(
        tmp_path,
        "select_container_runtime && ensure_container_runtime_ready",
        extra_env={"INVOCATION_LOG": str(invocation_log)},
    )

    assert result.returncode == 0, result.stderr
    assert invocation_log.read_text(encoding="utf-8") == "podman info\n"


def test_docker_readiness_invokes_only_info(tmp_path: Path) -> None:
    invocation_log = tmp_path / "invocations.log"
    _install_runtime(
        tmp_path,
        "docker",
        'printf "docker %s\\n" "$*" >> "$INVOCATION_LOG"\nexit 0',
    )

    result = _run_adapter(
        tmp_path,
        "select_container_runtime && ensure_container_runtime_ready",
        extra_env={"INVOCATION_LOG": str(invocation_log)},
    )

    assert result.returncode == 0, result.stderr
    assert invocation_log.read_text(encoding="utf-8") == "docker info\n"


def test_apple_container_readiness_starts_service_after_failed_status(
    tmp_path: Path,
) -> None:
    invocation_log = tmp_path / "invocations.log"
    _install_runtime(
        tmp_path,
        "container",
        """printf "container %s\\n" "$*" >> "$INVOCATION_LOG"
if [[ "$*" == "system status" ]]; then
    exit 1
fi
if [[ "$*" == "system start --disable-kernel-install" ]]; then
    exit 0
fi
exit 99""",
    )

    result = _run_adapter(
        tmp_path,
        "select_container_runtime && ensure_container_runtime_ready",
        extra_env={"INVOCATION_LOG": str(invocation_log)},
    )

    assert result.returncode == 0, result.stderr
    assert invocation_log.read_text(encoding="utf-8") == (
        "container system status\n"
        "container system start --disable-kernel-install\n"
    )


def test_podman_readiness_failure_does_not_fall_back_to_docker(
    tmp_path: Path,
) -> None:
    invocation_log = tmp_path / "invocations.log"
    _install_runtime(
        tmp_path,
        "podman",
        'printf "podman %s\\n" "$*" >> "$INVOCATION_LOG"\nexit 1',
    )
    _install_runtime(
        tmp_path,
        "docker",
        'printf "docker %s\\n" "$*" >> "$INVOCATION_LOG"\nexit 0',
    )

    result = _run_adapter(
        tmp_path,
        "select_container_runtime && ensure_container_runtime_ready",
        extra_env={"INVOCATION_LOG": str(invocation_log)},
    )

    assert result.returncode != 0
    assert invocation_log.read_text(encoding="utf-8") == "podman info\n"
    assert "daemonless Podman" in result.stderr


def test_docker_readiness_failure_tells_user_to_start_daemon(
    tmp_path: Path,
) -> None:
    invocation_log = tmp_path / "invocations.log"
    _install_runtime(
        tmp_path,
        "docker",
        'printf "docker %s\\n" "$*" >> "$INVOCATION_LOG"\nexit 1',
    )

    result = _run_adapter(
        tmp_path,
        "select_container_runtime && ensure_container_runtime_ready",
        extra_env={"INVOCATION_LOG": str(invocation_log)},
    )

    assert result.returncode != 0
    assert invocation_log.read_text(encoding="utf-8") == "docker info\n"
    assert "start the Docker daemon" in result.stderr


def test_readiness_fails_when_runtime_has_not_been_selected(tmp_path: Path) -> None:
    result = _run_adapter(tmp_path, "ensure_container_runtime_ready")

    assert result.returncode != 0
    assert "selected" in result.stderr
