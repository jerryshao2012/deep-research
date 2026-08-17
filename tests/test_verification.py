"""Tests for post-generation verification and adversarial gap analysis."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from research_agent import agent as agent_module
from research_agent.completion_guard import CompletionState
from research_agent.research_subagent.utils import verification as verification_module
from research_agent.research_subagent.utils.verification import (
    VerificationVerdict,
    _adversarial_gap_analysis,
    _check_report_sufficiency,
    _extract_citations_from_report,
    format_feedback,
    make_verdict,
)


class _FakeJudgeModel:
    def __init__(self, content: str) -> None:
        self.content = content

    def invoke(self, messages):
        assert messages
        return AIMessage(content=self.content)


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
        from research_agent.research_subagent.utils.citation_validator import (
            ValidationResult,
        )

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
# _check_report_sufficiency  (model boundary replaced with deterministic fake)
# ---------------------------------------------------------------------------


class TestCheckReportSufficiency:
    def test_complete_report_scores_high(self, monkeypatch):
        monkeypatch.setattr(
            verification_module,
            "get_configured_model",
            lambda: _FakeJudgeModel(
                '{"sufficiency_score": 0.95, "reason": "Complete."}'
            ),
        )

        score, reason = _check_report_sufficiency(
            question="What is 2+2?",
            report="2+2 equals 4. This is a fundamental arithmetic fact.",
        )
        assert score == 0.95
        assert reason == "Complete."

    def test_empty_report_scores_zero(self):
        score, reason = _check_report_sufficiency(
            question="What is 2+2?",
            report="",
        )
        assert score == 0.0
        assert reason


# ---------------------------------------------------------------------------
# _adversarial_gap_analysis  (model boundary replaced with deterministic fake)
# ---------------------------------------------------------------------------


class TestAdversarialGapAnalysis:
    def test_thin_report_finds_gaps(self, monkeypatch):
        monkeypatch.setattr(
            verification_module,
            "get_configured_model",
            lambda: _FakeJudgeModel(
                '{"gaps": ["Missing framework-specific tradeoffs."], '
                '"critique_summary": "Too thin."}'
            ),
        )

        gaps = _adversarial_gap_analysis(
            question="Compare Python vs JavaScript for web development.",
            report="Python is good. JavaScript is also good.",
        )
        assert gaps == ["Missing framework-specific tradeoffs."]

    def test_empty_report_returns_gap(self):
        gaps = _adversarial_gap_analysis(question="What is X?", report="")
        assert len(gaps) >= 1
        assert any("empty" in g.lower() for g in gaps)


# ---------------------------------------------------------------------------
# ResearchStateMiddleware verification routing
# ---------------------------------------------------------------------------


def _report(content: str = "Final report", *, modified_at: str = "report-v1") -> dict[str, Any]:
    return {
        "content": [content],
        "encoding": "utf-8",
        "created_at": "created",
        "modified_at": modified_at,
    }


def _verification_state(
    *,
    todos: list[dict[str, str]] | None = None,
    report_owned: bool = True,
    report_modified_at: str = "report-v1",
    baseline_modified_at: str | None = "prior-report",
    verification_round: int = 0,
) -> dict[str, Any]:
    return {
        "messages": [
            HumanMessage(content="What is graph engineering?"),
            AIMessage(
                content="Research complete.",
                id="terminal-response",
                response_metadata={"model": "gemma4", "finish_reason": "stop"},
            ),
        ],
        "files": {
            "/research_request.md": _report(
                "What is graph engineering?", modified_at="request-v1"
            ),
            "/final_report.md": _report(modified_at=report_modified_at),
        },
        "todos": todos
        if todos is not None
        else [
            {
                "id": "research",
                "content": "Research graph engineering",
                "status": "completed",
            }
        ],
        "verification_round": verification_round,
        "verification_feedback": None,
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": report_owned,
        "completion_report_baseline_modified_at": baseline_modified_at,
        "completion_verified_report_modified_at": None,
        "completion_accepted_at_limit_report_modified_at": None,
        "completion_attempts": 2,
        "_streamed_files": ["/final_report.md"],
    }


def _run_after_model(
    middleware: agent_module.ResearchStateMiddleware,
    state: dict[str, Any],
    *,
    async_: bool,
) -> dict[str, Any] | None:
    if async_:
        return asyncio.run(middleware.aafter_model(state=state, runtime=None))
    return middleware.after_model(state=state, runtime=None)


def _needs_revision_verdict() -> VerificationVerdict:
    return VerificationVerdict(
        status="needs_revision",
        sufficiency_score=0.5,
        sufficiency_reason="Add missing evidence.",
    )


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "state_update",
    [
        {"completion_report_owned": False},
        {"completion_plan_owner_generation": "prior-generation"},
        {
            "todos": [
                {
                    "id": "research",
                    "content": "Research graph engineering",
                    "status": "in_progress",
                }
            ]
        },
        {"completion_report_baseline_modified_at": "report-v1"},
        {"completion_verified_report_modified_at": "report-v1"},
    ],
)
def test_verification_runs_only_for_current_owned_report_after_research_completes(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    state_update: dict[str, Any],
) -> None:
    calls = 0

    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        nonlocal calls
        calls += 1
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    state = {**_verification_state(), **state_update}

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )

    assert calls == 0
    assert result is None


@pytest.mark.parametrize("async_", [False, True])
def test_nonfinal_revision_routes_to_model_without_persisted_system_message(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        assert question == "What is graph engineering?"
        assert report == "Final report"
        return _needs_revision_verdict()

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 2)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    state = _verification_state()

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )

    assert result is not None
    assert result["jump_to"] == "model"
    assert result["verification_round"] == 1
    assert "<VerificationFeedback>" in result["verification_feedback"]
    assert "completion_attempts" not in result
    assert state["completion_attempts"] == 2
    assert all(not isinstance(message, SystemMessage) for message in result["messages"])
    tagged = next(
        message
        for message in result["messages"]
        if isinstance(message, AIMessage) and message.id == "terminal-response"
    )
    assert tagged.response_metadata == {
        "model": "gemma4",
        "finish_reason": "stop",
        "resume_intermediate": True,
    }


def test_next_model_request_injects_verification_feedback_system_first() -> None:
    feedback = "<VerificationFeedback>Add evidence.</VerificationFeedback>"
    state = _verification_state()
    state["verification_feedback"] = feedback
    request = ModelRequest(
        model=FakeListChatModel(responses=["done"]),
        messages=[HumanMessage(content="Continue the report")],
        system_message=SystemMessage(content="Existing orchestrator guidance"),
        state=state,
    )

    configured = agent_module.ResearchStateMiddleware().configure_request(request)

    assert configured.system_message is not None
    assert str(configured.system_message.content).startswith(
        "Existing orchestrator guidance"
    )
    assert feedback in str(configured.system_message.content)
    assert all(
        not isinstance(message, SystemMessage) for message in configured.messages
    )
    assert request.messages == [HumanMessage(content="Continue the report")]


@pytest.mark.parametrize("async_", [False, True])
def test_passing_verification_records_current_report_ownership(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(),
        _verification_state(report_modified_at="report-v2"),
        async_=async_,
    )

    assert result is not None
    assert result["completion_verified_report_modified_at"] == "report-v2"
    assert "completion_accepted_at_limit_report_modified_at" not in result
    assert "jump_to" not in result
    assert "completion_attempts" not in result


@pytest.mark.parametrize("async_", [False, True])
def test_final_revision_limit_accepts_only_current_owned_report_without_jump(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        return _needs_revision_verdict()

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 2)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(),
        _verification_state(
            report_modified_at="report-v2", verification_round=1
        ),
        async_=async_,
    )

    assert result is not None
    assert result["completion_accepted_at_limit_report_modified_at"] == "report-v2"
    assert result["verification_round"] == 2
    assert result["verification_feedback"] is None
    assert "completion_verified_report_modified_at" not in result
    assert "jump_to" not in result
    assert "completion_attempts" not in result


def test_research_state_extends_completion_state() -> None:
    assert CompletionState in agent_module.ResearchState.__orig_bases__


def test_verification_hooks_declare_explicit_model_and_end_routes() -> None:
    middleware = agent_module.ResearchStateMiddleware()

    assert getattr(middleware.after_model, "__can_jump_to__") == ["model", "end"]
    assert getattr(middleware.aafter_model, "__can_jump_to__") == ["model", "end"]
