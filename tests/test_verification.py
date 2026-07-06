"""Tests for post-generation verification and adversarial gap analysis."""

from __future__ import annotations

import pytest

from research_agent.utils.verification import (
    VerificationVerdict,
    _adversarial_gap_analysis,
    _check_report_sufficiency,
    _extract_citations_from_report,
    format_feedback,
    make_verdict,
)


# ---------------------------------------------------------------------------
# make_verdict
# ---------------------------------------------------------------------------


class TestMakeVerdict:
    def test_complete_when_all_thresholds_met(self):
        assert make_verdict(0.9, 0, []) == "complete"

    def test_needs_revision_when_low_sufficiency(self):
        assert make_verdict(0.5, 0, []) == "needs_revision"

    def test_needs_revision_when_grounding_failures(self):
        assert make_verdict(0.9, 1, []) == "needs_revision"

    def test_needs_revision_when_too_many_gaps(self):
        assert make_verdict(0.9, 0, ["gap1", "gap2"]) == "needs_revision"

    def test_needs_revision_when_all_fail(self):
        assert make_verdict(0.3, 3, ["gap1", "gap2", "gap3"]) == "needs_revision"

    def test_boundary_sufficiency_at_threshold(self):
        """Score exactly at threshold (0.7) should be complete."""
        assert make_verdict(0.7, 0, []) == "complete"

    def test_boundary_gaps_at_max(self):
        """Exactly 1 gap should be tolerable."""
        assert make_verdict(0.8, 0, ["one gap"]) == "complete"


# ---------------------------------------------------------------------------
# format_feedback
# ---------------------------------------------------------------------------


class TestFormatFeedback:
    def test_produces_xml_block(self):
        verdict = VerificationVerdict(
            status="needs_revision",
            sufficiency_score=0.5,
            adversarial_gaps=["Missing counter-argument about X."],
            sufficiency_reason="Lacks depth.",
        )
        text = format_feedback(verdict)
        assert "<VerificationFeedback>" in text
        assert "</VerificationFeedback>" in text
        assert "0.50" in text
        assert "Missing counter-argument about X." in text

    def test_produces_xml_block_with_grounding_issues(self):
        from research_agent.utils.citation_validator import ValidationResult

        verdict = VerificationVerdict(
            status="needs_revision",
            sufficiency_score=0.6,
            grounding_results=[
                ValidationResult(
                    url="https://example.com",
                    reachable=False,
                    grounded=False,
                    reason="HTTP 404",
                ),
            ],
            adversarial_gaps=[],
            sufficiency_reason="",
        )
        text = format_feedback(verdict)
        assert "https://example.com" in text
        assert "HTTP 404" in text

    def test_no_gaps_section_when_empty(self):
        verdict = VerificationVerdict(
            status="needs_revision",
            sufficiency_score=0.4,
            adversarial_gaps=[],
            sufficiency_reason="Thin.",
        )
        text = format_feedback(verdict)
        assert "Gaps and missing perspectives" not in text


# ---------------------------------------------------------------------------
# _extract_citations_from_report
# ---------------------------------------------------------------------------


class TestExtractCitationsFromReport:
    def test_extracts_sources_block_entries(self):
        report = """
Some findings [1]. More info [2].

### Sources
1. Example Site: https://example.com
2. Other Site: https://other.com/page
"""
        citations = _extract_citations_from_report(report)
        urls = {c.url for c in citations}
        assert "https://example.com" in urls
        assert "https://other.com/page" in urls

    def test_empty_for_report_without_sources(self):
        report = "Just some text without citations."
        assert _extract_citations_from_report(report) == []

    def test_handles_malformed_sources_block(self):
        report = """
### Sources
- Not a properly formatted source
- Another bad line
"""
        citations = _extract_citations_from_report(report)
        assert citations == []


# ---------------------------------------------------------------------------
# _check_report_sufficiency  (requires model — skipped in CI w/o keys)
# ---------------------------------------------------------------------------


class TestCheckReportSufficiency:
    @pytest.mark.skip(reason="Requires configured model — manual / integration only.")
    def test_complete_report_scores_high(self):
        score, reason = _check_report_sufficiency(
            question="What is 2+2?",
            report="2+2 equals 4. This is a fundamental arithmetic fact.",
        )
        assert score >= 0.7
        assert reason

    def test_empty_report_scores_zero(self):
        score, reason = _check_report_sufficiency(
            question="What is 2+2?",
            report="",
        )
        assert score == 0.0
        assert reason


# ---------------------------------------------------------------------------
# _adversarial_gap_analysis  (requires model — skipped in CI w/o keys)
# ---------------------------------------------------------------------------


class TestAdversarialGapAnalysis:
    @pytest.mark.skip(reason="Requires configured model — manual / integration only.")
    def test_thin_report_finds_gaps(self):
        gaps = _adversarial_gap_analysis(
            question="Compare Python vs JavaScript for web development.",
            report="Python is good. JavaScript is also good.",
        )
        # A thin report should have gaps.
        assert len(gaps) > 0

    def test_empty_report_returns_gap(self):
        gaps = _adversarial_gap_analysis(question="What is X?", report="")
        assert len(gaps) >= 1
        assert any("empty" in g.lower() for g in gaps)
