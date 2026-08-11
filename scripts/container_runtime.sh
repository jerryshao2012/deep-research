#!/usr/bin/env bash

CONTAINER_RUNTIME_COMMAND=

_resolve_container_runtime_executable() {
    local runtime="$1"
    local executable

    command -v "$runtime" >/dev/null 2>&1 || return 1
    executable="$(type -P "$runtime" 2>/dev/null)" || return 1
    [[ -n "$executable" && -f "$executable" && -x "$executable" ]] || return 1
    printf '%s\n' "$executable"
}

select_container_runtime() {
    local runtime
    local executable

    CONTAINER_RUNTIME_COMMAND=

    if [[ ${CONTAINER_RUNTIME+x} == x ]]; then
        case "$CONTAINER_RUNTIME" in
            container | podman | docker) ;;
            *)
                printf '%s\n' \
                    'Error: CONTAINER_RUNTIME must be container, podman, or docker.' >&2
                return 1
                ;;
        esac

        if ! executable="$(_resolve_container_runtime_executable "$CONTAINER_RUNTIME")"; then
            printf 'Error: requested container runtime %q is unavailable; choose container, podman, or docker.\n' \
                "$CONTAINER_RUNTIME" >&2
            return 1
        fi

        CONTAINER_RUNTIME_COMMAND="$executable"
        return 0
    fi

    for runtime in container podman docker; do
        if executable="$(_resolve_container_runtime_executable "$runtime")"; then
            CONTAINER_RUNTIME="$runtime"
            CONTAINER_RUNTIME_COMMAND="$executable"
            return 0
        fi
    done

    printf '%s\n' \
        'Error: No supported container runtime found; install container, podman, or docker.' >&2
    return 1
}

ensure_container_runtime_ready() {
    _require_container_runtime || return

    case "${CONTAINER_RUNTIME-}" in
        container)
            if ! "$CONTAINER_RUNTIME_COMMAND" system status >/dev/null 2>&1; then
                printf '%s\n' \
                    'Apple container service is not running; starting it now.' >&2
                if ! "$CONTAINER_RUNTIME_COMMAND" system start --disable-kernel-install; then
                    printf '%s\n' \
                        'Error: failed to start the Apple container service.' >&2
                    return 1
                fi
            fi
            ;;
        podman)
            if ! "$CONTAINER_RUNTIME_COMMAND" info >/dev/null 2>&1; then
                printf '%s\n' \
                    'Error: podman info failed; daemonless Podman is not ready. Check the Podman installation and configuration.' >&2
                return 1
            fi
            ;;
        docker)
            if ! "$CONTAINER_RUNTIME_COMMAND" info >/dev/null 2>&1; then
                printf '%s\n' \
                    'Error: docker info failed; start the Docker daemon and try again.' >&2
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
        container | podman | docker) ;;
        *)
            printf '%s\n' \
                'Error: no container runtime is selected; select container, podman, or docker before using container runtime commands.' >&2
            return 1
            ;;
    esac

    if [[ -z "${CONTAINER_RUNTIME_COMMAND-}" \
        || ! -f "$CONTAINER_RUNTIME_COMMAND" \
        || ! -x "$CONTAINER_RUNTIME_COMMAND" ]]; then
        printf '%s\n' \
            'Error: no resolved executable is selected; select container, podman, or docker before using container runtime commands.' >&2
        return 1
    fi

    return 0
}

container_runtime_build() {
    _require_container_runtime || return
    "$CONTAINER_RUNTIME_COMMAND" build "$@"
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
            "$CONTAINER_RUNTIME_COMMAND" registry login -u "$username" --password-stdin "$registry"
            ;;
        podman | docker)
            "$CONTAINER_RUNTIME_COMMAND" login --username "$username" --password-stdin "$registry"
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
            "$CONTAINER_RUNTIME_COMMAND" image tag "$source" "$target"
            ;;
        podman | docker)
            "$CONTAINER_RUNTIME_COMMAND" tag "$source" "$target"
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
            "$CONTAINER_RUNTIME_COMMAND" image push "$image"
            ;;
        podman | docker)
            "$CONTAINER_RUNTIME_COMMAND" push "$image"
            ;;
    esac
}
