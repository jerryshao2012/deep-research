"""Passkey configuration and trusted-BFF API contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from webapp import passkeys as passkeys_module
from webapp.auth_store import CredentialRecord, SQLiteAuthStore
from webapp.passkeys import (
    InvalidPasskeyError,
    PasskeyConfig,
    PasskeyConfigurationError,
    PasskeyService,
    ReauthenticationRequired,
    _validate_origin,
    register_passkey_routes,
    safe_oauth_return_path,
)


def _enabled_env(tmp_path, **overrides: str) -> dict[str, str]:
    values = {
        "PASSKEY_ENABLED": "true",
        "PASSKEY_RP_ID": "app.example.com",
        "PASSKEY_RP_NAME": "BMO Deep Agent",
        "PASSKEY_ORIGINS": "https://app.example.com",
        "PASSKEY_PROXY_ID": "web-bff",
        "PASSKEY_PROXY_SECRET": "proxy-secret-with-32-bytes-minimum",
        "GOOGLE_CLIENT_ID": "google-client",
        "GOOGLE_CLIENT_SECRET": "google-secret",
        "OAUTH_SECRET_KEY": "oauth-session-secret-with-32-bytes-minimum",
        "AUTH_STORE_TYPE": "sqlite",
        "SQLITE_DB_PATH": str(tmp_path / "auth.db"),
    }
    values.update(overrides)
    return values


def _multi_rp_env(tmp_path, **overrides: str) -> dict[str, str]:
    values = _enabled_env(tmp_path)
    values.pop("PASSKEY_RP_ID")
    values.update(
        {
            "PASSKEY_RP_IDS": (
                "bmo-deepagent-ui-0312.azurewebsites.net,bmo-deepagent-ui.vercel.app"
            ),
            "PASSKEY_ORIGINS": (
                "https://bmo-deepagent-ui-0312.azurewebsites.net,"
                "https://bmo-deepagent-ui.vercel.app"
            ),
        }
    )
    values.update(overrides)
    return values


def _derived_frontend_env(tmp_path, **overrides: str) -> dict[str, str]:
    values = _enabled_env(tmp_path)
    values.pop("PASSKEY_RP_ID")
    values.pop("PASSKEY_ORIGINS")
    values.update(
        {
            "PASSKEY_DERIVE_FROM_FRONTEND_URLS": "true",
            "FRONTEND_URLS": (
                "https://ui.example.com,https://bmo-deepagent-ui.vercel.app"
            ),
        }
    )
    values.update(overrides)
    return values


NUMERIC_ENDING_HOSTS = (
    "127.1",
    "2130706433",
    "0x7f.1",
    "127.0x0.0.1",
    "1.2.3.0x4",
    "0177.0.0.1",
    "0x7f000001",
    "99999999999999999999999999999999999999999999999999",
    "127\u30021",
    "0x7f\u30021",
    "example.1",
    "example.1.",
    "08.1",
    "1.2.3.4.5",
    "example.0x",
    "example.0xgg",
)


def test_passkeys_default_disabled_needs_no_secrets():
    config = PasskeyConfig.from_environ({})

    assert config.enabled is False
    assert config.challenge_ttl_seconds == 300
    assert config.recent_auth_seconds == 600


def test_disabled_passkeys_ignore_incomplete_or_malformed_optional_settings():
    config = PasskeyConfig.from_environ(
        {
            "PASSKEY_ENABLED": "false",
            "PASSKEY_CHALLENGE_TTL_SECONDS": "not-a-number",
            "PASSKEY_ORIGINS": "http://production.example.com/path",
        }
    )

    assert config == PasskeyConfig(enabled=False)


def test_derive_from_frontend_urls_maps_each_origin_to_its_own_hostname(tmp_path):
    config = PasskeyConfig.from_environ(_derived_frontend_env(tmp_path))

    assert config.origins == (
        "https://ui.example.com",
        "https://bmo-deepagent-ui.vercel.app",
    )
    assert config.rp_ids == (
        "ui.example.com",
        "bmo-deepagent-ui.vercel.app",
    )
    assert config.origin_rp_ids == (
        ("https://ui.example.com", "ui.example.com"),
        (
            "https://bmo-deepagent-ui.vercel.app",
            "bmo-deepagent-ui.vercel.app",
        ),
    )


def test_derive_from_frontend_urls_normalizes_root_path_and_default_port(tmp_path):
    config = PasskeyConfig.from_environ(
        _derived_frontend_env(
            tmp_path,
            FRONTEND_URLS="https://UI.example.com:443/",
        )
    )

    assert config.origins == ("https://ui.example.com",)
    assert config.origin_rp_ids == (("https://ui.example.com", "ui.example.com"),)


def test_derive_from_frontend_urls_accepts_localhost_development(tmp_path):
    config = PasskeyConfig.from_environ(
        _derived_frontend_env(tmp_path, FRONTEND_URLS="http://localhost:3000")
    )

    assert config.origin_rp_ids == (("http://localhost:3000", "localhost"),)
    assert config.oauth_cookie_secure is False


def test_derive_flag_false_preserves_explicit_passkey_configuration(tmp_path):
    config = PasskeyConfig.from_environ(
        _enabled_env(
            tmp_path,
            PASSKEY_DERIVE_FROM_FRONTEND_URLS="false",
            FRONTEND_URLS="https://ignored.example.net",
        )
    )

    assert config.origins == ("https://app.example.com",)
    assert config.rp_ids == ("app.example.com",)


def test_derive_flag_does_not_enable_passkeys():
    config = PasskeyConfig.from_environ(
        {
            "PASSKEY_DERIVE_FROM_FRONTEND_URLS": "true",
            "FRONTEND_URLS": "https://ui.example.com",
        }
    )

    assert config == PasskeyConfig(enabled=False)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("PASSKEY_RP_ID", ""),
        ("PASSKEY_RP_ID", "ui.example.com"),
        ("PASSKEY_RP_IDS", ""),
        ("PASSKEY_RP_IDS", "ui.example.com"),
        ("PASSKEY_ORIGINS", ""),
        ("PASSKEY_ORIGINS", "https://ui.example.com"),
    ],
)
def test_derive_from_frontend_urls_rejects_present_explicit_settings(
    tmp_path, setting, value
):
    env = _derived_frontend_env(tmp_path)
    env[setting] = value

    with pytest.raises(PasskeyConfigurationError, match="must not be set"):
        PasskeyConfig.from_environ(env)


def test_derive_from_frontend_urls_requires_frontend_urls(tmp_path):
    env = _derived_frontend_env(tmp_path)
    env.pop("FRONTEND_URLS")

    with pytest.raises(PasskeyConfigurationError, match="FRONTEND_URLS"):
        PasskeyConfig.from_environ(env)


@pytest.mark.parametrize(
    "frontend_urls",
    [
        "",
        " ",
        ",https://ui.example.com",
        "https://ui.example.com,",
        "https://ui.example.com,,https://other.example.com",
    ],
)
def test_derive_from_frontend_urls_rejects_empty_tokens(tmp_path, frontend_urls):
    with pytest.raises(PasskeyConfigurationError, match="FRONTEND_URLS"):
        PasskeyConfig.from_environ(
            _derived_frontend_env(tmp_path, FRONTEND_URLS=frontend_urls)
        )


@pytest.mark.parametrize(
    "frontend_urls",
    [
        "https://ui.example.com,https://ui.example.com/",
        "https://ui.example.com:443,https://UI.example.com",
        "https://b\u00fccher.com,https://xn--bcher-kva.com",
    ],
)
def test_derive_from_frontend_urls_rejects_duplicate_normalized_origins(
    tmp_path, frontend_urls
):
    with pytest.raises(PasskeyConfigurationError, match="duplicate"):
        PasskeyConfig.from_environ(
            _derived_frontend_env(tmp_path, FRONTEND_URLS=frontend_urls)
        )


@pytest.mark.parametrize(
    "frontend_url",
    [
        "https://user:password@ui.example.com",
        "https://ui.example.com/not-root",
        "https://ui.example.com?query=yes",
        "https://ui.example.com#fragment",
        "https://*.example.com",
        "http://ui.example.com",
        "https://bad_label.example.com",
    ],
)
def test_derive_from_frontend_urls_rejects_noncanonical_origins(tmp_path, frontend_url):
    with pytest.raises(PasskeyConfigurationError) as exc_info:
        PasskeyConfig.from_environ(
            _derived_frontend_env(tmp_path, FRONTEND_URLS=frontend_url)
        )

    message = str(exc_info.value)
    assert "FRONTEND_URLS" in message
    assert "PASSKEY_ORIGINS" not in message


def test_explicit_origin_validation_keeps_passkey_origins_error_label(tmp_path):
    with pytest.raises(PasskeyConfigurationError) as exc_info:
        PasskeyConfig.from_environ(
            _enabled_env(
                tmp_path,
                PASSKEY_ORIGINS="https://user:password@ui.example.com",
            )
        )

    assert str(exc_info.value) == (
        "PASSKEY_ORIGINS entries must be exact origins without credentials, "
        "paths, queries, or fragments"
    )


def test_plural_rp_ids_map_each_requested_origin(tmp_path):
    config = PasskeyConfig.from_environ(_multi_rp_env(tmp_path))

    assert config.rp_ids == (
        "bmo-deepagent-ui-0312.azurewebsites.net",
        "bmo-deepagent-ui.vercel.app",
    )
    assert (
            config.rp_id_for_origin("https://bmo-deepagent-ui-0312.azurewebsites.net")
            == "bmo-deepagent-ui-0312.azurewebsites.net"
    )
    assert (
            config.rp_id_for_origin("https://bmo-deepagent-ui.vercel.app")
            == "bmo-deepagent-ui.vercel.app"
    )
    with pytest.raises(PasskeyConfigurationError, match="singular"):
        _ = config.rp_id


def test_rp_mapping_uses_longest_compatible_suffix(tmp_path):
    config = PasskeyConfig.from_environ(
        _multi_rp_env(
            tmp_path,
            PASSKEY_RP_IDS="example.com,login.example.com",
            PASSKEY_ORIGINS=("https://account.example.com,https://login.example.com"),
        )
    )

    assert config.rp_id_for_origin("https://login.example.com") == ("login.example.com")
    assert config.rp_id_for_origin("https://account.example.com") == "example.com"


def test_singular_rp_id_is_absent_only_fallback(tmp_path):
    config = PasskeyConfig.from_environ(
        _enabled_env(
            tmp_path,
            PASSKEY_RP_ID="example.com",
            PASSKEY_ORIGINS="https://login.example.com",
        )
    )

    assert config.rp_ids == ("example.com",)
    assert config.rp_id == "example.com"
    assert config.rp_id_for_origin("https://login.example.com") == "example.com"


def test_disabled_mode_ignores_malformed_rp_settings():
    config = PasskeyConfig.from_environ(
        {
            "PASSKEY_ENABLED": "false",
            "PASSKEY_RP_ID": "https://bad.example/path",
            "PASSKEY_RP_IDS": ",*,\ud800",
        }
    )

    assert config == PasskeyConfig(enabled=False)


@pytest.mark.parametrize(
    "rp_ids",
    ["", ",host.example", "host.example,", "host.example,,other.example"],
)
def test_present_plural_rejects_empty_tokens(tmp_path, rp_ids):
    with pytest.raises(PasskeyConfigurationError, match="PASSKEY_RP_IDS"):
        PasskeyConfig.from_environ(_multi_rp_env(tmp_path, PASSKEY_RP_IDS=rp_ids))


@pytest.mark.parametrize(
    ("singular", "plural"),
    [
        ("app.example.com", "other.example.com"),
        ("", "other.example.com"),
        ("app.example.com", ""),
        ("", ""),
    ],
)
def test_both_singular_and_plural_rp_variables_are_rejected_even_when_one_is_empty(
        tmp_path, singular, plural
):
    env = _enabled_env(tmp_path, PASSKEY_RP_ID=singular, PASSKEY_RP_IDS=plural)

    with pytest.raises(PasskeyConfigurationError, match="must not both be set"):
        PasskeyConfig.from_environ(env)


@pytest.mark.parametrize(
    "rp_ids",
    [
        "EXAMPLE.com.,example.com",
        "b\u00fccher.com,xn--bcher-kva.com",
    ],
)
def test_rp_ids_reject_duplicates_after_normalization(tmp_path, rp_ids):
    with pytest.raises(PasskeyConfigurationError, match="duplicate"):
        PasskeyConfig.from_environ(
            _multi_rp_env(
                tmp_path,
                PASSKEY_RP_IDS=rp_ids,
                PASSKEY_ORIGINS="https://example.com",
            )
        )


@pytest.mark.parametrize(
    "rp_ids",
    [
        "https://app.example.com",
        "app.example.com:443",
        "app.example.com/path",
        "*.example.com",
        ".example.com",
        "app.example.com..",
        f"{'a' * 64}.example.com",
        "bad_label.example.com",
        ".".join(["a" * 63] * 4),
        "\ud800.example.com",
    ],
)
def test_rp_ids_reject_noncanonical_dns_values(tmp_path, rp_ids):
    with pytest.raises(PasskeyConfigurationError, match="PASSKEY_RP_IDS"):
        PasskeyConfig.from_environ(_multi_rp_env(tmp_path, PASSKEY_RP_IDS=rp_ids))


@pytest.mark.parametrize("rp_id", ["com", "vercel.app", "azurewebsites.net"])
def test_rp_ids_reject_public_suffixes(tmp_path, rp_id):
    with pytest.raises(PasskeyConfigurationError, match="registrable"):
        PasskeyConfig.from_environ(_multi_rp_env(tmp_path, PASSKEY_RP_IDS=rp_id))


@pytest.mark.parametrize(
    ("rp_id", "origin"),
    [
        (
                "bmo-deepagent-ui-0312.azurewebsites.net",
                "https://bmo-deepagent-ui-0312.azurewebsites.net",
        ),
        (
                "bmo-deepagent-ui.vercel.app",
                "https://bmo-deepagent-ui.vercel.app",
        ),
        ("localhost", "http://localhost:3000"),
    ],
)
def test_rp_ids_accept_tenant_hosts_and_localhost(tmp_path, rp_id, origin):
    config = PasskeyConfig.from_environ(
        _multi_rp_env(tmp_path, PASSKEY_RP_IDS=rp_id, PASSKEY_ORIGINS=origin)
    )

    assert config.rp_ids == (rp_id,)
    assert config.rp_id_for_origin(origin) == rp_id


@pytest.mark.parametrize(
    ("configured_origin", "browser_origin"),
    [
        ("https://b\u00fccher.com", "https://xn--bcher-kva.com"),
        ("https://b\u00fccher.com:8443", "https://xn--bcher-kva.com:8443"),
    ],
)
def test_unicode_origin_hostname_matches_ascii_browser_origin(
        tmp_path, configured_origin, browser_origin
):
    config = PasskeyConfig.from_environ(
        _multi_rp_env(
            tmp_path,
            PASSKEY_RP_IDS="b\u00fccher.com",
            PASSKEY_ORIGINS=configured_origin,
        )
    )

    assert config.rp_ids == ("xn--bcher-kva.com",)
    assert config.origins == (browser_origin,)
    assert config.rp_id_for_origin(browser_origin) == "xn--bcher-kva.com"


@pytest.mark.parametrize(
    ("rp_id", "configured_origin", "browser_origin"),
    [
        ("example.com", "https://app.example.com:443", "https://app.example.com"),
        ("localhost", "http://localhost:80", "http://localhost"),
    ],
)
def test_origin_normalization_strips_browser_default_ports(
        tmp_path, rp_id, configured_origin, browser_origin
):
    config = PasskeyConfig.from_environ(
        _enabled_env(
            tmp_path,
            PASSKEY_RP_ID=rp_id,
            PASSKEY_ORIGINS=configured_origin,
        )
    )

    assert config.origins == (browser_origin,)
    assert config.rp_id_for_origin(browser_origin) == rp_id


def test_origin_normalization_preserves_single_dns_trailing_dot(tmp_path):
    browser_origin = "https://app.example.com."
    config = PasskeyConfig.from_environ(
        _enabled_env(
            tmp_path,
            PASSKEY_RP_ID="example.com",
            PASSKEY_ORIGINS=browser_origin,
        )
    )

    assert config.origins == (browser_origin,)
    assert config.rp_id_for_origin(browser_origin) == "example.com"


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.com..",
        "https://app.example.com...",
    ],
)
def test_origin_rejects_multiple_trailing_dns_dots(tmp_path, origin):
    with pytest.raises(PasskeyConfigurationError, match="PASSKEY_ORIGINS"):
        PasskeyConfig.from_environ(_enabled_env(tmp_path, PASSKEY_ORIGINS=origin))


@pytest.mark.parametrize("origin", ["https://127.1", "https://127.00.0.1"])
def test_origin_rejects_legacy_noncanonical_ipv4_spellings(origin):
    with pytest.raises(PasskeyConfigurationError, match="origin"):
        _validate_origin(origin)


@pytest.mark.parametrize("hostname", NUMERIC_ENDING_HOSTS)
def test_origin_rejects_numeric_ending_hostnames(hostname):
    with pytest.raises(PasskeyConfigurationError, match="origin"):
        _validate_origin(f"https://{hostname}")


@pytest.mark.parametrize("rp_id", NUMERIC_ENDING_HOSTS)
def test_rp_id_rejects_numeric_ending_hostnames(tmp_path, rp_id):
    with pytest.raises(PasskeyConfigurationError, match="PASSKEY_RP_ID"):
        PasskeyConfig.from_environ(_enabled_env(tmp_path, PASSKEY_RP_ID=rp_id))


@pytest.mark.parametrize("hostname", ["v1.example", "1.example"])
def test_nonnumeric_final_dns_label_is_not_mistaken_for_ipv4(tmp_path, hostname):
    config = PasskeyConfig.from_environ(
        _enabled_env(
            tmp_path,
            PASSKEY_RP_ID=hostname,
            PASSKEY_ORIGINS=f"https://{hostname}",
        )
    )

    assert config.rp_id == hostname
    assert config.rp_id_for_origin(f"https://{hostname}") == hostname


def test_canonical_ipv4_and_normal_tenant_origins_remain_valid(tmp_path):
    assert _validate_origin("https://127.0.0.1") == (
        "https://127.0.0.1",
        "127.0.0.1",
    )
    config = PasskeyConfig.from_environ(_multi_rp_env(tmp_path))
    assert config.rp_id_for_origin("https://bmo-deepagent-ui.vercel.app") == (
        "bmo-deepagent-ui.vercel.app"
    )


@pytest.mark.parametrize("rp_id", ["127.0.0.1", "::1"])
def test_rp_id_rejects_canonical_ip_addresses(tmp_path, rp_id):
    with pytest.raises(PasskeyConfigurationError, match="PASSKEY_RP_ID"):
        PasskeyConfig.from_environ(_enabled_env(tmp_path, PASSKEY_RP_ID=rp_id))


def test_every_origin_must_match_a_configured_rp_id(tmp_path):
    with pytest.raises(PasskeyConfigurationError, match="Every passkey origin"):
        PasskeyConfig.from_environ(
            _multi_rp_env(
                tmp_path,
                PASSKEY_RP_IDS="app.example.com",
                PASSKEY_ORIGINS="https://other.example.net",
            )
        )


def test_every_rp_id_must_be_used_by_an_origin(tmp_path):
    with pytest.raises(PasskeyConfigurationError, match="Every passkey RP ID"):
        PasskeyConfig.from_environ(
            _multi_rp_env(
                tmp_path,
                PASSKEY_RP_IDS="example.com,unused.example.net",
                PASSKEY_ORIGINS="https://app.example.com",
            )
        )


@pytest.mark.parametrize(
    ("rp_id", "origins"),
    [
        ("app.example.com", "https://app.example.com"),
        ("localhost", "http://localhost:3000"),
    ],
)
def test_enabled_config_accepts_https_and_localhost_origins(tmp_path, rp_id, origins):
    env = _enabled_env(tmp_path, PASSKEY_RP_ID=rp_id, PASSKEY_ORIGINS=origins)

    config = PasskeyConfig.from_environ(env)

    assert config.enabled is True
    assert config.origins == (origins,)
    assert config.authenticated_rate_limit == 20
    assert config.anonymous_rate_limit == 300
    assert config.oauth_cookie_secure is origins.startswith("https://")


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("PASSKEY_RP_ID", "", "PASSKEY_RP_ID"),
        ("PASSKEY_ORIGINS", "", "PASSKEY_ORIGINS"),
        ("PASSKEY_ORIGINS", "http://app.example.com", "HTTPS"),
        ("PASSKEY_PROXY_ID", "", "PASSKEY_PROXY_ID"),
        ("PASSKEY_PROXY_SECRET", "short", "PASSKEY_PROXY_SECRET"),
        ("GOOGLE_CLIENT_SECRET", "", "OAuth"),
        ("OAUTH_SECRET_KEY", "short", "OAUTH_SECRET_KEY"),
        ("SQLITE_DB_PATH", ":memory:", "durable"),
    ],
)
def test_enabled_config_fails_closed_for_incomplete_production_settings(
        tmp_path, key, value, message
):
    env = _enabled_env(tmp_path, **{key: value})

    with pytest.raises(PasskeyConfigurationError, match=message):
        PasskeyConfig.from_environ(env)


@pytest.mark.parametrize(
    "oauth_secret",
    [
        "your-secret-key-for-session-signing",
        "replace-with-a-secure-random-secret",
        "<different-at-least-32-random-bytes>",
        "generate-a-random-secret-string-here",
        "a" * 32,
    ],
)
def test_enabled_config_rejects_predictable_oauth_session_secrets(
        tmp_path, oauth_secret
):
    with pytest.raises(PasskeyConfigurationError, match="OAUTH_SECRET_KEY"):
        PasskeyConfig.from_environ(
            _enabled_env(tmp_path, OAUTH_SECRET_KEY=oauth_secret)
        )


def test_enabled_config_rejects_documented_proxy_secret_placeholder(tmp_path):
    with pytest.raises(PasskeyConfigurationError, match="PASSKEY_PROXY_SECRET"):
        PasskeyConfig.from_environ(
            _enabled_env(
                tmp_path,
                PASSKEY_PROXY_SECRET="<at-least-32-random-bytes>",
            )
        )


def test_origin_must_be_exact_without_paths_queries_or_fragments(tmp_path):
    for origin in (
            "https://app.example.com/path",
            "https://app.example.com?query=yes",
            "https://app.example.com#fragment",
            "https://user@app.example.com",
    ):
        with pytest.raises(PasskeyConfigurationError, match="origin"):
            PasskeyConfig.from_environ(_enabled_env(tmp_path, PASSKEY_ORIGINS=origin))


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.com:",
        "https://app.example.com:not-a-port",
        "https://app.example.com:65536",
    ],
)
def test_origin_rejects_invalid_ports(tmp_path, origin):
    with pytest.raises(PasskeyConfigurationError, match="origin"):
        PasskeyConfig.from_environ(_enabled_env(tmp_path, PASSKEY_ORIGINS=origin))


def test_origin_normalization_preserves_ipv6_loopback_and_port():
    assert _validate_origin("http://[::1]:3000") == (
        "http://[::1]:3000",
        "::1",
    )


def test_rp_id_must_equal_or_suffix_match_every_origin_hostname(tmp_path):
    with pytest.raises(PasskeyConfigurationError, match="RP ID"):
        PasskeyConfig.from_environ(
            _enabled_env(
                tmp_path,
                PASSKEY_RP_ID="other.example.com",
                PASSKEY_ORIGINS="https://app.example.com",
            )
        )


def test_config_rejects_invalid_rate_and_ttl_values(tmp_path):
    for key, value in (
            ("PASSKEY_CHALLENGE_TTL_SECONDS", "0"),
            ("PASSKEY_RECENT_AUTH_SECONDS", "0"),
            ("PASSKEY_AUTHENTICATED_RATE_LIMIT", "0"),
            ("PASSKEY_ANONYMOUS_RATE_LIMIT", "not-a-number"),
    ):
        with pytest.raises(PasskeyConfigurationError, match=key):
            PasskeyConfig.from_environ(_enabled_env(tmp_path, **{key: value}))


def test_enabled_config_rejects_mismatched_multi_github_oauth_credentials(tmp_path):
    with pytest.raises(PasskeyConfigurationError, match="OAuth"):
        PasskeyConfig.from_environ(
            _enabled_env(
                tmp_path,
                GOOGLE_CLIENT_ID="",
                GOOGLE_CLIENT_SECRET="",
                GITHUB_CLIENT_IDS="app.example.com:client-id",
                GITHUB_CLIENT_SECRETS="other.example.com:client-secret",
            )
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("PASSKEY_RP_ID", "https://app.example.com", "RP ID"),
        ("PASSKEY_RP_ID", "a" * 256, "RP ID"),
        ("PASSKEY_RP_NAME", "n" * 201, "RP_NAME"),
        ("PASSKEY_PROXY_ID", "p" * 256, "PROXY_ID"),
        ("PASSKEY_PROXY_SECRET", "s" * 4097, "PROXY_SECRET"),
    ],
)
def test_enabled_config_bounds_persisted_and_secret_fields(
        tmp_path, key, value, message
):
    with pytest.raises(PasskeyConfigurationError, match=message):
        PasskeyConfig.from_environ(_enabled_env(tmp_path, **{key: value}))


def test_recent_auth_boundary_uses_original_authenticated_time(tmp_path):
    config = PasskeyConfig.from_environ(_enabled_env(tmp_path))

    assert config.is_recent_auth(time.time() - 599)
    assert not config.is_recent_auth(time.time() - 601)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _client_data(kind: str, challenge: str, origin: str) -> bytes:
    return json.dumps(
        {"type": kind, "challenge": challenge, "origin": origin},
        separators=(",", ":"),
    ).encode()


def _registration_response(
        *, challenge: str, origin: str, rp_id: str, credential_id: bytes, key
) -> dict:
    public = key.public_key().public_numbers()
    cose_key = cbor2.dumps(
        {
            1: 2,
            3: -7,
            -1: 1,
            -2: public.x.to_bytes(32, "big"),
            -3: public.y.to_bytes(32, "big"),
        }
    )
    auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([0x45])
            + struct.pack(">I", 0)
            + (b"\0" * 16)
            + struct.pack(">H", len(credential_id))
            + credential_id
            + cose_key
    )
    attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
    return {
        "id": _b64url(credential_id),
        "rawId": _b64url(credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": _b64url(
                _client_data("webauthn.create", challenge, origin)
            ),
            "attestationObject": _b64url(attestation),
            "transports": ["internal"],
        },
        "clientExtensionResults": {},
        "authenticatorAttachment": "platform",
    }


def _authentication_response(
        *,
        challenge: str,
        origin: str,
        rp_id: str,
        credential_id: bytes,
        user_handle: str,
        key,
        sign_count: int = 1,
) -> dict:
    client_data = _client_data("webauthn.get", challenge, origin)
    auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([0x05])
            + struct.pack(">I", sign_count)
    )
    signature = key.sign(
        auth_data + hashlib.sha256(client_data).digest(),
        ec.ECDSA(hashes.SHA256()),
    )
    return {
        "id": _b64url(credential_id),
        "rawId": _b64url(credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": _b64url(client_data),
            "authenticatorData": _b64url(auth_data),
            "signature": _b64url(signature),
            "userHandle": user_handle,
        },
        "clientExtensionResults": {},
        "authenticatorAttachment": "platform",
    }


@pytest.fixture
def passkey_service(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    user = {
        "identity": "google:subject-123",
        "provider": "google",
        "email": "person@example.com",
        "name": "Person",
        "picture": "https://example.com/person.png",
    }
    token = store.create_session(user, "google")
    config = PasskeyConfig.from_environ(_enabled_env(tmp_path))
    return PasskeyService(config, store), store, user, token


def _multi_rp_service(tmp_path):
    store = SQLiteAuthStore(tmp_path / "multi-auth.db")
    user = {
        "identity": "google:subject-123",
        "provider": "google",
        "email": "person@example.com",
        "name": "Person",
    }
    token = store.create_session(user, "google")
    config = PasskeyConfig.from_environ(
        _multi_rp_env(tmp_path, SQLITE_DB_PATH=str(tmp_path / "multi-auth.db"))
    )
    return PasskeyService(config, store), store, user, token


def test_authentication_options_select_rp_for_each_requested_domain(tmp_path):
    service, store, _user, _token = _multi_rp_service(tmp_path)

    for origin, rp_id in (
            (
                    "https://bmo-deepagent-ui-0312.azurewebsites.net",
                    "bmo-deepagent-ui-0312.azurewebsites.net",
            ),
            (
                    "https://bmo-deepagent-ui.vercel.app",
                    "bmo-deepagent-ui.vercel.app",
            ),
    ):
        result = service.authentication_options(origin=origin, proxy_id="web-bff")
        assert result["options"]["rpId"] == rp_id
        claimed = store.claim_challenge(result["ceremony_id"])
        assert claimed is not None
        assert claimed.rp_id == rp_id


def test_registration_options_select_rp_and_filter_exclusions(tmp_path):
    service, store, user, token = _multi_rp_service(tmp_path)
    azure_rp = "bmo-deepagent-ui-0312.azurewebsites.net"
    vercel_rp = "bmo-deepagent-ui.vercel.app"
    for credential_id, rp_id in (("azure_A", azure_rp), ("vercel_B", vercel_rp)):
        store.create_credential(
            identity=user["identity"],
            rp_id=rp_id,
            credential_id=credential_id,
            public_key=b"public-key",
            sign_count=0,
            transports=["internal"],
            device_type="single_device",
            backed_up=False,
            label=None,
        )

    result = service.registration_options(
        session_token=token,
        origin="https://bmo-deepagent-ui-0312.azurewebsites.net",
        proxy_id="web-bff",
    )

    assert result["options"]["rp"]["id"] == azure_rp
    assert [item["id"] for item in result["options"]["excludeCredentials"]] == [
        "azure_A"
    ]
    claimed = store.claim_challenge(result["ceremony_id"])
    assert claimed is not None
    assert claimed.rp_id == azure_rp


def _assert_multi_rp_crypto_round_trip(tmp_path, origin, rp_id):
    service, store, user, token = _multi_rp_service(tmp_path)
    credential_id = f"credential-{rp_id}".encode()
    key = ec.generate_private_key(ec.SECP256R1())
    registration = service.registration_options(
        session_token=token, origin=origin, proxy_id="web-bff"
    )
    service.verify_registration(
        session_token=token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=origin,
            rp_id=rp_id,
            credential_id=credential_id,
            key=key,
        ),
    )
    persisted = store.get_credential(_b64url(credential_id))
    assert persisted is not None
    assert persisted.rp_id == rp_id

    authentication = service.authentication_options(origin=origin, proxy_id="web-bff")
    account = store.get_account(user["identity"])
    result = service.verify_authentication(
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=authentication["ceremony_id"],
        response=_authentication_response(
            challenge=authentication["options"]["challenge"],
            origin=origin,
            rp_id=rp_id,
            credential_id=credential_id,
            user_handle=account.webauthn_user_handle,
            key=key,
        ),
    )

    assert result["user"]["identity"] == user["identity"]
    assert result["user"]["auth_method"] == "passkey"


def test_azure_registration_and_authentication_crypto_round_trip(tmp_path):
    _assert_multi_rp_crypto_round_trip(
        tmp_path,
        "https://bmo-deepagent-ui-0312.azurewebsites.net",
        "bmo-deepagent-ui-0312.azurewebsites.net",
    )


def test_vercel_registration_and_authentication_crypto_round_trip(tmp_path):
    _assert_multi_rp_crypto_round_trip(
        tmp_path,
        "https://bmo-deepagent-ui.vercel.app",
        "bmo-deepagent-ui.vercel.app",
    )


def test_unbound_legacy_credential_is_rejected_without_mutation_for_multiple_rps(
        tmp_path,
):
    service, store, user, token = _multi_rp_service(tmp_path)
    store.create_credential(
        identity=user["identity"],
        rp_id="bmo-deepagent-ui.vercel.app",
        credential_id="legacy_A",
        public_key=b"public-key",
        sign_count=0,
        transports=[],
        device_type="single_device",
        backed_up=False,
        label=None,
    )
    with store._lock:
        store._connection.execute(
            "UPDATE auth_credentials SET rp_id = NULL WHERE credential_id = ?",
            ("legacy_A",),
        )

    with pytest.raises(InvalidPasskeyError):
        service.registration_options(
            session_token=token,
            origin="https://bmo-deepagent-ui.vercel.app",
            proxy_id="web-bff",
        )

    assert store.get_credential("legacy_A").rp_id is None
    authentication = service.authentication_options(
        origin="https://bmo-deepagent-ui.vercel.app", proxy_id="web-bff"
    )
    with pytest.raises(InvalidPasskeyError):
        service.verify_authentication(
            origin="https://bmo-deepagent-ui.vercel.app",
            proxy_id="web-bff",
            ceremony_id=authentication["ceremony_id"],
            response={"id": "legacy_A", "response": {}},
        )
    assert store.get_credential("legacy_A").rp_id is None


def test_empty_legacy_rp_id_is_treated_as_unbound(tmp_path):
    service, store, user, token = _multi_rp_service(tmp_path)
    store.create_credential(
        identity=user["identity"],
        rp_id="bmo-deepagent-ui.vercel.app",
        credential_id="legacy_empty",
        public_key=b"public-key",
        sign_count=0,
        transports=[],
        device_type="single_device",
        backed_up=False,
        label=None,
    )
    with store._lock:
        store._connection.execute(
            "UPDATE auth_credentials SET rp_id = '' WHERE credential_id = ?",
            ("legacy_empty",),
        )

    with pytest.raises(InvalidPasskeyError):
        service.registration_options(
            session_token=token,
            origin="https://bmo-deepagent-ui.vercel.app",
            proxy_id="web-bff",
        )

    assert store.get_credential("legacy_empty").rp_id == ""


def test_unbound_legacy_credential_binds_only_for_single_rp(passkey_service):
    service, store, user, token = passkey_service
    origin = "https://app.example.com"
    rp_id = "app.example.com"
    key = ec.generate_private_key(ec.SECP256R1())
    credential_id = b"legacy-single-rp"
    registration = service.registration_options(
        session_token=token, origin=origin, proxy_id="web-bff"
    )
    service.verify_registration(
        session_token=token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=origin,
            rp_id=rp_id,
            credential_id=credential_id,
            key=key,
        ),
    )
    encoded_id = _b64url(credential_id)
    with store._lock:
        store._connection.execute(
            "UPDATE auth_credentials SET rp_id = NULL WHERE credential_id = ?",
            (encoded_id,),
        )

    # Registration options migrate all legacy credentials before exclusions.
    options = service.registration_options(
        session_token=token, origin=origin, proxy_id="web-bff"
    )
    assert store.get_credential(encoded_id).rp_id == rp_id
    assert encoded_id in {
        item["id"] for item in options["options"]["excludeCredentials"]
    }

    # Authentication also migrates a legacy row when no registration occurs first.
    with store._lock:
        store._connection.execute(
            "UPDATE auth_credentials SET rp_id = NULL WHERE credential_id = ?",
            (encoded_id,),
        )
    authentication = service.authentication_options(origin=origin, proxy_id="web-bff")
    account = store.get_account(user["identity"])
    result = service.verify_authentication(
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=authentication["ceremony_id"],
        response=_authentication_response(
            challenge=authentication["options"]["challenge"],
            origin=origin,
            rp_id=rp_id,
            credential_id=credential_id,
            user_handle=account.webauthn_user_handle,
            key=key,
        ),
    )
    assert result["ok"] is True
    assert store.get_credential(encoded_id).rp_id == rp_id


def test_authentication_rejects_credential_bound_to_other_rp(tmp_path):
    service, store, user, token = _multi_rp_service(tmp_path)
    azure_origin = "https://bmo-deepagent-ui-0312.azurewebsites.net"
    azure_rp = "bmo-deepagent-ui-0312.azurewebsites.net"
    vercel_origin = "https://bmo-deepagent-ui.vercel.app"
    vercel_rp = "bmo-deepagent-ui.vercel.app"
    credential_id = b"azure-only-credential"
    encoded_id = _b64url(credential_id)
    key = ec.generate_private_key(ec.SECP256R1())
    registration = service.registration_options(
        session_token=token, origin=azure_origin, proxy_id="web-bff"
    )
    service.verify_registration(
        session_token=token,
        origin=azure_origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=azure_origin,
            rp_id=azure_rp,
            credential_id=credential_id,
            key=key,
        ),
    )
    authentication = service.authentication_options(
        origin=vercel_origin, proxy_id="web-bff"
    )
    account = store.get_account(user["identity"])
    with store._lock:
        session_count = store._connection.execute(
            "SELECT COUNT(*) FROM auth_sessions"
        ).fetchone()[0]

    with pytest.raises(InvalidPasskeyError):
        service.verify_authentication(
            origin=vercel_origin,
            proxy_id="web-bff",
            ceremony_id=authentication["ceremony_id"],
            response=_authentication_response(
                challenge=authentication["options"]["challenge"],
                origin=vercel_origin,
                rp_id=vercel_rp,
                credential_id=credential_id,
                user_handle=account.webauthn_user_handle,
                key=key,
            ),
        )

    assert store.get_credential(encoded_id).sign_count == 0
    with store._lock:
        assert (
                store._connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[
                    0
                ]
                == session_count
        )


@pytest.mark.parametrize("kind", ["registration", "authentication"])
def test_wrong_allowed_domain_burns_challenge_before_correct_domain_retry(
        tmp_path, kind
):
    service, _store, _user, token = _multi_rp_service(tmp_path)
    azure_origin = "https://bmo-deepagent-ui-0312.azurewebsites.net"
    vercel_origin = "https://bmo-deepagent-ui.vercel.app"
    if kind == "registration":
        ceremony = service.registration_options(
            session_token=token, origin=azure_origin, proxy_id="web-bff"
        )

        def verify(origin):
            return service.verify_registration(
                session_token=token,
                origin=origin,
                proxy_id="web-bff",
                ceremony_id=ceremony["ceremony_id"],
                response={},
            )

    else:
        ceremony = service.authentication_options(
            origin=azure_origin, proxy_id="web-bff"
        )

        def verify(origin):
            return service.verify_authentication(
                origin=origin,
                proxy_id="web-bff",
                ceremony_id=ceremony["ceremony_id"],
                response={},
            )

    with pytest.raises(InvalidPasskeyError):
        verify(vercel_origin)
    with pytest.raises(InvalidPasskeyError):
        verify(azure_origin)


def test_concurrent_service_verification_allows_only_one_claim(passkey_service):
    service, store, user, token = passkey_service
    origin = "https://app.example.com"
    credential_id = b"concurrent-credential"
    encoded_id = _b64url(credential_id)
    key = ec.generate_private_key(ec.SECP256R1())
    registration = service.registration_options(
        session_token=token, origin=origin, proxy_id="web-bff"
    )
    service.verify_registration(
        session_token=token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=origin,
            rp_id="app.example.com",
            credential_id=credential_id,
            key=key,
        ),
    )
    authentication = service.authentication_options(origin=origin, proxy_id="web-bff")
    account = store.get_account(user["identity"])
    response = _authentication_response(
        challenge=authentication["options"]["challenge"],
        origin=origin,
        rp_id="app.example.com",
        credential_id=credential_id,
        user_handle=account.webauthn_user_handle,
        key=key,
    )
    with store._lock:
        sessions_before = store._connection.execute(
            "SELECT COUNT(*) FROM auth_sessions"
        ).fetchone()[0]

    def verify():
        try:
            service.verify_authentication(
                origin=origin,
                proxy_id="web-bff",
                ceremony_id=authentication["ceremony_id"],
                response=response,
            )
        except InvalidPasskeyError:
            return "rejected"
        return "authenticated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: verify(), range(2)))

    assert sorted(results) == ["authenticated", "rejected"]
    assert store.get_credential(encoded_id).sign_count == 1
    with store._lock:
        sessions_after = store._connection.execute(
            "SELECT COUNT(*) FROM auth_sessions"
        ).fetchone()[0]
    assert sessions_after == sessions_before + 1


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_kind",
        "expired_challenge",
        "wrong_proxy",
        "missing_session",
        "expired_session",
        "malformed_response",
    ],
)
def test_registration_verification_claim_precedes_validation_failures(
        passkey_service, failure
):
    service, store, _user, token = passkey_service
    origin = "https://app.example.com"
    registration = service.registration_options(
        session_token=token, origin=origin, proxy_id="web-bff"
    )
    ceremony_id = registration["ceremony_id"]
    session_token = token
    response = {}
    with store._lock:
        if failure == "wrong_kind":
            store._connection.execute(
                "UPDATE auth_challenges SET kind = 'authentication' WHERE ceremony_id = ?",
                (ceremony_id,),
            )
        elif failure == "expired_challenge":
            store._connection.execute(
                "UPDATE auth_challenges SET expires_at = 0 WHERE ceremony_id = ?",
                (ceremony_id,),
            )
        elif failure == "wrong_proxy":
            store._connection.execute(
                "UPDATE auth_challenges SET proxy_id = 'different-bff' WHERE ceremony_id = ?",
                (ceremony_id,),
            )
        elif failure == "expired_session":
            store._connection.execute("UPDATE auth_sessions SET expires_at = 0")
    if failure == "missing_session":
        session_token = None
    if failure == "malformed_response":
        response = []

    with pytest.raises(Exception):
        service.verify_registration(
            session_token=session_token,
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=ceremony_id,
            response=response,
        )
    assert store.claim_challenge(ceremony_id) is None
    with pytest.raises(InvalidPasskeyError):
        service.verify_registration(
            session_token=token,
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=ceremony_id,
            response={},
        )


@pytest.mark.parametrize(
    "failure", ["malformed_response", "missing_credential", "wrong_credential"]
)
def test_authentication_verification_claim_precedes_credential_validation(
        passkey_service, failure
):
    service, store, user, _token = passkey_service
    origin = "https://app.example.com"
    authentication = service.authentication_options(origin=origin, proxy_id="web-bff")
    ceremony_id = authentication["ceremony_id"]
    if failure == "malformed_response":
        response = []
    elif failure == "missing_credential":
        response = {"id": "missing_credential", "response": {}}
    else:
        store.create_credential(
            identity=user["identity"],
            rp_id="app.example.com",
            credential_id="wrong_credential",
            public_key=b"not-a-real-public-key",
            sign_count=0,
            transports=[],
            device_type="single_device",
            backed_up=False,
            label=None,
        )
        response = {"id": "wrong_credential", "response": {}}

    with pytest.raises(InvalidPasskeyError):
        service.verify_authentication(
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=ceremony_id,
            response=response,
        )
    assert store.claim_challenge(ceremony_id) is None
    with pytest.raises(InvalidPasskeyError):
        service.verify_authentication(
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=ceremony_id,
            response={},
        )


def test_registration_and_identifier_free_authentication_cryptographic_round_trip(
        passkey_service,
):
    service, store, user, oauth_token = passkey_service
    origin = "https://app.example.com"
    rp_id = "app.example.com"
    credential_id = b"credential-id-1"
    key = ec.generate_private_key(ec.SECP256R1())

    registration = service.registration_options(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
    )
    response = _registration_response(
        challenge=registration["options"]["challenge"],
        origin=origin,
        rp_id=rp_id,
        credential_id=credential_id,
        key=key,
    )
    enrolled = service.verify_registration(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=response,
        label="MacBook Touch ID",
    )

    assert enrolled["credential_id"] == _b64url(credential_id)
    assert enrolled["label"] == "MacBook Touch ID"
    assert "public_key" not in enrolled
    authentication = service.authentication_options(
        origin=origin,
        proxy_id="web-bff",
    )
    assert "allowCredentials" not in authentication["options"]
    account = store.get_account(user["identity"])
    auth_response = _authentication_response(
        challenge=authentication["options"]["challenge"],
        origin=origin,
        rp_id=rp_id,
        credential_id=credential_id,
        user_handle=account.webauthn_user_handle,
        key=key,
    )
    authenticated = service.verify_authentication(
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=authentication["ceremony_id"],
        response=auth_response,
    )

    assert authenticated["ok"] is True
    assert authenticated["user"]["identity"] == user["identity"]
    assert authenticated["user"]["provider"] == "google"
    assert authenticated["user"]["auth_method"] == "passkey"
    assert (
            store.validate_session(authenticated["session_token"])["auth_method"]
            == "passkey"
    )
    assert store.get_credential(_b64url(credential_id)).sign_count == 1


@pytest.mark.parametrize("label", [None, "", "   \t\n"])
def test_registration_generates_default_label_for_missing_or_blank_input(
        passkey_service, label
):
    service, store, _user, oauth_token = passkey_service
    origin = "https://app.example.com"
    credential_id = f"default-label-{label!r}".encode()
    key = ec.generate_private_key(ec.SECP256R1())
    registration = service.registration_options(
        session_token=oauth_token, origin=origin, proxy_id="web-bff"
    )

    enrolled = service.verify_registration(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=origin,
            rp_id="app.example.com",
            credential_id=credential_id,
            key=key,
        ),
        label=label,
    )

    assert enrolled["label"].startswith("Device passkey · ")
    assert store.get_credential(_b64url(credential_id)).label == enrolled["label"]


@pytest.mark.parametrize(
    ("device_type", "transports", "expected_prefix"),
    [
        ("multi_device", ["internal"], "Synced passkey"),
        ("single_device", ["internal"], "Device passkey"),
        ("single_device", ["usb"], "Security key"),
        ("single_device", ["nfc"], "Security key"),
        ("single_device", ["ble"], "Security key"),
        ("single_device", ["smart-card"], "Security key"),
        ("single_device", [], "Passkey"),
        ("single_device", ["hybrid"], "Passkey"),
        ("unexpected", ["telepathy"], "Passkey"),
    ],
)
def test_default_label_classifies_verified_authenticator_metadata(
        device_type, transports, expected_prefix
):
    timestamp = datetime(2026, 8, 3, 0, 0, tzinfo=UTC).timestamp()

    label = passkeys_module._default_passkey_label(
        device_type=device_type,
        transports=transports,
        created_at=timestamp,
    )

    assert label == f"{expected_prefix} · Aug 3, 2026"


def test_default_label_uses_utc_across_date_boundary():
    before_midnight = datetime(2026, 8, 2, 23, 59, 59, tzinfo=UTC).timestamp()
    midnight = datetime(2026, 8, 3, 0, 0, tzinfo=UTC).timestamp()

    assert passkeys_module._default_passkey_label(
        device_type="single_device",
        transports=["internal"],
        created_at=before_midnight,
    ) == "Device passkey · Aug 2, 2026"
    assert passkeys_module._default_passkey_label(
        device_type="single_device",
        transports=["internal"],
        created_at=midnight,
    ) == "Device passkey · Aug 3, 2026"


@pytest.mark.parametrize("label", ["\ud800", "\udfff", "valid\ud800label"])
def test_label_normalization_rejects_non_scalar_unicode(label):
    with pytest.raises(InvalidPasskeyError):
        passkeys_module._normalize_passkey_label(label, allow_default=False)


def test_label_normalization_matches_ecmascript_trim_without_trimming_u0085():
    assert (
            passkeys_module._normalize_passkey_label(
                "\ufeff\u00a0Laptop\u3000\ufeff", allow_default=False
            )
            == "Laptop"
    )
    assert (
            passkeys_module._normalize_passkey_label(
                "\u0085Laptop\u0085", allow_default=False
            )
            == "\u0085Laptop\u0085"
    )


def test_label_normalization_counts_astral_unicode_code_points():
    assert passkeys_module._normalize_passkey_label(
        "😀" * 100, allow_default=False
    ) == ("😀" * 100)
    with pytest.raises(InvalidPasskeyError):
        passkeys_module._normalize_passkey_label("😀" * 101, allow_default=False)


@pytest.mark.parametrize("created_at", [None, float("nan"), float("inf"), "bad"])
def test_legacy_invalid_timestamp_uses_stable_generic_label(created_at):
    credential = SimpleNamespace(
        credential_id="legacy_invalid_timestamp",
        label=None,
        transports=(),
        device_type="single_device",
        backed_up=False,
        created_at=created_at,
        last_used_at=None,
    )

    assert PasskeyService._credential_json(credential)["label"] == "Passkey"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("  Laptop  ", "Laptop"),
        ("  " + "x" * 100 + "  ", "x" * 100),
        ("  😀  ", "😀"),
    ],
)
def test_registration_trims_valid_explicit_label(passkey_service, label, expected):
    service, store, _user, oauth_token = passkey_service
    origin = "https://app.example.com"
    credential_id = f"explicit-{len(expected)}-{ord(expected[0])}".encode()
    key = ec.generate_private_key(ec.SECP256R1())
    registration = service.registration_options(
        session_token=oauth_token, origin=origin, proxy_id="web-bff"
    )

    enrolled = service.verify_registration(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=origin,
            rp_id="app.example.com",
            credential_id=credential_id,
            key=key,
        ),
        label=label,
    )

    assert enrolled["label"] == expected
    assert store.get_credential(_b64url(credential_id)).label == expected


def test_registration_uses_ecmascript_trim_for_explicit_label(passkey_service):
    service, store, _user, oauth_token = passkey_service
    origin = "https://app.example.com"
    credential_id = b"ecmascript-trim-registration"
    key = ec.generate_private_key(ec.SECP256R1())
    registration = service.registration_options(
        session_token=oauth_token, origin=origin, proxy_id="web-bff"
    )

    enrolled = service.verify_registration(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=origin,
            rp_id="app.example.com",
            credential_id=credential_id,
            key=key,
        ),
        label="\ufeffLaptop\ufeff",
    )

    assert enrolled["label"] == "Laptop"
    assert store.get_credential(_b64url(credential_id)).label == "Laptop"


@pytest.mark.parametrize("label", ["x" * 101, 42, ["Laptop"]])
def test_invalid_registration_label_burns_challenge_before_crypto(
        passkey_service, monkeypatch, label
):
    service, store, _user, oauth_token = passkey_service
    registration = service.registration_options(
        session_token=oauth_token,
        origin="https://app.example.com",
        proxy_id="web-bff",
    )
    crypto_called = False

    def unexpected_crypto(**_kwargs):
        nonlocal crypto_called
        crypto_called = True
        raise AssertionError("invalid label reached WebAuthn verification")

    monkeypatch.setattr(passkeys_module, "verify_registration_response", unexpected_crypto)

    with pytest.raises(InvalidPasskeyError):
        service.verify_registration(
            session_token=oauth_token,
            origin="https://app.example.com",
            proxy_id="web-bff",
            ceremony_id=registration["ceremony_id"],
            response={},
            label=label,
        )

    assert crypto_called is False
    assert store.claim_challenge(registration["ceremony_id"]) is None
    assert store.list_credentials("google:subject-123") == []


@pytest.mark.parametrize("label", ["\ud800", "\udfff"])
def test_registration_rejects_surrogate_label_before_crypto(
        passkey_service, monkeypatch, label
):
    service, store, _user, oauth_token = passkey_service
    origin = "https://app.example.com"
    registration = service.registration_options(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
    )
    crypto_called = False

    def unexpected_crypto(**_kwargs):
        nonlocal crypto_called
        crypto_called = True

    monkeypatch.setattr(passkeys_module, "verify_registration_response", unexpected_crypto)

    with pytest.raises(InvalidPasskeyError):
        service.verify_registration(
            session_token=oauth_token,
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=registration["ceremony_id"],
            response=_registration_response(
                challenge=registration["options"]["challenge"],
                origin=origin,
                rp_id="app.example.com",
                credential_id=b"surrogate-label",
                key=ec.generate_private_key(ec.SECP256R1()),
            ),
            label=label,
        )

    assert crypto_called is False
    assert store.claim_challenge(registration["ceremony_id"]) is None


def test_successful_registration_samples_post_verify_clock_once_for_label_and_created_at(
        tmp_path, monkeypatch
):
    store = SQLiteAuthStore(tmp_path / "clock-auth.db")
    user = {
        "identity": "google:clock-user",
        "provider": "google",
        "email": "clock@example.com",
        "name": "Clock User",
    }
    token = store.create_session(user, "google")
    calls: list[float] = []
    timestamp = datetime(2026, 8, 3, 0, 0, tzinfo=UTC).timestamp()
    with store._lock:
        store._connection.execute(
            "UPDATE auth_sessions SET authenticated_at = ?", (timestamp,)
        )

    def clock():
        calls.append(timestamp)
        return timestamp

    service = PasskeyService(
        PasskeyConfig.from_environ(_enabled_env(tmp_path)), store, clock=clock
    )
    registration = service.registration_options(
        session_token=token,
        origin="https://app.example.com",
        proxy_id="web-bff",
    )
    response = _registration_response(
        challenge=registration["options"]["challenge"],
        origin="https://app.example.com",
        rp_id="app.example.com",
        credential_id=b"clock-credential",
        key=ec.generate_private_key(ec.SECP256R1()),
    )
    original_verify = passkeys_module.verify_registration_response
    calls_at_crypto: list[int] = []

    def observe_crypto(**kwargs):
        calls_at_crypto.append(len(calls))
        return original_verify(**kwargs)

    monkeypatch.setattr(passkeys_module, "verify_registration_response", observe_crypto)

    enrolled = service.verify_registration(
        session_token=token,
        origin="https://app.example.com",
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=response,
    )

    assert len(calls) == calls_at_crypto[0] + 1
    assert enrolled["created_at"] == calls[-1]
    assert enrolled["label"] == "Device passkey · Aug 3, 2026"


def test_failed_crypto_does_not_sample_post_verify_clock_or_create_label(
        passkey_service, monkeypatch
):
    service, store, _user, oauth_token = passkey_service
    registration = service.registration_options(
        session_token=oauth_token,
        origin="https://app.example.com",
        proxy_id="web-bff",
    )
    calls_at_crypto: list[int] = []
    original_clock = service._clock
    clock_calls = 0

    def counting_clock():
        nonlocal clock_calls
        clock_calls += 1
        return original_clock()

    def fail_crypto(**_kwargs):
        calls_at_crypto.append(clock_calls)
        raise ValueError("invalid attestation")

    service._clock = counting_clock
    monkeypatch.setattr(passkeys_module, "verify_registration_response", fail_crypto)

    with pytest.raises(InvalidPasskeyError):
        service.verify_registration(
            session_token=oauth_token,
            origin="https://app.example.com",
            proxy_id="web-bff",
            ceremony_id=registration["ceremony_id"],
            response=_registration_response(
                challenge=registration["options"]["challenge"],
                origin="https://app.example.com",
                rp_id="app.example.com",
                credential_id=b"failed-clock-credential",
                key=ec.generate_private_key(ec.SECP256R1()),
            ),
        )

    assert clock_calls == calls_at_crypto[0]
    assert store.list_credentials("google:subject-123") == []


def test_verification_attempt_consumes_challenge_even_when_origin_is_wrong(
        passkey_service,
):
    service, _store, _user, oauth_token = passkey_service
    registration = service.registration_options(
        session_token=oauth_token,
        origin="https://app.example.com",
        proxy_id="web-bff",
    )

    with pytest.raises(InvalidPasskeyError):
        service.verify_registration(
            session_token=oauth_token,
            origin="https://evil.example",
            proxy_id="web-bff",
            ceremony_id=registration["ceremony_id"],
            response={},
        )
    with pytest.raises(InvalidPasskeyError):
        service.verify_registration(
            session_token=oauth_token,
            origin="https://app.example.com",
            proxy_id="web-bff",
            ceremony_id=registration["ceremony_id"],
            response={},
        )


def test_authentication_requires_matching_stored_user_handle(passkey_service):
    service, store, user, oauth_token = passkey_service
    origin = "https://app.example.com"
    key = ec.generate_private_key(ec.SECP256R1())
    credential_id = b"credential-id-2"
    registration = service.registration_options(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
    )
    service.verify_registration(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=origin,
            rp_id="app.example.com",
            credential_id=credential_id,
            key=key,
        ),
        label=None,
    )
    authentication = service.authentication_options(origin=origin, proxy_id="web-bff")
    wrong_handle = _b64url(b"different-user-handle")

    with pytest.raises(InvalidPasskeyError):
        service.verify_authentication(
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=authentication["ceremony_id"],
            response=_authentication_response(
                challenge=authentication["options"]["challenge"],
                origin=origin,
                rp_id="app.example.com",
                credential_id=credential_id,
                user_handle=wrong_handle,
                key=key,
            ),
        )

    assert store.get_account(user["identity"]).webauthn_user_handle != wrong_handle


@pytest.mark.parametrize("failure", ["challenge", "rp_id", "expiry"])
def test_registration_rejects_cryptographic_mismatch_and_expiry(
        passkey_service, failure
):
    service, store, _user, oauth_token = passkey_service
    origin = "https://app.example.com"
    registration = service.registration_options(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
    )
    challenge = registration["options"]["challenge"]
    rp_id = "app.example.com"
    if failure == "challenge":
        challenge = _b64url(b"a-different-challenge")
    if failure == "rp_id":
        rp_id = "evil.example"
    if failure == "expiry":
        with store._lock:
            store._connection.execute(
                "UPDATE auth_challenges SET expires_at = ? WHERE ceremony_id = ?",
                (time.time() - 1, registration["ceremony_id"]),
            )
    response = _registration_response(
        challenge=challenge,
        origin=origin,
        rp_id=rp_id,
        credential_id=b"credential-negative",
        key=ec.generate_private_key(ec.SECP256R1()),
    )

    with pytest.raises(InvalidPasskeyError):
        service.verify_registration(
            session_token=oauth_token,
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=registration["ceremony_id"],
            response=response,
        )

    assert store.get_credential(_b64url(b"credential-negative")) is None


def test_registration_rejects_packed_attestation_even_when_valid(
        passkey_service,
):
    service, store, _user, oauth_token = passkey_service
    origin = "https://app.example.com"
    registration = service.registration_options(
        session_token=oauth_token, origin=origin, proxy_id="web-bff"
    )
    key = ec.generate_private_key(ec.SECP256R1())
    response = _registration_response(
        challenge=registration["options"]["challenge"],
        origin=origin,
        rp_id="app.example.com",
        credential_id=b"packed-credential",
        key=key,
    )
    encoded_attestation = response["response"]["attestationObject"]
    attestation = cbor2.loads(
        base64.urlsafe_b64decode(
            encoded_attestation + "=" * (-len(encoded_attestation) % 4)
        )
    )
    encoded_client_data = response["response"]["clientDataJSON"]
    client_data = base64.urlsafe_b64decode(
        encoded_client_data + "=" * (-len(encoded_client_data) % 4)
    )
    signature = key.sign(
        attestation["authData"] + hashlib.sha256(client_data).digest(),
        ec.ECDSA(hashes.SHA256()),
    )
    attestation["fmt"] = "packed"
    attestation["attStmt"] = {"alg": -7, "sig": signature}
    response["response"]["attestationObject"] = _b64url(cbor2.dumps(attestation))

    with pytest.raises(InvalidPasskeyError):
        service.verify_registration(
            session_token=oauth_token,
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=registration["ceremony_id"],
            response=response,
        )

    assert store.get_credential(_b64url(b"packed-credential")) is None


def test_authentication_counter_conflict_never_issues_session(
        passkey_service, monkeypatch
):
    service, store, user, oauth_token = passkey_service
    origin = "https://app.example.com"
    credential_id = b"credential-counter-conflict"
    key = ec.generate_private_key(ec.SECP256R1())
    registration = service.registration_options(
        session_token=oauth_token, origin=origin, proxy_id="web-bff"
    )
    service.verify_registration(
        session_token=oauth_token,
        origin=origin,
        proxy_id="web-bff",
        ceremony_id=registration["ceremony_id"],
        response=_registration_response(
            challenge=registration["options"]["challenge"],
            origin=origin,
            rp_id="app.example.com",
            credential_id=credential_id,
            key=key,
        ),
    )
    authentication = service.authentication_options(origin=origin, proxy_id="web-bff")
    account = store.get_account(user["identity"])
    with store._lock:
        before = store._connection.execute(
            "SELECT COUNT(*) FROM auth_sessions"
        ).fetchone()[0]
    monkeypatch.setattr(
        store, "update_credential_state", lambda *_args, **_kwargs: False
    )

    with pytest.raises(InvalidPasskeyError):
        service.verify_authentication(
            origin=origin,
            proxy_id="web-bff",
            ceremony_id=authentication["ceremony_id"],
            response=_authentication_response(
                challenge=authentication["options"]["challenge"],
                origin=origin,
                rp_id="app.example.com",
                credential_id=credential_id,
                user_handle=account.webauthn_user_handle,
                key=key,
            ),
        )

    with store._lock:
        after = store._connection.execute(
            "SELECT COUNT(*) FROM auth_sessions"
        ).fetchone()[0]
    assert after == before


def test_registration_requires_session_authenticated_within_ten_minutes(
        passkey_service, monkeypatch
):
    service, store, _user, oauth_token = passkey_service
    with store._lock:
        store._connection.execute(
            "UPDATE auth_sessions SET authenticated_at = ?",
            (time.time() - 601,),
        )

    with pytest.raises(ReauthenticationRequired) as error:
        service.registration_options(
            session_token=oauth_token,
            origin="https://app.example.com",
            proxy_id="web-bff",
        )

    assert error.value.provider == "google"


def test_registration_verification_burns_challenge_before_recent_auth_check(
        passkey_service,
):
    service, store, _user, oauth_token = passkey_service
    registration = service.registration_options(
        session_token=oauth_token,
        origin="https://app.example.com",
        proxy_id="web-bff",
    )
    with store._lock:
        store._connection.execute(
            "UPDATE auth_sessions SET authenticated_at = ?", (time.time() - 601,)
        )

    with pytest.raises(ReauthenticationRequired):
        service.verify_registration(
            session_token=oauth_token,
            origin="https://app.example.com",
            proxy_id="web-bff",
            ceremony_id=registration["ceremony_id"],
            response={},
        )

    with store._lock:
        consumed_at = store._connection.execute(
            "SELECT consumed_at FROM auth_challenges WHERE ceremony_id = ?",
            (registration["ceremony_id"],),
        ).fetchone()[0]
    assert consumed_at is not None


def test_list_rename_and_revoke_require_recent_auth(passkey_service):
    service, store, _user, oauth_token = passkey_service
    store.create_credential(
        identity="google:subject-123",
        rp_id="example.com",
        credential_id="credential_X",
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label="Old label",
    )

    listed = service.list_credentials(oauth_token)
    assert listed[0]["label"] == "Old label"
    assert "public_key" not in listed[0]
    renamed = service.rename_credential(oauth_token, "credential_X", "New label")
    assert renamed["label"] == "New label"
    assert service.delete_credential(oauth_token, "credential_X") is True
    assert service.list_credentials(oauth_token) == []


@pytest.mark.parametrize(
    ("device_type", "transports", "created_at", "expected"),
    [
        (
                "multi_device",
                ["internal"],
                datetime(2026, 8, 3, tzinfo=UTC).timestamp(),
                "Synced passkey · Aug 3, 2026",
        ),
        (
                "single_device",
                ["usb"],
                datetime(2026, 8, 3, tzinfo=UTC).timestamp(),
                "Security key · Aug 3, 2026",
        ),
    ],
)
@pytest.mark.parametrize("legacy_label", [None, "", "   "])
def test_legacy_blank_label_serializes_stably_without_storage_mutation(
        passkey_service,
        device_type,
        transports,
        created_at,
        expected,
        legacy_label,
):
    service, store, user, oauth_token = passkey_service
    credential_id = _b64url(
        hashlib.sha256(
            repr((device_type, transports, created_at, legacy_label)).encode()
        ).digest()
    )
    store.create_credential(
        identity=user["identity"],
        rp_id="app.example.com",
        credential_id=credential_id,
        public_key=b"public-key",
        sign_count=0,
        transports=transports,
        device_type=device_type,
        backed_up=device_type == "multi_device",
        label=legacy_label,
        created_at=created_at,
    )

    first = service.list_credentials(oauth_token)[0]["label"]
    second = service.list_credentials(oauth_token)[0]["label"]

    assert first == expected
    assert second == expected
    assert isinstance(first, str) and first
    assert store.get_credential(credential_id).label == legacy_label


@pytest.mark.parametrize(
    ("legacy_label", "expected"),
    [
        ("\ufeff", "Device passkey · Aug 3, 2026"),
        ("\u0085", "\u0085"),
    ],
)
def test_legacy_label_serialization_uses_ecmascript_trim_without_storage_mutation(
        passkey_service, legacy_label, expected
):
    service, store, user, oauth_token = passkey_service
    credential_id = _b64url(hashlib.sha256(legacy_label.encode()).digest())
    created_at = datetime(2026, 8, 3, tzinfo=UTC).timestamp()
    store.create_credential(
        identity=user["identity"],
        rp_id="app.example.com",
        credential_id=credential_id,
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label=legacy_label,
        created_at=created_at,
    )

    listed = service.list_credentials(oauth_token)

    assert listed[0]["label"] == expected
    assert store.get_credential(credential_id).label == legacy_label


@pytest.mark.parametrize("legacy_label", ["\ud800", "\udfff"])
def test_legacy_surrogate_label_falls_back_without_mutating_adapter_record(
        passkey_service, monkeypatch, legacy_label
):
    service, store, user, oauth_token = passkey_service
    record = CredentialRecord(
        credential_id="legacy_surrogate",
        identity=user["identity"],
        rp_id="app.example.com",
        public_key=b"public-key",
        sign_count=0,
        transports=("internal",),
        device_type="single_device",
        backed_up=False,
        label=legacy_label,
        created_at=datetime(2026, 8, 3, tzinfo=UTC).timestamp(),
        last_used_at=None,
    )
    monkeypatch.setattr(store, "list_credentials", lambda _identity: [record])

    listed = service.list_credentials(oauth_token)

    assert listed[0]["label"] == "Device passkey · Aug 3, 2026"
    assert record.label == legacy_label


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("  New label  ", "New label"),
        ("  " + "x" * 100 + "  ", "x" * 100),
        ("😀", "😀"),
    ],
)
def test_rename_trims_and_accepts_one_to_one_hundred_code_points(
        passkey_service, label, expected
):
    service, store, user, oauth_token = passkey_service
    credential_id = f"rename-valid-{len(expected)}-{ord(expected[0])}"
    store.create_credential(
        identity=user["identity"],
        rp_id="app.example.com",
        credential_id=credential_id,
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label="Old label",
    )

    renamed = service.rename_credential(oauth_token, credential_id, label)

    assert renamed["label"] == expected
    assert store.get_credential(credential_id).label == expected


def test_rename_preserves_u0085_because_ecmascript_trim_does_not_remove_it(
        passkey_service
):
    service, store, user, oauth_token = passkey_service
    store.create_credential(
        identity=user["identity"],
        rp_id="app.example.com",
        credential_id="rename-u0085",
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label="Old label",
    )

    renamed = service.rename_credential(
        oauth_token, "rename-u0085", "\u0085New label\u0085"
    )

    assert renamed["label"] == "\u0085New label\u0085"
    assert store.get_credential("rename-u0085").label == "\u0085New label\u0085"


@pytest.mark.parametrize("label", ["", "   ", "x" * 101, None, 17])
def test_rename_rejects_blank_overlength_and_non_string_without_mutation(
        passkey_service, label
):
    service, store, user, oauth_token = passkey_service
    credential_id = _b64url(hashlib.sha256(repr(label).encode()).digest())
    store.create_credential(
        identity=user["identity"],
        rp_id="app.example.com",
        credential_id=credential_id,
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label="Old label",
    )

    with pytest.raises(InvalidPasskeyError):
        service.rename_credential(oauth_token, credential_id, label)

    assert store.get_credential(credential_id).label == "Old label"


@pytest.fixture
def passkey_client(passkey_service):
    service, store, user, token = passkey_service
    app = FastAPI()
    register_passkey_routes(app, service=service, config=service.config)
    return TestClient(app), store, user, token


@pytest.fixture
def multi_rp_passkey_client(tmp_path):
    service, store, user, token = _multi_rp_service(tmp_path)
    app = FastAPI()
    register_passkey_routes(app, service=service, config=service.config)
    return TestClient(app), service, store, user, token


def _proxy_headers(*, token: str | None = None, origin: str | None = None):
    headers = {
        "X-Passkey-Proxy-Id": "web-bff",
        "X-Passkey-Proxy-Secret": "proxy-secret-with-32-bytes-minimum",
        "X-Passkey-Origin": origin or "https://app.example.com",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@pytest.mark.parametrize(
    ("service_method", "http_method", "path", "service_result"),
    [
        ("list_credentials", "get", "/auth/passkeys", []),
        (
                "registration_options",
                "post",
                "/auth/passkeys/registration/options",
                {"ceremony_id": "ceremony", "options": {}},
        ),
    ],
)
def test_passkey_management_service_calls_run_off_event_loop(
        passkey_service,
        monkeypatch,
        service_method,
        http_method,
        path,
        service_result,
):
    service, _store, _user, token = passkey_service
    app = FastAPI()
    register_passkey_routes(app, service=service, config=service.config)
    client = TestClient(app)

    def service_call(*_args, **_kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return service_result

    monkeypatch.setattr(service, service_method, service_call)

    response = getattr(client, http_method)(path, headers=_proxy_headers(token=token))

    assert response.status_code == 200


def test_registration_verification_service_call_runs_off_event_loop(
        passkey_service, monkeypatch
):
    service, _store, _user, token = passkey_service
    app = FastAPI()
    register_passkey_routes(app, service=service, config=service.config)
    client = TestClient(app)

    def verify_registration(**_kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return {"credential_id": "worker-thread", "label": "Device passkey"}

    monkeypatch.setattr(service, "verify_registration", verify_registration)

    response = client.post(
        "/auth/passkeys/registration/verify",
        headers=_proxy_headers(token=token),
        json={"ceremony_id": "ceremony", "response": {}},
    )

    assert response.status_code == 200
    assert response.json()["passkey"]["label"] == "Device passkey"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        _proxy_headers() | {"X-Passkey-Proxy-Id": "other"},
        _proxy_headers() | {"X-Passkey-Proxy-Secret": "wrong"},
        _proxy_headers(origin="https://evil.example"),
    ],
)
def test_passkey_routes_require_trusted_proxy_and_exact_origin(passkey_client, headers):
    client, _store, _user, _token = passkey_client

    response = client.post("/auth/passkeys/authentication/options", headers=headers)

    assert response.status_code == 403
    assert response.json() == {"code": "passkey_request_rejected"}


def test_unknown_origin_remains_exact_passkey_request_rejected_403(
        multi_rp_passkey_client,
):
    client, _service, _store, _user, _token = multi_rp_passkey_client

    response = client.post(
        "/auth/passkeys/authentication/options",
        headers=_proxy_headers(origin="https://unknown.example.com"),
    )

    assert response.status_code == 403
    assert response.json() == {"code": "passkey_request_rejected"}


def test_allowed_origin_wrong_rp_returns_generic_invalid_response_400_without_config(
        multi_rp_passkey_client,
):
    client, _service, store, _user, _token = multi_rp_passkey_client
    azure_origin = "https://bmo-deepagent-ui-0312.azurewebsites.net"
    vercel_rp = "bmo-deepagent-ui.vercel.app"
    challenge = store.create_challenge(
        challenge=b"wrong-rp-challenge",
        kind="authentication",
        identity=None,
        origin=azure_origin,
        rp_id=vercel_rp,
        proxy_id="web-bff",
        created_at=time.time(),
        expires_at=time.time() + 300,
    )

    response = client.post(
        "/auth/passkeys/authentication/verify",
        headers=_proxy_headers(origin=azure_origin),
        json={"ceremony_id": challenge.ceremony_id, "response": {}},
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_passkey_response"}
    assert "rp" not in response.text.lower()
    assert vercel_rp not in response.text


def test_wrong_rp_credential_returns_generic_invalid_response_400_without_config(
        multi_rp_passkey_client,
):
    client, _service, store, user, _token = multi_rp_passkey_client
    azure_origin = "https://bmo-deepagent-ui-0312.azurewebsites.net"
    azure_rp = "bmo-deepagent-ui-0312.azurewebsites.net"
    vercel_rp = "bmo-deepagent-ui.vercel.app"
    credential = store.create_credential(
        identity=user["identity"],
        rp_id=vercel_rp,
        credential_id="wrong-rp-credential",
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label=None,
    )
    challenge = store.create_challenge(
        challenge=b"correct-rp-challenge",
        kind="authentication",
        identity=None,
        origin=azure_origin,
        rp_id=azure_rp,
        proxy_id="web-bff",
        created_at=time.time(),
        expires_at=time.time() + 300,
    )

    response = client.post(
        "/auth/passkeys/authentication/verify",
        headers=_proxy_headers(origin=azure_origin),
        json={
            "ceremony_id": challenge.ceremony_id,
            "response": {
                "id": credential.credential_id,
                "response": {"userHandle": user["identity"]},
            },
        },
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_passkey_response"}
    assert "rp" not in response.text.lower()
    assert azure_rp not in response.text
    assert vercel_rp not in response.text


def test_registration_options_require_bearer_session(passkey_client):
    client, _store, _user, _token = passkey_client

    response = client.post(
        "/auth/passkeys/registration/options", headers=_proxy_headers()
    )

    assert response.status_code == 401
    assert response.json() == {"code": "invalid_session"}


def test_stale_registration_returns_exact_reauth_payload(passkey_client):
    client, store, _user, token = passkey_client
    with store._lock:
        store._connection.execute(
            "UPDATE auth_sessions SET authenticated_at = ?", (time.time() - 601,)
        )

    response = client.post(
        "/auth/passkeys/registration/options",
        headers=_proxy_headers(token=token),
    )

    assert response.status_code == 403
    assert response.json() == {"code": "reauth_required", "provider": "google"}


def test_management_routes_list_rename_and_delete(passkey_client):
    client, store, _user, token = passkey_client
    store.create_credential(
        identity="google:subject-123",
        rp_id="example.com",
        credential_id="credential_Y",
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label="Phone",
    )
    headers = _proxy_headers(token=token)

    listed = client.get("/auth/passkeys", headers=headers)
    renamed = client.patch(
        "/auth/passkeys/credential_Y",
        headers=headers,
        json={"label": "Pixel"},
    )
    deleted = client.delete("/auth/passkeys/credential_Y", headers=headers)

    assert listed.status_code == 200
    assert listed.json()["passkeys"][0]["label"] == "Phone"
    assert renamed.status_code == 200
    assert renamed.json()["passkey"]["label"] == "Pixel"
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}


def test_registration_route_without_label_returns_generated_string(passkey_client):
    client, store, user, token = passkey_client
    headers = _proxy_headers(token=token)
    options_response = client.post(
        "/auth/passkeys/registration/options", headers=headers
    )
    options = options_response.json()
    credential_id = b"route-default-label"

    verified = client.post(
        "/auth/passkeys/registration/verify",
        headers=headers,
        json={
            "ceremony_id": options["ceremony_id"],
            "response": _registration_response(
                challenge=options["options"]["challenge"],
                origin="https://app.example.com",
                rp_id="app.example.com",
                credential_id=credential_id,
                key=ec.generate_private_key(ec.SECP256R1()),
            ),
        },
    )
    listed = client.get("/auth/passkeys", headers=headers)

    assert verified.status_code == 200
    assert isinstance(verified.json()["passkey"]["label"], str)
    assert verified.json()["passkey"]["label"].startswith("Device passkey · ")
    assert listed.status_code == 200
    assert listed.json()["passkeys"][0]["label"] == (
        verified.json()["passkey"]["label"]
    )
    assert store.list_credentials(user["identity"])[0].label == (
        verified.json()["passkey"]["label"]
    )


@pytest.mark.parametrize("label", [None, 17, ["Phone"]])
def test_rename_route_rejects_non_string_label(passkey_client, label):
    client, store, user, token = passkey_client
    store.create_credential(
        identity=user["identity"],
        rp_id="app.example.com",
        credential_id="route-rename-invalid",
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label="Phone",
    )

    response = client.patch(
        "/auth/passkeys/route-rename-invalid",
        headers=_proxy_headers(token=token),
        json={"label": label},
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_request"}
    assert store.get_credential("route-rename-invalid").label == "Phone"


@pytest.mark.parametrize("label", ["\ud800", "\udfff"])
def test_rename_route_rejects_surrogate_label(passkey_client, label):
    client, store, user, token = passkey_client
    store.create_credential(
        identity=user["identity"],
        rp_id="app.example.com",
        credential_id="route-rename-surrogate",
        public_key=b"public-key",
        sign_count=0,
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
        label="Phone",
    )

    response = client.patch(
        "/auth/passkeys/route-rename-surrogate",
        headers=_proxy_headers(token=token),
        content=json.dumps({"label": label}).encode("utf-8"),
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_passkey_response"}
    assert store.get_credential("route-rename-surrogate").label == "Phone"


def test_verify_route_rejects_oversized_payload_before_json_parsing(passkey_client):
    client, _store, _user, token = passkey_client

    response = client.post(
        "/auth/passkeys/registration/verify",
        headers=_proxy_headers(token=token) | {"Content-Type": "application/json"},
        content=b"{" + b"x" * 70_000,
    )

    assert response.status_code == 413
    assert response.json() == {"code": "payload_too_large"}


def test_verify_route_rejects_excessively_nested_json(passkey_client):
    client, _store, _user, token = passkey_client

    response = client.post(
        "/auth/passkeys/registration/verify",
        headers=_proxy_headers(token=token) | {"Content-Type": "application/json"},
        content=(b"[" * 2_000) + (b"]" * 2_000),
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_request"}


def test_verify_route_rejects_near_limit_deep_json_without_leaking_payload(
        passkey_client,
):
    client, _store, _user, token = passkey_client
    client_without_server_exceptions = TestClient(
        client.app, raise_server_exceptions=False
    )
    depth = 9_999
    payload = (b'{"x":' * depth) + b'{"private-nesting":0}' + (b"}" * depth)

    response = client_without_server_exceptions.post(
        "/auth/passkeys/registration/verify",
        headers=_proxy_headers(token=token) | {"Content-Type": "application/json"},
        content=payload,
    )

    assert len(payload) < 64 * 1024
    assert response.status_code == 400
    assert response.json() == {"code": "invalid_request"}
    assert "private-nesting" not in response.text


def test_registration_verify_burns_identified_challenge_when_bearer_missing(
        passkey_client,
):
    client, store, _user, token = passkey_client
    options = client.post(
        "/auth/passkeys/registration/options",
        headers=_proxy_headers(token=token),
    ).json()
    payload = {"ceremony_id": options["ceremony_id"], "response": {}}

    missing = client.post(
        "/auth/passkeys/registration/verify",
        headers=_proxy_headers(),
        json=payload,
    )
    with store._lock:
        consumed_after_first = store._connection.execute(
            "SELECT consumed_at FROM auth_challenges WHERE ceremony_id = ?",
            (options["ceremony_id"],),
        ).fetchone()[0]
    retry = client.post(
        "/auth/passkeys/registration/verify",
        headers=_proxy_headers(token=token),
        json=payload,
    )

    assert missing.status_code == 401
    assert missing.json() == {"code": "invalid_session"}
    assert consumed_after_first is not None
    assert retry.status_code == 400
    with store._lock:
        consumed = store._connection.execute(
            "SELECT consumed_at FROM auth_challenges WHERE ceremony_id = ?",
            (options["ceremony_id"],),
        ).fetchone()[0]
    assert consumed is not None


def test_registration_verify_burns_identified_challenge_for_malformed_response(
        passkey_client,
):
    client, store, _user, token = passkey_client
    headers = _proxy_headers(token=token)
    options = client.post("/auth/passkeys/registration/options", headers=headers).json()
    malformed = {"ceremony_id": options["ceremony_id"], "response": []}

    first = client.post(
        "/auth/passkeys/registration/verify", headers=headers, json=malformed
    )
    retry = client.post(
        "/auth/passkeys/registration/verify",
        headers=headers,
        json={"ceremony_id": options["ceremony_id"], "response": {}},
    )

    assert first.status_code == 400
    assert first.json() == {"code": "invalid_passkey_response"}
    assert retry.status_code == 400
    with store._lock:
        consumed = store._connection.execute(
            "SELECT consumed_at FROM auth_challenges WHERE ceremony_id = ?",
            (options["ceremony_id"],),
        ).fetchone()[0]
    assert consumed is not None


def test_authentication_options_exposes_busy_safe_json_contract(passkey_client):
    client, _store, _user, _token = passkey_client

    response = client.post(
        "/auth/passkeys/authentication/options", headers=_proxy_headers()
    )

    assert response.status_code == 200
    assert set(response.json()) == {"ceremony_id", "options"}
    assert response.json()["options"]["userVerification"] == "required"
    assert "allowCredentials" not in response.json()["options"]


def test_anonymous_rate_limit_is_enforced_per_proxy(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    config = PasskeyConfig.from_environ(
        _enabled_env(tmp_path, PASSKEY_ANONYMOUS_RATE_LIMIT="1")
    )
    service = PasskeyService(config, store)
    app = FastAPI()
    register_passkey_routes(app, service=service, config=config)
    client = TestClient(app)

    first = client.post(
        "/auth/passkeys/authentication/options", headers=_proxy_headers()
    )
    second = client.post(
        "/auth/passkeys/authentication/options", headers=_proxy_headers()
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json() == {"code": "rate_limited"}


def test_anonymous_rate_limit_survives_restart_and_meters_verify(tmp_path):
    store = SQLiteAuthStore(tmp_path / "auth.db")
    config = PasskeyConfig.from_environ(
        _enabled_env(tmp_path, PASSKEY_ANONYMOUS_RATE_LIMIT="2")
    )
    first = PasskeyService(config, store)
    ceremony = first.authentication_options(
        origin="https://app.example.com", proxy_id="web-bff"
    )
    restarted = PasskeyService(config, store)

    with pytest.raises(InvalidPasskeyError):
        restarted.verify_authentication(
            origin="https://app.example.com",
            proxy_id="web-bff",
            ceremony_id=ceremony["ceremony_id"],
            response={},
        )
    with pytest.raises(Exception, match="rate limit"):
        restarted.authentication_options(
            origin="https://app.example.com", proxy_id="web-bff"
        )


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("/chat?manage=passkeys", "/chat?manage=passkeys"),
        (None, None),
        ("/chat", None),
        ("//evil.example/chat?manage=passkeys", None),
        ("https://evil.example/chat?manage=passkeys", None),
        ("/chat?manage=passkeys&next=evil", None),
        ("/chat%3Fmanage%3Dpasskeys", None),
    ],
)
def test_oauth_return_path_has_one_exact_allowlisted_value(candidate, expected):
    assert safe_oauth_return_path(candidate) == expected


def test_application_registers_passkey_routes_only_when_enabled(monkeypatch):
    import webapp.routes as routes

    app = FastAPI()
    calls = []
    for name in (
            "register_health_routes",
            "register_storage_routes",
            "register_document_routes",
            "register_markdown_image_routes",
            "register_oauth_routes",
            "register_skills_routes",
            "register_chat_thread_routes",
    ):
        monkeypatch.setattr(routes, name, lambda _app: None)
    monkeypatch.setattr(routes._cfg, "PASSKEY_ENABLED", True, raising=False)
    monkeypatch.setattr(routes._cfg, "PASSKEY_CONFIG", "config", raising=False)
    monkeypatch.setattr(routes._cfg, "PASSKEY_SERVICE", "service", raising=False)
    monkeypatch.setattr(
        routes,
        "register_passkey_routes",
        lambda actual_app, *, service, config: calls.append(
            (actual_app, service, config)
        ),
        raising=False,
    )

    routes.register_all_routes(app)

    assert calls == [(app, "service", "config")]


def test_application_oauth_cookie_uses_validated_secret_and_secure_flag():
    source = (Path(__file__).resolve().parents[1] / "webapp" / "__init__.py").read_text(
        encoding="utf-8"
    )

    assert "secret_key=_config.OAUTH_SESSION_SECRET" in source
    assert "https_only=_config.PASSKEY_CONFIG.oauth_cookie_secure" in source
    assert "oauth-session-secret-key-fallback" not in source


@pytest.mark.parametrize(
    ("return_path", "expected"),
    [
        ("/chat?manage=passkeys", ["/chat?manage=passkeys"]),
        ("//evil.example", None),
    ],
)
def test_oauth_callback_forwards_only_safe_passkey_management_return_path(
        monkeypatch, return_path, expected
):
    import webapp.routes as routes

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
    monkeypatch.setattr(routes._cfg, "OAUTH_ENABLED", True)
    monkeypatch.setattr(routes._cfg, "FRONTEND_ORIGINS", ["http://testserver"])
    monkeypatch.setattr(
        routes._cfg,
        "get_oauth_login_url",
        AsyncMock(return_value="https://accounts.example/authorize"),
    )
    monkeypatch.setattr(
        routes._cfg,
        "handle_google_callback",
        AsyncMock(return_value={"session_token": "raw-session-token"}),
    )
    routes.register_oauth_routes(app)
    client = TestClient(app, follow_redirects=False)

    login = client.get(
        "/auth/login/google",
        params={"redirect_url": "http://testserver", "return_path": return_path},
    )
    callback = client.get("/auth/callback/google")

    assert login.status_code in {302, 307}
    assert login.headers["location"] == "https://accounts.example/authorize"
    assert callback.status_code in {302, 307}
    query = parse_qs(urlsplit(callback.headers["location"]).query)
    assert query["token"] == ["raw-session-token"]
    assert query.get("return_path") == expected


def test_existing_session_validation_exposes_optional_passkey_auth_method(
        monkeypatch,
):
    import webapp.routes as routes

    user = {
        "identity": "google:subject-123",
        "provider": "google",
        "email": "person@example.com",
        "name": "Person",
        "auth_method": "passkey",
    }

    def load_session(_token):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return user

    app = FastAPI()
    monkeypatch.setattr(routes._cfg, "OAUTH_ENABLED", True)
    monkeypatch.setattr(
        routes._cfg,
        "user_manager",
        SimpleNamespace(
            validate_session=load_session,
            refresh_session=load_session,
        ),
    )
    routes.register_oauth_routes(app)
    client = TestClient(app)

    validated = client.get(
        "/auth/session/validate", headers={"X-API-Key": "session-token"}
    )
    refreshed = client.post(
        "/auth/session/refresh", headers={"X-API-Key": "session-token"}
    )

    assert validated.status_code == 200
    assert validated.json()["user"]["auth_method"] == "passkey"
    assert refreshed.status_code == 200
    assert refreshed.json()["user"]["auth_method"] == "passkey"
