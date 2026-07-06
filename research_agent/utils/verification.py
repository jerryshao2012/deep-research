"""Post-generation verification and adversarial gap analysis for research reports.

Composes existing capabilities (citation grounding from citation_validator.py,
LLM-as-judge sufficiency from the wiki-complete evaluation pattern) with a new
adversarial gap-analysis pass.  Returns structured verdicts that the agent
middleware uses to decide whether to loop back for revision or terminate.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage

from model_factory import get_configured_model
from research_agent.utils.citation_validator import (
    ValidationResult,
    validate_web_citations,
)
from research_agent.utils.json_utils import robust_json_loads
from thread_wiki.models import SourceCitation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_VERIFICATION_ROUNDS = int(os.environ.get("MAX_VERIFICATION_ROUNDS", "2"))
ENABLE_VERIFICATION = os.environ.get("ENABLE_VERIFICATION", "true").lower() not in (
    "false", "0", "no", "off",
)

# How many citations to spot-check when a report has many (cap cost).
_MAX_CITATION_SPOT_CHECKS = 5

# Thresholds for the composite verdict.
_SUFFICIENCY_THRESHOLD = 0.7  # LLM judge score 0-1 — must be >= this
_MAX_ADVERSARIAL_GAPS = 1  # tolerable number of adversarial gaps


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationVerdict:
    """Structured result of a post-generation verification pass."""

    status: str  # "complete" | "needs_revision" | "error"
    sufficiency_score: float
    grounding_results: list[ValidationResult] = field(default_factory=list)
    adversarial_gaps: list[str] = field(default_factory=list)
    sufficiency_reason: str = ""
    error_message: str = ""


# ---------------------------------------------------------------------------
# Citation extraction helpers
# ---------------------------------------------------------------------------


def _extract_citations_from_report(report_text: str) -> list[SourceCitation]:
    """Parse ``[1], [2], ...`` citation markers and ``### Sources`` block from a report.

    Returns a list of ``SourceCitation`` objects suitable for the existing
    ``validate_web_citations`` function.
    """
    citations: list[SourceCitation] = []

    # Parse the Sources block for numbered URL entries.
    # Handles both "[1] Title: URL" and "1. Title: URL" (markdown numbered list).
    sources_pattern = re.compile(
        r"^\s*(?:\[(\d{1,3})]|(\d{1,3})\.)\s*(.*?):\s*(https?://\S+)\s*$",
        re.MULTILINE,
    )
    for match in sources_pattern.finditer(report_text):
        url = match.group(4).strip()
        citations.append(SourceCitation(
            kind="web",
            url=url,
            raw_path=None,
            page=None,
            locator=None,
        ))

    return citations


# ---------------------------------------------------------------------------
# LLM-as-judge sufficiency check
# ---------------------------------------------------------------------------


def _check_report_sufficiency(question: str, report: str) -> tuple[float, str]:
    """Ask an LLM evaluator whether the report fully answers the question.

    Returns a tuple of ``(score 0.0–1.0, reason_string)``.  Reuses the proven
    prompt pattern from ``_check_if_needs_deep_research`` in agent.py.
    """
    if not report or not report.strip():
        return 0.0, "Empty report."

    try:
        model = get_configured_model()
    except Exception as exc:
        logger.warning("Cannot create model for sufficiency check: %s", exc)
        return 0.5, f"Model unavailable: {exc}"

    prompt = (
        "You are an expert research evaluator. Analyze whether the following "
        "report fully and comprehensively answers the user's question.\n\n"
        "Evaluation dimensions:\n"
        "- Completeness: Are ALL aspects/sub-questions of the query addressed?\n"
        "- Factual consistency: Are there any internal contradictions?\n"
        "- Citation coverage: Does every major factual claim have a nearby citation?\n"
        "- Depth: Is the analysis surface-level or does it provide meaningful detail?\n\n"
        f"User's Question: {question}\n\n"
        f"Report:\n{report[:8000]}\n\n"
        "Respond with a JSON object ONLY (no other text):\n"
        "{\n"
        '  "sufficiency_score": 0.0-1.0,\n'
        '  "reason": "Detailed reasoning for the score"\n'
        "}"
    )

    def _invoke():
        return model.invoke([HumanMessage(content=prompt)])

    try:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is not None and current_loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(_invoke).result(timeout=60)
        else:
            response = _invoke()

        data = robust_json_loads(response.content.strip())
        score = float(data.get("sufficiency_score", 0.5))
        reason = str(data.get("reason", "No reason provided."))
        # Clamp to valid range.
        score = max(0.0, min(1.0, score))
        return score, reason
    except Exception as exc:
        logger.warning("Sufficiency check failed: %s. Defaulting to neutral.", exc)
        return 0.5, f"Sufficiency check error: {exc}"


# ---------------------------------------------------------------------------
# Adversarial gap analysis
# ---------------------------------------------------------------------------


def _adversarial_gap_analysis(question: str, report: str) -> list[str]:
    """Prompt an LLM in a devil's-advocate role to surface missing perspectives.

    Returns a list of gap descriptions (empty list = no gaps found).
    """
    if not report or not report.strip():
        return ["Report is empty — no content to analyze."]

    try:
        model = get_configured_model()
    except Exception as exc:
        logger.warning("Cannot create model for adversarial analysis: %s", exc)
        return [f"Adversarial analysis unavailable: {exc}"]

    prompt = (
        "You are a skeptical peer reviewer. Your task is to find weaknesses, "
        "gaps, and missing perspectives in a research report.\n\n"
        "Ask yourself:\n"
        "- What would someone who disagrees with this report say is missing or wrong?\n"
        "- What alternative perspective or counter-argument is NOT represented?\n"
        "- Are there logical leaps, unsupported assertions, or vague claims?\n"
        "- What important dimension of the question was NOT explored?\n\n"
        f"User's Question: {question}\n\n"
        f"Report:\n{report[:8000]}\n\n"
        "Respond with a JSON object ONLY (no other text):\n"
        "{\n"
        '  "gaps": ["Gap description 1", "Gap description 2"],\n'
        '  "critique_summary": "One-sentence summary of overall assessment"\n'
        "}\n"
        "List ONLY substantial, actionable gaps. If the report is thorough, "
        "return an empty gaps array."
    )

    def _invoke():
        return model.invoke([HumanMessage(content=prompt)])

    try:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is not None and current_loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(_invoke).result(timeout=60)
        else:
            response = _invoke()

        data = robust_json_loads(response.content.strip())
        gaps = data.get("gaps", [])
        if not isinstance(gaps, list):
            gaps = []
        return [str(g) for g in gaps[:5]]  # Cap at 5 gaps.
    except Exception as exc:
        logger.warning("Adversarial analysis failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Composite verification
# ---------------------------------------------------------------------------


def make_verdict(
        sufficiency_score: float,
        grounding_failures: int,
        adversarial_gaps: list[str],
) -> str:
    """Combine sub-scores into a binary verdict.

    Returns ``"complete"`` when all thresholds are met, ``"needs_revision"``
    otherwise.
    """
    if grounding_failures > 0:
        return "needs_revision"
    if sufficiency_score < _SUFFICIENCY_THRESHOLD:
        return "needs_revision"
    if len(adversarial_gaps) > _MAX_ADVERSARIAL_GAPS:
        return "needs_revision"
    return "complete"


def format_feedback(verdict: VerificationVerdict) -> str:
    """Render a ``VerificationVerdict`` as a ``SystemMessage``-ready XML block.

    The block follows the same pattern as ``<wiki_context>`` and
    ``<WikiCompleteAnswer>`` in agent.py so the model can parse it naturally.
    """
    lines: list[str] = [
        "<VerificationFeedback>",
        "The previous version of your report was reviewed and needs improvement.",
        "",
        f"Sufficiency score: {verdict.sufficiency_score:.2f} / 1.0",
    ]

    if verdict.grounding_results:
        failed = [
            r for r in verdict.grounding_results
            if not r.grounded or not r.reachable
        ]
        if failed:
            lines.append("")
            lines.append("Citation issues found:")
            for r in failed:
                lines.append(f"  - [{r.url}] {r.reason}")

    if verdict.adversarial_gaps:
        lines.append("")
        lines.append("Gaps and missing perspectives:")
        for i, gap in enumerate(verdict.adversarial_gaps, 1):
            lines.append(f"  {i}. {gap}")

    lines.append("")
    lines.append(
        "Please revise `/final_report.md` to address ALL of the issues above. "
        "After revising, call `write_file` to overwrite `/final_report.md` with "
        "the improved version."
    )
    lines.append("</VerificationFeedback>")
    return "\n".join(lines)


async def verify_report(
        question: str,
        report: str,
        fetched_contents: dict[str, str] | None = None,
) -> VerificationVerdict:
    """Run all verification checks against a final report.

    This is the main entry point.  It composes:
    1. Citation grounding (reuses ``validate_web_citations``).
    2. LLM-as-judge sufficiency scoring.
    3. Adversarial gap analysis.

    Args:
        question: The original user question.
        report: The full text of ``/final_report.md``.
        fetched_contents: Optional pre-fetched URL → content mapping.

    Returns:
        A ``VerificationVerdict`` with the composite status and all sub-results.
    """
    # ── 1. Citation grounding ──────────────────────────────────────────
    citations = _extract_citations_from_report(report)
    grounding_results: list[ValidationResult] = []

    if citations:
        # Spot-check: when there are many citations, validate a random subset
        # to keep verification latency reasonable.
        if len(citations) > _MAX_CITATION_SPOT_CHECKS:
            import random
            citations = random.sample(citations, _MAX_CITATION_SPOT_CHECKS)

        try:
            grounding_results = await validate_web_citations(
                citations=citations,
                text_content=report,
                fetched_contents=fetched_contents,
            )
        except Exception as exc:
            logger.warning("Citation grounding failed: %s", exc)

    grounding_failures = sum(
        1 for r in grounding_results if not r.grounded or not r.reachable
    )

    # ── 2. LLM-as-judge sufficiency ────────────────────────────────────
    sufficiency_score, sufficiency_reason = _check_report_sufficiency(
        question, report
    )

    # ── 3. Adversarial gap analysis ────────────────────────────────────
    adversarial_gaps = _adversarial_gap_analysis(question, report)

    # ── Composite verdict ──────────────────────────────────────────────
    status = make_verdict(sufficiency_score, grounding_failures, adversarial_gaps)

    return VerificationVerdict(
        status=status,
        sufficiency_score=sufficiency_score,
        grounding_results=grounding_results,
        adversarial_gaps=adversarial_gaps,
        sufficiency_reason=sufficiency_reason,
    )
