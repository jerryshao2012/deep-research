#!/usr/bin/env python3
"""Validate metadata-only Azure Key Vault secret-version query output."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


class ValidationError(Exception):
    """Raised when supplied metadata is unsafe or does not match expectations."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValidationError("invalid JSON number")


def validate(path: Path, vault_name: str, secret_name: str) -> None:
    """Validate exact version metadata and require at least one enabled version."""
    if re.fullmatch(r"[A-Za-z0-9-]+", vault_name) is None:
        raise ValidationError("invalid expected vault name")
    if re.fullmatch(r"[A-Za-z0-9-]+", secret_name) is None:
        raise ValidationError("invalid expected secret name")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError("invalid secret-version JSON metadata") from error
    if not isinstance(payload, list):
        raise ValidationError("secret-version metadata must be an array")

    seen_ids: set[str] = set()
    seen_versions: set[str] = set()
    enabled_found = False
    for raw_item in payload:
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "id",
            "name",
            "version",
            "enabled",
        }:
            raise ValidationError("invalid secret-version metadata schema")
        secret_id = raw_item["id"]
        name = raw_item["name"]
        reported_version = raw_item["version"]
        enabled = raw_item["enabled"]
        if not isinstance(secret_id, str) or not secret_id:
            raise ValidationError("invalid versioned secret ID metadata")
        if not isinstance(name, str) or name.casefold() != secret_name.casefold():
            raise ValidationError("secret name metadata does not match expected name")
        if not isinstance(enabled, bool):
            raise ValidationError("invalid enabled secret-version metadata")

        try:
            parsed = urlsplit(secret_id)
            parsed_port = parsed.port
        except ValueError as error:
            raise ValidationError(
                "versioned secret ID vault/name binding is invalid"
            ) from error
        parts = parsed.path.split("/")
        if (
            parsed.scheme != "https"
            or parsed.hostname != f"{vault_name}.vault.azure.net"
            or parsed.username is not None
            or parsed.password is not None
            or parsed_port is not None
            or parsed.query
            or parsed.fragment
            or len(parts) != 4
            or parts[0] != ""
            or parts[1].casefold() != "secrets"
            or parts[2].casefold() != secret_name.casefold()
            or not parts[3]
        ):
            raise ValidationError("versioned secret ID vault/name binding is invalid")
        version = parts[3]
        if reported_version is not None and (
            not isinstance(reported_version, str) or reported_version != version
        ):
            raise ValidationError("reported secret version does not match versioned ID")
        folded_id = secret_id.casefold()
        folded_version = version.casefold()
        if folded_id in seen_ids or folded_version in seen_versions:
            raise ValidationError("duplicate secret-version metadata")
        seen_ids.add(folded_id)
        seen_versions.add(folded_version)
        enabled_found = enabled_found or enabled

    if not enabled_found:
        raise ValidationError("no enabled secret version metadata")


def main() -> int:
    """Validate CLI arguments without emitting metadata bytes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("vault_name")
    parser.add_argument("secret_name")
    arguments = parser.parse_args()
    try:
        validate(arguments.metadata, arguments.vault_name, arguments.secret_name)
    except ValidationError as error:
        print(f"Error: {error}", file=sys.stderr)  # noqa: T201
        return 65
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
