"""Tests for verification todo progress shown to clients."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from deepagents.backends.utils import create_file_data
from langchain_core.messages import HumanMessage

from research_agent import agent as agent_module
from research_agent.completion_guard import inspect_completion
from research_agent.research_subagent.utils.verification import VerificationVerdict

POLICY_CASES = [
    (
        "needs_revision",
        0,
        "in_progress",
        "Verification 1/2 complete — revision required",
    ),
    (
        "complete",
        0,
        "completed",
        "Verified report quality (round 1/2)",
    ),
    (
        "needs_revision",
        1,
        "completed",
        "Verification 2/2 complete — revision limit reached",
    ),
]


@pytest.mark.parametrize(
    ("verdict_status", "verification_round", "expected_status", "expected_content"),
    POLICY_CASES,
)
def test_build_verification_todo_reflects_terminal_state(
    verdict_status: str,
    verification_round: int,
    expected_status: str,
    expected_content: str,
) -> None:
    assert agent_module._build_verification_todo(
        verdict_status=verdict_status,
        verification_round=verification_round,
        max_rounds=2,
    ) == {
        "id": "verification_pass",
        "content": expected_content,
        "status": expected_status,
    }


def test_citation_correction_progress_is_not_labeled_verified() -> None:
    todo = agent_module._build_citation_correction_todo(
        correction=1,
        limit=1,
    )

    assert todo == {
        "id": "verification_pass",
        "content": "Citation correction 1/1 requested",
        "status": "in_progress",
    }
    assert "Verified" not in todo["content"]


@pytest.mark.parametrize("middleware_path", ["sync", "async"])
@pytest.mark.parametrize(
    ("verdict_status", "verification_round", "expected_status", "expected_content"),
    POLICY_CASES,
)
def test_middleware_paths_publish_verification_progress_policy(
    monkeypatch: pytest.MonkeyPatch,
    middleware_path: str,
    verdict_status: str,
    verification_round: int,
    expected_status: str,
    expected_content: str,
) -> None:
    async def fake_verify_report(
        *, question: str, report: str, **_kwargs: Any
    ) -> VerificationVerdict:
        assert question == "What is graph engineering?"
        assert report == "Final report"
        return VerificationVerdict(
            status=verdict_status,
            sufficiency_score=0.5 if verdict_status == "needs_revision" else 1.0,
            sufficiency_reason="Needs revision" if verdict_status == "needs_revision" else "",
        )

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 2)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)

    middleware = agent_module.ResearchStateMiddleware()
    state: dict[str, Any] = {
        "messages": [
            HumanMessage(content="What is graph engineering?"),
            agent_module.AIMessage(content="Done", id="terminal-response"),
        ],
        "files": {"/final_report.md": create_file_data("Final report")},
        "todos": [
            {"id": "research", "content": "Research graph engineering", "status": "completed"},
            {"id": "verification_pass", "content": "old", "status": "in_progress"},
        ],
        "verification_round": verification_round,
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "prior-report",
        "_streamed_files": ["/final_report.md"],
    }

    if middleware_path == "async":
        result = asyncio.run(middleware.aafter_model(state=state, runtime=None))
    else:
        result = middleware.after_model(state=state, runtime=None)

    assert result is not None
    assert result["todos"] == [
        {"id": "research", "content": "Research graph engineering", "status": "completed"},
        {
            "id": "verification_pass",
            "content": expected_content,
            "status": expected_status,
        },
    ]


@pytest.mark.parametrize("async_", [False, True])
def test_second_verification_round_excludes_only_internal_verification_todo(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    calls = 0

    async def fake_verify_report(
        *, question: str, report: str, **_kwargs: Any
    ) -> VerificationVerdict:
        nonlocal calls
        calls += 1
        return VerificationVerdict(status="complete", sufficiency_score=1.0)

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", True)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 2)
    monkeypatch.setattr(agent_module, "verify_report", fake_verify_report)
    report = create_file_data("Revised final report")
    state: dict[str, Any] = {
        "messages": [
            HumanMessage(content="What is graph engineering?"),
            agent_module.AIMessage(content="Revised", id="terminal-response"),
        ],
        "files": {"/final_report.md": report},
        "todos": [
            {"id": "research", "content": "Research", "status": "completed"},
            {
                "id": "verification_pass",
                "content": "Verification 1/2 complete — revision required",
                "status": "in_progress",
            },
        ],
        "verification_round": 1,
        "verification_feedback": "previous feedback",
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "prior-report",
        "_streamed_files": ["/final_report.md"],
    }

    middleware = agent_module.ResearchStateMiddleware()
    if async_:
        result = asyncio.run(middleware.aafter_model(state, runtime=None))
    else:
        result = middleware.after_model(state, runtime=None)

    assert calls == 1
    assert result is not None
    assert result["todos"][-1]["id"] == "verification_pass"
    assert result["todos"][-1]["status"] == "completed"
    assert agent_module.research_todos_complete(state["todos"]) is True


def test_research_todos_complete_does_not_exclude_similarly_named_user_todo() -> None:
    todos = [
        {"id": "research", "content": "Research", "status": "completed"},
        {
            "id": "verification_pass_external",
            "content": "Verify external source",
            "status": "in_progress",
        },
    ]

    assert agent_module.research_todos_complete(todos) is False


def test_final_readiness_still_requires_completed_verification_todo() -> None:
    report = create_file_data("Final report")
    todos = [
        {"id": "research", "content": "Research", "status": "completed"},
        {
            "id": "verification_pass",
            "content": "Verification 1/2 complete — revision required",
            "status": "in_progress",
        },
    ]

    pending = inspect_completion(
        todos=todos,
        files={"/final_report.md": report},
        plan_active=True,
        report_owned=True,
        report_baseline_modified_at="prior-report",
    )
    completed = inspect_completion(
        todos=[*todos[:-1], {**todos[-1], "status": "completed"}],
        files={"/final_report.md": report},
        plan_active=True,
        report_owned=True,
        report_baseline_modified_at="prior-report",
    )

    assert pending.ready is False
    assert completed.ready is True
