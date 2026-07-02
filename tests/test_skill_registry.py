#!/usr/bin/env python3
"""Test that SkillRegistry works with 0 legacy skills (all migrated to .deepagents/skills/)."""

from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from research_agent.utils.skill_registry import SkillRegistry


def test_basic_loading():
    registry = SkillRegistry()
    print(f"\n✓ Registry initialized: {registry}")
    print(f"✓ Number of skills loaded: {registry.num_skills}")
    print(f"✓ Skill IDs: {registry.skill_ids}")
    print(f"✓ Migrated SKILL_IDS: {sorted(registry.SKILL_IDS)}")

    assert registry.num_skills >= 9, "At least 9 skills should be loaded"
    assert len(registry.SKILL_IDS) >= 9, "At least 9 migrated skills"
    print("✅ Basic loading test PASSED\n")


def test_skill_summaries():
    registry = SkillRegistry()
    summaries = registry.get_all_summaries()
    assert len(summaries) >= 9, "Skill summaries should be populated"
    print("✅ Skill summaries test PASSED\n")


def test_skill_catalog_is_populated():
    registry = SkillRegistry()
    catalog = registry.format_skill_catalog()
    assert len(catalog) > 0, "Skill catalog should not be empty"
    assert "autoresearch-universal" in catalog
    print("✅ Skill catalog test PASSED\n")


def test_migrated_skill_ids_include_all_skills():
    registry = SkillRegistry()
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
    assert expected.issubset(registry.SKILL_IDS), \
        f"Missing: {expected - registry.SKILL_IDS}"
    print("✅ Migrated skill IDs test PASSED\n")


def main():
    print("\n" + "=" * 80)
    print("SKILL REGISTRY TEST SUITE (POST-MIGRATION)")
    print("=" * 80 + "\n")

    try:
        test_basic_loading()
        test_skill_summaries()
        test_skill_catalog_is_populated()
        test_migrated_skill_ids_include_all_skills()

        print("=" * 80)
        print("ALL TESTS PASSED ✅")
        print("=" * 80)
        return 0
    except Exception as e:
        print("\n" + "=" * 80)
        print(f"TEST FAILED ❌: {e}")
        print("=" * 80)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
