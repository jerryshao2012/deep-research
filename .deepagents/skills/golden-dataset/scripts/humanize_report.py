"""Post-process a generated report to remove AI writing patterns.

Uses the same LLM judge model configured for the golden dataset skill
and the full humanizer SKILL.md as the system prompt to rewrite the
final report in a more natural, human tone.
"""

from __future__ import annotations

from pathlib import Path

import sys
from langchain_core.messages import HumanMessage, SystemMessage

# Add project root to path for retry_utils and utils imports
_sys_path_root = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_sys_path_root) not in sys.path:
    sys.path.insert(0, str(_sys_path_root))

# Local import from same scripts directory
from skill_model_factory import (
    get_configured_model,
)

# Resolve the humanizer SKILL.md relative to this file's location.
# scripts/ -> golden-dataset/ -> skills/ (which is .deepagents/skills/)
_HUMANIZER_SKILL_PATH = (
        Path(__file__).resolve().parent.parent  # -> skills/ (.deepagents/skills/)
        / "humanizer"
        / "SKILL.md"
)


def _load_humanizer_prompt() -> str:
    """Read the humanizer SKILL.md and return its body (after the YAML frontmatter)."""
    content = _HUMANIZER_SKILL_PATH.read_text(encoding="utf-8")
    # Strip YAML frontmatter (between --- markers) if present
    if content.startswith("---"):
        end = content.index("---", 3)
        content = content[end + 3:].strip()
    return content


def humanize_report(report: str) -> str:
    """Rewrite *report* to sound more natural using the configured LLM.

    The full humanizer skill instructions are loaded from
    ``skills/humanizer/SKILL.md`` and sent as the system prompt.

    If the LLM call fails for any reason the original report is returned
    unmodified so the pipeline never breaks because of the humanizer.
    """
    try:
        humanizer_prompt = _load_humanizer_prompt()
        model = get_configured_model()
        messages = [
            SystemMessage(content=humanizer_prompt),
            HumanMessage(
                content=(
                    "Humanize the following Markdown report. Preserve ALL "
                    "factual data, tables, numbers, file paths, and Markdown "
                    "structure (headings, bullet lists, code blocks, tables). "
                    "Return ONLY the rewritten Markdown — no preamble, no "
                    "commentary.\n\n"
                    f"{report}"
                )
            ),
        ]
        response = model.invoke(messages)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "\n".join(str(part) for part in content)
        rewritten = str(content).strip()
        # Sanity check: if the rewrite is suspiciously short, keep the original
        if len(rewritten) < len(report) * 0.3:
            return report
        return rewritten
    except Exception as exc:  # noqa: BLE001
        print(f"[humanize_report] LLM rewrite failed, keeping original: {exc}")
        return report
