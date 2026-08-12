#!/usr/bin/env bash
set -euo pipefail

METADATA_PATH="$PWD/.resolved-azure-endpoints.json"
METADATA_TEMP=""
RECORD=false
RECORD_EXPECTED_PATH=""

cleanup() {
    if [[ -n "$METADATA_TEMP" ]]; then
        /bin/rm -f "$METADATA_TEMP"
    fi
}
preserve_status_and_cleanup() {
    local status=$?
    cleanup
    exit "$status"
}
trap preserve_status_and_cleanup EXIT

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 2
}

shell_quote() {
    local value="$1"
    value="${value//\'/\'\"\'\"\'}"
    printf "'%s'" "$value"
}

emit_assignment() {
    printf '%s=' "$1"
    shell_quote "$2"
    printf '\n'
}

emit_all_assignments() {
    emit_assignment "AZURE_ENVIRONMENT_ID" "$AZURE_ENVIRONMENT_ID"
    emit_assignment "AZURE_ENVIRONMENT_DEFAULT_DOMAIN" "$AZURE_ENVIRONMENT_DEFAULT_DOMAIN"
    emit_assignment "BACKEND_APP_NAME" "$BACKEND_APP_NAME"
    emit_assignment "UI_APP_NAME" "$UI_APP_NAME"
    emit_assignment "BACKEND_URL" "$BACKEND_URL"
    emit_assignment "AZURE_UI_URL" "$AZURE_UI_URL"
    emit_assignment "FRONTEND_URLS" "$FRONTEND_URLS"
    emit_assignment "GOOGLE_CALLBACK_URL" "$GOOGLE_CALLBACK_URL"
    emit_assignment "GITHUB_CALLBACK_URL" "$GITHUB_CALLBACK_URL"
    emit_assignment "GITHUB_HOMEPAGE_URL" "$GITHUB_HOMEPAGE_URL"
    emit_assignment "CHANGED" "$CHANGED"
}

is_dns_name() {
    local value="$1"
    [[ ${#value} -le 253 ]] &&
        [[ "$value" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]
}

is_subscription_id() {
    [[ "$1" =~ ^[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}$ ]]
}

is_resource_group_name() {
    local value="$1"
    [[ ${#value} -ge 1 && ${#value} -le 90 ]] &&
        [[ "$value" =~ ^[A-Za-z0-9_().-]+$ ]] &&
        [[ "$value" != *. ]]
}

is_environment_name() {
    local value="$1"
    [[ ${#value} -ge 2 && ${#value} -le 60 ]] &&
        [[ "$value" =~ ^[a-z][a-z0-9-]*[a-z0-9]$ ]]
}

is_container_app_name() {
    local value="$1"
    [[ ${#value} -ge 2 && ${#value} -le 32 ]] &&
        [[ "$value" =~ ^[a-z][a-z0-9-]*[a-z0-9]$ ]] &&
        [[ "$value" != *--* ]]
}

case "$#" in
    0) ;;
    1)
        [[ "$1" == --record ]] || fail "unknown argument: $1"
        RECORD=true
        ;;
    2)
        [[ "$1" == --record-if-current ]] || fail "unknown argument: $1"
        RECORD=true
        RECORD_EXPECTED_PATH="$2"
        ;;
    *) fail "expected no arguments, --record, or --record-if-current FILE" ;;
esac

[[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]] || fail "AZURE_SUBSCRIPTION_ID must be nonempty"
[[ -n "${RESOURCE_GROUP:-}" ]] || fail "RESOURCE_GROUP must be nonempty"
[[ -n "${ENV_NAME:-}" ]] || fail "ENV_NAME must be nonempty"
[[ -n "${BACKEND_APP_NAME:-}" ]] || fail "BACKEND_APP_NAME must be nonempty"
[[ -n "${UI_APP_NAME:-}" ]] || fail "UI_APP_NAME must be nonempty"

is_subscription_id "$AZURE_SUBSCRIPTION_ID" ||
    fail "AZURE_SUBSCRIPTION_ID must be a UUID"
is_resource_group_name "$RESOURCE_GROUP" ||
    fail "RESOURCE_GROUP must use conservative Azure resource-group naming rules"
is_environment_name "$ENV_NAME" ||
    fail "ENV_NAME must use conservative Azure managed-environment naming rules"

is_container_app_name "$BACKEND_APP_NAME" ||
    fail "BACKEND_APP_NAME must be a valid Azure Container App name"
is_container_app_name "$UI_APP_NAME" ||
    fail "UI_APP_NAME must be a valid Azure Container App name"

AZ_QUERY_RESULT="$(
    az containerapp env show \
        --subscription "$AZURE_SUBSCRIPTION_ID" \
        --resource-group "$RESOURCE_GROUP" \
        --name "$ENV_NAME" \
        --query '[id,properties.defaultDomain,properties.provisioningState]' \
        --output tsv
    query_status=$?
    printf '\034%s' "$query_status"
)"
AZ_QUERY_STATUS="${AZ_QUERY_RESULT##*$'\034'}"
AZ_QUERY_OUTPUT="${AZ_QUERY_RESULT%$'\034'*}"
if [[ "$AZ_QUERY_STATUS" != 0 ]]; then
    printf '%s' "$AZ_QUERY_OUTPUT"
    exit "$AZ_QUERY_STATUS"
fi

AZ_QUERY_LINE="${AZ_QUERY_OUTPUT%$'\n'}"
if [[ "$AZ_QUERY_LINE" == *$'\n'* || "$AZ_QUERY_LINE" != *$'\t'* ]]; then
    fail "Azure environment query returned an invalid response"
fi
AZURE_ENVIRONMENT_ID="${AZ_QUERY_LINE%%$'\t'*}"
AZ_QUERY_REMAINDER="${AZ_QUERY_LINE#*$'\t'}"
if [[ "$AZ_QUERY_REMAINDER" != *$'\t'* ]]; then
    fail "Azure environment query returned an invalid response"
fi
AZURE_ENVIRONMENT_DEFAULT_DOMAIN="${AZ_QUERY_REMAINDER%%$'\t'*}"
PROVISIONING_STATE="${AZ_QUERY_REMAINDER#*$'\t'}"
if [[ "$PROVISIONING_STATE" == *$'\t'* ]]; then
    fail "Azure environment query returned an invalid response"
fi

if [[ "$PROVISIONING_STATE" != "Succeeded" ]]; then
    fail "Azure environment provisioning state must be Succeeded"
fi
EXPECTED_ENVIRONMENT_ID="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.App/managedEnvironments/${ENV_NAME}"
# Azure Resource Manager identifiers and resource names compare case-insensitively.
ACTUAL_ENVIRONMENT_ID_FOLDED="$(printf '%s' "$AZURE_ENVIRONMENT_ID" | /usr/bin/tr '[:upper:]' '[:lower:]')"
EXPECTED_ENVIRONMENT_ID_FOLDED="$(printf '%s' "$EXPECTED_ENVIRONMENT_ID" | /usr/bin/tr '[:upper:]' '[:lower:]')"
if [[ "$ACTUAL_ENVIRONMENT_ID_FOLDED" != "$EXPECTED_ENVIRONMENT_ID_FOLDED" ]]; then
    fail "Azure environment resource ID does not match requested subscription/resource group/environment"
fi
if ! is_dns_name "$AZURE_ENVIRONMENT_DEFAULT_DOMAIN"; then
    fail "Azure environment default domain is empty or invalid"
fi

BACKEND_URL="https://${BACKEND_APP_NAME}.${AZURE_ENVIRONMENT_DEFAULT_DOMAIN}"
AZURE_UI_URL="https://${UI_APP_NAME}.${AZURE_ENVIRONMENT_DEFAULT_DOMAIN}"
FRONTEND_URLS="${AZURE_UI_URL},https://bmo-deepagent-ui.vercel.app"
GOOGLE_CALLBACK_URL="${BACKEND_URL}/auth/callback/google"
GITHUB_CALLBACK_URL="${BACKEND_URL}/auth/callback/github"
GITHUB_HOMEPAGE_URL="$AZURE_UI_URL"

CHANGED=true
if [[ -e "$METADATA_PATH" ]]; then
    compare_status=0
    python3 -c '
import json
import sys

keys = (
    "azure_environment_id",
    "azure_environment_default_domain",
    "backend_app_name",
    "ui_app_name",
    "backend_url",
    "azure_ui_url",
    "frontend_urls",
    "google_callback_url",
    "github_callback_url",
    "github_homepage_url",
)
current = dict(zip(keys, sys.argv[2:]))
try:
    with open(sys.argv[1], encoding="utf-8") as existing_stream:
        existing = json.load(existing_stream)
except (OSError, UnicodeError, json.JSONDecodeError):
    sys.stderr.write(f"Error: metadata file is malformed: {sys.argv[1]}\n")
    sys.exit(65)
if (
    not isinstance(existing, dict)
    or set(existing) != set(current)
    or not all(isinstance(value, str) for value in existing.values())
):
    sys.stderr.write(f"Error: metadata file is malformed: {sys.argv[1]}\n")
    sys.exit(65)
sys.exit(0 if existing == current else 3)
' \
        "$METADATA_PATH" \
        "$AZURE_ENVIRONMENT_ID" \
        "$AZURE_ENVIRONMENT_DEFAULT_DOMAIN" \
        "$BACKEND_APP_NAME" \
        "$UI_APP_NAME" \
        "$BACKEND_URL" \
        "$AZURE_UI_URL" \
        "$FRONTEND_URLS" \
        "$GOOGLE_CALLBACK_URL" \
        "$GITHUB_CALLBACK_URL" \
        "$GITHUB_HOMEPAGE_URL" || compare_status=$?
    case "$compare_status" in
        0) CHANGED=false ;;
        3) CHANGED=true ;;
        *) exit "$compare_status" ;;
    esac
fi

CURRENT_OUTPUT="$(emit_all_assignments)"
if [[ -n "$RECORD_EXPECTED_PATH" ]]; then
    if python3 -c '
import os
import stat
import sys

path = sys.argv[1]
try:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise OSError("expected assignments must be a single-link regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        content = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            content += chunk
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise OSError("expected assignments changed while opening")
except OSError as exc:
    sys.stderr.write(f"Error: guarded record input is unsafe: {exc}\n")
    sys.exit(66)
expected = (sys.argv[2] + "\n").encode("utf-8")
sys.exit(0 if content == expected else 3)
' "$RECORD_EXPECTED_PATH" "$CURRENT_OUTPUT"; then
        :
    else
        status=$?
        if [[ "$status" == 3 ]]; then
            fail "current endpoints do not match expected assignments; metadata was not recorded"
        fi
        exit "$status"
    fi
fi

if [[ "$RECORD" == true ]]; then
    METADATA_TEMP="$(mktemp "${METADATA_PATH}.tmp.XXXXXX")"
    if python3 -c '
import json
import sys

keys = (
    "azure_environment_id",
    "azure_environment_default_domain",
    "backend_app_name",
    "ui_app_name",
    "backend_url",
    "azure_ui_url",
    "frontend_urls",
    "google_callback_url",
    "github_callback_url",
    "github_homepage_url",
)
json.dump(dict(zip(keys, sys.argv[1:])), sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
' \
        "$AZURE_ENVIRONMENT_ID" \
        "$AZURE_ENVIRONMENT_DEFAULT_DOMAIN" \
        "$BACKEND_APP_NAME" \
        "$UI_APP_NAME" \
        "$BACKEND_URL" \
        "$AZURE_UI_URL" \
        "$FRONTEND_URLS" \
        "$GOOGLE_CALLBACK_URL" \
        "$GITHUB_CALLBACK_URL" \
        "$GITHUB_HOMEPAGE_URL" >"$METADATA_TEMP"; then
        :
    else
        status=$?
        /bin/cat "$METADATA_TEMP"
        exit "$status"
    fi
    chmod 600 "$METADATA_TEMP"
    mv "$METADATA_TEMP" "$METADATA_PATH"
    METADATA_TEMP=""
fi

printf '%s\n' "$CURRENT_OUTPUT"

if [[ "$CHANGED" == true ]]; then
    printf '%s\n' \
        'ACTION REQUIRED: update and verify Google/GitHub OAuth provider settings before deployment.' >&2
else
    printf '%s\n' \
        'OAuth provider reminder: verify the following URLs remain configured.' >&2
fi
printf '%s\n' \
    "Google authorized redirect URI: $GOOGLE_CALLBACK_URL" \
    "GitHub authorization callback URL: $GITHUB_CALLBACK_URL" \
    "GitHub homepage / frontend origin: $GITHUB_HOMEPAGE_URL" >&2
