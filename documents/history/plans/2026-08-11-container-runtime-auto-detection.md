# Container Runtime Auto-Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `../../../build.sh` and `../../../build-aws.sh` select Apple `container`, daemonless Podman, or Docker automatically and use the selected CLI for build, registry login, tag, and push operations.

**Architecture:** Add one source-only Bash adapter that owns runtime selection, readiness checks, and CLI-specific subcommand mappings. Both cloud build scripts load environment configuration before selection, source the adapter through an absolute script-relative path, and retain their existing registry, build-context, image, and version behavior.

**Tech Stack:** Bash, Apple `container`, Podman, Docker, pytest, Python `subprocess`

---

## File Map

- Create `../../../scripts/container_runtime.sh`: runtime allowlist, selection order, readiness checks, and build/login/tag/push wrappers.
- Create `../../../tests/test_container_runtime_scripts.py`: isolated adapter tests with temporary executable stubs plus build-script and documentation contracts.
- Modify `../../../build.sh`: load adapter, select runtime after environment files, and replace direct Apple `container` calls.
- Modify `../../../build-aws.sh`: load adapter, select runtime after `../../../env-aws.sh`, and replace direct Apple `container` calls.
- Modify `../../../tests/test_azure_persistence_scripts.py`: preserve staged-context contract while expecting the runtime-neutral build wrapper.
- Modify `../../deployment/azure/README.md`: supported runtimes, priority, override, and readiness guidance.
- Modify `../../deployment/aws.md`: same runtime guidance for the ECR build path.

## Runtime Contract

- Automatic selection uses command presence only: `container`, then `podman`, then `docker`.
- Readiness failure stops the build; it does not fall through to another installed runtime.
- `CONTAINER_RUNTIME` accepts exactly `container`, `podman`, or `docker`. An explicit but unavailable runtime fails without fallback.
- Apple `container` retains safe system auto-start. Podman runs `podman info` without a service. Docker requires `docker info` to succeed and is never auto-started.
- `container_runtime_login USER REGISTRY` consumes password bytes from standard input. Password must never become an argument.

### Task 1: Runtime Selection and Readiness

**Files:**
- Create: `../../../tests/test_container_runtime_scripts.py`
- Create: `../../../scripts/container_runtime.sh`

- [ ] **Step 1: Write failing selection tests**

Create the test harness and selection cases:

```python
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADAPTER = PROJECT_ROOT / "scripts" / "container_runtime.sh"


def _install_runtime(bin_dir: Path, name: str, body: str = "exit 0") -> None:
    executable = bin_dir / name
    executable.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)


def _run_adapter(
    bin_dir: Path,
    shell: str,
    *,
    runtime: str | None = None,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env.pop("CONTAINER_RUNTIME", None)
    if runtime is not None:
        env["CONTAINER_RUNTIME"] = runtime
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["/bin/bash", "-c", f'source "{ADAPTER}"; {shell}'],
        check=False,
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
    )


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        (("container", "podman", "docker"), "container"),
        (("podman", "docker"), "podman"),
        (("docker",), "docker"),
    ],
)
def test_select_container_runtime_uses_priority_order(
    tmp_path: Path,
    available: tuple[str, ...],
    expected: str,
) -> None:
    for runtime in available:
        _install_runtime(tmp_path, runtime)

    completed = _run_adapter(
        tmp_path,
        'select_container_runtime; printf "%s" "$CONTAINER_RUNTIME"',
    )

    assert completed.returncode == 0
    assert completed.stdout == expected


def test_select_container_runtime_honors_valid_override(tmp_path: Path) -> None:
    _install_runtime(tmp_path, "container")
    _install_runtime(tmp_path, "podman")

    completed = _run_adapter(
        tmp_path,
        'select_container_runtime; printf "%s" "$CONTAINER_RUNTIME"',
        runtime="podman",
    )

    assert completed.returncode == 0
    assert completed.stdout == "podman"


@pytest.mark.parametrize("runtime", ["", "buildah", "docker"])
def test_select_container_runtime_rejects_invalid_or_unavailable_override(
    tmp_path: Path,
    runtime: str,
) -> None:
    completed = _run_adapter(tmp_path, "select_container_runtime", runtime=runtime)

    assert completed.returncode != 0
    assert "container, podman, or docker" in completed.stderr


def test_select_container_runtime_fails_when_none_are_installed(tmp_path: Path) -> None:
    completed = _run_adapter(tmp_path, "select_container_runtime")

    assert completed.returncode != 0
    assert "No supported container runtime found" in completed.stderr
```

- [ ] **Step 2: Run selection tests and verify RED**

Run:

```bash
uv run pytest tests/test_container_runtime_scripts.py -q
```

Expected: FAIL because `../../../scripts/container_runtime.sh` does not exist.

- [ ] **Step 3: Implement runtime selection**

Create the adapter header and selector:

```bash
#!/bin/bash

select_container_runtime() {
  local candidate

  if [[ ${CONTAINER_RUNTIME+x} == x ]]; then
    case "$CONTAINER_RUNTIME" in
      container|podman|docker) ;;
      *)
        echo "Error: CONTAINER_RUNTIME must be container, podman, or docker." >&2
        return 1
        ;;
    esac
    if ! command -v "$CONTAINER_RUNTIME" >/dev/null 2>&1; then
      echo "Error: Requested container runtime '$CONTAINER_RUNTIME' is not installed; choose container, podman, or docker." >&2
      return 1
    fi
    return 0
  fi

  for candidate in container podman docker; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CONTAINER_RUNTIME="$candidate"
      return 0
    fi
  done

  echo "Error: No supported container runtime found; install container, podman, or docker." >&2
  return 1
}
```

- [ ] **Step 4: Run selection tests and verify GREEN**

Run: `uv run pytest tests/test_container_runtime_scripts.py -q`

Expected: PASS for all selection cases.

- [ ] **Step 5: Write failing readiness tests**

Extend the test file. Runtime stubs append their executable name and arguments to `RUNTIME_ARGV_LOG`:

```python
RUNTIME_LOGGER = r'''
printf '%s' "${0##*/}" >> "$RUNTIME_ARGV_LOG"
printf ' %s' "$@" >> "$RUNTIME_ARGV_LOG"
printf '\n' >> "$RUNTIME_ARGV_LOG"
if [[ "${0##*/} $*" == "container system status" ]]; then
  exit "${CONTAINER_STATUS_CODE:-0}"
fi
if [[ "${0##*/} $*" == "podman info" ]]; then
  exit "${PODMAN_INFO_CODE:-0}"
fi
if [[ "${0##*/} $*" == "docker info" ]]; then
  exit "${DOCKER_INFO_CODE:-0}"
fi
'''


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [("podman", "podman info\n"), ("docker", "docker info\n")],
)
def test_readiness_checks_daemonless_podman_and_docker(
    tmp_path: Path,
    runtime: str,
    expected: str,
) -> None:
    log = tmp_path / "argv.log"
    _install_runtime(tmp_path, runtime, RUNTIME_LOGGER)

    completed = _run_adapter(
        tmp_path,
        "select_container_runtime; ensure_container_runtime_ready",
        runtime=runtime,
        extra_env={"RUNTIME_ARGV_LOG": str(log)},
    )

    assert completed.returncode == 0
    assert log.read_text(encoding="utf-8") == expected


def test_apple_container_readiness_autostarts_system(tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    _install_runtime(tmp_path, "container", RUNTIME_LOGGER)

    completed = _run_adapter(
        tmp_path,
        "select_container_runtime; ensure_container_runtime_ready",
        runtime="container",
        extra_env={
            "RUNTIME_ARGV_LOG": str(log),
            "CONTAINER_STATUS_CODE": "1",
        },
    )

    assert completed.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        "container system status",
        "container system start --disable-kernel-install",
    ]


def test_failed_podman_readiness_does_not_fall_back_to_docker(tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    _install_runtime(tmp_path, "podman", RUNTIME_LOGGER)
    _install_runtime(tmp_path, "docker", RUNTIME_LOGGER)

    completed = _run_adapter(
        tmp_path,
        "select_container_runtime; ensure_container_runtime_ready",
        extra_env={"RUNTIME_ARGV_LOG": str(log), "PODMAN_INFO_CODE": "1"},
    )

    assert completed.returncode != 0
    assert log.read_text(encoding="utf-8") == "podman info\n"
    assert "daemonless Podman" in completed.stderr


def test_failed_docker_readiness_requires_running_daemon(tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    _install_runtime(tmp_path, "docker", RUNTIME_LOGGER)

    completed = _run_adapter(
        tmp_path,
        "select_container_runtime; ensure_container_runtime_ready",
        runtime="docker",
        extra_env={"RUNTIME_ARGV_LOG": str(log), "DOCKER_INFO_CODE": "1"},
    )

    assert completed.returncode != 0
    assert log.read_text(encoding="utf-8") == "docker info\n"
    assert "start the Docker daemon" in completed.stderr
```

- [ ] **Step 6: Run readiness tests and verify RED**

Run: `uv run pytest tests/test_container_runtime_scripts.py -q`

Expected: FAIL because `ensure_container_runtime_ready` is undefined.

- [ ] **Step 7: Implement readiness without fallback**

Append:

```bash
ensure_container_runtime_ready() {
  case "${CONTAINER_RUNTIME:-}" in
    container)
      if ! container system status >/dev/null 2>&1; then
        echo "Container system is not running. Auto-starting..."
        container system start --disable-kernel-install
      fi
      ;;
    podman)
      if ! podman info >/dev/null 2>&1; then
        echo "Error: daemonless Podman readiness check 'podman info' failed." >&2
        return 1
      fi
      ;;
    docker)
      if ! docker info >/dev/null 2>&1; then
        echo "Error: Docker readiness check 'docker info' failed; start the Docker daemon." >&2
        return 1
      fi
      ;;
    *)
      echo "Error: select a container runtime before checking readiness." >&2
      return 1
      ;;
  esac
}
```

- [ ] **Step 8: Run Task 1 tests and verify GREEN**

Run: `uv run pytest tests/test_container_runtime_scripts.py -q`

Expected: PASS.

- [ ] **Step 9: Commit selection and readiness**

```bash
git add scripts/container_runtime.sh tests/test_container_runtime_scripts.py
git commit -m "feat: detect supported container runtimes"
```

### Task 2: Runtime Command Wrappers

**Files:**
- Modify: `../../../tests/test_container_runtime_scripts.py`
- Modify: `../../../scripts/container_runtime.sh`

- [ ] **Step 1: Write failing command-mapping tests**

Add a stub that records NUL-separated arguments and stdin, then cover each runtime:

```python
COMMAND_LOGGER = r'''
printf '%s\0' "${0##*/}" "$@" > "$RUNTIME_ARGV_LOG"
if [[ " $* " == *" --password-stdin "* ]]; then
  /bin/cat > "$RUNTIME_STDIN_LOG"
else
  : > "$RUNTIME_STDIN_LOG"
fi
'''


@pytest.mark.parametrize(
    ("runtime", "shell", "expected"),
    [
        ("container", 'container_runtime_build --platform linux/amd64 -t "image name" .', ["container", "build", "--platform", "linux/amd64", "-t", "image name", "."]),
        ("podman", 'container_runtime_build --platform linux/amd64 -t "image name" .', ["podman", "build", "--platform", "linux/amd64", "-t", "image name", "."]),
        ("docker", 'container_runtime_build --platform linux/amd64 -t "image name" .', ["docker", "build", "--platform", "linux/amd64", "-t", "image name", "."]),
        ("container", 'container_runtime_tag "source image" "target image"', ["container", "image", "tag", "source image", "target image"]),
        ("podman", 'container_runtime_tag "source image" "target image"', ["podman", "tag", "source image", "target image"]),
        ("docker", 'container_runtime_tag "source image" "target image"', ["docker", "tag", "source image", "target image"]),
        ("container", 'container_runtime_push "registry/image:tag"', ["container", "image", "push", "registry/image:tag"]),
        ("podman", 'container_runtime_push "registry/image:tag"', ["podman", "push", "registry/image:tag"]),
        ("docker", 'container_runtime_push "registry/image:tag"', ["docker", "push", "registry/image:tag"]),
    ],
)
def test_runtime_wrappers_preserve_arguments(
    tmp_path: Path,
    runtime: str,
    shell: str,
    expected: list[str],
) -> None:
    argv_log = tmp_path / "argv.log"
    stdin_log = tmp_path / "stdin.log"
    _install_runtime(tmp_path, runtime, COMMAND_LOGGER)

    completed = _run_adapter(
        tmp_path,
        f"select_container_runtime; {shell}",
        runtime=runtime,
        extra_env={
            "RUNTIME_ARGV_LOG": str(argv_log),
            "RUNTIME_STDIN_LOG": str(stdin_log),
        },
    )

    assert completed.returncode == 0
    assert argv_log.read_bytes().split(b"\0")[:-1] == [
        value.encode() for value in expected
    ]


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        ("container", ["container", "registry", "login", "-u", "AWS", "--password-stdin", "registry.example"]),
        ("podman", ["podman", "login", "--username", "AWS", "--password-stdin", "registry.example"]),
        ("docker", ["docker", "login", "--username", "AWS", "--password-stdin", "registry.example"]),
    ],
)
def test_runtime_login_keeps_secret_on_stdin(
    tmp_path: Path,
    runtime: str,
    expected: list[str],
) -> None:
    argv_log = tmp_path / "argv.log"
    stdin_log = tmp_path / "stdin.log"
    _install_runtime(tmp_path, runtime, COMMAND_LOGGER)

    completed = _run_adapter(
        tmp_path,
        'select_container_runtime; container_runtime_login "AWS" "registry.example"',
        runtime=runtime,
        input_text="registry-secret\n",
        extra_env={
            "RUNTIME_ARGV_LOG": str(argv_log),
            "RUNTIME_STDIN_LOG": str(stdin_log),
        },
    )

    argv = argv_log.read_bytes().split(b"\0")[:-1]
    assert completed.returncode == 0
    assert argv == [value.encode() for value in expected]
    assert b"registry-secret" not in argv
    assert stdin_log.read_text(encoding="utf-8") == "registry-secret\n"
```

- [ ] **Step 2: Run wrapper tests and verify RED**

Run: `uv run pytest tests/test_container_runtime_scripts.py -q`

Expected: FAIL because wrapper functions are undefined.

- [ ] **Step 3: Implement wrapper mappings**

Append:

```bash
container_runtime_build() {
  "$CONTAINER_RUNTIME" build "$@"
}

container_runtime_login() {
  local username="$1"
  local registry="$2"

  case "$CONTAINER_RUNTIME" in
    container)
      container registry login -u "$username" --password-stdin "$registry"
      ;;
    podman|docker)
      "$CONTAINER_RUNTIME" login --username "$username" --password-stdin "$registry"
      ;;
  esac
}

container_runtime_tag() {
  local source_image="$1"
  local target_image="$2"

  case "$CONTAINER_RUNTIME" in
    container) container image tag "$source_image" "$target_image" ;;
    podman|docker) "$CONTAINER_RUNTIME" tag "$source_image" "$target_image" ;;
  esac
}

container_runtime_push() {
  local image="$1"

  case "$CONTAINER_RUNTIME" in
    container) container image push "$image" ;;
    podman|docker) "$CONTAINER_RUNTIME" push "$image" ;;
  esac
}
```

- [ ] **Step 4: Run wrapper tests and verify GREEN**

Run: `uv run pytest tests/test_container_runtime_scripts.py -q`

Expected: PASS with argument-boundary and stdin-secret assertions.

- [ ] **Step 5: Commit wrappers**

```bash
git add scripts/container_runtime.sh tests/test_container_runtime_scripts.py
git commit -m "feat: map container runtime commands"
```

### Task 3: Integrate Both Build Scripts

**Files:**
- Modify: `../../../tests/test_container_runtime_scripts.py`
- Modify: `tests/test_azure_persistence_scripts.py:13-26`
- Modify: `build.sh:5-8,46-59,85-100,112-151`
- Modify: `build-aws.sh:4-41,72-110`

- [ ] **Step 1: Write failing build-script contracts**

Add:

```python
@pytest.mark.parametrize(("script", "environment_source"), [("build.sh", "source ./env.sh"), ("build-aws.sh", "source ./env-aws.sh")])
def test_build_scripts_use_runtime_adapter_after_environment(
    script: str,
    environment_source: str,
) -> None:
    source = (PROJECT_ROOT / script).read_text(encoding="utf-8")

    assert 'source "$SCRIPT_DIR/scripts/container_runtime.sh"' in source
    assert source.index(environment_source) < source.index("select_container_runtime")
    assert "ensure_container_runtime_ready" in source
    assert "container_runtime_build" in source
    assert "container_runtime_login" in source
    assert "container_runtime_tag" in source
    assert "container_runtime_push" in source
    assert "container system status" not in source
    assert "container system start" not in source
    assert "container registry login" not in source
    assert "container image tag" not in source
    assert "container image push" not in source


@pytest.mark.parametrize("script", ["scripts/container_runtime.sh", "build.sh", "build-aws.sh"])
def test_container_runtime_shell_syntax(script: str) -> None:
    completed = subprocess.run(
        ["/bin/bash", "-n", str(PROJECT_ROOT / script)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
```

Update the Azure staged-context assertion to require:

```python
assert (
    'container_runtime_build --platform linux/amd64 -t "$FULL_IMAGE_NAME" '
    '"$BUILD_CONTEXT_DIR"'
    in source
)
```

- [ ] **Step 2: Run integration contracts and verify RED**

Run:

```bash
uv run pytest \
  tests/test_container_runtime_scripts.py \
  tests/test_azure_persistence_scripts.py::test_azure_build_stages_context_without_git_metadata \
  -q
```

Expected: FAIL because both build scripts still use raw Apple `container` commands.

- [ ] **Step 3: Load and select the adapter after environment configuration**

Near each script header, resolve the directory and source the adapter:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/scripts/container_runtime.sh"
```

Keep existing environment-file loading in place. Immediately after all environment sources in each script, add:

```bash
select_container_runtime
ensure_container_runtime_ready
echo "Using container runtime: $CONTAINER_RUNTIME"
```

Do not retry another runtime when readiness fails. Remove the later duplicate `SCRIPT_DIR` assignments and Apple-specific readiness blocks.

- [ ] **Step 4: Replace Azure runtime calls**

Use these exact runtime-neutral calls while preserving staged context and full Docker Hub image names:

```bash
printf '%s\n' "$DOCKER_HUB_PAT" \
  | container_runtime_login "$DOCKER_HUB_USERNAME" docker.io

container_runtime_build --platform linux/amd64 -t "$FULL_IMAGE_NAME" "$BUILD_CONTEXT_DIR"
container_runtime_push "$FULL_IMAGE_NAME"
container_runtime_tag "$FULL_IMAGE_NAME" "$VERSIONED_IMAGE_NAME"
container_runtime_push "$VERSIONED_IMAGE_NAME"
```

Keep `git ls-files`, tar staging, explicit `../../../.env.docker` copy, cleanup trap, and immediate post-build cleanup unchanged.

- [ ] **Step 5: Replace AWS runtime calls**

Preserve `--no-cache`, `linux/amd64`, `Dockerfile-aws`, ECR URL, and both tags:

```bash
container_runtime_build \
  --no-cache \
  --platform linux/amd64 \
  -f Dockerfile-aws \
  -t "$IMAGE_TAG" \
  .

aws ecr get-login-password --region "$AWS_REGION" \
  | container_runtime_login AWS "$ECR_URL"
container_runtime_push "$IMAGE_TAG"
container_runtime_tag "$IMAGE_TAG" "$VERSIONED_IMAGE_TAG"
container_runtime_push "$VERSIONED_IMAGE_TAG"
```

Change runtime-specific headings such as “Docker Image Build & Push” to “Container Image Build & Push.”

- [ ] **Step 6: Run integration contracts and verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 7: Run existing cloud script contracts**

Run:

```bash
uv run pytest tests/test_azure_persistence_scripts.py tests/test_aws_persistence_scripts.py -q
```

Expected: PASS. Fix only assertions made stale by the runtime adapter; do not alter unrelated persistence behavior.

- [ ] **Step 8: Commit build-script integration**

```bash
git add \
  build.sh \
  build-aws.sh \
  tests/test_container_runtime_scripts.py \
  tests/test_azure_persistence_scripts.py
git commit -m "feat: use detected runtime in build scripts"
```

### Task 4: Document Runtime Choice

**Files:**
- Modify: `../../../tests/test_container_runtime_scripts.py`
- Modify: `documents/deployment/azure/README.md:28-49,95-101`
- Modify: `documents/deployment/aws.md:21-39,83-89`

- [ ] **Step 1: Write failing documentation contracts**

Add:

```python
@pytest.mark.parametrize(
    "document",
    ["documents/deployment/azure/README.md", "documents/deployment/aws.md"],
)
def test_deployment_docs_explain_container_runtime_selection(document: str) -> None:
    source = (PROJECT_ROOT / document).read_text(encoding="utf-8")

    assert "container → podman → docker" in source
    assert "CONTAINER_RUNTIME" in source
    assert "daemonless" in source
    assert "docker info" in source
```

- [ ] **Step 2: Run documentation contract and verify RED**

Run:

```bash
uv run pytest \
  tests/test_container_runtime_scripts.py::test_deployment_docs_explain_container_runtime_selection \
  -q
```

Expected: FAIL because deployment guides describe only Apple `container`.

- [ ] **Step 3: Update Azure deployment guidance**

Replace the Apple-only prerequisite with:

```markdown
- one supported local container runtime: Apple `container`, daemonless Podman, or Docker. `build.sh` auto-detects in `container → podman → docker` order; set `CONTAINER_RUNTIME` to force one installed runtime;
```

Replace the Apple-only check with:

```bash
command -v container || command -v podman || command -v docker
```

Explain after the build command that Podman runs without a service, Docker must pass `docker info`, and examples can force selection with `CONTAINER_RUNTIME=podman ./build.sh` or `CONTAINER_RUNTIME=docker ./build.sh`.

- [ ] **Step 4: Update AWS deployment guidance**

Apply the same prerequisite and check wording to `../../../build-aws.sh`. Include override examples using `./build-aws.sh`. Do not change AWS authentication or ECR instructions.

- [ ] **Step 5: Run documentation tests and verify GREEN**

Run:

```bash
uv run pytest \
  tests/test_container_runtime_scripts.py \
  tests/test_documentation.py \
  -q
```

Expected: PASS.

- [ ] **Step 6: Commit documentation**

```bash
git add \
  documents/deployment/azure/README.md \
  documents/deployment/aws.md \
  tests/test_container_runtime_scripts.py
git commit -m "docs: describe supported build runtimes"
```

### Task 5: Final Verification

**Files:**
- Verify only; no planned modifications.

- [ ] **Step 1: Run shell syntax checks**

```bash
for script in scripts/container_runtime.sh build.sh build-aws.sh; do
  bash -n "$script"
done
```

Expected: exit 0 with no output.

- [ ] **Step 2: Run focused and regression tests**

```bash
uv run pytest \
  tests/test_container_runtime_scripts.py \
  tests/test_azure_persistence_scripts.py \
  tests/test_aws_persistence_scripts.py \
  tests/test_documentation.py \
  -q
```

Expected: all selected tests pass with zero failures.

- [ ] **Step 3: Run lint on changed Python tests**

```bash
uv run ruff check \
  tests/test_container_runtime_scripts.py \
  tests/test_azure_persistence_scripts.py
```

Expected: exit 0.

- [ ] **Step 4: Inspect final diff and whitespace**

```bash
git diff --check HEAD~4..HEAD
git status --short
```

Expected: no whitespace errors and no uncommitted task files.

- [ ] **Step 5: Record durable code areas**

Record `../../../scripts/container_runtime.sh`, `../../../build.sh`, and `../../../build-aws.sh` with concise descriptions in Code Context Engine after verification.
