# Container Runtime Auto-Detection Design

## Context

`build.sh` and `build-aws.sh` currently call Apple's `container` CLI directly for runtime startup, image builds, registry login, tagging, and pushing. Operators with Podman or Docker installed cannot use the build scripts without rewriting those commands.

## Goals

- Keep Apple `container` as first choice when multiple supported CLIs are installed.
- Fall back automatically to daemonless Podman, then Docker.
- Allow an explicit `CONTAINER_RUNTIME=container`, `CONTAINER_RUNTIME=podman`, or `CONTAINER_RUNTIME=docker` override.
- Preserve existing Azure, AWS, registry, image naming, build-context, and versioning behavior.
- Fail before a build with a clear action when no supported runtime is usable.

## Non-goals

- Buildah or container runtimes other than Apple `container`, Podman, and Docker.
- Starting a Podman service; supported Podman operation is daemonless.
- Automatically launching Docker Desktop or a Docker daemon.
- Changing cloud authentication, deployment, Dockerfiles, image names, or tags.
- Combining the Azure and AWS build scripts.

## Design

Add `scripts/container_runtime.sh` as the single adapter used by both build scripts. It exposes a small shell API:

- `select_container_runtime` validates an explicit override when present. Without an override, it checks `container`, `podman`, then `docker` with `command -v`. It stores only an allowlisted runtime name. Missing or invalid commands produce an actionable error.
- `ensure_container_runtime_ready` preserves `container system status` and `container system start --disable-kernel-install` behavior for Apple's runtime. Podman runs `podman info` as a daemonless usability check and never starts a service. Docker runs `docker info`; failure tells the operator to start the Docker daemon. The script does not attempt platform-specific Docker startup.
- `container_runtime_build` forwards quoted build arguments to the selected CLI.
- `container_runtime_login` reads the registry password from standard input. It maps to `container registry login -u ... --password-stdin`, `podman login --username ... --password-stdin`, or `docker login --username ... --password-stdin` without placing secrets in arguments or logs.
- `container_runtime_tag` maps to `container image tag`, `podman tag`, or `docker tag`.
- `container_runtime_push` maps to `container image push`, `podman push`, or `docker push`.

Each build script resolves its existing script directory, sources the adapter, selects and checks the runtime before its first runtime operation, and replaces direct `container` calls with adapter functions. Arguments and image references remain quoted. User-facing build labels become runtime-neutral where they currently say Docker or Container inconsistently.

## Runtime Selection

| Configuration and commands | Selected runtime |
|---|---|
| `CONTAINER_RUNTIME=container`, command exists | Apple `container` |
| `CONTAINER_RUNTIME=podman`, command exists | Podman |
| `CONTAINER_RUNTIME=docker`, command exists | Docker |
| Override names unsupported runtime | Fail with allowed values |
| No override; multiple commands exist | First available in `container`, `podman`, `docker` order |
| No override; only `container` exists | Apple `container` |
| No override; only `podman` exists | Podman |
| No override; only `docker` exists | Docker |
| No supported command exists | Fail with installation guidance |

An explicit override never silently falls back. This prevents a misspelled or unavailable requested runtime from producing an image through a different tool.

## Error Handling and Security

- Detection errors identify supported commands and the override variable.
- Podman readiness errors identify `podman info` as the failed daemonless usability check and never suggest starting a daemon.
- Docker daemon errors identify `docker info` as the failed readiness check and ask the operator to start Docker.
- Apple runtime retains its existing safe auto-start behavior.
- Registry passwords continue to travel through standard input.
- Runtime values are allowlisted before command execution; arbitrary command strings are rejected.
- Existing `set -e` and Azure `pipefail` behavior remain intact.

## Tests

Add focused tests for the adapter using temporary fake executables on `PATH`, avoiding cloud access and real container builds:

1. Apple `container` wins when all supported commands exist.
2. Podman is selected before Docker when Apple `container` is absent.
3. Docker is selected when Apple `container` and Podman are absent.
4. A valid explicit override wins and an unavailable or invalid override fails.
5. Missing runtimes fail with the expected guidance.
6. Readiness checks preserve Apple startup, keep Podman daemonless, and require a running Docker daemon.
7. Build, login, tag, and push wrappers emit the correct CLI/subcommand and preserve argument boundaries for each runtime.
8. Login tests verify secrets arrive through standard input and never appear in captured argument vectors.
9. Both build scripts source and use the adapter instead of direct runtime-specific image commands.
10. `bash -n` accepts the adapter and both build scripts.

Run the new focused test file plus existing Azure and AWS build-script contract tests. Documentation tests run after deployment guide updates.

## Documentation

Update Azure and AWS deployment prerequisites to state that Apple `container`, daemonless Podman, or Docker is supported; document selection order and the `CONTAINER_RUNTIME` override; and explain that Docker must already have a running daemon. Existing build invocations remain unchanged.

## Acceptance Criteria

- Existing Apple `container` users see unchanged selection and image behavior.
- Podman-only users can execute both build scripts without starting a Podman service.
- Docker-only users can execute both build scripts without editing them.
- Operators can force any supported runtime explicitly.
- No registry password is exposed on a command line.
- Focused runtime, Azure, AWS, shell-syntax, and documentation checks pass.
