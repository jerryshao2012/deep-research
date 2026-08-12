#!/usr/bin/env python3
"""Fail closed unless Azure role definitions grant effective Key Vault secret read."""

from __future__ import annotations

import fnmatch
import json
import sys

SECRET_READ_ACTION = "Microsoft.KeyVault/vaults/secrets/getSecret/action"


def _matches(pattern: str, action: str) -> bool:
    return fnmatch.fnmatchcase(action.casefold(), pattern.casefold())


def main() -> int:
    """Return success only for an effective secret-read data-plane grant."""
    try:
        roles = json.load(sys.stdin)
        if not isinstance(roles, list) or not roles:
            raise ValueError
        allowed = False
        for role in roles:
            if not isinstance(role, dict) or not isinstance(
                role.get("permissions"), list
            ):
                raise ValueError
            if not role["permissions"]:
                raise ValueError
            data_actions: list[str] = []
            excluded_actions: list[str] = []
            for permission in role["permissions"]:
                if not isinstance(permission, dict):
                    raise ValueError
                for key in ("actions", "notActions", "dataActions", "notDataActions"):
                    values = permission.get(key)
                    if not isinstance(values, list) or not all(
                        isinstance(value, str) and value for value in values
                    ):
                        raise ValueError
                data_actions.extend(permission["dataActions"])
                excluded_actions.extend(permission["notDataActions"])
            role_allows = any(
                _matches(pattern, SECRET_READ_ACTION) for pattern in data_actions
            ) and not any(
                _matches(pattern, SECRET_READ_ACTION) for pattern in excluded_actions
            )
            allowed = allowed or role_allows
        return 0 if allowed else 1
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        sys.stderr.write("Error: invalid role definition response\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
