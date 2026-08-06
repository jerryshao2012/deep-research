"""Source-layout contracts for the nested researcher implementation."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESEARCHER_ENTRIES = (
    "__init__.py",
    "prompts.py",
    "tools.py",
    "clarification",
    "resume",
    "utils",
)


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
