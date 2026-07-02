"""Contract tests for skill definitions and parsing."""

from research_agent.utils.cli import build_parser
from research_agent.utils.skill_registry import get_skill_registry

# After full migration, zero legacy skills remain in the SkillRegistry.
# All skills are loaded by SkillsMiddleware from .deepagents/skills/.

ALL_SKILL_TARGET_IDS: set[str] = set()  # empty — no legacy skills

STRUCTURED_TARGET_IDS: set[str] = set()  # empty — no legacy structured skills

UNSTRUCTURED_TARGET_IDS = ALL_SKILL_TARGET_IDS - STRUCTURED_TARGET_IDS  # empty


def test_all_skill_skills_are_discoverable() -> None:
    assert set(get_skill_registry().list_skill_ids()) == get_skill_registry().SKILL_IDS


def test_parser_exposes_all_skill_skills() -> None:
    parser = build_parser()
    # Parser choices include migrated skill IDs + "list"
    expected = {"list"}
    for sid in get_skill_registry().SKILL_IDS:
        expected.add(sid)
    assert set(parser._option_string_actions["--skill"].choices) == expected


def test_structured_and_unstructured_skill_classification() -> None:
    # No legacy skills remain — both loops iterate over empty sets
    for skill_id in STRUCTURED_TARGET_IDS:
        definition = get_skill_registry().get_skill_definition(skill_id)
        assert definition["schema"] is not None
        assert definition["render"] is not None

    for skill_id in UNSTRUCTURED_TARGET_IDS:
        definition = get_skill_registry().get_skill_definition(skill_id)
        assert definition["schema"] is None
        assert definition["render"] is None


def test_skills_middleware_loads_migrated_skills() -> None:
    """Verify all 9 migrated skills exist in .deepagents/skills/."""
    from pathlib import Path

    skills_dir = Path(__file__).resolve().parent.parent / ".deepagents" / "skills"
    migrated_dirs = {
        d.name
        for d in skills_dir.iterdir()
        if d.is_dir()
    }

    expected = {
        "autoresearch-universal",
        "code-generator",
        "find-skills",
        "frontend-slides",
        "golden-dataset",
        "humanizer",
        "interview",
        "interview-coach-pro",
        "study-slides",
    }
    assert migrated_dirs == expected
