#!/usr/bin/env python3
"""Capture and compare Azure passkey deployment security metadata."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SECRET_NAMES = (
    "TAVILY-API-KEY",
    "LANGCHAIN-API-KEY",
    "UPLOAD-API-KEY",
    "STORAGE-ACCOUNT-NAME",
    "STORAGE-ACCOUNT-KEY",
    "AZURE-STORAGE-CONTAINER-NAME",
    "GOOGLE-API-KEY",
    "DOCKER-HUB-PAT",
    "PASSKEY-PROXY-SECRET",
)

APP_QUERY = (
    "{id:id,location:location,identity:identity,"
    "environmentId:properties.managedEnvironmentId,"
    "secretMetadata:properties.configuration.secrets[]."
    "{name:name,keyVaultUrl:keyVaultUrl,identity:identity},"
    "registries:properties.configuration.registries[]."
    "{server:server,username:username,passwordSecretRef:passwordSecretRef},"
    "revisionSuffix:properties.template.revisionSuffix,"
    "latestRevisionName:properties.latestRevisionName,"
    "containers:properties.template.containers[]."
    "{name:name,image:image,volumeMounts:volumeMounts},"
    "volumes:properties.template.volumes}"
)
VAULT_QUERY = (
    "{id:id,location:location,"
    "enableRbacAuthorization:properties.enableRbacAuthorization,"
    "accessPolicies:properties.accessPolicies}"
)
SECRET_QUERY = (
    "{id:id,attributes:{enabled:attributes.enabled,created:attributes.created,"
    "updated:attributes.updated,recoveryLevel:attributes.recoveryLevel}}"
)
ROLE_QUERY = (
    "[].{id:id,principalId:principalId,roleDefinitionId:roleDefinitionId,"
    "scope:scope,condition:condition,conditionVersion:conditionVersion}"
)
ENVIRONMENT_QUERY = "{id:id,location:location}"
STORAGE_QUERY = (
    "[].{name:name,azureFile:{accountName:properties.azureFile.accountName,"
    "shareName:properties.azureFile.shareName,"
    "accessMode:properties.azureFile.accessMode}}"
)


class SnapshotError(ValueError):
    """Safe metadata capture or comparison failure."""


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotError(f"invalid {label} metadata")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise SnapshotError(f"invalid {label} metadata")
    return value


def _string(value: object, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n\x00"):
        raise SnapshotError(f"invalid {label} metadata")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SnapshotError(f"invalid {label} metadata schema")


def _az_json(arguments: list[str], label: str) -> Any:
    result = subprocess.run(
        ["az", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SnapshotError(f"Azure {label} metadata query failed")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SnapshotError(
            f"Azure {label} metadata query returned invalid JSON"
        ) from error


def _principal_ids(identity: dict[str, Any], label: str) -> list[str]:
    principals: set[str] = set()
    principal = identity.get("principalId")
    if principal is not None:
        principals.add(_string(principal, f"{label} principal") or "")
    assigned = identity.get("userAssignedIdentities", {})
    if not isinstance(assigned, dict):
        raise SnapshotError(f"invalid {label} identity metadata")
    for resource_id, metadata in assigned.items():
        _string(resource_id, f"{label} identity resource ID")
        item = _mapping(metadata, f"{label} user-assigned identity")
        assigned_principal = item.get("principalId")
        if assigned_principal is not None:
            principals.add(
                _string(assigned_principal, f"{label} assigned principal") or ""
            )
    if not principals:
        raise SnapshotError(f"{label} has no capturable identity principal metadata")
    return sorted(principals)


def _normalize_named_metadata(
    values: object, keys: set[str], identity: str, label: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _list(values, label):
        item = _mapping(raw, label)
        _exact_keys(item, keys, label)
        name = _string(item.get(identity), f"{label} {identity}")
        assert name is not None
        if name.casefold() in seen:
            raise SnapshotError(f"duplicate {label} metadata")
        seen.add(name.casefold())
        normalized: dict[str, Any] = {}
        for key in sorted(keys):
            normalized[key] = _string(
                item.get(key), f"{label} {key}", nullable=key != identity
            )
        result.append(normalized)
    return sorted(result, key=lambda item: str(item[identity]).casefold())


def _normalize_app(raw: object, label: str) -> tuple[dict[str, Any], list[str]]:
    app = _mapping(raw, label)
    _exact_keys(
        app,
        {
            "id",
            "location",
            "identity",
            "environmentId",
            "secretMetadata",
            "registries",
            "revisionSuffix",
            "latestRevisionName",
            "containers",
            "volumes",
        },
        label,
    )
    identity = _mapping(app["identity"], f"{label} identity")
    principals = _principal_ids(identity, label)
    containers = _list(app["containers"], f"{label} containers")
    images: list[str] = []
    mounts: list[dict[str, Any]] = []
    for raw_container in containers:
        container = _mapping(raw_container, f"{label} container")
        _exact_keys(container, {"name", "image", "volumeMounts"}, f"{label} container")
        container_name = _string(container["name"], f"{label} container name")
        image = _string(container["image"], f"{label} container image")
        assert container_name is not None and image is not None
        images.append(image)
        for raw_mount in _list(container["volumeMounts"] or [], f"{label} mounts"):
            mount = _mapping(raw_mount, f"{label} mount")
            mounts.append({key: mount[key] for key in sorted(mount)})
    volumes = []
    for raw_volume in _list(app["volumes"] or [], f"{label} volumes"):
        volume = _mapping(raw_volume, f"{label} volume")
        volumes.append({key: volume[key] for key in sorted(volume) if key != "secrets"})
    normalized = {
        "id": _string(app["id"], f"{label} resource ID"),
        "location": _string(app["location"], f"{label} location"),
        "environment_id": _string(app["environmentId"], f"{label} environment"),
        "identity": identity,
        "secret_references": _normalize_named_metadata(
            app["secretMetadata"],
            {"name", "keyVaultUrl", "identity"},
            "name",
            f"{label} secret reference",
        ),
        "registries": _normalize_named_metadata(
            app["registries"],
            {"server", "username", "passwordSecretRef"},
            "server",
            f"{label} registry",
        ),
        "storage_bindings": {
            "volume_mounts": sorted(
                mounts, key=lambda item: json.dumps(item, sort_keys=True)
            ),
            "volumes": sorted(
                volumes, key=lambda item: json.dumps(item, sort_keys=True)
            ),
        },
        "deployment": {
            "revision_suffix": _string(
                app["revisionSuffix"], f"{label} revision suffix", nullable=True
            ),
            "latest_revision_name": _string(
                app["latestRevisionName"], f"{label} latest revision", nullable=True
            ),
            "images": sorted(images),
        },
    }
    return normalized, principals


def _normalize_roles(raw: object) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    keys = {
        "id",
        "principalId",
        "roleDefinitionId",
        "scope",
        "condition",
        "conditionVersion",
    }
    for raw_role in _list(raw, "role assignments"):
        role = _mapping(raw_role, "role assignment")
        _exact_keys(role, keys, "role assignment")
        result.append(
            {
                key: _string(
                    role[key],
                    f"role assignment {key}",
                    nullable=key.startswith("condition"),
                )
                for key in sorted(keys)
            }
        )
    return sorted(result, key=lambda item: str(item["id"]).casefold())


def _arm_name(resource_id: str, resource_type: str) -> tuple[str, str]:
    pattern = rf"^/subscriptions/[^/]+/resourceGroups/([^/]+)/providers/{re.escape(resource_type)}/([^/]+)$"
    match = re.fullmatch(pattern, resource_id, re.IGNORECASE)
    if match is None:
        raise SnapshotError("invalid Container Apps environment resource ID metadata")
    return match.group(1), match.group(2)


def _capture(arguments: argparse.Namespace) -> dict[str, Any]:
    common = ["--subscription", arguments.subscription]
    apps: dict[str, dict[str, Any]] = {}
    principals: set[str] = set()
    for logical_name, app_name in (
        ("backend", arguments.backend_app),
        ("ui", arguments.ui_app),
    ):
        raw = _az_json(
            [
                "containerapp",
                "show",
                *common,
                "--resource-group",
                arguments.resource_group,
                "--name",
                app_name,
                "--query",
                APP_QUERY,
                "--output",
                "json",
            ],
            f"{logical_name} Container App",
        )
        apps[logical_name], app_principals = _normalize_app(raw, logical_name)
        principals.update(app_principals)

    vault_raw = _mapping(
        _az_json(
            [
                "keyvault",
                "show",
                *common,
                "--resource-group",
                arguments.resource_group,
                "--name",
                arguments.vault_name,
                "--query",
                VAULT_QUERY,
                "--output",
                "json",
            ],
            "Key Vault",
        ),
        "Key Vault",
    )
    _exact_keys(
        vault_raw,
        {"id", "location", "enableRbacAuthorization", "accessPolicies"},
        "Key Vault",
    )
    if not isinstance(vault_raw["enableRbacAuthorization"], bool):
        raise SnapshotError("invalid Key Vault RBAC metadata")
    policies = _list(vault_raw["accessPolicies"] or [], "Key Vault access policies")
    normalized_policies = sorted(
        (_mapping(policy, "Key Vault access policy") for policy in policies),
        key=lambda policy: json.dumps(policy, sort_keys=True),
    )
    secrets: list[dict[str, Any]] = []
    for secret_name in SECRET_NAMES:
        raw_secret = _mapping(
            _az_json(
                [
                    "keyvault",
                    "secret",
                    "show",
                    *common,
                    "--vault-name",
                    arguments.vault_name,
                    "--name",
                    secret_name,
                    "--query",
                    SECRET_QUERY,
                    "--output",
                    "json",
                ],
                f"Key Vault secret {secret_name}",
            ),
            "Key Vault secret",
        )
        _exact_keys(raw_secret, {"id", "attributes"}, "Key Vault secret")
        secret_id = _string(raw_secret["id"], "Key Vault secret ID")
        assert secret_id is not None
        version = secret_id.rstrip("/").rsplit("/", 1)[-1]
        if not version or version.casefold() == secret_name.casefold():
            raise SnapshotError("invalid versioned Key Vault secret ID metadata")
        attributes = _mapping(raw_secret["attributes"], "Key Vault secret attributes")
        _exact_keys(
            attributes,
            {"enabled", "created", "updated", "recoveryLevel"},
            "secret attributes",
        )
        secrets.append(
            {
                "name": secret_name,
                "id": secret_id,
                "version": version,
                "attributes": attributes,
            }
        )

    roles: list[dict[str, Any]] = []
    for principal in sorted(principals):
        roles.extend(
            _normalize_roles(
                _az_json(
                    [
                        "role",
                        "assignment",
                        "list",
                        *common,
                        "--assignee-object-id",
                        principal,
                        "--all",
                        "--query",
                        ROLE_QUERY,
                        "--output",
                        "json",
                    ],
                    "role assignment",
                )
            )
        )
    roles.sort(key=lambda item: str(item["id"]).casefold())

    environment_ids = {str(app["environment_id"]) for app in apps.values()}
    if len(environment_ids) != 1:
        raise SnapshotError("backend and UI environment binding metadata differ")
    environment_id = environment_ids.pop()
    environment_group, environment_name = _arm_name(
        environment_id, "Microsoft.App/managedEnvironments"
    )
    environment = _mapping(
        _az_json(
            [
                "containerapp",
                "env",
                "show",
                *common,
                "--resource-group",
                environment_group,
                "--name",
                environment_name,
                "--query",
                ENVIRONMENT_QUERY,
                "--output",
                "json",
            ],
            "Container Apps environment",
        ),
        "Container Apps environment",
    )
    _exact_keys(environment, {"id", "location"}, "Container Apps environment")
    storage = _az_json(
        [
            "containerapp",
            "env",
            "storage",
            "list",
            *common,
            "--resource-group",
            environment_group,
            "--name",
            environment_name,
            "--query",
            STORAGE_QUERY,
            "--output",
            "json",
        ],
        "Container Apps environment storage",
    )
    normalized_storage = sorted(
        (
            _mapping(item, "environment storage")
            for item in _list(storage, "environment storage")
        ),
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    return {
        "schema_version": 1,
        "apps": apps,
        "azure_environment": {
            "id": _string(environment["id"], "environment ID"),
            "location": _string(environment["location"], "environment location"),
            "storage": normalized_storage,
        },
        "key_vault": {
            "id": _string(vault_raw["id"], "Key Vault ID"),
            "location": _string(vault_raw["location"], "Key Vault location"),
            "rbac_enabled": vault_raw["enableRbacAuthorization"],
            "access_policies": normalized_policies,
            "secrets": sorted(secrets, key=lambda item: item["name"]),
        },
        "role_assignments": roles,
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise SnapshotError("snapshot output directory does not exist")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _load_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapshotError("snapshot file is unreadable or invalid JSON") from error
    snapshot = _mapping(payload, "snapshot")
    _exact_keys(
        snapshot,
        {
            "schema_version",
            "apps",
            "azure_environment",
            "key_vault",
            "role_assignments",
        },
        "snapshot",
    )
    if snapshot["schema_version"] != 1:
        raise SnapshotError("unsupported snapshot schema version")
    apps = _mapping(snapshot["apps"], "snapshot apps")
    _exact_keys(apps, {"backend", "ui"}, "snapshot apps")
    for label in ("backend", "ui"):
        app = _mapping(apps[label], f"snapshot {label}")
        _exact_keys(
            app,
            {
                "id",
                "location",
                "environment_id",
                "identity",
                "secret_references",
                "registries",
                "storage_bindings",
                "deployment",
            },
            f"snapshot {label}",
        )
        _string(app["id"], f"snapshot {label} resource ID")
        _string(app["location"], f"snapshot {label} location")
        _string(app["environment_id"], f"snapshot {label} environment")
        _mapping(app["identity"], f"snapshot {label} identity")
        _list(app["secret_references"], f"snapshot {label} secret references")
        _list(app["registries"], f"snapshot {label} registries")
        storage = _mapping(app["storage_bindings"], f"snapshot {label} storage")
        _exact_keys(
            storage,
            {"volume_mounts", "volumes"},
            f"snapshot {label} storage",
        )
        _list(storage["volume_mounts"], f"snapshot {label} volume mounts")
        _list(storage["volumes"], f"snapshot {label} volumes")
        deployment = _mapping(app.get("deployment"), f"snapshot {label} deployment")
        _exact_keys(
            deployment,
            {"revision_suffix", "latest_revision_name", "images"},
            f"snapshot {label} deployment",
        )
        _list(deployment["images"], f"snapshot {label} images")
    environment = _mapping(snapshot["azure_environment"], "snapshot environment")
    _exact_keys(environment, {"id", "location", "storage"}, "snapshot environment")
    _string(environment["id"], "snapshot environment ID")
    _string(environment["location"], "snapshot environment location")
    _list(environment["storage"], "snapshot environment storage")
    vault = _mapping(snapshot["key_vault"], "snapshot Key Vault")
    _exact_keys(
        vault,
        {"id", "location", "rbac_enabled", "access_policies", "secrets"},
        "snapshot Key Vault",
    )
    _string(vault["id"], "snapshot Key Vault ID")
    _string(vault["location"], "snapshot Key Vault location")
    if not isinstance(vault["rbac_enabled"], bool):
        raise SnapshotError("invalid snapshot Key Vault RBAC metadata schema")
    _list(vault["access_policies"], "snapshot Key Vault access policies")
    _list(vault["secrets"], "snapshot Key Vault secrets")
    _list(snapshot["role_assignments"], "snapshot role assignments")
    return snapshot


def _protected_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    protected = copy.deepcopy(snapshot)
    for app in protected["apps"].values():
        app.pop("deployment")
    return protected


def _first_difference(before: object, after: object, path: str = "$") -> str | None:
    if type(before) is not type(after):
        return path
    if isinstance(before, dict):
        if set(before) != set(after):
            return path
        for key in sorted(before):
            difference = _first_difference(before[key], after[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(before, list):
        if len(before) != len(after):
            return path
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            difference = _first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
        return None
    return None if before == after else path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture metadata-only Azure passkey deployment state or compare two snapshots."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser(
        "capture", help="Query read-only Azure metadata and write a mode-0600 snapshot."
    )
    capture.add_argument("--subscription", required=True)
    capture.add_argument("--resource-group", required=True)
    capture.add_argument("--vault-name", required=True)
    capture.add_argument("--backend-app", required=True)
    capture.add_argument("--ui-app", required=True)
    capture.add_argument("--output", required=True, type=Path)
    compare = subparsers.add_parser(
        "compare", help="Fail when protected metadata differs between snapshots."
    )
    compare.add_argument("--before", required=True, type=Path)
    compare.add_argument("--after", required=True, type=Path)
    return parser


def main() -> int:
    """Run metadata capture or comparison without emitting queried payloads."""
    arguments = _parser().parse_args()
    try:
        if arguments.command == "capture":
            _write_atomic(arguments.output, _capture(arguments))
            return 0
        before = _load_snapshot(arguments.before)
        after = _load_snapshot(arguments.after)
        difference = _first_difference(
            _protected_snapshot(before), _protected_snapshot(after)
        )
        if difference:
            raise SnapshotError(f"protected Azure metadata drift at {difference}")
        return 0
    except SnapshotError as error:
        print(f"Error: {error}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
