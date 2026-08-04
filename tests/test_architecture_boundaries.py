from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.check_architecture import check_architecture


def test_repository_provides_architecture_checker() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert (repo_root / "scripts" / "check_architecture.py").is_file()


def test_architecture_policy_and_contract_snapshot_are_versioned() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert (repo_root / "documents" / "architecture" / "clean-architecture.md").is_file()
    assert (repo_root / "contracts" / "custom-api.openapi.json").is_file()
    assert (repo_root / "scripts" / "snapshot_openapi.py").is_file()
    workflow = (repo_root / ".github" / "workflows" / "architecture.yml").read_text(
        encoding="utf-8"
    )
    assert "scripts/check_architecture.py" in workflow
    assert "scripts/snapshot_openapi.py --check" in workflow
    assert "tests/test_architecture_boundaries.py" in workflow


def test_deprecated_server_is_not_a_production_entrypoint() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    for relative_path in ("Dockerfile", "Dockerfile-aws", "entrypoint.sh", "langgraph.json"):
        contents = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "python server.py" not in contents
        assert "server:app" not in contents


def test_openapi_snapshot_script_loads_app_from_repository_root() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/snapshot_openapi.py", "--check"],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_openapi_snapshot_ignores_ambient_passkey_configuration(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PASSKEY_")
    }
    env.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
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
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from scripts.snapshot_openapi import rendered_schema; "
                "print(rendered_schema(), end='')"
            ),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    schema = json.loads(result.stdout)
    assert not any(path.startswith("/auth/passkeys") for path in schema["paths"])


def _write(root: Path, relative_path: str, content: str) -> None:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_domain_and_application_reject_outward_dependencies(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "webapp/features/auth/domain/user.py",
        "from fastapi import Request\n",
    )
    _write(
        tmp_path,
        "webapp/features/auth/application/login.py",
        "from webapp.features.auth.infrastructure.store import Store\n",
    )
    _write(
        tmp_path,
        "webapp/features/auth/infrastructure/store.py",
        "class Store: pass\n",
    )

    violations = check_architecture(root_dir=tmp_path)

    assert sorted(item["rule"] for item in violations) == [
        "application-outward-import",
        "domain-framework-import",
    ]


def test_features_consume_other_features_through_public_entrypoints(
        tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "webapp/features/chat/application/send.py",
        "from webapp.features.threads.infrastructure.store import load_thread\n",
    )
    _write(
        tmp_path,
        "webapp/features/threads/infrastructure/store.py",
        "def load_thread(): return None\n",
    )

    violations = check_architecture(root_dir=tmp_path)

    assert violations[0]["rule"] == "cross-feature-internal-import"


def test_local_python_dependency_cycles_are_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "webapp/features/chat/application/a.py",
        "from webapp.features.chat.application import b\n",
    )
    _write(
        tmp_path,
        "webapp/features/chat/application/b.py",
        "from webapp.features.chat.application import a\n",
    )

    violations = check_architecture(root_dir=tmp_path)

    assert violations[0]["rule"] == "dependency-cycle"
