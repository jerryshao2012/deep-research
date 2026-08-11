from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADAPTER = PROJECT_ROOT / "scripts" / "container_runtime.sh"
_UNSET = object()
_SUBPROCESS_TIMEOUT_SECONDS = 10


def _install_runtime(tmp_path: Path, name: str, body: str = "exit 0") -> Path:
    runtime = tmp_path / name
    runtime.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    runtime.chmod(0o755)
    return runtime


def _install_recording_runtime(tmp_path: Path, name: str) -> Path:
    return _install_runtime(
        tmp_path,
        name,
        r'''printf '%s\0' "${0##*/}" "$@" > "$RUNTIME_ARGV_LOG"
read_stdin=0
for arg in "$@"; do
    if [[ "$arg" == "--password-stdin" ]]; then
        read_stdin=1
        break
    fi
done
if (( read_stdin == 1 )); then
    /bin/cat > "$RUNTIME_STDIN_LOG"
else
    : > "$RUNTIME_STDIN_LOG"
fi
exit "${RUNTIME_EXIT_CODE:-0}"''',
    )


def _run_adapter(
    tmp_path: Path,
    command: str,
    *,
    override: str | object = _UNSET,
    stdin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": str(tmp_path), "ADAPTER": str(ADAPTER)}
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
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )


def _select_runtime(tmp_path: Path, *, override: str | object = _UNSET):
    return _run_adapter(
        tmp_path,
        'select_container_runtime && printf "%s\\n" "$CONTAINER_RUNTIME"',
        override=override,
    )


def _run_recorded_runtime_command(
    tmp_path: Path,
    runtime: str,
    command: str,
    *,
    stdin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], bytes, bytes]:
    argv_log = tmp_path / "runtime-argv.log"
    stdin_log = tmp_path / "runtime-stdin.log"
    argv_log.write_bytes(b"")
    stdin_log.write_bytes(b"")
    _install_recording_runtime(tmp_path, runtime)

    runtime_env = {
        "RUNTIME_ARGV_LOG": str(argv_log),
        "RUNTIME_STDIN_LOG": str(stdin_log),
    }
    if extra_env:
        runtime_env.update(extra_env)

    result = _run_adapter(
        tmp_path,
        f"select_container_runtime && {command}",
        override=runtime,
        stdin=stdin,
        extra_env=runtime_env,
    )

    return result, argv_log.read_bytes(), stdin_log.read_bytes()


def _run_recorded_wrapper_without_selection(
    tmp_path: Path,
    command: str,
    *,
    override: str | object = _UNSET,
    stdin: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], bytes, bytes]:
    argv_log = tmp_path / "runtime-argv.log"
    stdin_log = tmp_path / "runtime-stdin.log"
    argv_log.write_bytes(b"")
    stdin_log.write_bytes(b"")
    runtime_name = "container" if override is _UNSET else str(override)
    _install_recording_runtime(tmp_path, runtime_name)

    result = _run_adapter(
        tmp_path,
        command,
        override=override,
        stdin=stdin,
        extra_env={
            "RUNTIME_ARGV_LOG": str(argv_log),
            "RUNTIME_STDIN_LOG": str(stdin_log),
        },
    )

    return result, argv_log.read_bytes(), stdin_log.read_bytes()


@pytest.mark.parametrize("runtime", ["container", "podman", "docker"])
def test_build_maps_to_selected_runtime_and_preserves_arguments(
    tmp_path: Path,
    runtime: str,
) -> None:
    result, argv, stdin = _run_recorded_runtime_command(
        tmp_path,
        runtime,
        'container_runtime_build --platform linux/amd64 -t "image tag" .',
    )

    assert result.returncode == 0, result.stderr
    assert argv == (
        f"{runtime}\0build\0--platform\0linux/amd64\0-t\0image tag\0.\0"
    ).encode()
    assert stdin == b""


@pytest.mark.parametrize(
    ("runtime", "expected_command"),
    [
        ("container", ("image", "tag")),
        ("podman", ("tag",)),
        ("docker", ("tag",)),
    ],
)
def test_tag_maps_to_selected_runtime(
    tmp_path: Path,
    runtime: str,
    expected_command: tuple[str, ...],
) -> None:
    result, argv, stdin = _run_recorded_runtime_command(
        tmp_path,
        runtime,
        'container_runtime_tag "source image" "target image"',
    )

    assert result.returncode == 0, result.stderr
    expected = (runtime, *expected_command, "source image", "target image")
    assert argv == ("\0".join(expected) + "\0").encode()
    assert stdin == b""


@pytest.mark.parametrize(
    ("runtime", "expected_command"),
    [
        ("container", ("image", "push")),
        ("podman", ("push",)),
        ("docker", ("push",)),
    ],
)
def test_push_maps_to_selected_runtime(
    tmp_path: Path,
    runtime: str,
    expected_command: tuple[str, ...],
) -> None:
    result, argv, stdin = _run_recorded_runtime_command(
        tmp_path,
        runtime,
        "container_runtime_push registry.example/image:tag",
    )

    assert result.returncode == 0, result.stderr
    expected = (runtime, *expected_command, "registry.example/image:tag")
    assert argv == ("\0".join(expected) + "\0").encode()
    assert stdin == b""


@pytest.mark.parametrize(
    ("runtime", "expected_command"),
    [
        ("container", ("registry", "login", "-u")),
        ("podman", ("login", "--username")),
        ("docker", ("login", "--username")),
    ],
)
def test_login_maps_to_selected_runtime_and_passes_secret_only_via_stdin(
    tmp_path: Path,
    runtime: str,
    expected_command: tuple[str, ...],
) -> None:
    secret = "registry-secret\n"
    result, argv, stdin = _run_recorded_runtime_command(
        tmp_path,
        runtime,
        "container_runtime_login build-user registry.example",
        stdin=secret,
    )

    assert result.returncode == 0, result.stderr
    expected = (
        runtime,
        *expected_command,
        "build-user",
        "--password-stdin",
        "registry.example",
    )
    assert argv == ("\0".join(expected) + "\0").encode()
    assert b"registry-secret" not in argv
    assert stdin == secret.encode()


@pytest.mark.parametrize(
    "override",
    [_UNSET, "buildah"],
    ids=("unset", "invalid"),
)
@pytest.mark.parametrize(
    ("command", "stdin"),
    [
        ("container_runtime_build .", None),
        ("container_runtime_login user registry.example", "secret\n"),
        ("container_runtime_tag source target", None),
        ("container_runtime_push image:tag", None),
    ],
    ids=("build", "login", "tag", "push"),
)
def test_wrapper_rejects_unsupported_runtime_without_invocation(
    tmp_path: Path,
    override: str | object,
    command: str,
    stdin: str | None,
) -> None:
    result, argv, captured_stdin = _run_recorded_wrapper_without_selection(
        tmp_path,
        command,
        override=override,
        stdin=stdin,
    )

    assert result.returncode != 0
    assert "container, podman, or docker" in result.stderr
    assert argv == b""
    assert captured_stdin == b""


@pytest.mark.parametrize(
    ("command", "stdin"),
    [
        ("container_runtime_build .", None),
        ("container_runtime_login user registry.example", "registry-secret\n"),
        ("container_runtime_tag source target", None),
        ("container_runtime_push image:tag", None),
    ],
    ids=("build", "login", "tag", "push"),
)
def test_wrapper_propagates_runtime_failure(
    tmp_path: Path,
    command: str,
    stdin: str | None,
) -> None:
    result, argv, captured_stdin = _run_recorded_runtime_command(
        tmp_path,
        "container",
        command,
        stdin=stdin,
        extra_env={"RUNTIME_EXIT_CODE": "23"},
    )

    assert result.returncode == 23
    assert argv != b""
    assert captured_stdin == (stdin or "").encode()


@pytest.mark.parametrize(
    ("command", "usage"),
    [
        ("container_runtime_login user", "container_runtime_login USER REGISTRY"),
        (
            "container_runtime_login user registry.example extra",
            "container_runtime_login USER REGISTRY",
        ),
        ("container_runtime_tag source", "container_runtime_tag SOURCE TARGET"),
        (
            "container_runtime_tag source target extra",
            "container_runtime_tag SOURCE TARGET",
        ),
        ("container_runtime_push", "container_runtime_push IMAGE"),
        ("container_runtime_push image extra", "container_runtime_push IMAGE"),
    ],
    ids=(
        "login-missing",
        "login-extra",
        "tag-missing",
        "tag-extra",
        "push-missing",
        "push-extra",
    ),
)
def test_fixed_arity_wrapper_rejects_wrong_argument_count_without_invocation(
    tmp_path: Path,
    command: str,
    usage: str,
) -> None:
    result, argv, stdin = _run_recorded_runtime_command(
        tmp_path,
        "docker",
        command,
        stdin="",
    )

    assert result.returncode == 2
    assert f"Usage: {usage}" in result.stderr
    assert argv == b""
    assert stdin == b""


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


def test_selection_ignores_inherited_exported_runtime_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BASH_FUNC_container%%", "() {  return 0\n}")

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


def test_apple_container_readiness_does_not_start_when_already_ready(
    tmp_path: Path,
) -> None:
    invocation_log = tmp_path / "invocations.log"
    _install_runtime(
        tmp_path,
        "container",
        'printf "container %s\\n" "$*" >> "$INVOCATION_LOG"\nexit 0',
    )

    result = _run_adapter(
        tmp_path,
        "select_container_runtime && ensure_container_runtime_ready",
        extra_env={"INVOCATION_LOG": str(invocation_log)},
    )

    assert result.returncode == 0, result.stderr
    assert invocation_log.read_text(encoding="utf-8") == (
        "container system status\n"
    )


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


def test_apple_container_readiness_propagates_start_failure(tmp_path: Path) -> None:
    invocation_log = tmp_path / "invocations.log"
    _install_runtime(
        tmp_path,
        "container",
        """printf "container %s\\n" "$*" >> "$INVOCATION_LOG"
if [[ "$*" == "system status" ]]; then
    exit 1
fi
if [[ "$*" == "system start --disable-kernel-install" ]]; then
    exit 17
fi
exit 99""",
    )

    result = _run_adapter(
        tmp_path,
        "select_container_runtime && ensure_container_runtime_ready",
        extra_env={"INVOCATION_LOG": str(invocation_log)},
    )

    assert result.returncode != 0
    assert invocation_log.read_text(encoding="utf-8") == (
        "container system status\n"
        "container system start --disable-kernel-install\n"
    )
    assert "failed to start the Apple container service" in result.stderr


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
