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
from urllib.parse import urlsplit

SECRET_NAMES = (
    "TAVILY-API-KEY",
    "LANGCHAIN-API-KEY",
    "UPLOAD-API-KEY",
    "STORAGE-ACCOUNT-NAME",
    "STORAGE-ACCOUNT-KEY",
    "AZURE-STORAGE-CONTAINER-NAME",
    "GOOGLE-API-KEY",
    "GOOGLE-CLIENT-ID",
    "GOOGLE-CLIENT-SECRET",
    "OAUTH-SECRET-KEY",
    "DOCKER-HUB-PAT",
    "PASSKEY-PROXY-SECRET",
)

APP_QUERY = (
    "{id:id,location:location,identity:{type:identity.type,"
    "principalId:identity.principalId,tenantId:identity.tenantId,"
    "userAssignedIdentities:identity.userAssignedIdentities},"
    "environmentId:properties.managedEnvironmentId,"
    "secretMetadata:properties.configuration.secrets[]."
    "{name:name,keyVaultUrl:keyVaultUrl,identity:identity},"
    "registries:properties.configuration.registries[]."
    "{server:server,username:username,passwordSecretRef:passwordSecretRef},"
    "revisionSuffix:properties.template.revisionSuffix,"
    "latestRevisionName:properties.latestRevisionName,"
    "containers:properties.template.containers[]."
    "{name:name,image:image,volumeMounts:volumeMounts[]."
    "{volumeName:volumeName,mountPath:mountPath,subPath:subPath}},"
    "volumes:properties.template.volumes[]."
    "{name:name,storageName:storageName,storageType:storageType}}"
)
VAULT_QUERY = (
    "{id:id,location:location,"
    "enableRbacAuthorization:properties.enableRbacAuthorization,"
    "accessPolicies:properties.accessPolicies[]."
    "{tenantId:tenantId,objectId:objectId,applicationId:applicationId,"
    "permissions:{certificates:permissions.certificates,keys:permissions.keys,"
    "secrets:permissions.secrets,storage:permissions.storage}}}"
)
SECRET_VERSIONS_QUERY = (
    "[].{id:id,name:name,version:version,enabled:attributes.enabled,"
    "created:attributes.created,updated:attributes.updated}"
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


def _nullable_string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in _list(value, label):
        item = _string(raw, label)
        assert item is not None
        if item.casefold() in seen:
            raise SnapshotError(f"duplicate {label} metadata")
        seen.add(item.casefold())
        result.append(item)
    return sorted(result, key=str.casefold)


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


def _normalize_identity(raw: object, label: str) -> tuple[dict[str, Any], list[str]]:
    identity = _mapping(raw, f"{label} identity")
    _exact_keys(
        identity,
        {"type", "principalId", "tenantId", "userAssignedIdentities"},
        f"{label} identity",
    )
    principals: set[str] = set()
    principal = identity.get("principalId")
    if principal is not None:
        principals.add(_string(principal, f"{label} principal") or "")
    assigned = _mapping(
        identity.get("userAssignedIdentities"), f"{label} user-assigned identities"
    )
    normalized_assigned: list[dict[str, str | None]] = []
    for resource_id, metadata in assigned.items():
        normalized_resource_id = _string(resource_id, f"{label} identity resource ID")
        item = _mapping(metadata, f"{label} user-assigned identity")
        if set(item) not in (set(), {"clientId", "principalId"}):
            raise SnapshotError(
                f"invalid {label} user-assigned identity metadata schema"
            )
        client_id = _string(
            item.get("clientId"), f"{label} assigned client", nullable=True
        )
        assigned_principal = item.get("principalId")
        normalized_principal = _string(
            assigned_principal, f"{label} assigned principal", nullable=True
        )
        if assigned_principal is not None:
            principals.add(normalized_principal or "")
        normalized_assigned.append(
            {
                "resource_id": normalized_resource_id,
                "client_id": client_id,
                "principal_id": normalized_principal,
            }
        )
    if not principals:
        raise SnapshotError(f"{label} has no capturable identity principal metadata")
    normalized = {
        "type": _string(identity["type"], f"{label} identity type"),
        "principal_id": _string(
            identity["principalId"], f"{label} principal", nullable=True
        ),
        "tenant_id": _string(
            identity["tenantId"], f"{label} identity tenant", nullable=True
        ),
        "user_assigned": sorted(
            normalized_assigned, key=lambda item: str(item["resource_id"]).casefold()
        ),
    }
    return normalized, sorted(principals)


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
    identity, principals = _normalize_identity(app["identity"], label)
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
            _exact_keys(mount, {"volumeName", "mountPath", "subPath"}, f"{label} mount")
            mounts.append(
                {
                    "container_name": container_name,
                    "volume_name": _string(
                        mount["volumeName"], f"{label} mount volume"
                    ),
                    "mount_path": _string(mount["mountPath"], f"{label} mount path"),
                    "sub_path": _string(
                        mount["subPath"], f"{label} mount subpath", nullable=True
                    ),
                }
            )
    volumes = []
    for raw_volume in _list(app["volumes"] or [], f"{label} volumes"):
        volume = _mapping(raw_volume, f"{label} volume")
        _exact_keys(volume, {"name", "storageName", "storageType"}, f"{label} volume")
        volumes.append(
            {
                "name": _string(volume["name"], f"{label} volume name"),
                "storage_name": _string(
                    volume["storageName"], f"{label} storage name", nullable=True
                ),
                "storage_type": _string(
                    volume["storageType"], f"{label} storage type", nullable=True
                ),
            }
        )
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
    seen: set[str] = set()
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
        role_id = _string(role["id"], "role assignment id")
        assert role_id is not None
        if role_id.casefold() in seen:
            raise SnapshotError("duplicate role assignment metadata")
        seen.add(role_id.casefold())
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


def _normalize_access_policies(raw: object) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for raw_policy in _list(raw, "Key Vault access policies"):
        policy = _mapping(raw_policy, "Key Vault access policy")
        _exact_keys(
            policy,
            {"tenantId", "objectId", "applicationId", "permissions"},
            "Key Vault access policy",
        )
        tenant_id = _string(policy["tenantId"], "Key Vault policy tenant")
        object_id = _string(policy["objectId"], "Key Vault policy object")
        application_id = _string(
            policy["applicationId"], "Key Vault policy application", nullable=True
        )
        assert tenant_id is not None and object_id is not None
        identity = (
            object_id.casefold(),
            application_id.casefold() if application_id else None,
        )
        if identity in seen:
            raise SnapshotError("duplicate Key Vault access policy metadata")
        seen.add(identity)
        permissions = _mapping(policy["permissions"], "Key Vault policy permissions")
        _exact_keys(
            permissions,
            {"certificates", "keys", "secrets", "storage"},
            "Key Vault policy permissions",
        )
        result.append(
            {
                "tenant_id": tenant_id,
                "object_id": object_id,
                "application_id": application_id,
                "permissions": {
                    name: _nullable_string_list(
                        permissions[name], f"Key Vault policy {name} permission"
                    )
                    for name in ("certificates", "keys", "secrets", "storage")
                },
            }
        )
    return sorted(
        result,
        key=lambda item: (
            str(item["object_id"]).casefold(),
            str(item["application_id"] or "").casefold(),
        ),
    )


def _normalize_environment_storage(raw: object) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in _list(raw, "environment storage"):
        item = _mapping(raw_item, "environment storage")
        _exact_keys(item, {"name", "azureFile"}, "environment storage")
        name = _string(item["name"], "environment storage name")
        assert name is not None
        if name.casefold() in seen:
            raise SnapshotError("duplicate environment storage metadata")
        seen.add(name.casefold())
        azure_file = _mapping(item["azureFile"], "environment Azure Files binding")
        _exact_keys(
            azure_file,
            {"accountName", "shareName", "accessMode"},
            "environment Azure Files binding",
        )
        result.append(
            {
                "name": name,
                "azure_file": {
                    "account_name": _string(
                        azure_file["accountName"], "environment storage account"
                    ),
                    "share_name": _string(
                        azure_file["shareName"], "environment storage share"
                    ),
                    "access_mode": _string(
                        azure_file["accessMode"], "environment storage access mode"
                    ),
                },
            }
        )
    return sorted(result, key=lambda item: str(item["name"]).casefold())


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
    normalized_policies = _normalize_access_policies(vault_raw["accessPolicies"] or [])
    secrets: list[dict[str, Any]] = []
    for secret_name in SECRET_NAMES:
        raw_versions = _list(
            _az_json(
                [
                    "keyvault",
                    "secret",
                    "list-versions",
                    *common,
                    "--vault-name",
                    arguments.vault_name,
                    "--name",
                    secret_name,
                    "--query",
                    SECRET_VERSIONS_QUERY,
                    "--output",
                    "json",
                ],
                f"Key Vault secret {secret_name}",
            ),
            "Key Vault secret versions",
        )
        versions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_versions: set[str] = set()
        for raw_version in raw_versions:
            item = _mapping(raw_version, "Key Vault secret version")
            _exact_keys(
                item,
                {"id", "name", "version", "enabled", "created", "updated"},
                "Key Vault secret version",
            )
            name = _string(item["name"], "Key Vault secret version name")
            secret_id = _string(item["id"], "Key Vault secret version ID")
            assert name is not None and secret_id is not None
            if name.casefold() != secret_name.casefold():
                raise SnapshotError("invalid Key Vault secret version name metadata")
            parsed_id = urlsplit(secret_id)
            path_parts = parsed_id.path.split("/")
            if (
                parsed_id.scheme != "https"
                or parsed_id.hostname != f"{arguments.vault_name}.vault.azure.net"
                or parsed_id.username is not None
                or parsed_id.password is not None
                or parsed_id.port is not None
                or parsed_id.query
                or parsed_id.fragment
                or len(path_parts) != 4
                or path_parts[0] != ""
                or path_parts[1].casefold() != "secrets"
                or path_parts[2].casefold() != secret_name.casefold()
                or not path_parts[3]
            ):
                raise SnapshotError("invalid versioned Key Vault secret ID metadata")
            version = path_parts[3]
            reported_version = item["version"]
            if reported_version is not None and (
                not isinstance(reported_version, str) or reported_version != version
            ):
                raise SnapshotError("invalid Key Vault secret version metadata")
            folded_id = secret_id.casefold()
            if folded_id in seen_ids:
                raise SnapshotError("duplicate Key Vault secret version ID metadata")
            seen_ids.add(folded_id)
            if version.casefold() in seen_versions:
                raise SnapshotError("duplicate Key Vault secret version metadata")
            seen_versions.add(version.casefold())
            if not isinstance(item["enabled"], bool):
                raise SnapshotError("invalid secret enabled metadata schema")
            versions.append(
                {
                    "id": secret_id,
                    "version": version,
                    "enabled": item["enabled"],
                    "created": _string(item["created"], "secret created timestamp"),
                    "updated": _string(item["updated"], "secret updated timestamp"),
                }
            )
        if not versions:
            raise SnapshotError(f"no secret version metadata for {secret_name}")
        versions.sort(key=lambda item: (str(item["version"]).casefold(), item["id"]))
        secrets.append(
            {
                "name": secret_name,
                "versions": versions,
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
    if len({str(role["id"]).casefold() for role in roles}) != len(roles):
        raise SnapshotError("duplicate role assignment metadata")

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
    normalized_storage = _normalize_environment_storage(storage)
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


def _require_canonical(actual: object, normalized: object, label: str) -> None:
    if actual != normalized:
        raise SnapshotError(f"invalid {label} metadata schema")


def _validate_snapshot_identity(raw: object, label: str) -> None:
    identity = _mapping(raw, label)
    _exact_keys(
        identity,
        {"type", "principal_id", "tenant_id", "user_assigned"},
        label,
    )
    _string(identity["type"], f"{label} type")
    _string(identity["principal_id"], f"{label} principal", nullable=True)
    _string(identity["tenant_id"], f"{label} tenant", nullable=True)
    assigned = _list(identity["user_assigned"], f"{label} user assigned")
    normalized: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for raw_item in assigned:
        item = _mapping(raw_item, f"{label} user assigned")
        _exact_keys(
            item,
            {"resource_id", "client_id", "principal_id"},
            f"{label} user assigned",
        )
        resource_id = _string(item["resource_id"], f"{label} resource ID")
        assert resource_id is not None
        if resource_id.casefold() in seen:
            raise SnapshotError(f"duplicate {label} user-assigned metadata")
        seen.add(resource_id.casefold())
        normalized.append(
            {
                "resource_id": resource_id,
                "client_id": _string(
                    item["client_id"], f"{label} client", nullable=True
                ),
                "principal_id": _string(
                    item["principal_id"], f"{label} principal", nullable=True
                ),
            }
        )
    normalized.sort(key=lambda item: str(item["resource_id"]).casefold())
    _require_canonical(assigned, normalized, f"{label} user assigned")


def _validate_snapshot_storage_bindings(raw: object, label: str) -> None:
    storage = _mapping(raw, label)
    _exact_keys(storage, {"volume_mounts", "volumes"}, label)
    mounts = _list(storage["volume_mounts"], f"{label} volume mounts")
    normalized_mounts: list[dict[str, str | None]] = []
    seen_mounts: set[tuple[str, str]] = set()
    for raw_mount in mounts:
        mount = _mapping(raw_mount, f"{label} volume mount")
        _exact_keys(
            mount,
            {"container_name", "volume_name", "mount_path", "sub_path"},
            f"{label} volume mount",
        )
        container_name = _string(mount["container_name"], f"{label} mount container")
        volume_name = _string(mount["volume_name"], f"{label} mount volume")
        assert container_name is not None and volume_name is not None
        mount_key = (container_name.casefold(), volume_name.casefold())
        if mount_key in seen_mounts:
            raise SnapshotError(f"duplicate {label} volume mount metadata")
        seen_mounts.add(mount_key)
        normalized_mounts.append(
            {
                "container_name": container_name,
                "volume_name": volume_name,
                "mount_path": _string(mount["mount_path"], f"{label} mount path"),
                "sub_path": _string(
                    mount["sub_path"], f"{label} mount subpath", nullable=True
                ),
            }
        )
    normalized_mounts.sort(key=lambda item: json.dumps(item, sort_keys=True))
    _require_canonical(mounts, normalized_mounts, f"{label} volume mounts")
    volumes = _list(storage["volumes"], f"{label} volumes")
    normalized_volumes: list[dict[str, str | None]] = []
    seen_volumes: set[str] = set()
    for raw_volume in volumes:
        volume = _mapping(raw_volume, f"{label} volume")
        _exact_keys(volume, {"name", "storage_name", "storage_type"}, f"{label} volume")
        name = _string(volume["name"], f"{label} volume name")
        assert name is not None
        if name.casefold() in seen_volumes:
            raise SnapshotError(f"duplicate {label} volume metadata")
        seen_volumes.add(name.casefold())
        normalized_volumes.append(
            {
                "name": name,
                "storage_name": _string(
                    volume["storage_name"], f"{label} storage name", nullable=True
                ),
                "storage_type": _string(
                    volume["storage_type"], f"{label} storage type", nullable=True
                ),
            }
        )
    normalized_volumes.sort(key=lambda item: json.dumps(item, sort_keys=True))
    _require_canonical(volumes, normalized_volumes, f"{label} volumes")


def _validate_snapshot_policies(raw: object) -> None:
    policies = _list(raw, "snapshot Key Vault access policies")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for raw_policy in policies:
        policy = _mapping(raw_policy, "snapshot Key Vault access policy")
        _exact_keys(
            policy,
            {"tenant_id", "object_id", "application_id", "permissions"},
            "snapshot Key Vault access policy",
        )
        tenant_id = _string(policy["tenant_id"], "snapshot policy tenant")
        object_id = _string(policy["object_id"], "snapshot policy object")
        application_id = _string(
            policy["application_id"], "snapshot policy application", nullable=True
        )
        assert object_id is not None
        key = (
            object_id.casefold(),
            application_id.casefold() if application_id else None,
        )
        if key in seen:
            raise SnapshotError("duplicate snapshot Key Vault policy metadata")
        seen.add(key)
        permissions = _mapping(policy["permissions"], "snapshot policy permissions")
        _exact_keys(
            permissions,
            {"certificates", "keys", "secrets", "storage"},
            "snapshot policy permissions",
        )
        normalized.append(
            {
                "tenant_id": tenant_id,
                "object_id": object_id,
                "application_id": application_id,
                "permissions": {
                    name: _nullable_string_list(
                        permissions[name], f"snapshot policy {name} permission"
                    )
                    for name in ("certificates", "keys", "secrets", "storage")
                },
            }
        )
    normalized.sort(
        key=lambda item: (
            str(item["object_id"]).casefold(),
            str(item["application_id"] or "").casefold(),
        )
    )
    _require_canonical(policies, normalized, "snapshot Key Vault access policies")


def _validate_snapshot_environment_storage(raw: object) -> None:
    storage = _list(raw, "snapshot environment storage")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in storage:
        item = _mapping(raw_item, "snapshot environment storage")
        _exact_keys(item, {"name", "azure_file"}, "snapshot environment storage")
        name = _string(item["name"], "snapshot environment storage name")
        assert name is not None
        if name.casefold() in seen:
            raise SnapshotError("duplicate snapshot environment storage metadata")
        seen.add(name.casefold())
        azure_file = _mapping(item["azure_file"], "snapshot Azure Files binding")
        _exact_keys(
            azure_file,
            {"account_name", "share_name", "access_mode"},
            "snapshot Azure Files binding",
        )
        normalized.append(
            {
                "name": name,
                "azure_file": {
                    "account_name": _string(
                        azure_file["account_name"], "snapshot storage account"
                    ),
                    "share_name": _string(
                        azure_file["share_name"], "snapshot storage share"
                    ),
                    "access_mode": _string(
                        azure_file["access_mode"], "snapshot storage access mode"
                    ),
                },
            }
        )
    normalized.sort(key=lambda item: str(item["name"]).casefold())
    _require_canonical(storage, normalized, "snapshot environment storage")


def _validate_snapshot_secrets(raw: object, vault_name: str) -> None:
    secrets = _list(raw, "snapshot Key Vault secrets")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_secret in secrets:
        secret = _mapping(raw_secret, "snapshot Key Vault secret")
        _exact_keys(
            secret,
            {"name", "versions"},
            "snapshot Key Vault secret",
        )
        name = _string(secret["name"], "snapshot Key Vault secret name")
        assert name is not None
        if name.casefold() in seen:
            raise SnapshotError("duplicate snapshot Key Vault secret metadata")
        seen.add(name.casefold())
        raw_versions = _list(secret["versions"], "snapshot secret versions")
        versions: list[dict[str, Any]] = []
        seen_version_ids: set[str] = set()
        seen_versions: set[str] = set()
        for raw_version in raw_versions:
            version = _mapping(raw_version, "snapshot secret version")
            _exact_keys(
                version,
                {"id", "version", "enabled", "created", "updated"},
                "snapshot secret version",
            )
            version_id = _string(version["id"], "snapshot secret version ID")
            version_name = _string(version["version"], "snapshot secret version")
            assert version_id is not None and version_name is not None
            parsed_id = urlsplit(version_id)
            path_parts = parsed_id.path.split("/")
            if (
                parsed_id.scheme != "https"
                or parsed_id.hostname != f"{vault_name}.vault.azure.net"
                or parsed_id.username is not None
                or parsed_id.password is not None
                or parsed_id.port is not None
                or parsed_id.query
                or parsed_id.fragment
                or path_parts != ["", "secrets", name, version_name]
            ):
                raise SnapshotError("invalid snapshot versioned secret ID metadata")
            if (
                version_id.casefold() in seen_version_ids
                or version_name.casefold() in seen_versions
            ):
                raise SnapshotError("duplicate snapshot secret version metadata")
            seen_version_ids.add(version_id.casefold())
            seen_versions.add(version_name.casefold())
            if not isinstance(version["enabled"], bool):
                raise SnapshotError("invalid snapshot secret enabled metadata schema")
            versions.append(
                {
                    "id": version_id,
                    "version": version_name,
                    "enabled": version["enabled"],
                    "created": _string(version["created"], "snapshot secret created"),
                    "updated": _string(version["updated"], "snapshot secret updated"),
                }
            )
        if not versions:
            raise SnapshotError("snapshot secret versions must not be empty")
        versions.sort(key=lambda item: (str(item["version"]).casefold(), item["id"]))
        _require_canonical(raw_versions, versions, "snapshot secret versions")
        normalized.append(
            {
                "name": name,
                "versions": versions,
            }
        )
    normalized.sort(key=lambda item: str(item["name"]).casefold())
    _require_canonical(secrets, normalized, "snapshot Key Vault secrets")
    if {str(item["name"]) for item in normalized} != set(SECRET_NAMES):
        raise SnapshotError("invalid snapshot Key Vault secret set metadata schema")


def _validate_snapshot_roles(raw: object) -> None:
    roles = _normalize_roles(raw)
    _require_canonical(raw, roles, "snapshot role assignments")


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
        _validate_snapshot_identity(app["identity"], f"snapshot {label} identity")
        secret_references = _normalize_named_metadata(
            app["secret_references"],
            {"name", "keyVaultUrl", "identity"},
            "name",
            f"snapshot {label} secret reference",
        )
        _require_canonical(
            app["secret_references"],
            secret_references,
            f"snapshot {label} secret references",
        )
        registries = _normalize_named_metadata(
            app["registries"],
            {"server", "username", "passwordSecretRef"},
            "server",
            f"snapshot {label} registry",
        )
        _require_canonical(
            app["registries"], registries, f"snapshot {label} registries"
        )
        _validate_snapshot_storage_bindings(
            app["storage_bindings"], f"snapshot {label} storage"
        )
        deployment = _mapping(app.get("deployment"), f"snapshot {label} deployment")
        _exact_keys(
            deployment,
            {"revision_suffix", "latest_revision_name", "images"},
            f"snapshot {label} deployment",
        )
        _string(
            deployment["revision_suffix"],
            f"snapshot {label} revision suffix",
            nullable=True,
        )
        _string(
            deployment["latest_revision_name"],
            f"snapshot {label} latest revision",
            nullable=True,
        )
        images = _nullable_string_list(deployment["images"], f"snapshot {label} image")
        _require_canonical(deployment["images"], images, f"snapshot {label} images")
    environment = _mapping(snapshot["azure_environment"], "snapshot environment")
    _exact_keys(environment, {"id", "location", "storage"}, "snapshot environment")
    _string(environment["id"], "snapshot environment ID")
    _string(environment["location"], "snapshot environment location")
    _validate_snapshot_environment_storage(environment["storage"])
    vault = _mapping(snapshot["key_vault"], "snapshot Key Vault")
    _exact_keys(
        vault,
        {"id", "location", "rbac_enabled", "access_policies", "secrets"},
        "snapshot Key Vault",
    )
    vault_id = _string(vault["id"], "snapshot Key Vault ID")
    assert vault_id is not None
    vault_name = vault_id.rstrip("/").rsplit("/", 1)[-1]
    if not vault_name:
        raise SnapshotError("invalid snapshot Key Vault ID metadata schema")
    _string(vault["location"], "snapshot Key Vault location")
    if not isinstance(vault["rbac_enabled"], bool):
        raise SnapshotError("invalid snapshot Key Vault RBAC metadata schema")
    _validate_snapshot_policies(vault["access_policies"])
    _validate_snapshot_secrets(vault["secrets"], vault_name)
    _validate_snapshot_roles(snapshot["role_assignments"])
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
