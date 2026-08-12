#!/usr/bin/env python3
"""Render deployment-owned Azure Container App configuration safely."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def _require_match(label: str, value: str, pattern: str) -> str:
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _validate_frontend_urls(value: str) -> str:
    if not value or any(character in value for character in "\r\n\x00"):
        raise ValueError("invalid frontend URLs")
    for url in value.split(","):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in ("", "/")
        ):
            raise ValueError("invalid frontend URLs")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid frontend URLs")
    return value


def _secret(name: str, vault_name: str, secret_name: str, identity: str) -> dict:
    return {
        "name": name,
        "keyVaultUrl": f"https://{vault_name}.vault.azure.net/secrets/{secret_name}",
        "identity": identity,
    }


def _value(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


def _secret_ref(name: str, reference: str) -> dict[str, str]:
    return {"name": name, "secretRef": reference}


def main() -> int:
    """Validate scalar inputs and serialize complete desired YAML."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker-username", required=True)
    parser.add_argument("--build-version", required=True)
    parser.add_argument("--identity-id", required=True)
    parser.add_argument("--key-vault-name", required=True)
    parser.add_argument("--frontend-urls", required=True)
    parser.add_argument("--storage-name", required=True)
    parser.add_argument("--restart-trigger", required=True)
    parser.add_argument("--revision-suffix", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()

    username = _require_match(
        "Docker Hub username", arguments.docker_username, r"[a-z0-9][a-z0-9_-]{0,254}"
    )
    version = _require_match("build version", arguments.build_version, r"[0-9]{14}")
    identity = _require_match(
        "managed identity resource ID", arguments.identity_id, r"/[A-Za-z0-9_./()-]+"
    )
    vault_name = _require_match(
        "Key Vault name",
        arguments.key_vault_name,
        r"[A-Za-z0-9][A-Za-z0-9-]{1,22}[A-Za-z0-9]",
    )
    frontend_urls = _validate_frontend_urls(arguments.frontend_urls)
    storage_name = _require_match(
        "Container Apps storage name",
        arguments.storage_name,
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}",
    )
    restart_trigger = _require_match(
        "restart trigger", arguments.restart_trigger, r"[0-9]{1,20}"
    )
    revision_suffix = _require_match(
        "revision suffix",
        arguments.revision_suffix,
        r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?",
    )
    if "--" in revision_suffix:
        raise ValueError("invalid revision suffix")

    secret_specs = (
        ("tavily-api-key", "TAVILY-API-KEY"),
        ("langchain-api-key", "LANGCHAIN-API-KEY"),
        ("upload-api-key", "UPLOAD-API-KEY"),
        ("storage-account-name", "STORAGE-ACCOUNT-NAME"),
        ("storage-account-key", "STORAGE-ACCOUNT-KEY"),
        ("azure-storage-container-name", "AZURE-STORAGE-CONTAINER-NAME"),
        ("google-api-key", "GOOGLE-API-KEY"),
        ("docker-hub-pat", "DOCKER-HUB-PAT"),
        ("passkey-proxy-secret", "PASSKEY-PROXY-SECRET"),
    )
    values = (
        ("RESTART_TRIGGER", restart_trigger),
        ("VERIFY_SSL", "false"),
        ("LOG_LEVEL", "INFO"),
        ("LANGCHAIN_TRACING_V2", "true"),
        ("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
        ("LANGCHAIN_PROJECT", "deep-research-production"),
        ("ENABLE_EVAL_TRACKING", "true"),
        ("ALLOW_ALL_THREADS", "false"),
        ("MODEL_TPM", "120000"),
        ("MODEL_RPM", "500"),
        ("GRAPH_RECURSION_LIMIT", "200"),
        ("MAX_CONCURRENT_RESEARCH_UNITS", "3"),
        ("MAX_RESEARCHER_ITERATIONS", "3"),
        ("MAX_GLOB_DEPTH", "3"),
        ("MAX_FILES_TO_READ", "20"),
        ("MAX_TOTAL_SIZE_MB", "50"),
        ("MODEL_MAX_RETRIES", "5"),
        ("MODEL_INITIAL_BACKOFF", "1.0"),
        ("MODEL_MAX_BACKOFF", "60.0"),
        ("MODEL_BACKOFF_MULTIPLIER", "2.0"),
        ("MODEL_RETRY_JITTER", "true"),
        ("DB_TYPE", "sqlite"),
        ("MEMORY_TYPE", ""),
        ("REPORTS_OUTPUT_FOLDER", "/deps/deep_research/output"),
        (
            "EVAL_HISTORY_FILE",
            "/deps/deep_research/output/eval_history/server_runs.jsonl",
        ),
        ("DOC_FOLDER", "/deps/deep_research/docs"),
        ("WIKI_BASE_DIR", "/deps/deep_research"),
        ("INPUT_FOLDER", "/deps/deep_research/input"),
        ("SQLITE_DB_PATH", "/mnt/auth/auth.db"),
        ("AUTH_SQLITE_JOURNAL_MODE", "DELETE"),
        ("FRONTEND_URLS", frontend_urls),
        ("PASSKEY_DERIVE_FROM_FRONTEND_URLS", "true"),
        ("PASSKEY_ENABLED", "true"),
        ("PASSKEY_PROXY_ID", "web-bff"),
    )
    references = (
        ("PASSKEY_PROXY_SECRET", "passkey-proxy-secret"),
        ("TAVILY_API_KEY", "tavily-api-key"),
        ("LANGCHAIN_API_KEY", "langchain-api-key"),
        ("UPLOAD_API_KEY", "upload-api-key"),
        ("STORAGE_ACCOUNT_NAME", "storage-account-name"),
        ("STORAGE_ACCOUNT_KEY", "storage-account-key"),
        ("AZURE_STORAGE_CONTAINER_NAME", "azure-storage-container-name"),
        ("GOOGLE_API_KEY", "google-api-key"),
    )
    desired = {
        "properties": {
            "configuration": {
                "ingress": {"external": True, "targetPort": 2024, "transport": "auto"},
                "secrets": [
                    _secret(name, vault_name, secret_name, identity)
                    for name, secret_name in secret_specs
                ],
                "registries": [
                    {
                        "server": "docker.io",
                        "username": username,
                        "passwordSecretRef": "docker-hub-pat",
                    }
                ],
            },
            "template": {
                "revisionSuffix": revision_suffix,
                "containers": [
                    {
                        "name": "deep-research-agent",
                        "image": f"{username}/deep-research-agent:{version}",
                        "resources": {"cpu": 2.0, "memory": "4Gi"},
                        "env": [
                            *(_value(name, value) for name, value in values),
                            *(
                                _secret_ref(name, reference)
                                for name, reference in references
                            ),
                        ],
                        "volumeMounts": [
                            {"volumeName": "auth-sqlite", "mountPath": "/mnt/auth"}
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "auth-sqlite",
                        "storageType": "AzureFile",
                        "storageName": storage_name,
                    }
                ],
                "scale": {"minReplicas": 0, "maxReplicas": 1},
            },
        }
    }
    with arguments.output.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(desired, stream, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
