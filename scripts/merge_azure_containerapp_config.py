#!/usr/bin/env python3
"""Build a narrow Azure Container App template JSON Merge Patch."""

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

SERVER_GENERATED_CONTAINER_FIELDS = {"imageType"}


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


def _strip_server_generated_fields(template: dict[str, object]) -> None:
    for field in ("containers", "initContainers"):
        containers = template.get(field)
        if containers is None:
            continue
        if not isinstance(containers, list) or not all(
            isinstance(container, dict) for container in containers
        ):
            raise ValueError(f"Azure Container App {field} must be a list of mappings")
        for container in containers:
            for generated_field in SERVER_GENERATED_CONTAINER_FIELDS:
                container.pop(generated_field, None)


def main() -> int:
    """Parse, validate, merge template state, and write a narrow patch."""
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
    existing_location = existing.get("location")
    existing_properties = existing.get("properties")
    desired_properties = desired.get("properties")
    if not isinstance(existing_location, str) or not existing_location:
        raise ValueError("existing Azure Container App location is required")
    if not isinstance(existing_properties, dict) or not isinstance(
        desired_properties, dict
    ):
        raise ValueError("Azure Container App properties must be mappings")
    existing_template = existing_properties.get("template")
    desired_template = desired_properties.get("template")
    if not isinstance(existing_template, dict) or not isinstance(
        desired_template, dict
    ):
        raise ValueError("Azure Container App templates must be mappings")
    merged_template = _merge(existing_template, desired_template, ("template",))
    _strip_server_generated_fields(merged_template)
    merged = {
        "location": existing_location,
        "properties": {"template": merged_template},
    }
    with arguments.output_yaml.open("w", encoding="utf-8") as stream:
        json.dump(merged, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
