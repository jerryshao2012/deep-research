#!/usr/bin/env bash

select_container_runtime() {
    local runtime

    if [[ ${CONTAINER_RUNTIME+x} == x ]]; then
        case "$CONTAINER_RUNTIME" in
            container | podman | docker) ;;
            *)
                printf '%s\n' \
                    'Error: CONTAINER_RUNTIME must be container, podman, or docker.' >&2
                return 1
                ;;
        esac

        if ! command -v "$CONTAINER_RUNTIME" >/dev/null 2>&1; then
            printf 'Error: requested container runtime %q is unavailable; choose container, podman, or docker.\n' \
                "$CONTAINER_RUNTIME" >&2
            return 1
        fi

        return 0
    fi

    for runtime in container podman docker; do
        if command -v "$runtime" >/dev/null 2>&1; then
            CONTAINER_RUNTIME="$runtime"
            return 0
        fi
    done

    printf '%s\n' \
        'Error: No supported container runtime found; install container, podman, or docker.' >&2
    return 1
}

ensure_container_runtime_ready() {
    case "${CONTAINER_RUNTIME-}" in
        container)
            if ! container system status >/dev/null 2>&1; then
                printf '%s\n' \
                    'Apple container service is not running; starting it now.' >&2
                if ! container system start --disable-kernel-install; then
                    printf '%s\n' \
                        'Error: failed to start the Apple container service.' >&2
                    return 1
                fi
            fi
            ;;
        podman)
            if ! podman info >/dev/null 2>&1; then
                printf '%s\n' \
                    'Error: daemonless Podman is not ready; check the Podman installation and configuration.' >&2
                return 1
            fi
            ;;
        docker)
            if ! docker info >/dev/null 2>&1; then
                printf '%s\n' \
                    'Error: Docker is not ready; start the Docker daemon and try again.' >&2
                return 1
            fi
            ;;
        *)
            printf '%s\n' \
                'Error: no supported container runtime is selected; run select_container_runtime first.' >&2
            return 1
            ;;
    esac

    return 0
}

_require_container_runtime() {
    case "${CONTAINER_RUNTIME-}" in
        container | podman | docker)
            return 0
            ;;
        *)
            printf '%s\n' \
                'Error: select container, podman, or docker before using container runtime commands.' >&2
            return 1
            ;;
    esac
}

container_runtime_build() {
    _require_container_runtime || return
    "$CONTAINER_RUNTIME" build "$@"
}

container_runtime_login() {
    _require_container_runtime || return
    if (( $# != 2 )); then
        printf '%s\n' 'Usage: container_runtime_login USER REGISTRY' >&2
        return 2
    fi

    local username="$1"
    local registry="$2"

    case "$CONTAINER_RUNTIME" in
        container)
            container registry login -u "$username" --password-stdin "$registry"
            ;;
        podman | docker)
            "$CONTAINER_RUNTIME" login --username "$username" --password-stdin "$registry"
            ;;
    esac
}

container_runtime_tag() {
    _require_container_runtime || return
    if (( $# != 2 )); then
        printf '%s\n' 'Usage: container_runtime_tag SOURCE TARGET' >&2
        return 2
    fi

    local source="$1"
    local target="$2"

    case "$CONTAINER_RUNTIME" in
        container)
            container image tag "$source" "$target"
            ;;
        podman | docker)
            "$CONTAINER_RUNTIME" tag "$source" "$target"
            ;;
    esac
}

container_runtime_push() {
    _require_container_runtime || return
    if (( $# != 1 )); then
        printf '%s\n' 'Usage: container_runtime_push IMAGE' >&2
        return 2
    fi

    local image="$1"

    case "$CONTAINER_RUNTIME" in
        container)
            container image push "$image"
            ;;
        podman | docker)
            "$CONTAINER_RUNTIME" push "$image"
            ;;
    esac
}
