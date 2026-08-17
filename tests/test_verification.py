"""Tests for post-generation verification and adversarial gap analysis."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from research_agent import agent as agent_module
from research_agent import completion_guard
from research_agent.completion_guard import CompletionState
from research_agent.model_call_guard import ModelCallTimeoutError
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

    async def ainvoke(self, messages):
        return self.invoke(messages)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, 2),
        ("", 2),
        ("invalid", 2),
        ("0", 0),
        ("-3", 0),
        ("1", 1),
        ("2", 2),
    ],
)
def test_parse_max_verification_rounds_uses_safe_effective_value(
    raw_value: str | None,
    expected: int,
) -> None:
    assert verification_module._parse_max_verification_rounds(raw_value) == expected


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

        score, reason = asyncio.run(
            _check_report_sufficiency(
                question="What is 2+2?",
                report="2+2 equals 4. This is a fundamental arithmetic fact.",
            )
        )
        assert score == 0.95
        assert reason == "Complete."

    def test_empty_report_scores_zero(self):
        score, reason = asyncio.run(
            _check_report_sufficiency(
                question="What is 2+2?",
                report="",
            )
        )
        assert score == 0.0
        assert reason

    def test_uses_async_model_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class AsyncOnlyJudge:
            def invoke(self, messages: list[HumanMessage]) -> AIMessage:
                raise AssertionError("sufficiency judge must use ainvoke")

            async def ainvoke(self, messages: list[HumanMessage]) -> AIMessage:
                assert messages
                return AIMessage(
                    content='{"sufficiency_score": 0.8, "reason": "Complete."}'
                )

        monkeypatch.setattr(
            verification_module, "get_configured_model", lambda: AsyncOnlyJudge()
        )

        assert asyncio.run(
            _check_report_sufficiency("What is X?", "X is explained.")
        ) == (0.8, "Complete.")

    @pytest.mark.parametrize("error", [
        ModelCallTimeoutError("ollama", 1.0, False),
        asyncio.CancelledError(),
    ])
    def test_control_errors_propagate(self, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
        class FailingJudge:
            async def ainvoke(self, messages: list[HumanMessage]) -> AIMessage:
                raise error

        monkeypatch.setattr(
            verification_module, "get_configured_model", lambda: FailingJudge()
        )

        with pytest.raises(type(error)):
            asyncio.run(_check_report_sufficiency("What is X?", "X is explained."))


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

        gaps = asyncio.run(
            _adversarial_gap_analysis(
                question="Compare Python vs JavaScript for web development.",
                report="Python is good. JavaScript is also good.",
            )
        )
        assert gaps == ["Missing framework-specific tradeoffs."]

    def test_empty_report_returns_gap(self):
        gaps = asyncio.run(
            _adversarial_gap_analysis(question="What is X?", report="")
        )
        assert len(gaps) >= 1
        assert any("empty" in g.lower() for g in gaps)

    def test_control_errors_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = ModelCallTimeoutError("ollama", 1.0, False)

        class FailingJudge:
            async def ainvoke(self, messages: list[HumanMessage]) -> AIMessage:
                raise error

        monkeypatch.setattr(
            verification_module, "get_configured_model", lambda: FailingJudge()
        )

        with pytest.raises(ModelCallTimeoutError) as raised:
            asyncio.run(_adversarial_gap_analysis("What is X?", "X is explained."))
        assert raised.value is error


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
    final_report = _report(modified_at=report_modified_at)
    report_fingerprint = completion_guard.artifact_fingerprint(final_report)
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
            "/final_report.md": final_report,
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
        "completion_report_baseline_fingerprint": "prior-report-fingerprint",
        "completion_report_owned_fingerprint": (
            report_fingerprint if report_owned else None
        ),
        "completion_verified_report_modified_at": None,
        "completion_verified_report_fingerprint": None,
        "completion_accepted_at_limit_report_modified_at": None,
        "completion_accepted_at_limit_report_fingerprint": None,
        "completion_cited_baseline_fingerprints": {},
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
    "error",
    [ModelCallTimeoutError("ollama", 1.0, False), asyncio.CancelledError()],
)
def test_verification_control_errors_propagate_without_completion_update(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    error: BaseException,
) -> None:
    async def failing_verify_report(**kwargs: Any) -> VerificationVerdict:
        raise error

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "verify_report", failing_verify_report)
    state = _verification_state()
    original_attempts = state["completion_attempts"]

    with pytest.raises(type(error)):
        _run_after_model(
            agent_module.ResearchStateMiddleware(), state, async_=async_
        )

    assert state["completion_attempts"] == original_attempts


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
        {
            "completion_report_baseline_modified_at": "report-v1",
            "completion_report_baseline_fingerprint": (
                completion_guard.artifact_fingerprint(_report())
            ),
        },
        {
            "completion_verified_report_modified_at": "report-v1",
            "completion_verified_report_fingerprint": (
                completion_guard.artifact_fingerprint(_report())
            ),
        },
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


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("request_file_kind", ["valid", "empty", "malformed"])
def test_resumed_verification_uses_original_current_generation_question(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    request_file_kind: str,
) -> None:
    seen_questions: list[str] = []

    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        seen_questions.append(question)
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    state = _verification_state()
    state["messages"] = [
        HumanMessage(content="Stale prior-generation question"),
        HumanMessage(content="Original current-generation research question"),
        HumanMessage(content="continue"),
        AIMessage(content="Research complete.", id="terminal-response"),
    ]
    state["completion_resume_adopted_generation"] = "generation-v1"
    request_files: dict[str, object] = {
        "valid": _report(
            "Original current-generation research question",
            modified_at="request-v1",
        ),
        "empty": _report("   ", modified_at="request-v1"),
        "malformed": {
            "content": object(),
            "encoding": "utf-8",
            "modified_at": "request-v1",
        },
    }
    state["files"]["/research_request.md"] = request_files[request_file_kind]

    _run_after_model(agent_module.ResearchStateMiddleware(), state, async_=async_)

    assert seen_questions == ["Original current-generation research question"]
    assert "Stale prior-generation question" not in seen_questions
    assert "continue" not in seen_questions


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("request_file_kind", ["missing", "malformed"])
def test_repeated_resume_controls_never_replace_generation_owned_question(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    request_file_kind: str,
) -> None:
    original_question = "Continue comparing graph systems with evidence"
    seen_questions: list[str] = []

    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        seen_questions.append(question)
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    state = _verification_state()
    state["messages"] = [
        HumanMessage(content="Stale prior-generation question"),
        HumanMessage(content=original_question),
        HumanMessage(content="continue"),
        HumanMessage(content="please proceed"),
        AIMessage(content="Research complete.", id="terminal-response"),
    ]
    state["completion_resume_adopted_generation"] = "generation-v1"
    state["_last_user_msg_hash"] = hashlib.md5(original_question.encode()).hexdigest()
    if request_file_kind == "missing":
        del state["files"]["/research_request.md"]
    else:
        state["files"]["/research_request.md"] = {
            "content": object(),
            "encoding": "utf-8",
            "modified_at": "request-v1",
        }

    _run_after_model(agent_module.ResearchStateMiddleware(), state, async_=async_)

    assert seen_questions == [original_question]
    assert "Stale prior-generation question" not in seen_questions
    assert "continue" not in seen_questions
    assert "please proceed" not in seen_questions


@pytest.mark.parametrize("request_path", ["direct", "sync", "async"])
def test_next_model_request_removes_legacy_verification_feedback_system_message(
    request_path: str,
) -> None:
    feedback = "<VerificationFeedback>Add evidence.</VerificationFeedback>"
    legacy_feedback = (
        "<VerificationFeedback>Stale checkpoint feedback.</VerificationFeedback>"
    )
    state = _verification_state()
    state["verification_feedback"] = feedback
    persisted_messages = [
        HumanMessage(content="Original request"),
        AIMessage(content="Draft complete"),
        SystemMessage(content=legacy_feedback),
        HumanMessage(content="continue"),
    ]
    request = ModelRequest(
        model=FakeListChatModel(responses=["done"]),
        messages=persisted_messages,
        system_message=SystemMessage(content="Existing orchestrator guidance"),
        state=state,
    )
    middleware = agent_module.ResearchStateMiddleware()
    captured: list[ModelRequest] = []

    if request_path == "direct":
        configured = middleware.configure_request(request)
    elif request_path == "sync":
        middleware.wrap_model_call(
            request,
            lambda configured_request: captured.append(configured_request),
        )
        configured = captured[0]
    else:
        async def handler(configured_request: ModelRequest) -> None:
            captured.append(configured_request)

        asyncio.run(middleware.awrap_model_call(request, handler))
        configured = captured[0]

    assert configured.system_message is not None
    assert str(configured.system_message.content).startswith(
        "Existing orchestrator guidance"
    )
    assert feedback in str(configured.system_message.content)
    assert legacy_feedback not in str(configured.system_message.content)
    assert [type(message) for message in configured.messages] == [
        HumanMessage,
        AIMessage,
        HumanMessage,
    ]
    assert request.messages == persisted_messages


def test_next_model_request_preserves_arbitrary_persisted_system_message() -> None:
    user_system_message = SystemMessage(content="User-managed system guidance")
    request = ModelRequest(
        model=FakeListChatModel(responses=["done"]),
        messages=[HumanMessage(content="Question"), user_system_message],
        state=_verification_state(),
    )

    configured = agent_module.ResearchStateMiddleware().configure_request(request)

    assert user_system_message in configured.messages


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

    state = _verification_state(report_modified_at="report-v2")
    state["completion_accepted_at_limit_report_modified_at"] = "old-accepted"
    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )

    assert result is not None
    assert result["completion_verified_report_modified_at"] == "report-v2"
    assert result["completion_accepted_at_limit_report_modified_at"] is None
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

    state = _verification_state(
        report_modified_at="report-v2", verification_round=1
    )
    state["completion_verified_report_modified_at"] = "old-verified"
    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )

    assert result is not None
    assert result["completion_accepted_at_limit_report_modified_at"] == "report-v2"
    assert result["verification_round"] == 2
    assert result["verification_feedback"] is None
    assert result["completion_verified_report_modified_at"] is None
    assert "jump_to" not in result
    assert "completion_attempts" not in result


def _streamable_state(
    *,
    todos: list[dict[str, str]] | None = None,
    report_modified_at: str = "report-v1",
) -> dict[str, Any]:
    state = _verification_state(
        todos=todos,
        report_modified_at=report_modified_at,
    )
    state["files"].update(
        {
            "/cited_response_2.md": _report(
                "Second finding", modified_at="citation-v2"
            ),
            "/cited_response.md": _report(
                "First finding", modified_at="citation-v1"
            ),
        }
    )
    state["_streamed_files"] = []
    return state


def _message_texts(update: dict[str, Any] | None) -> list[str]:
    if update is None:
        return []
    return [
        str(message.content)
        for message in update.get("messages", [])
        if isinstance(message, AIMessage)
    ]


@pytest.mark.parametrize("async_", [False, True])
def test_pending_todo_report_continues_without_streaming_report_messages(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    state = _streamable_state(
        todos=[
            {
                "id": "research",
                "content": "Research graph engineering",
                "status": "in_progress",
            }
        ]
    )

    research_update = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )
    completion_update = _run_after_model(
        completion_guard.CompletionGuardMiddleware(
            config_getter=lambda: {"run_id": "run-v1"}
        ),
        {**state, **(research_update or {})},
        async_=async_,
    )

    assert not any(
        text.startswith(("**LLM Wiki Query Findings:**", "**Final Report:**"))
        or text == "---"
        for text in _message_texts(research_update)
    )
    assert research_update is None or "_streamed_files" not in research_update
    assert completion_update is not None
    assert completion_update["jump_to"] == "model"


@pytest.mark.parametrize("async_", [False, True])
def test_verification_revision_does_not_stream_provisional_report(
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
        _streamable_state(),
        async_=async_,
    )

    assert result is not None
    assert result["jump_to"] == "model"
    assert not any(
        text.startswith(("**LLM Wiki Query Findings:**", "**Final Report:**"))
        or text == "---"
        for text in _message_texts(result)
    )
    assert "_streamed_files" not in result


@pytest.mark.parametrize("async_", [False, True])
def test_accepted_report_streams_ordered_output_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    middleware = agent_module.ResearchStateMiddleware()
    state = _streamable_state()

    first = _run_after_model(middleware, state, async_=async_)

    assert first is not None
    assert _message_texts(first) == [
        "**LLM Wiki Query Findings:**\n\nFirst finding",
        "**LLM Wiki Query Findings:**\n\nSecond finding",
        "---",
        "**Final Report:**\n\nFinal report",
    ]
    assert set(first["_streamed_files"]) == {
        "/cited_response.md",
        "/cited_response_2.md",
        "/final_report.md",
    }

    repeated_state = {
        **state,
        "_streamed_files": first["_streamed_files"],
        "completion_verified_report_modified_at": first[
            "completion_verified_report_modified_at"
        ],
    }
    repeated = _run_after_model(middleware, repeated_state, async_=async_)

    assert not _message_texts(repeated)
    assert repeated is None or "_streamed_files" not in repeated


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "acceptance_field",
    [
        "completion_verified_report_modified_at",
        "completion_accepted_at_limit_report_modified_at",
    ],
)
def test_edited_report_invalidates_prior_acceptance_and_stays_provisional(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    acceptance_field: str,
) -> None:
    calls = 0

    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        nonlocal calls
        calls += 1
        return _needs_revision_verdict()

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 2)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    state = _streamable_state(report_modified_at="report-v2")
    state[acceptance_field] = "report-v1"

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )

    assert calls == 1
    assert result is not None
    assert result["jump_to"] == "model"
    assert not any(
        text.startswith(("**LLM Wiki Query Findings:**", "**Final Report:**"))
        or text == "---"
        for text in _message_texts(result)
    )
    assert "_streamed_files" not in result


def test_eval_logging_uses_same_accepted_report_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged_reports: list[dict[str, Any]] = []

    async def fake_log_server_metrics(**kwargs: Any) -> dict[str, bool]:
        logged_reports.append(kwargs["files"])
        return {"logged": True}

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", True)
    monkeypatch.setattr(agent_module, "log_server_metrics", fake_log_server_metrics)
    middleware = agent_module.ResearchStateMiddleware()
    pending = _streamable_state(
        todos=[
            {
                "id": "research",
                "content": "Research graph engineering",
                "status": "pending",
            }
        ]
    )

    pending_result = asyncio.run(
        middleware.aafter_model(state=pending, runtime=None)
    )

    assert pending_result is None or "_eval_logged" not in pending_result
    assert logged_reports == []

    accepted = _streamable_state()
    accepted_result = asyncio.run(
        middleware.aafter_model(state=accepted, runtime=None)
    )

    assert accepted_result is not None
    assert accepted_result["_eval_logged"] is True
    assert logged_reports == [accepted["files"]]


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("max_rounds", [-2, 0, 1, 2])
def test_verification_round_limit_controls_verification_and_finalization_once(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    max_rounds: int,
) -> None:
    verification_calls = 0
    eval_calls = 0

    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        nonlocal verification_calls
        verification_calls += 1
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    async def fake_log_server_metrics(**kwargs: Any) -> dict[str, bool]:
        nonlocal eval_calls
        eval_calls += 1
        return {"logged": True}

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", max_rounds)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", True)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    monkeypatch.setattr(agent_module, "log_server_metrics", fake_log_server_metrics)
    middleware = agent_module.ResearchStateMiddleware()
    state = _streamable_state()

    first = _run_after_model(middleware, state, async_=async_)
    repeated = _run_after_model(
        middleware,
        {**state, **(first or {})},
        async_=async_,
    )

    assert verification_calls == (0 if max_rounds <= 0 else 1)
    assert eval_calls == 1
    assert first is not None
    assert first["_eval_logged"] is True
    assert _message_texts(first).count("**Final Report:**\n\nFinal report") == 1
    assert not _message_texts(repeated)


@pytest.mark.parametrize("async_", [False, True])
def test_finalizer_excludes_unchanged_prior_generation_citations(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    state = _streamable_state()
    prior_citation = state["files"]["/cited_response.md"]
    same_timestamp_changed = _report(
        "Current-generation changed finding",
        modified_at=state["files"]["/cited_response_2.md"]["modified_at"],
    )
    state["files"]["/cited_response_2.md"] = same_timestamp_changed
    state["files"]["/cited_response_3.md"] = _report(
        "Current-generation new finding",
        modified_at="citation-v3",
    )
    state["completion_cited_baseline_fingerprints"] = {
        "/cited_response.md": completion_guard.artifact_fingerprint(
            prior_citation
        ),
        "/cited_response_2.md": completion_guard.artifact_fingerprint(
            _report("Second finding", modified_at="citation-v2")
        ),
    }

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )

    assert _message_texts(result) == [
        "**LLM Wiki Query Findings:**\n\nCurrent-generation changed finding",
        "**LLM Wiki Query Findings:**\n\nCurrent-generation new finding",
        "---",
        "**Final Report:**\n\nFinal report",
    ]
    assert "First finding" not in "\n".join(_message_texts(result))


@pytest.mark.parametrize("async_", [False, True])
def test_explicit_resume_keeps_generation_baseline_and_never_emits_prior_citation(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    prior = _report("Prior-generation finding", modified_at="citation-a")
    ordinary_state = {
        "messages": [AIMessage(content="Starting")],
        "files": {"/cited_response.md": prior},
    }
    guard = completion_guard.CompletionGuardMiddleware(
        config_getter=lambda: {"run_id": "run-b", "configurable": {}}
    )
    started = {**ordinary_state, **(guard.before_agent(ordinary_state, None) or {})}
    current_citation = _report(
        "Current-generation finding",
        modified_at="citation-b",
    )
    started.update(
        {
            "messages": [HumanMessage(content="Research"), AIMessage(content="Done")],
            "todos": [{"content": "Research", "status": "completed"}],
            "files": {
                **ordinary_state["files"],
                "/cited_response_2.md": current_citation,
                "/final_report.md": _report(
                    "Current final report", modified_at="report-b"
                ),
            },
            "completion_plan_owner_generation": started[
                "completion_request_generation"
            ],
            "completion_report_owned": True,
            "completion_report_owned_fingerprint": (
                completion_guard.artifact_fingerprint(
                    _report("Current final report", modified_at="report-b")
                )
            ),
        }
    )
    resume_guard = completion_guard.CompletionGuardMiddleware(
        config_getter=lambda: {
            "run_id": "resume-b",
            "configurable": {"resume_incomplete_todos": True},
        }
    )
    resumed = {**started, **(resume_guard.before_agent(started, None) or {})}

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), resumed, async_=async_
    )

    assert _message_texts(result) == [
        "**LLM Wiki Query Findings:**\n\nCurrent-generation finding",
        "---",
        "**Final Report:**\n\nCurrent final report",
    ]
    assert "Prior-generation finding" not in "\n".join(_message_texts(result))


@pytest.mark.parametrize("async_", [False, True])
def test_same_timestamp_report_edit_blocks_streaming_and_eval(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    calls = 0

    async def fake_log_server_metrics(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", True)
    monkeypatch.setattr(agent_module, "log_server_metrics", fake_log_server_metrics)
    original = _report("Original report", modified_at="report-v1")
    state = _streamable_state(report_modified_at="report-v1")
    state["files"]["/final_report.md"] = _report(
        "Mutated report", modified_at="report-v1"
    )
    original_fingerprint = completion_guard.artifact_fingerprint(original)
    state["completion_report_owned_fingerprint"] = original_fingerprint
    state["completion_verified_report_modified_at"] = "report-v1"
    state["completion_verified_report_fingerprint"] = original_fingerprint

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )

    assert not _message_texts(result)
    assert result is None or "_streamed_files" not in result
    assert result is None or "_eval_logged" not in result
    assert calls == 0


@pytest.mark.parametrize("async_", [False, True])
def test_same_timestamp_changed_report_is_verified_and_finalized(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    calls = 0

    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        nonlocal calls
        calls += 1
        assert report == "Final report"
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    state = _streamable_state(report_modified_at="same-timestamp")
    state["completion_report_baseline_modified_at"] = "same-timestamp"
    state["completion_report_baseline_fingerprint"] = (
        completion_guard.artifact_fingerprint(
            _report("Prior report", modified_at="same-timestamp")
        )
    )

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )

    assert calls == 1
    assert result is not None
    current_fingerprint = completion_guard.artifact_fingerprint(
        state["files"]["/final_report.md"]
    )
    assert result["completion_verified_report_modified_at"] == "same-timestamp"
    assert result["completion_verified_report_fingerprint"] == current_fingerprint
    assert "**Final Report:**\n\nFinal report" in _message_texts(result)


@pytest.mark.parametrize("async_", [False, True])
def test_same_timestamp_unchanged_report_is_rejected_and_continued(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    calls = 0

    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
        nonlocal calls
        calls += 1
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    state = _streamable_state(report_modified_at="same-timestamp")
    fingerprint = completion_guard.artifact_fingerprint(
        state["files"]["/final_report.md"]
    )
    state["completion_report_baseline_modified_at"] = "same-timestamp"
    state["completion_report_baseline_fingerprint"] = fingerprint

    research_update = _run_after_model(
        agent_module.ResearchStateMiddleware(), state, async_=async_
    )
    completion_update = _run_after_model(
        completion_guard.CompletionGuardMiddleware(
            config_getter=lambda: {"run_id": "run-v1"}
        ),
        {**state, **(research_update or {})},
        async_=async_,
    )

    assert calls == 0
    assert not _message_texts(research_update)
    assert completion_update is not None
    assert completion_update["jump_to"] == "model"


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("result_kind", ["success", "none", "raise"])
def test_eval_logged_only_after_sync_and_async_logger_success(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    result_kind: str,
) -> None:
    calls = 0

    async def fake_log_server_metrics(**kwargs: Any) -> dict[str, bool] | None:
        nonlocal calls
        calls += 1
        if result_kind == "raise":
            raise RuntimeError("metrics unavailable")
        if result_kind == "none":
            return None
        return {"logged": True}

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", True)
    monkeypatch.setattr(agent_module, "log_server_metrics", fake_log_server_metrics)

    result = _run_after_model(
        agent_module.ResearchStateMiddleware(),
        _streamable_state(),
        async_=async_,
    )

    assert calls == 1
    assert result is not None
    assert result.get("_eval_logged", False) is (result_kind == "success")
    assert result.get("_eval_pending", False) is False


@pytest.mark.parametrize("returns_success", [True, False])
def test_sync_eval_logging_uses_result_inside_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    returns_success: bool,
) -> None:
    calls = 0

    async def fake_log_server_metrics(**kwargs: Any) -> dict[str, bool] | None:
        nonlocal calls
        calls += 1
        return {"logged": True} if returns_success else None

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", True)
    monkeypatch.setattr(agent_module, "log_server_metrics", fake_log_server_metrics)
    middleware = agent_module.ResearchStateMiddleware()

    async def invoke_sync_hook() -> dict[str, Any] | None:
        return middleware.after_model(_streamable_state(), runtime=None)

    result = asyncio.run(invoke_sync_hook())

    assert calls == 1
    assert result is not None
    assert result.get("_eval_logged", False) is returns_success


def test_sync_eval_timeout_inside_running_loop_returns_promptly_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    cleaned_up = threading.Event()

    async def stalled_log_server_metrics(**kwargs: Any) -> dict[str, bool]:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaned_up.set()
        return {"logged": True}

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", True)
    monkeypatch.setattr(
        agent_module,
        "SYNC_EVAL_LOG_TIMEOUT_SECONDS",
        0.02,
    )
    monkeypatch.setattr(
        agent_module,
        "log_server_metrics",
        stalled_log_server_metrics,
    )
    middleware = agent_module.ResearchStateMiddleware()

    async def invoke_sync_hook() -> tuple[dict[str, Any] | None, float]:
        started_at = time.monotonic()
        result = middleware.after_model(_streamable_state(), runtime=None)
        return result, time.monotonic() - started_at

    result, elapsed = asyncio.run(invoke_sync_hook())

    assert started.wait(timeout=0.5)
    assert elapsed < 0.5
    assert result is not None
    assert result.get("_eval_logged", False) is False
    assert cleaned_up.wait(timeout=1.0)


def test_sync_eval_timeout_without_running_loop_returns_promptly_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    cleaned_up = threading.Event()

    async def stalled_log_server_metrics(**kwargs: Any) -> dict[str, bool]:
        started.set()
        try:
            await asyncio.sleep(0.5)
        finally:
            cleaned_up.set()
        return {"logged": True}

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", True)
    monkeypatch.setattr(
        agent_module,
        "SYNC_EVAL_LOG_TIMEOUT_SECONDS",
        0.02,
    )
    monkeypatch.setattr(
        agent_module,
        "log_server_metrics",
        stalled_log_server_metrics,
    )
    started_at = time.monotonic()

    result = agent_module.ResearchStateMiddleware().after_model(
        _streamable_state(),
        runtime=None,
    )
    elapsed = time.monotonic() - started_at

    assert started.wait(timeout=0.5)
    assert elapsed < 0.2
    assert result is not None
    assert result.get("_eval_logged", False) is False
    assert cleaned_up.wait(timeout=1.0)


def test_sync_eval_timeout_does_not_schedule_duplicate_inflight_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    worker_calls = 0

    def blocking_side_effect() -> dict[str, bool]:
        nonlocal worker_calls
        worker_calls += 1
        started.set()
        release.wait(timeout=1.0)
        completed.set()
        return {"logged": True}

    async def stalled_log_server_metrics(**kwargs: Any) -> dict[str, bool]:
        return await asyncio.to_thread(blocking_side_effect)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", True)
    monkeypatch.setattr(agent_module, "SYNC_EVAL_LOG_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(
        agent_module,
        "log_server_metrics",
        stalled_log_server_metrics,
    )
    middleware = agent_module.ResearchStateMiddleware()
    state = _streamable_state()

    first = middleware.after_model(state, runtime=None)
    assert started.wait(timeout=0.5)
    second = middleware.after_model({**state, **(first or {})}, runtime=None)
    release.set()

    assert first is not None
    assert first.get("_eval_logged", False) is False
    assert first["_eval_pending"] is True
    assert second is None or second.get("_eval_logged", False) is False
    assert worker_calls == 1
    assert completed.wait(timeout=1.0)


def test_research_state_extends_completion_state() -> None:
    assert CompletionState in agent_module.ResearchState.__orig_bases__


def test_verification_hooks_declare_explicit_model_and_end_routes() -> None:
    middleware = agent_module.ResearchStateMiddleware()

    assert getattr(middleware.after_model, "__can_jump_to__") == ["model", "end"]
    assert getattr(middleware.aafter_model, "__can_jump_to__") == ["model", "end"]
