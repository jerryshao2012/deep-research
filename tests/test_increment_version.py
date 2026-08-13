"""Tests for increment_version.py."""

from __future__ import annotations

import sys
from pathlib import Path
import subprocess

import pytest

# Ensure repo root is in python path to import increment_version
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from increment_version import increment_version


def test_increment_version_updates_both_files(tmp_path: Path) -> None:
    # 1. Create fake webapp/config.py
    webapp_dir = tmp_path / "webapp"
    webapp_dir.mkdir(parents=True)
    config_file = webapp_dir / "config.py"
    config_file.write_text(
        '# Some comments\nAPI_VERSION: str = "1.8.130"\n# other config\n',
        encoding="utf-8",
    )

    # 2. Create fake contracts/custom-api.openapi.json
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir(parents=True)
    openapi_file = contracts_dir / "custom-api.openapi.json"
    openapi_file.write_text(
        '{\n  "info": {\n    "title": "Document Upload API",\n    "version": "1.8.130"\n  }\n}\n',
        encoding="utf-8",
    )

    # Run increment_version
    new_version = increment_version(config_file)

    assert new_version == "1.8.131"

    # Verify both files are updated
    updated_config = config_file.read_text(encoding="utf-8")
    assert 'API_VERSION: str = "1.8.131"' in updated_config

    updated_openapi = openapi_file.read_text(encoding="utf-8")
    assert '"version": "1.8.131"' in updated_openapi


def test_increment_version_works_without_openapi_file(tmp_path: Path) -> None:
    # 1. Create fake webapp/config.py
    webapp_dir = tmp_path / "webapp"
    webapp_dir.mkdir(parents=True)
    config_file = webapp_dir / "config.py"
    config_file.write_text(
        'API_VERSION = "1.8.130"\n',
        encoding="utf-8",
    )

    # Run increment_version
    new_version = increment_version(config_file)

    assert new_version == "1.8.131"
    updated_config = config_file.read_text(encoding="utf-8")
    assert 'API_VERSION = "1.8.131"' in updated_config


def test_increment_version_fails_on_malformed_openapi_file(tmp_path: Path) -> None:
    # 1. Create fake webapp/config.py
    webapp_dir = tmp_path / "webapp"
    webapp_dir.mkdir(parents=True)
    config_file = webapp_dir / "config.py"
    config_file.write_text(
        'API_VERSION = "1.8.130"\n',
        encoding="utf-8",
    )

    # 2. Create fake contracts/custom-api.openapi.json but without version field
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir(parents=True)
    openapi_file = contracts_dir / "custom-api.openapi.json"
    openapi_file.write_text(
        '{\n  "info": {\n    "title": "Document Upload API"\n  }\n}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Could not find version string"):
        increment_version(config_file)


def test_increment_version_cli(tmp_path: Path) -> None:
    # 1. Create fake webapp/config.py
    webapp_dir = tmp_path / "webapp"
    webapp_dir.mkdir(parents=True)
    config_file = webapp_dir / "config.py"
    config_file.write_text(
        'API_VERSION = "1.8.130"\n',
        encoding="utf-8",
    )

    # Run script via CLI
    script_path = PROJECT_ROOT / "increment_version.py"
    result = subprocess.run(
        [sys.executable, str(script_path), str(config_file)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Version incremented to 1.8.131" in result.stdout
    updated_config = config_file.read_text(encoding="utf-8")
    assert 'API_VERSION = "1.8.131"' in updated_config
