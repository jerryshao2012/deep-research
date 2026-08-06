"""Source-layout contracts for the nested researcher implementation."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_FILES = {
    "agent.py": "research_agent/agent.py",
    "auth.py": "research_agent/auth.py",
    "azure_storage.py": "research_agent/azure_storage.py",
    "db.py": "research_agent/db.py",
    "db_sql.py": "research_agent/db_sql.py",
    "langgraph_snapshot.py": "research_agent/langgraph_snapshot.py",
    "logger_utils.py": "research_agent/logger_utils.py",
    "model_factory.py": "research_agent/model_factory.py",
    "research_agent_cli.py": "research_agent/cli.py",
    "retry_utils.py": "research_agent/retry_utils.py",
    "run.py": "research_agent/run.py",
    "s3_storage.py": "research_agent/s3_storage.py",
    "server.py": "research_agent/server.py",
    "utils.py": "research_agent/cli_utils.py",
}
ROOT_MAINTENANCE_SCRIPTS = {
    "increment_version.py",
    "migrate_sqlite_to_cosmos.py",
}
RESEARCHER_ENTRIES = (
    "__init__.py",
    "prompts.py",
    "tools.py",
    "clarification",
    "resume",
    "utils",
)
ACTIVE_PYTHON_PATHS = (
    ROOT / "research_agent",
    ROOT / "webapp",
    ROOT / "thread_wiki",
    ROOT / "tests",
    ROOT / ".deepagents" / "skills",
    ROOT / "scripts",
    ROOT / "increment_version.py",
    ROOT / "migrate_sqlite_to_cosmos.py",
)
LEGACY_ROOT_MODULES = {
    "agent",
    "auth",
    "azure_storage",
    "db",
    "db_sql",
    "langgraph_snapshot",
    "logger_utils",
    "model_factory",
    "research_agent_cli",
    "retry_utils",
    "s3_storage",
    "server",
    "utils",
}
LEGACY_RESEARCHER_PREFIXES = {
    "research_agent.prompts",
    "research_agent.tools",
    "research_agent.clarification",
    "research_agent.resume",
    "research_agent.utils",
}


def _is_legacy_import(module: str) -> bool:
    root_module = module.partition(".")[0]
    return root_module in LEGACY_ROOT_MODULES or any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in LEGACY_RESEARCHER_PREFIXES
    )


def _python_sources() -> list[Path]:
    sources: list[Path] = []
    for path in ACTIVE_PYTHON_PATHS:
        if path.is_file():
            sources.append(path)
        else:
            sources.extend(path.rglob("*.py"))
    return sorted(sources)


def _legacy_imports_in_source(source: str, display_path: Path) -> list[str]:
    tree = ast.parse(source, filename=str(display_path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = (
                (node.module,)
                if _is_legacy_import(node.module)
                else tuple(f"{node.module}.{alias.name}" for alias in node.names)
            )
        else:
            continue

        findings.extend(
            f"{display_path}:{node.lineno}: {module}"
            for module in modules
            if _is_legacy_import(module)
        )
    return findings


def _legacy_imports_in(path: Path) -> list[str]:
    return _legacy_imports_in_source(
        path.read_text(encoding="utf-8"), path.relative_to(ROOT)
    )


def test_active_python_sources_include_maintained_executables() -> None:
    expected = {
        ROOT / "increment_version.py",
        ROOT / "migrate_sqlite_to_cosmos.py",
        *(ROOT / "scripts").rglob("*.py"),
    }

    assert expected <= set(_python_sources())


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from research_agent import prompts as p\n",
            ["fixture.py:1: research_agent.prompts"],
        ),
        (
            "from research_agent import db, prompts as p, tools\n",
            [
                "fixture.py:1: research_agent.prompts",
                "fixture.py:1: research_agent.tools",
            ],
        ),
        ("from research_agent import cli_utils as utils\n", []),
        ("from model_factory import get_model\n", ["fixture.py:1: model_factory"]),
        ("import json, server\n", ["fixture.py:1: server"]),
    ],
)
def test_legacy_import_extraction(source: str, expected: list[str]) -> None:
    assert _legacy_imports_in_source(source, Path("fixture.py")) == expected


def test_python_sources_do_not_use_legacy_imports() -> None:
    findings = [
        finding
        for path in _python_sources()
        for finding in _legacy_imports_in(path)
    ]

    assert findings == []


def test_application_modules_live_in_package() -> None:
    assert all((ROOT / target).is_file() for target in APPLICATION_FILES.values())


def test_only_maintenance_scripts_remain_at_repository_root() -> None:
    root_python_files = {path.name for path in ROOT.glob("*.py")}

    assert root_python_files == ROOT_MAINTENANCE_SCRIPTS


def test_packaged_storage_resources_resolve_from_repository_root(monkeypatch) -> None:
    from research_agent.azure_storage import _resolve_tracked_folders as azure_folders
    from research_agent.s3_storage import _resolve_tracked_folders as s3_folders

    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", "/tmp/research-output")
    monkeypatch.setenv("INPUT_FOLDER", "/tmp/research-input")

    expected_common = [
        ("docs", ROOT / "docs"),
        ("output", Path("/tmp/research-output")),
        ("input", Path("/tmp/research-input")),
    ]
    assert s3_folders() == expected_common
    assert azure_folders() == expected_common + [
        (".langgraph_api", ROOT / ".langgraph_api"),
    ]


def test_golden_dataset_factory_uses_packaged_application_imports() -> None:
    factory = (
        ROOT
        / ".deepagents"
        / "skills"
        / "golden-dataset"
        / "scripts"
        / "skill_model_factory.py"
    )
    tree = ast.parse(factory.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "research_agent.retry_utils" in imports
    assert "research_agent.cli_utils" in imports
    assert "retry_utils" not in imports
    assert "utils" not in imports


def test_packaged_server_uses_packaged_run_import() -> None:
    tree = ast.parse(
        (ROOT / "research_agent" / "server.py").read_text(encoding="utf-8")
    )
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "research_agent.run" in imports
    assert "run" not in imports


def test_researcher_entries_live_in_nested_package() -> None:
    researcher_root = ROOT / "research_agent" / "research_subagent"

    assert all((researcher_root / entry).exists() for entry in RESEARCHER_ENTRIES)


def test_old_direct_researcher_entries_are_absent() -> None:
    old_root = ROOT / "research_agent"

    assert all(
        not (old_root / entry).exists()
        for entry in ("prompts.py", "tools.py", "clarification", "resume", "utils")
    )


def test_old_researcher_import_paths_do_not_resolve() -> None:
    old_paths = (
        "research_agent.prompts",
        "research_agent.tools",
        "research_agent.clarification",
        "research_agent.resume",
        "research_agent.utils",
    )

    assert all(importlib.util.find_spec(path) is None for path in old_paths)


def test_nested_resource_roots_still_point_to_repository() -> None:
    from research_agent.research_subagent.utils.knowledge_filesystem import (
        _PROJECT_ROOT,
    )
    from research_agent.research_subagent.utils.skill_registry import SkillRegistry

    registry = SkillRegistry()

    assert _PROJECT_ROOT == ROOT
    assert registry.skills_dirs[:2] == [
        ROOT / ".deepagents" / "skills",
        ROOT / "docs" / ".deepagents" / "skills",
    ]


def test_langgraph_loads_packaged_application_entrypoints() -> None:
    config = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8"))

    assert config["graphs"]["research"] == "./research_agent/agent.py:agent"
    assert config["auth"]["path"] == "./research_agent/auth.py:auth"
    assert config["http"]["app"] == "./webapp/__init__.py:app"


def test_eval_workflow_watches_and_imports_packaged_modules() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "eval-regression.yml"
    ).read_text(encoding="utf-8")

    assert '      - "research_agent/agent.py"' in workflow
    assert '      - "research_agent/model_factory.py"' in workflow
    assert '      - "agent.py"' not in workflow
    assert '      - "model_factory.py"' not in workflow
    assert (
        "from research_agent.research_subagent.utils.eval_tracking import"
        in workflow
    )
    assert (
        "from research_agent.research_subagent.utils.learning import"
        in workflow
    )
    assert "from research_agent.utils" not in workflow
