#!/usr/bin/env python3
"""Merge managed Azure Container App YAML into an existing JSON resource."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

LIST_IDENTITIES = {
    "containers": "name",
    "env": "name",
    "registries": "server",
    "secrets": "name",
    "volumes": "name",
    "volumeMounts": "volumeName",
}


def _merge(existing: object, desired: object, path: tuple[str, ...] = ()) -> object:
    if isinstance(existing, dict) and isinstance(desired, dict):
        result = dict(existing)
        for key, value in desired.items():
            result[key] = (
                _merge(result[key], value, (*path, key)) if key in result else value
            )
        return result
    if isinstance(existing, list) and isinstance(desired, list):
        identity = LIST_IDENTITIES.get(path[-1] if path else "")
        if identity is None:
            return existing
        if not all(
            isinstance(item, dict) and identity in item for item in existing + desired
        ):
            raise ValueError(f"invalid named array at {'.'.join(path)}")
        result = list(existing)
        positions = {item[identity]: index for index, item in enumerate(result)}
        if len(positions) != len(result):
            raise ValueError(f"duplicate array identity at {'.'.join(path)}")
        for item in desired:
            item_identity = item[identity]
            if item_identity in positions:
                index = positions[item_identity]
                result[index] = _merge(result[index], item, (*path, str(item_identity)))
            else:
                if path and path[-1] == "containers":
                    raise ValueError(
                        "managed container is absent from existing topology"
                    )
                positions[item_identity] = len(result)
                result.append(item)
        return result
    return desired


def main() -> int:
    """Parse, validate, merge, and write resulting resource YAML."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing-json", required=True, type=Path)
    parser.add_argument("--desired-yaml", required=True, type=Path)
    parser.add_argument("--output-yaml", required=True, type=Path)
    arguments = parser.parse_args()
    with arguments.existing_json.open(encoding="utf-8") as stream:
        existing = json.load(stream)
    with arguments.desired_yaml.open(encoding="utf-8") as stream:
        desired = yaml.safe_load(stream)
    if not isinstance(existing, dict) or not isinstance(desired, dict):
        raise ValueError("Azure Container App configurations must be mappings")
    merged = _merge(existing, desired)
    with arguments.output_yaml.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(merged, stream, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
