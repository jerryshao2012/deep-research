"""Command-line interface (CLI) argument parsing and validation helpers.

Constructs options for model configurations, document folders, web search flags,
SSL validations, list_skills summaries, and text search integrations.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml

from research_agent.utils.skill_registry import SkillRegistry, get_skill_registry


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the research agent."""
    parser = argparse.ArgumentParser(
        description="Run the Deep Research Agent",
        add_help=False,
    )
    parser.add_argument(
        "subject",
        type=str,
        nargs="?",
        default="",
        help="Research subject. If omitted, a subject file may be used instead.",
    )
    parser.add_argument(
        "--subject-file",
        type=str,
        help="Optional file path to read the research subject from",
    )
    parser.add_argument(
        "--verify_ssl",
        default="True",
        nargs="?",
        const="True",
        help=(
            "Verify SSL certificates (default: True). "
            "Set to False to skip SSL verification"
        ),
    )
    parser.add_argument(
        "--ssl-ca-files",
        type=str,
        help="Path to a PEN CA buddle to use for HTTPS verification",
    )
    parser.add_argument(
        "--verbose",
        default="True",
        nargs="?",
        const="True",
        help="Show progress (default: True). When False, runs agent without progress display",
    )
    parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        help="Show this help message and exit",
    )
    parser.add_argument(
        "--doc-folder",
        type=str,
        help="Optional folder containing supported documents to use as research material",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable web search (Tavily) during research",
    )
    _all_skill_ids = get_skill_registry().list_skill_ids() + list(
        get_skill_registry().SKILL_IDS
    )
    parser.add_argument(
        "--skill",
        choices=["list", *_all_skill_ids],
        help="Optional skill. Use '--skill list' to see all options.",
    )
    parser.add_argument("--title", type=str, help="Optional research title for output file")
    parser.add_argument(
        "--thread-id",
        type=str,
        help="Optional thread ID for state tracking (auto-generated if not provided)",
    )
    return parser


def list_skills() -> None:
    """Print available research skills to console."""
    registry = get_skill_registry()
    print("\nAvailable research skills:")
    base_dir = Path(__file__).resolve().parent.parent.parent
    for sid in sorted(registry.SKILL_IDS):
        skill_file = base_dir / ".deepagents" / "skills" / sid / "SKILL.md"
        if not skill_file.is_file():
            skill_file = base_dir / "docs" / ".deepagents" / "skills" / sid / "SKILL.md"
        desc = ""
        if skill_file.is_file():
            match = SkillRegistry._FRONTMATTER_RE.match(
                skill_file.read_text(encoding="utf-8")
            )
            if match:
                fm = yaml.safe_load(match.group(1))
                desc = fm.get("description", "")[:120] if isinstance(fm, dict) else ""
        print(f"  {sid}" + (f" — {desc}" if desc else ""))
    print("\nUse --skill <id> to select one.")


def build_instruction(
        subject: str,
        doc_folder: str | None = None,
        skill: str | None = None,
        subject_file: str | None = None,
        no_web: bool = False,
) -> str:
    """Build the user instruction sent to the agent."""
    if not subject and subject_file and os.path.exists(subject_file):
        with open(subject_file, "r", encoding="utf-8") as handle:
            subject = handle.read().strip()

    instruction = f"Research the following subject: {subject}"

    if doc_folder:
        instruction += (
            "\n\nPlease use the 'read_docs_folder' tool to read supported documents "
            f"from this folder first: '{doc_folder}'. Ground your answer in those docs "
            "when they are relevant."
        )

    if no_web:
        instruction += (
            "\n\n**CRITICAL: Do NOT use web search for this research task.** "
            "Use only provided documentation or your internal knowledge."
        )

    if skill:
        instruction += (
            f"\n\nThe requested output skill is `{skill}`. "
            f"Use `read_file` to load the skill's SKILL.md for full instructions "
            f"and follow its workflow precisely. "
            f"Save your final output using `write_file` to `/final_report.md`."
        )

    return instruction
