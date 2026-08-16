"""Tests for verification todo progress shown to clients."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from deepagents.backends.utils import create_file_data
from langchain_core.messages import HumanMessage

from research_agent import agent as agent_module
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
    async def fake_verify_report(*, question: str, report: str) -> VerificationVerdict:
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
        "messages": [HumanMessage(content="What is graph engineering?")],
        "files": {"/final_report.md": create_file_data("Final report")},
        "todos": [
            {"id": "research", "content": "Research graph engineering", "status": "completed"},
            {"id": "verification_pass", "content": "old", "status": "in_progress"},
        ],
        "verification_round": verification_round,
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
