"""Pure completion-policy tests."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware, hook_config
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_api.serde import default as serialize_default

import research_agent.completion_guard as completion_guard
from research_agent import citation_failure
from research_agent.completion_guard import (
    CompletionInspection,
    get_max_completion_attempts,
    inspect_completion,
)
from research_agent.research_subagent.clarification.middleware import (
    ClarificationMiddleware,
)
from research_agent.research_subagent.resume.middleware import ResumeMiddleware
from research_agent.research_subagent.tools import write_file


def _middleware(
    *, run_id: UUID | str | None, resume: bool = False
) -> completion_guard.CompletionGuardMiddleware:
    config: dict[str, Any] = {"configurable": {}}
    if run_id is not None:
        config["run_id"] = run_id
    if resume:
        config["configurable"]["resume_incomplete_todos"] = True
    return completion_guard.CompletionGuardMiddleware(config_getter=lambda: config)


def _apply(state: dict[str, Any], update: dict[str, Any] | None) -> dict[str, Any]:
    return {**state, **(update or {})}


def _runtime(run_id: UUID | str | None) -> SimpleNamespace:
    return SimpleNamespace(
        execution_info=SimpleNamespace(run_id=run_id),
    )


def test_compiled_guard_schema_accepts_minimal_ordinary_input() -> None:
    graph = create_agent(
        FakeListChatModel(responses=["done"]),
        middleware=[
            TodoListMiddleware(system_prompt=""),
            completion_guard.CompletionGuardMiddleware(),
        ],
    )

    validated = graph.get_input_schema().model_validate({"messages": []})

    assert validated.root == {"messages": []}


def test_compiled_guard_schema_hides_and_drops_forged_completion_controls() -> None:
    graph = create_agent(
        FakeListChatModel(responses=["done"]),
        middleware=[
            TodoListMiddleware(system_prompt=""),
            completion_guard.CompletionGuardMiddleware(),
        ],
    )
    input_properties = graph.get_input_jsonschema()["properties"]

    assert not any(name.startswith("completion_") for name in input_properties)
    assert not any(name.startswith("citation_") for name in input_properties)
    assert "_eval_pending" not in input_properties

    forged = graph.get_input_schema().model_validate(
        {
            "messages": [],
            "completion_request_generation": "forged-generation",
            "completion_plan_owner_generation": "forged-generation",
            "completion_report_owned": True,
            "citation_failure_run_id": "forged-run",
            "citation_accepted_report_fingerprint": "forged-report",
            "citation_corrections_used": 99,
            "_eval_pending": True,
        }
    )

    assert forged.root == {"messages": []}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 3),
        ("bad", 3),
        ("0", 3),
        ("-1", 3),
        ("1", 1),
        ("2", 2),
        ("3", 3),
        ("4", 3),
    ],
)
def test_get_max_completion_attempts_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
    expected: int,
) -> None:
    if raw is None:
        monkeypatch.delenv("MAX_COMPLETION_ATTEMPTS", raising=False)
    else:
        monkeypatch.setenv("MAX_COMPLETION_ATTEMPTS", raw)

    assert get_max_completion_attempts() == expected


def _file(content: Any, *, modified_at: Any = "current") -> dict[str, Any]:
    return {
        "content": content,
        "encoding": "utf-8",
        "created_at": "created",
        "modified_at": modified_at,
    }


def _fingerprint(file_data: object) -> str:
    fingerprint = getattr(completion_guard, "artifact_fingerprint", None)
    assert fingerprint is not None
    value = fingerprint(file_data)
    assert isinstance(value, str)
    return value


def test_citation_failure_helpers_fail_closed_without_leaking_report_data() -> None:
    report = _file("Private claim https://secret.invalid/token=do-not-echo")
    fingerprint = _fingerprint(report)
    terminal = AIMessage(content="Private terminal report", id="terminal")
    update = citation_failure.build_citation_failure_update(
        run_id="run-current",
        report_fingerprint=fingerprint,
        defects=[
            citation_failure.CitationFailureDefect(
                code="missing_url",
                detail="https://secret.invalid/token=do-not-echo",
            ),
            citation_failure.CitationFailureDefect(
                code="unresolved_reference",
                detail="source:7",
            ),
        ],
        terminal=terminal,
    )

    assert update["jump_to"] == "end"
    assert update["citation_failure_run_id"] == "run-current"
    assert update["citation_failure_report_fingerprint"] == fingerprint
    assert update["citation_failure_defects"] == [
        {"code": "missing_url", "detail": "redacted"},
        {"code": "unresolved_reference", "detail": "source:7"},
    ]
    assert update["messages"][0].response_metadata["resume_intermediate"] is True
    assert "_streamed_files" not in update
    assert update["completion_verified_report_fingerprint"] is None
    assert update["completion_verified_report_run_id"] is None
    assert update["completion_accepted_at_limit_report_fingerprint"] is None
    assert update["completion_accepted_at_limit_report_run_id"] is None
    assert update["citation_accepted_report_fingerprint"] is None

    state = {
        **update,
        "files": {"/final_report.md": report},
        "completion_current_run_id": "run-current",
    }
    assert citation_failure.citation_failure_is_current(
        state,
        run_id="run-current",
        report_fingerprint=fingerprint,
    )
    assert citation_failure.citation_failure_blocks_finalization(
        state,
        report_fingerprint=fingerprint,
    )
    assert not citation_failure.citation_acceptance_ready(
        state,
        report_fingerprint=fingerprint,
        strict_required=True,
    )

    with pytest.raises(citation_failure.ReportCitationError) as caught:
        citation_failure.raise_current_citation_failure(
            state,
            run_id="run-current",
            report_fingerprint=fingerprint,
        )
    serialized = serialize_default(caught.value)
    assert serialized["error"] == "ReportCitationError"
    assert "secret.invalid" not in repr(serialized)
    assert "do-not-echo" not in repr(serialized)
    assert "Private" not in repr(serialized)


@pytest.mark.parametrize(
    "state_update",
    [
        {"citation_failure_run_id": "stale-run"},
        {"citation_failure_report_fingerprint": "changed-report"},
        {"citation_failure_defects": [{"code": "unknown", "detail": "web"}]},
        {"citation_failure_defects": "malformed"},
    ],
)
def test_stale_or_malformed_citation_failure_is_ignored_and_cleared(
    state_update: dict[str, object],
) -> None:
    state = {
        "citation_failure_run_id": "run-current",
        "citation_failure_report_fingerprint": "report-current",
        "citation_failure_defects": [
            {"code": "missing_url", "detail": "web"}
        ],
        **state_update,
    }

    assert not citation_failure.citation_failure_is_current(
        state,
        run_id="run-current",
        report_fingerprint="report-current",
    )
    assert citation_failure.clear_stale_citation_failure(
        state,
        run_id="run-current",
        report_fingerprint="report-current",
    ) == {
        "citation_failure_run_id": None,
        "citation_failure_report_fingerprint": None,
        "citation_failure_defects": [],
    }
    assert citation_failure.raise_current_citation_failure(
        state,
        run_id="run-current",
        report_fingerprint="report-current",
    ) is None


def test_citation_acceptance_requires_exact_fingerprint_only_in_strict_mode() -> None:
    state = {"citation_accepted_report_fingerprint": "report-current"}

    assert citation_failure.citation_acceptance_ready(
        state,
        report_fingerprint="report-current",
        strict_required=True,
    )
    assert not citation_failure.citation_acceptance_ready(
        state,
        report_fingerprint="report-changed",
        strict_required=True,
    )
    assert citation_failure.citation_acceptance_ready(
        {},
        report_fingerprint="report-current",
        strict_required=False,
    )


def test_citation_run_id_resolution_prefers_runtime_then_config_then_state() -> None:
    assert citation_failure.resolve_citation_run_id(
        {"run_id": "configured"},
        _runtime("actual"),
        fallback="checkpoint",
    ) == "actual"
    assert citation_failure.resolve_citation_run_id(
        {"run_id": "configured"},
        _runtime(None),
        fallback="checkpoint",
    ) == "configured"
    assert citation_failure.resolve_citation_run_id(
        {},
        _runtime(None),
        fallback="checkpoint",
    ) == "checkpoint"


@pytest.mark.parametrize("async_", [False, True])
def test_citation_after_agent_requires_durable_checkpointer_confirmation(
    async_: bool,
) -> None:
    import research_agent.agent as agent_module

    report = _file("Invalid report")
    fingerprint = _fingerprint(report)
    state = {
        "files": {"/final_report.md": report},
        "completion_current_run_id": "run-current",
        "citation_failure_run_id": "run-current",
        "citation_failure_report_fingerprint": fingerprint,
        "citation_failure_defects": [
            {"code": "missing_url", "detail": "web"}
        ],
    }
    middleware = agent_module.ResearchStateMiddleware(
        config_getter=lambda: {
            "run_id": "run-current",
            "configurable": {"thread_id": "no-checkpointer"},
        }
    )

    if async_:
        result = asyncio.run(middleware.aafter_agent(state, _runtime("run-current")))
    else:
        result = middleware.after_agent(state, _runtime("run-current"))

    assert result is None


@pytest.mark.parametrize("raw", [None, "", "bad", "0", "-1", "nan", "inf"])
def test_citation_checkpoint_confirmation_timeout_is_safe(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
) -> None:
    import research_agent.agent as agent_module

    if raw is None:
        monkeypatch.delenv(
            "CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS",
            raising=False,
        )
    else:
        monkeypatch.setenv("CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS", raw)

    assert agent_module._citation_checkpoint_confirm_timeout_seconds() >= 5.0


def test_citation_checkpoint_confirmation_timeout_accepts_bounded_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_agent.agent as agent_module

    monkeypatch.setenv("CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS", "0.45")

    assert agent_module._citation_checkpoint_confirm_timeout_seconds() == 0.45


def _inspect(
    *,
    todos: object,
    files: object | None = None,
    report_owned: bool = True,
    baseline: str | None = "prior",
) -> CompletionInspection:
    return inspect_completion(
        todos=todos,
        files={} if files is None else files,
        plan_active=True,
        report_owned=report_owned,
        report_baseline_modified_at=baseline,
    )


def test_completed_plan_requires_every_item_to_be_valid_and_completed() -> None:
    inspection = _inspect(
        todos=[
            {"content": "Research", "status": "completed"},
            {"content": "Write report", "status": "completed"},
        ],
        files={"/final_report.md": _file("Finished report")},
    )

    assert inspection.incomplete_todo_count == 0
    assert inspection.malformed_todo_count == 0
    assert inspection.ready is True

    pending = _inspect(
        todos=[
            {"content": "Research", "status": "completed"},
            {"content": "Write report", "status": "pending"},
        ],
        files={"/final_report.md": _file("Finished report")},
    )

    assert pending.incomplete_todo_count == 1
    assert pending.malformed_todo_count == 0
    assert pending.ready is False


@pytest.mark.parametrize("status", ["Completed", " COMPLETED ", "completed "])
def test_completed_status_requires_an_exact_schema_value(status: str) -> None:
    inspection = _inspect(
        todos=[{"content": "Research", "status": status}],
        files={"/final_report.md": _file("Finished report")},
    )

    assert inspection.incomplete_todo_count == 1
    assert inspection.malformed_todo_count == 1
    assert inspection.ready is False


@pytest.mark.parametrize(
    "todos",
    [
        [],
        "not-a-list",
        [None],
        [{"status": "completed"}],
        [{"content": "", "status": "completed"}],
        [{"content": "Research"}],
        [{"content": "Research", "status": "unknown"}],
        [{"content": "Research", "status": 3}],
    ],
)
def test_malformed_or_unknown_todo_is_incomplete(todos: object) -> None:
    inspection = _inspect(
        todos=todos,
        files={"/final_report.md": _file("Finished report")},
    )

    assert inspection.incomplete_todo_count >= 1
    assert inspection.malformed_todo_count >= 1
    assert inspection.ready is False


@pytest.mark.parametrize(
    ("files", "report_owned", "baseline", "reason"),
    [
        ({}, True, "prior", "missing"),
        ("not-a-file-map", True, "prior", "malformed"),
        ({"/final_report.md": _file("   ")}, True, "prior", "empty"),
        ({"/final_report.md": "not-file-data"}, True, "prior", "malformed"),
        ({"/final_report.md": _file(object())}, True, "prior", "malformed"),
        ({"/final_report.md": _file("Finished", modified_at=None)}, True, "prior", "malformed"),
        ({"/final_report.md": _file("Finished")}, False, "prior", "stale"),
        ({"/final_report.md": _file("Finished", modified_at="same")}, True, "same", "stale"),
    ],
)
def test_report_inspection_rejects_missing_empty_malformed_and_stale_files(
    files: object,
    report_owned: bool,
    baseline: str | None,
    reason: str,
) -> None:
    inspection = _inspect(
        todos=[{"content": "Research", "status": "completed"}],
        files=files,
        report_owned=report_owned,
        baseline=baseline,
    )

    assert inspection.report_reason == reason
    assert inspection.ready is False


def test_report_inspection_accepts_changed_nonempty_owned_report() -> None:
    inspection = _inspect(
        todos=[{"content": "Research", "status": "completed"}],
        files={"/final_report.md": _file(["Finished", " report"])},
        report_owned=True,
        baseline="prior",
    )

    assert inspection.report_reason is None
    assert inspection.ready is True


@pytest.mark.parametrize(
    ("verified", "accepted_at_limit", "expected"),
    [
        ("report-v2", None, True),
        (None, "report-v2", True),
        ("report-v1", None, False),
        (None, "report-v1", False),
        ("report-v1", "report-v1", False),
    ],
)
def test_finalization_acceptance_is_owned_by_exact_report_version(
    verified: str | None,
    accepted_at_limit: str | None,
    expected: bool,
) -> None:
    readiness = getattr(
        completion_guard,
        "completion_ready_for_finalization",
        None,
    )
    assert readiness is not None
    report = _file("Finished report", modified_at="report-v2")
    current_fingerprint = _fingerprint(report)
    state = {
        "todos": [{"content": "Research", "status": "completed"}],
        "files": {"/final_report.md": report},
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "prior-report",
        "completion_report_owned_fingerprint": current_fingerprint,
        "completion_verified_report_modified_at": verified,
        "completion_verified_report_fingerprint": (
            current_fingerprint if verified == "report-v2" else "old-fingerprint"
        ),
        "completion_accepted_at_limit_report_modified_at": accepted_at_limit,
        "completion_accepted_at_limit_report_fingerprint": (
            current_fingerprint
            if accepted_at_limit == "report-v2"
            else "old-fingerprint"
        ),
    }

    assert readiness(state, verification_enabled=True) is expected


def test_finalization_without_verification_still_requires_completion_readiness() -> None:
    readiness = getattr(
        completion_guard,
        "completion_ready_for_finalization",
        None,
    )
    assert readiness is not None
    report = _file("Premature report", modified_at="report-v1")
    state = {
        "todos": [{"content": "Research", "status": "pending"}],
        "files": {"/final_report.md": report},
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "prior-report",
        "completion_report_owned_fingerprint": _fingerprint(report),
    }

    assert readiness(state, verification_enabled=False) is False


def test_strict_finalization_requires_current_structural_acceptance() -> None:
    report = _file("Finished report", modified_at="report-v2")
    fingerprint = _fingerprint(report)
    state = {
        "todos": [{"content": "Research", "status": "completed"}],
        "files": {"/final_report.md": report},
        "completion_current_run_id": "run-current",
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "prior-report",
        "completion_report_owned_fingerprint": fingerprint,
        "strict_web_citations": True,
    }

    assert not completion_guard.completion_ready_for_finalization(
        state,
        verification_enabled=False,
    )

    state["citation_accepted_report_fingerprint"] = fingerprint
    assert completion_guard.completion_ready_for_finalization(
        state,
        verification_enabled=False,
    )

    state.update(
        {
            "citation_failure_run_id": "run-current",
            "citation_failure_report_fingerprint": fingerprint,
            "citation_failure_defects": [
                {"code": "missing_url", "detail": "web"}
            ],
        }
    )
    assert not completion_guard.completion_ready_for_finalization(
        state,
        verification_enabled=False,
    )
    assert not completion_guard._inspect_state_completion(state).ready


@pytest.mark.parametrize(
    "acceptance_prefix",
    ["completion_verified_report", "completion_accepted_at_limit_report"],
)
def test_same_timestamp_report_replacement_invalidates_accepted_content(
    acceptance_prefix: str,
) -> None:
    original = _file("Original report", modified_at="report-v1")
    replacement = _file("Replaced report", modified_at="report-v1")
    state = {
        "todos": [{"content": "Research", "status": "completed"}],
        "files": {"/final_report.md": replacement},
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "prior-report",
        "completion_report_baseline_fingerprint": "prior-fingerprint",
        "completion_report_owned_fingerprint": _fingerprint(original),
        "completion_verified_report_modified_at": None,
        "completion_verified_report_fingerprint": None,
        "completion_accepted_at_limit_report_modified_at": None,
        "completion_accepted_at_limit_report_fingerprint": None,
    }
    state[f"{acceptance_prefix}_modified_at"] = "report-v1"
    state[f"{acceptance_prefix}_fingerprint"] = _fingerprint(original)

    assert (
        completion_guard.completion_ready_for_finalization(
            state,
            verification_enabled=True,
        )
        is False
    )


def test_same_timestamp_changed_report_is_current_when_baseline_fingerprint_differs() -> None:
    baseline = _file("Prior report", modified_at="same-timestamp")
    current = _file("Current report", modified_at="same-timestamp")
    state = {
        "todos": [{"content": "Research", "status": "completed"}],
        "files": {"/final_report.md": current},
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "same-timestamp",
        "completion_report_baseline_fingerprint": _fingerprint(baseline),
        "completion_report_owned_fingerprint": _fingerprint(current),
    }

    assert completion_guard.completion_ready_for_finalization(
        state,
        verification_enabled=False,
    )


def test_unchanged_baseline_fingerprint_remains_stale() -> None:
    baseline = _file("Prior report", modified_at="same-timestamp")
    fingerprint = _fingerprint(baseline)
    state = {
        "todos": [{"content": "Research", "status": "completed"}],
        "files": {"/final_report.md": baseline},
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "same-timestamp",
        "completion_report_baseline_fingerprint": fingerprint,
        "completion_report_owned_fingerprint": fingerprint,
    }

    assert not completion_guard.completion_ready_for_finalization(
        state,
        verification_enabled=False,
    )


@pytest.mark.parametrize(
    "malformed",
    [{"unhashable": True}, ["unhashable"], {"nested": []}],
)
def test_malformed_acceptance_metadata_fails_closed(malformed: object) -> None:
    report = _file("Finished report", modified_at="report-v1")
    state = {
        "todos": [{"content": "Research", "status": "completed"}],
        "files": {"/final_report.md": report},
        "completion_request_generation": "generation-v1",
        "completion_plan_owner_generation": "generation-v1",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "prior-report",
        "completion_report_baseline_fingerprint": "prior-fingerprint",
        "completion_report_owned_fingerprint": _fingerprint(report),
        "completion_verified_report_modified_at": malformed,
        "completion_verified_report_fingerprint": malformed,
        "completion_accepted_at_limit_report_modified_at": malformed,
        "completion_accepted_at_limit_report_fingerprint": malformed,
    }

    assert (
        completion_guard.completion_ready_for_finalization(
            state,
            verification_enabled=True,
        )
        is False
    )


def test_ordinary_generation_snapshots_report_and_cited_artifact_fingerprints() -> None:
    run_id = uuid4()
    report = _file("Prior report", modified_at="report-v1")
    cited = _file("Prior citation", modified_at="citation-v1")
    state = {
        "messages": [],
        "files": {
            "/final_report.md": report,
            "/cited_response.md": cited,
            "/notes.md": _file("Not a citation", modified_at="notes-v1"),
        },
    }

    update = _middleware(run_id=run_id).before_agent(state, runtime=None)

    assert update is not None
    assert update["completion_report_baseline_fingerprint"] == _fingerprint(report)
    assert update["completion_report_owned_fingerprint"] is None
    assert update["completion_verified_report_fingerprint"] is None
    assert update["completion_accepted_at_limit_report_fingerprint"] is None
    assert update["citation_failure_run_id"] is None
    assert update["citation_failure_report_fingerprint"] is None
    assert update["citation_failure_defects"] == []
    assert update["citation_accepted_report_fingerprint"] is None
    assert update["citation_corrections_used"] == 0
    assert update["completion_cited_baseline_fingerprints"] == {
        "/cited_response.md": _fingerprint(cited)
    }


def test_new_resume_run_clears_failure_but_preserves_generation_correction_budget() -> None:
    report = _file("Invalid report", modified_at="report-v1")
    fingerprint = _fingerprint(report)
    state = {
        "messages": [],
        "files": {"/final_report.md": report},
        "completion_current_run_id": "prior-run",
        "completion_request_generation": "generation-v1",
        "citation_failure_run_id": "prior-run",
        "citation_failure_report_fingerprint": fingerprint,
        "citation_failure_defects": [{"code": "missing_url", "detail": "web"}],
        "citation_accepted_report_fingerprint": "prior-report",
        "citation_corrections_used": 2,
    }

    update = _middleware(run_id="new-run", resume=True).before_agent(
        state,
        runtime=None,
    )

    assert update is not None
    assert update["citation_failure_run_id"] is None
    assert update["citation_failure_report_fingerprint"] is None
    assert update["citation_failure_defects"] == []
    assert "citation_accepted_report_fingerprint" not in update
    assert "citation_corrections_used" not in update


def test_explicit_resume_preserves_generation_artifact_fingerprints() -> None:
    state = {
        "messages": [],
        "files": {},
        "completion_current_run_id": "prior-run",
        "completion_request_generation": "generation-b",
        "completion_plan_owner_generation": "generation-b",
        "completion_report_owned": True,
        "completion_report_baseline_fingerprint": "baseline-report",
        "completion_report_owned_fingerprint": "owned-report",
        "completion_verified_report_fingerprint": "verified-report",
        "completion_accepted_at_limit_report_fingerprint": None,
        "completion_cited_baseline_fingerprints": {
            "/cited_response.md": "generation-a-citation"
        },
    }

    resumed = _apply(
        state,
        _middleware(run_id=uuid4(), resume=True).before_agent(
            state,
            runtime=None,
        ),
    )

    assert resumed["completion_report_baseline_fingerprint"] == "baseline-report"
    assert resumed["completion_report_owned_fingerprint"] == "owned-report"
    assert resumed["completion_verified_report_fingerprint"] == "verified-report"
    assert resumed["completion_accepted_at_limit_report_fingerprint"] is None
    assert resumed["completion_cited_baseline_fingerprints"] == {
        "/cited_response.md": "generation-a-citation"
    }


def test_completion_requires_an_active_plan() -> None:
    inspection = inspect_completion(
        todos=[{"content": "Research", "status": "completed"}],
        files={"/final_report.md": _file("Finished report")},
        plan_active=False,
        report_owned=True,
        report_baseline_modified_at="prior",
    )

    assert inspection.plan_active is False
    assert inspection.ready is False


def test_ordinary_generation_resets_request_scoped_completion_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_COMPLETION_ATTEMPTS", "2")
    run_id = uuid4()
    state = {
        "messages": [],
        "todos": [{"content": "Stale task", "status": "completed"}],
        "files": {"/final_report.md": _file("Prior report", modified_at="prior")},
        "completion_current_run_id": "old-run",
        "completion_request_generation": "old-generation",
        "completion_plan_owner_generation": "old-generation",
        "completion_report_owned": True,
        "completion_resume_adopted_generation": "old-generation",
        "completion_attempts": 2,
        "completion_attempt_limit": 3,
        "completion_exhausted_run_id": "old-run",
        "completion_exhausted_incomplete_todo_count": 1,
        "completion_exhausted_malformed_todo_count": 1,
        "completion_exhausted_report_reason": "missing",
        "completion_verified_report_modified_at": "prior",
        "completion_accepted_at_limit_report_modified_at": "prior",
        "verification_round": 2,
        "verification_feedback": "Fix the report",
        "_eval_logged": True,
        "_eval_pending": True,
        "_streamed_files": ["/final_report.md"],
    }

    update = _middleware(run_id=run_id).before_agent(state, runtime=None)

    assert update == {
        "completion_current_run_id": str(run_id),
        "completion_request_generation": str(run_id),
        "completion_plan_owner_generation": None,
        "completion_report_owned": False,
        "completion_resume_adopted_generation": None,
        "completion_attempts": 0,
        "completion_attempt_limit": 2,
        "completion_report_baseline_modified_at": "prior",
        "completion_report_baseline_fingerprint": _fingerprint(
            state["files"]["/final_report.md"]
        ),
        "completion_report_owned_fingerprint": None,
        "completion_verified_report_modified_at": None,
        "completion_verified_report_fingerprint": None,
        "completion_verified_report_run_id": None,
        "completion_accepted_at_limit_report_modified_at": None,
        "completion_accepted_at_limit_report_fingerprint": None,
        "completion_accepted_at_limit_report_run_id": None,
        "completion_cited_baseline_fingerprints": {},
        "completion_exhausted_run_id": None,
        "completion_exhausted_incomplete_todo_count": 0,
        "completion_exhausted_malformed_todo_count": 0,
        "completion_exhausted_report_reason": None,
        "citation_failure_run_id": None,
        "citation_failure_report_fingerprint": None,
        "citation_failure_defects": [],
        "citation_accepted_report_fingerprint": None,
        "citation_corrections_used": 0,
        "todos": [],
        "verification_round": 0,
        "verification_feedback": None,
        "_eval_logged": False,
        "_eval_pending": False,
        "_streamed_files": [],
    }


def test_explicit_resume_preserves_generation_plan_and_report_ownership() -> None:
    resumed_run_id = uuid4()
    state = {
        "messages": [],
        "todos": [{"content": "Finish report", "status": "pending"}],
        "files": {"/final_report.md": _file("Draft", modified_at="draft")},
        "completion_current_run_id": "prior-run",
        "completion_request_generation": "generation-b",
        "completion_plan_owner_generation": "generation-b",
        "completion_report_owned": True,
        "completion_report_baseline_modified_at": "generation-a-report",
        "completion_verified_report_modified_at": "draft",
        "completion_accepted_at_limit_report_modified_at": "draft",
        "completion_attempts": 3,
        "completion_attempt_limit": 1,
        "completion_exhausted_run_id": "prior-run",
        "completion_exhausted_incomplete_todo_count": 1,
        "completion_exhausted_malformed_todo_count": 0,
        "completion_exhausted_report_reason": "stale",
        "verification_round": 2,
        "verification_feedback": "Retained feedback",
        "_eval_logged": True,
        "_eval_pending": True,
        "_streamed_files": ["/final_report.md"],
    }

    resumed = _apply(
        state,
        _middleware(run_id=resumed_run_id, resume=True).before_agent(
            state,
            runtime=None,
        ),
    )

    assert resumed["completion_current_run_id"] == str(resumed_run_id)
    assert resumed["completion_request_generation"] == "generation-b"
    assert resumed["completion_plan_owner_generation"] == "generation-b"
    assert resumed["completion_report_owned"] is True
    assert resumed["completion_report_baseline_modified_at"] == "generation-a-report"
    assert resumed["completion_verified_report_modified_at"] == "draft"
    assert resumed["completion_accepted_at_limit_report_modified_at"] == "draft"
    assert resumed["completion_resume_adopted_generation"] == "generation-b"
    assert resumed["completion_attempts"] == 0
    assert resumed["completion_exhausted_run_id"] is None
    assert resumed["todos"] == state["todos"]
    assert resumed["verification_round"] == 2
    assert resumed["verification_feedback"] == "Retained feedback"
    assert resumed["_eval_logged"] is True
    assert resumed["_eval_pending"] is True
    assert resumed["_streamed_files"] == ["/final_report.md"]


def test_identical_user_text_in_distinct_runs_creates_distinct_generations() -> None:
    first_run_id = uuid4()
    second_run_id = uuid4()
    state = {"messages": [{"role": "user", "content": "Same request"}], "files": {}}

    first = _apply(
        state,
        _middleware(run_id=first_run_id).before_agent(state, runtime=None),
    )
    second = _apply(
        first,
        _middleware(run_id=second_run_id).before_agent(first, runtime=None),
    )

    assert first["completion_request_generation"] == str(first_run_id)
    assert second["completion_request_generation"] == str(second_run_id)
    assert (
        first["completion_request_generation"]
        != second["completion_request_generation"]
    )


def test_identical_text_ordinary_generation_clears_all_prior_acceptance_state() -> None:
    state = {
        "messages": [{"role": "user", "content": "Same request"}],
        "files": {},
        "completion_verified_report_modified_at": "verified",
        "completion_accepted_at_limit_report_modified_at": "accepted",
        "verification_round": 2,
        "verification_feedback": "Prior feedback",
        "_eval_logged": True,
        "_streamed_files": ["/final_report.md"],
    }

    update = _middleware(run_id=uuid4()).before_agent(state, runtime=None)

    assert update is not None
    assert update["completion_verified_report_modified_at"] is None
    assert update["completion_accepted_at_limit_report_modified_at"] is None
    assert update["verification_round"] == 0
    assert update["verification_feedback"] is None
    assert update["_eval_logged"] is False
    assert update["_streamed_files"] == []


def test_resume_of_generation_b_never_adopts_generation_a_report() -> None:
    run_b = uuid4()
    resume_b = uuid4()
    generation_a = {
        "messages": [],
        "todos": [{"content": "A", "status": "completed"}],
        "files": {"/final_report.md": _file("Report A", modified_at="report-a")},
        "completion_request_generation": "generation-a",
        "completion_plan_owner_generation": "generation-a",
        "completion_report_owned": True,
    }
    ordinary_b = _apply(
        generation_a,
        _middleware(run_id=run_b).before_agent(generation_a, runtime=None),
    )
    ordinary_b.update(
        {
            "todos": [{"content": "B", "status": "pending"}],
            "completion_plan_owner_generation": ordinary_b[
                "completion_request_generation"
            ],
        }
    )

    resumed_b = _apply(
        ordinary_b,
        _middleware(run_id=resume_b, resume=True).before_agent(
            ordinary_b,
            runtime=None,
        ),
    )

    assert resumed_b["completion_current_run_id"] == str(resume_b)
    assert resumed_b["completion_request_generation"] == str(run_b)
    assert resumed_b["completion_plan_owner_generation"] == str(run_b)
    assert resumed_b["completion_resume_adopted_generation"] == str(run_b)
    assert resumed_b["completion_report_owned"] is False
    assert resumed_b["completion_report_baseline_modified_at"] == "report-a"


def test_explicit_resume_clears_stale_exhaustion_failure_metadata() -> None:
    state = {
        "messages": [],
        "files": {},
        "completion_current_run_id": "exhausted-run",
        "completion_request_generation": "generation-b",
        "completion_plan_owner_generation": "generation-b",
        "completion_report_owned": False,
        "completion_attempts": 3,
        "completion_exhausted_run_id": "exhausted-run",
        "completion_exhausted_incomplete_todo_count": 2,
        "completion_exhausted_malformed_todo_count": 1,
        "completion_exhausted_report_reason": "missing",
    }

    resumed = _apply(
        state,
        _middleware(run_id=uuid4(), resume=True).before_agent(state, runtime=None),
    )

    assert resumed["completion_attempts"] == 0
    assert resumed["completion_exhausted_run_id"] is None
    assert resumed["completion_exhausted_incomplete_todo_count"] == 0
    assert resumed["completion_exhausted_malformed_todo_count"] == 0
    assert resumed["completion_exhausted_report_reason"] is None


def test_attempt_limit_is_resolved_once_per_visible_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    middleware = _middleware(run_id=run_id)
    monkeypatch.setenv("MAX_COMPLETION_ATTEMPTS", "1")
    state = {"messages": [], "files": {}}
    started = _apply(state, middleware.before_agent(state, runtime=None))

    monkeypatch.setenv("MAX_COMPLETION_ATTEMPTS", "3")
    repeated_update = middleware.before_agent(started, runtime=None)

    assert started["completion_attempt_limit"] == 1
    assert repeated_update is None
    assert started["completion_attempt_limit"] == 1


def test_direct_before_agent_call_without_run_id_uses_uuid_generation() -> None:
    update = _middleware(run_id=None).before_agent(
        {"messages": [], "files": {}},
        runtime=None,
    )

    assert update is not None
    assert UUID(update["completion_current_run_id"])
    assert (
        update["completion_request_generation"]
        == update["completion_current_run_id"]
    )


def _tool_exchange(
    *,
    name: str,
    args: dict[str, Any],
    call_id: str = "call-1",
    result_id: str = "call-1",
    status: str | None = "success",
) -> list[AIMessage | ToolMessage]:
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )
    if status in {"success", "error"}:
        result = ToolMessage(
            content="done",
            tool_call_id=result_id,
            status=status,
        )
    else:
        result = ToolMessage.model_construct(
            content="done",
            tool_call_id=result_id,
            status=status,
            type="tool",
        )
    return [call, result]


def _activation_state(
    messages: list[Any],
    *,
    todos: object | None = None,
    files: object | None = None,
) -> dict[str, Any]:
    return {
        "messages": messages,
        "todos": [] if todos is None else todos,
        "files": {} if files is None else files,
        "completion_current_run_id": "run-b",
        "completion_request_generation": "generation-b",
        "completion_plan_owner_generation": None,
        "completion_report_owned": False,
        "completion_report_baseline_modified_at": "baseline",
    }


def test_write_todos_activates_plan_after_exact_matching_success() -> None:
    middleware = _middleware(run_id="run-b")
    state = _activation_state(
        _tool_exchange(name="write_todos", args={"todos": []}),
        todos=[{"content": "Research", "status": "pending"}],
    )

    sync_update = middleware.before_model(state, runtime=None)
    async_update = asyncio.run(middleware.abefore_model(state, runtime=None))

    expected = {"completion_plan_owner_generation": "generation-b"}
    assert sync_update == expected
    assert async_update == expected


@pytest.mark.parametrize(
    ("messages", "todos"),
    [
        (
            _tool_exchange(
                name="write_todos",
                args={"todos": []},
                status="error",
            ),
            [{"content": "Research", "status": "pending"}],
        ),
        (
            _tool_exchange(
                name="write_todos",
                args={"todos": []},
                result_id="different-call",
            ),
            [{"content": "Research", "status": "pending"}],
        ),
        (
            _tool_exchange(
                name="write_todos",
                args={"todos": []},
                status=None,
            ),
            [{"content": "Research", "status": "pending"}],
        ),
        (_tool_exchange(name="write_todos", args={"todos": []}), []),
        (
            _tool_exchange(name="write_todos", args={"todos": []}),
            [{"content": "", "status": "pending"}],
        ),
    ],
)
def test_write_todos_rejects_failed_mismatched_malformed_or_empty_results(
    messages: list[Any],
    todos: object,
) -> None:
    state = _activation_state(messages, todos=todos)

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


def test_write_todos_rejects_malformed_tool_call_id() -> None:
    call = AIMessage.model_construct(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": []},
                "id": None,
                "type": "tool_call",
            }
        ],
        type="ai",
    )
    result = ToolMessage(content="done", tool_call_id="call-1", status="success")
    state = _activation_state(
        [call, result],
        todos=[{"content": "Research", "status": "pending"}],
    )

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


@pytest.mark.parametrize("args", [{}, {"todos": "not-a-list"}])
def test_write_todos_rejects_malformed_tool_arguments(args: dict[str, Any]) -> None:
    state = _activation_state(
        _tool_exchange(name="write_todos", args=args),
        todos=[{"content": "Research", "status": "pending"}],
    )

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


@pytest.mark.parametrize(
    "args",
    [
        {"content": "Report"},
        {"file_path": "/final_report.md", "content": "Report"},
    ],
)
def test_write_file_owns_changed_nonempty_final_report_after_matching_success(
    args: dict[str, Any],
) -> None:
    middleware = _middleware(run_id="run-b")
    state = _activation_state(
        _tool_exchange(name="write_file", args=args),
        files={"/final_report.md": _file("Report", modified_at="current")},
    )

    sync_update = middleware.before_model(state, runtime=None)
    async_update = asyncio.run(middleware.abefore_model(state, runtime=None))

    expected = {
        "completion_report_owned": True,
        "completion_report_owned_fingerprint": _fingerprint(
            state["files"]["/final_report.md"]
        ),
    }
    assert sync_update == expected
    assert async_update == expected


def test_write_file_owns_same_timestamp_report_when_content_fingerprint_changes() -> None:
    baseline = _file("Prior report", modified_at="same-timestamp")
    current = _file("Current report", modified_at="same-timestamp")
    state = _activation_state(
        _tool_exchange(name="write_file", args={"content": "Current report"}),
        files={"/final_report.md": current},
    )
    state["completion_report_baseline_modified_at"] = "same-timestamp"
    state["completion_report_baseline_fingerprint"] = _fingerprint(baseline)

    update = _middleware(run_id="run-b").before_model(state, runtime=None)

    assert update == {
        "completion_report_owned": True,
        "completion_report_owned_fingerprint": _fingerprint(current),
    }


@pytest.mark.parametrize(
    ("messages", "files"),
    [
        (
            _tool_exchange(
                name="write_file",
                args={"content": "Report"},
                status="error",
            ),
            {"/final_report.md": _file("Report", modified_at="current")},
        ),
        (
            _tool_exchange(
                name="write_file",
                args={"content": "Report"},
                result_id="different-call",
            ),
            {"/final_report.md": _file("Report", modified_at="current")},
        ),
        (
            _tool_exchange(
                name="write_file",
                args={"content": "Report"},
                status=None,
            ),
            {"/final_report.md": _file("Report", modified_at="current")},
        ),
        (
            _tool_exchange(
                name="write_file",
                args={"file_path": 3, "content": "Report"},
            ),
            {"/final_report.md": _file("Report", modified_at="current")},
        ),
        (
            _tool_exchange(name="write_file", args={"content": "Report"}),
            {"/final_report.md": _file("Report", modified_at="baseline")},
        ),
        (
            _tool_exchange(name="write_file", args={"content": "Report"}),
            {"/final_report.md": "malformed"},
        ),
        (
            _tool_exchange(name="write_file", args={"content": "Report"}),
            {"/final_report.md": _file("   ", modified_at="current")},
        ),
        (
            _tool_exchange(
                name="write_file",
                args={"file_path": "/notes.md", "content": "Report"},
            ),
            {
                "/notes.md": _file("Report", modified_at="current"),
                "/final_report.md": _file("Old report", modified_at="current"),
            },
        ),
    ],
)
def test_write_file_rejects_failed_mismatched_malformed_stale_or_nonfinal_results(
    messages: list[Any],
    files: object,
) -> None:
    state = _activation_state(messages, files=files)

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


@pytest.mark.parametrize("args", [{}, {"content": "   "}])
def test_write_file_rejects_malformed_arguments_instead_of_assuming_default_path(
    args: dict[str, Any],
) -> None:
    state = _activation_state(
        _tool_exchange(name="write_file", args=args),
        files={"/final_report.md": _file("Report", modified_at="current")},
    )

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


def test_tool_correlation_rejects_duplicate_ids_shared_by_different_tools() -> None:
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"content": "Report"},
                "id": "duplicate",
                "type": "tool_call",
            },
            {
                "name": "unrelated",
                "args": {"value": 1},
                "id": "duplicate",
                "type": "tool_call",
            },
        ],
    )
    result = ToolMessage(
        content="done",
        tool_call_id="duplicate",
        status="success",
    )
    state = _activation_state(
        [call, result],
        files={"/final_report.md": _file("Report", modified_at="current")},
    )

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


def test_tool_correlation_rejects_duplicate_result_ids() -> None:
    messages = _tool_exchange(
        name="write_file",
        args={"content": "Report"},
    )
    messages.append(
        ToolMessage(
            content="duplicate",
            tool_call_id="call-1",
            status="success",
        )
    )
    state = _activation_state(
        messages,
        files={"/final_report.md": _file("Report", modified_at="current")},
    )

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


def test_tool_correlation_rejects_unmatched_result_ids() -> None:
    messages = _tool_exchange(
        name="write_file",
        args={"content": "Report"},
    )
    messages.append(
        ToolMessage(
            content="unmatched",
            tool_call_id="other-call",
            status="success",
        )
    )
    state = _activation_state(
        messages,
        files={"/final_report.md": _file("Report", modified_at="current")},
    )

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


def test_tool_correlation_rejects_call_without_a_result() -> None:
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"content": "Report"},
                "id": "call-1",
                "type": "tool_call",
            },
            {
                "name": "unrelated",
                "args": {"value": 1},
                "id": "call-2",
                "type": "tool_call",
            },
        ],
    )
    result = ToolMessage(content="done", tool_call_id="call-1", status="success")
    state = _activation_state(
        [call, result],
        files={"/final_report.md": _file("Report", modified_at="current")},
    )

    assert _middleware(run_id="run-b").before_model(state, runtime=None) is None


def _incomplete_state(
    message: AIMessage,
    *,
    attempts: int = 0,
    limit: int = 3,
    run_id: str = "run-b",
) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="Research privately"), message],
        "todos": [
            {"content": "Private research detail", "status": "in_progress"},
            {"content": "Write report", "status": "pending"},
        ],
        "files": {},
        "completion_current_run_id": run_id,
        "completion_request_generation": "generation-b",
        "completion_plan_owner_generation": "generation-b",
        "completion_report_owned": False,
        "completion_report_baseline_modified_at": None,
        "completion_attempts": attempts,
        "completion_attempt_limit": limit,
        "completion_exhausted_run_id": None,
    }


def _terminal_message() -> AIMessage:
    return AIMessage(
        content=[{"type": "text", "text": "Partial answer"}],
        id="terminal-message",
        name="researcher",
        additional_kwargs={"provider": "ollama"},
        response_metadata={"model": "gemma4", "finish_reason": "stop"},
        usage_metadata={
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
        },
    )


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_incomplete_terminal_response_consumes_exact_retry_budget(
    limit: int,
) -> None:
    middleware = _middleware(run_id="run-b")
    state = _incomplete_state(_terminal_message(), limit=limit)

    for expected_attempt in range(1, limit + 1):
        update = middleware.after_model(state, runtime=None)

        assert update is not None
        assert update["jump_to"] == "model"
        assert update["completion_attempts"] == expected_attempt
        state = _apply(state, update)

    exhausted = middleware.after_model(state, runtime=None)

    assert exhausted is not None
    assert exhausted["jump_to"] == "end"
    assert exhausted["completion_attempts"] == limit
    assert exhausted["completion_exhausted_run_id"] == "run-b"


def test_continuation_tags_replacement_preserves_terminal_message_metadata() -> None:
    message = _terminal_message()
    state = _incomplete_state(message)

    update = _middleware(run_id="run-b").after_model(state, runtime=None)

    assert update is not None
    assert update["jump_to"] == "model"
    assert update["completion_attempts"] == 1
    assert len(update["messages"]) == 1
    tagged = update["messages"][0]
    assert isinstance(tagged, AIMessage)
    assert tagged is not message
    assert tagged.id == message.id
    assert tagged.content == message.content
    assert tagged.content_blocks == message.content_blocks
    assert tagged.name == message.name
    assert tagged.additional_kwargs == message.additional_kwargs
    assert tagged.usage_metadata == message.usage_metadata
    assert tagged.response_metadata == {
        **message.response_metadata,
        "resume_intermediate": True,
    }
    assert update.keys() == {"messages", "completion_attempts", "jump_to"}


def test_async_after_model_matches_sync_continuation_update() -> None:
    state = _incomplete_state(_terminal_message())
    middleware = _middleware(run_id="run-b")

    sync_update = middleware.after_model(state, runtime=None)
    async_update = asyncio.run(middleware.aafter_model(state, runtime=None))

    assert async_update == sync_update
    assert getattr(middleware.after_model, "__can_jump_to__") == [
        "model",
        "end",
    ]
    assert getattr(middleware.aafter_model, "__can_jump_to__") == [
        "model",
        "end",
    ]


def test_after_model_ignores_nonterminal_tool_call_response() -> None:
    tool_call = AIMessage(
        content="Calling a tool",
        id="tool-call",
        tool_calls=[
            {
                "name": "think_tool",
                "args": {"reflection": "continue"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )
    state = _incomplete_state(tool_call)

    assert _middleware(run_id="run-b").after_model(state, runtime=None) is None


def test_after_model_is_inactive_without_current_plan_ownership() -> None:
    message = _terminal_message()
    state = _incomplete_state(message)
    state["completion_plan_owner_generation"] = None

    update = _middleware(run_id="run-b").after_model(state, runtime=None)

    assert update is None
    assert message.response_metadata == {
        "model": "gemma4",
        "finish_reason": "stop",
    }


def test_wrap_model_call_is_inactive_without_current_plan_ownership() -> None:
    state = _incomplete_state(_terminal_message(), attempts=1)
    state["completion_plan_owner_generation"] = None
    request = _model_request(state)
    captured: list[ModelRequest] = []

    _middleware(run_id="run-b").wrap_model_call(
        request,
        lambda configured: captured.append(configured) or "response",
    )

    assert captured == [request]
    assert captured[0].system_message == request.system_message


def _model_request(state: dict[str, Any]) -> ModelRequest:
    return ModelRequest(
        model=FakeListChatModel(responses=["done"]),
        messages=[HumanMessage(content="Continue")],
        system_message=SystemMessage(
            content="Existing system guidance",
            id="system-message",
            additional_kwargs={"provider": "ollama"},
        ),
        state=state,
    )


def test_sync_wrap_model_call_appends_ephemeral_leading_system_guidance() -> None:
    state = _incomplete_state(_terminal_message(), attempts=1)
    state["files"] = {
        "/final_report.md": _file(
            "Sensitive report prose",
            modified_at="current",
        )
    }
    request = _model_request(state)
    captured: list[ModelRequest] = []

    result = _middleware(run_id="run-b").wrap_model_call(
        request,
        lambda configured: captured.append(configured) or "response",
    )

    assert result == "response"
    assert len(captured) == 1
    configured = captured[0]
    assert configured.messages == request.messages
    assert configured.system_message is not None
    assert configured.system_message.id == "system-message"
    assert configured.system_message.additional_kwargs == {"provider": "ollama"}
    assert configured.system_message.content.startswith("Existing system guidance")
    guidance = str(configured.system_message.content)
    assert "<CompletionGuard>" in guidance
    assert "Continuation attempt 1 of 3" in guidance
    assert (
        "incomplete_todos=2, malformed_todos=0, report=stale" in guidance
    )
    assert "Private research detail" not in guidance
    assert "Sensitive report prose" not in guidance
    assert "Research privately" not in guidance
    assert "Partial answer" not in guidance
    assert request.system_message is not None
    assert "<CompletionGuard>" not in str(request.system_message.content)
    assert state["messages"][-1].content == _terminal_message().content


def test_async_wrap_model_call_matches_sync_ollama_message_ordering() -> None:
    state = _incomplete_state(_terminal_message(), attempts=1)
    request = _model_request(state)
    captured: list[ModelRequest] = []

    async def handler(configured: ModelRequest) -> str:
        captured.append(configured)
        return "response"

    result = asyncio.run(
        _middleware(run_id="run-b").awrap_model_call(request, handler)
    )

    assert result == "response"
    assert captured[0].messages == request.messages
    assert all(
        not isinstance(message, SystemMessage)
        for message in captured[0].messages
    )
    assert captured[0].system_message is not None
    assert str(captured[0].system_message.content).startswith(
        "Existing system guidance"
    )
    guidance = str(captured[0].system_message.content)
    assert "<CompletionGuard>" in guidance
    assert "Continuation attempt 1 of 3" in guidance
    assert (
        "incomplete_todos=2, malformed_todos=0, report=missing" in guidance
    )
    assert "Private research detail" not in guidance
    assert "Research privately" not in guidance
    assert "Partial answer" not in guidance
    assert request.messages == [HumanMessage(content="Continue")]
    assert state["messages"][-1].content == _terminal_message().content


def test_exhaustion_checkpoints_only_safe_summary_and_jumps_to_end() -> None:
    message = _terminal_message()
    state = _incomplete_state(message, attempts=2, limit=2)

    update = _middleware(run_id="run-b").after_model(state, runtime=None)

    assert update is not None
    assert update["jump_to"] == "end"
    assert update["completion_attempts"] == 2
    assert update["completion_exhausted_run_id"] == "run-b"
    assert update["completion_exhausted_incomplete_todo_count"] == 2
    assert update["completion_exhausted_malformed_todo_count"] == 0
    assert update["completion_exhausted_report_reason"] == "missing"
    tagged = update["messages"][0]
    assert tagged.id == message.id
    assert tagged.response_metadata["resume_intermediate"] is True
    assert "Private research detail" not in repr(
        {
            key: value
            for key, value in update.items()
            if key not in {"messages"}
        }
    )


def test_after_agent_raises_only_for_matching_current_exhausted_run() -> None:
    middleware = _middleware(run_id="run-b")
    matching = _incomplete_state(_terminal_message(), attempts=2, limit=2)
    matching.update(
        {
            "completion_exhausted_run_id": "run-b",
            "completion_exhausted_incomplete_todo_count": 2,
            "completion_exhausted_malformed_todo_count": 0,
            "completion_exhausted_report_reason": "missing",
        }
    )

    with pytest.raises(completion_guard.ResearchIncompleteError) as caught:
        middleware.after_agent(matching, runtime=None)

    assert "incomplete_todos=2" in str(caught.value)
    assert "malformed_todos=0" in str(caught.value)
    assert "report=missing" in str(caught.value)

    stale = {**matching, "completion_exhausted_run_id": "prior-run"}
    assert middleware.after_agent(stale, runtime=None) is None

    other_current = {**matching, "completion_current_run_id": "other-run"}
    assert middleware.after_agent(other_current, runtime=None) is None

    other_config_run = _middleware(run_id="run-c")
    assert other_config_run.after_agent(matching, runtime=None) is None


def test_after_agent_uses_before_agent_fallback_when_config_has_no_run_id() -> None:
    middleware = _middleware(run_id=None)
    started = _apply(
        {"messages": [], "files": {}},
        middleware.before_agent(
            {"messages": [], "files": {}},
            runtime=None,
        ),
    )
    fallback_run_id = started["completion_current_run_id"]
    started.update(
        {
            "completion_plan_owner_generation": started[
                "completion_request_generation"
            ],
            "todos": [{"content": "Finish report", "status": "pending"}],
            "completion_exhausted_run_id": fallback_run_id,
            "completion_exhausted_incomplete_todo_count": 1,
            "completion_exhausted_malformed_todo_count": 0,
            "completion_exhausted_report_reason": "missing",
        }
    )

    with pytest.raises(completion_guard.ResearchIncompleteError):
        middleware.after_agent(started, runtime=None)


def test_after_agent_is_inactive_without_current_plan_ownership() -> None:
    state = _incomplete_state(_terminal_message(), attempts=3, limit=3)
    state.update(
        {
            "completion_plan_owner_generation": None,
            "completion_exhausted_run_id": "run-b",
            "completion_exhausted_incomplete_todo_count": 2,
            "completion_exhausted_malformed_todo_count": 0,
            "completion_exhausted_report_reason": "missing",
        }
    )

    assert _middleware(run_id="run-b").after_agent(state, runtime=None) is None


def test_runtime_run_id_wins_conflicting_top_level_config_at_start() -> None:
    middleware = _middleware(run_id="stale-config-run")

    update = middleware.before_agent(
        {"messages": [], "files": {}},
        runtime=_runtime("actual-runtime-run"),
    )

    assert update is not None
    assert update["completion_current_run_id"] == "actual-runtime-run"
    assert update["completion_request_generation"] == "actual-runtime-run"


def test_runtime_run_id_prevents_stale_exhaustion_match_on_new_run() -> None:
    state = _incomplete_state(_terminal_message(), attempts=2, limit=2)
    state.update(
        {
            "completion_current_run_id": "old-run",
            "completion_exhausted_run_id": "old-run",
            "completion_exhausted_incomplete_todo_count": 1,
            "completion_exhausted_malformed_todo_count": 0,
            "completion_exhausted_report_reason": "missing",
        }
    )

    assert (
        _middleware(run_id="old-run").after_agent(
            state,
            runtime=_runtime("new-actual-run"),
        )
        is None
    )


def test_runtime_run_id_allows_matching_exhaustion_despite_stale_config() -> None:
    state = _incomplete_state(_terminal_message(), attempts=2, limit=2)
    state.update(
        {
            "completion_current_run_id": "actual-runtime-run",
            "completion_exhausted_run_id": "actual-runtime-run",
            "completion_exhausted_incomplete_todo_count": 1,
            "completion_exhausted_malformed_todo_count": 0,
            "completion_exhausted_report_reason": "missing",
        }
    )

    with pytest.raises(completion_guard.ResearchIncompleteError):
        _middleware(run_id="stale-config-run").after_agent(
            state,
            runtime=_runtime("actual-runtime-run"),
        )


def test_research_incomplete_error_serializes_safe_summary_only() -> None:
    error = completion_guard.ResearchIncompleteError(
        incomplete_todo_count=2,
        malformed_todo_count=1,
        report_reason="missing",
        attempt_limit=3,
    )

    serialized = serialize_default(error)

    assert serialized == {
        "error": "ResearchIncompleteError",
        "message": (
            "Research incomplete after automatic continuation limit "
            "(attempt_limit=3, incomplete_todos=2, malformed_todos=1, "
            "report=missing)."
        ),
    }
    assert "Private research detail" not in repr(serialized)
    assert "Partial answer" not in repr(serialized)


class _OwnedPlanCompletionGuard(completion_guard.CompletionGuardMiddleware):
    """Seed an owned plan so compiled tests can focus on guard routing."""

    def before_agent(
        self,
        state: completion_guard.CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        update = super().before_agent(state, runtime)
        assert update is not None
        generation = update["completion_request_generation"]
        return {
            **update,
            "completion_plan_owner_generation": generation,
            "todos": [{"content": "Private task", "status": "pending"}],
        }


class _OwnedCitationReportGuard(completion_guard.CompletionGuardMiddleware):
    """Seed invalid first-run report, then corrected next-run report."""

    def __init__(self) -> None:
        super().__init__()
        self.visible_runs = 0

    def before_agent(
        self,
        state: completion_guard.CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        update = super().before_agent(state, runtime)
        assert update is not None
        self.visible_runs += 1
        content = (
            "Invalid report without citations"
            if self.visible_runs == 1
            else "Corrected report https://public.publisher.org/report"
        )
        report = create_file_data(content)
        generation = update["completion_request_generation"]
        fingerprint = _fingerprint(report)
        return {
            **update,
            "files": {"/final_report.md": report},
            "todos": [{"content": "Research", "status": "completed"}],
            "completion_plan_owner_generation": generation,
            "completion_report_owned": True,
            "completion_report_owned_fingerprint": fingerprint,
            "completion_verified_report_fingerprint": fingerprint,
            "completion_verified_report_run_id": generation,
            "completion_accepted_at_limit_report_fingerprint": fingerprint,
            "completion_accepted_at_limit_report_run_id": generation,
            "citation_accepted_report_fingerprint": "legacy-citation-acceptance",
            "strict_web_citations": True,
            "effective_no_web": False,
        }


class _CitationPersistenceError(RuntimeError):
    """Sentinel saver error used to prove citation failures cannot mask it."""


class _CitationProbeSaver(InMemorySaver):
    """Observe exact durable reads and fail selected persistence operations."""

    def __init__(self, fail_operation: str | None = None) -> None:
        super().__init__()
        self.fail_operation = fail_operation
        self.confirmed_failure_reads = 0
        self.exact_read_observations: list[tuple[object, object, tuple[str, ...]]] = []

    @staticmethod
    def _failure_values(checkpoint: Any) -> bool:
        if not isinstance(checkpoint, dict):
            return False
        values = checkpoint.get("channel_values")
        return isinstance(values, dict) and bool(
            values.get("citation_failure_run_id")
        )

    def get_tuple(self, config):  # noqa: ANN001
        result = super().get_tuple(config)
        configurable = config.get("configurable", {})
        is_exact_read = bool(configurable.get("checkpoint_id"))
        if is_exact_read:
            values = (
                result.checkpoint.get("channel_values", {})
                if result is not None
                else {}
            )
            self.exact_read_observations.append(
                (
                    configurable.get("checkpoint_ns"),
                    configurable.get("checkpoint_id"),
                    tuple(sorted(values)),
                )
            )
        if (
            is_exact_read
            and result is not None
            and self._failure_values(result.checkpoint)
        ):
            self.confirmed_failure_reads += 1
            if self.fail_operation == "get":
                raise _CitationPersistenceError("checkpoint get failed")
        return result

    def put(self, config, checkpoint, metadata, new_versions):  # noqa: ANN001
        if self.fail_operation == "put" and self._failure_values(checkpoint):
            raise _CitationPersistenceError("checkpoint put failed")
        return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):  # noqa: ANN001
        if self.fail_operation == "put_writes" and any(
            channel == "citation_failure_run_id" and value
            for channel, value in writes
        ):
            raise _CitationPersistenceError("checkpoint put_writes failed")
        return super().put_writes(config, writes, task_id, task_path)


class _DelayedCitationSaver(_CitationProbeSaver):
    """Delay only terminal failure checkpoint persistence."""

    def __init__(self, delay_seconds: float) -> None:
        super().__init__()
        self.delay_seconds = delay_seconds

    def put(self, config, checkpoint, metadata, new_versions):  # noqa: ANN001
        if self._failure_values(checkpoint):
            time.sleep(self.delay_seconds)
        return InMemorySaver.put(self, config, checkpoint, metadata, new_versions)

    async def aput(self, config, checkpoint, metadata, new_versions):  # noqa: ANN001
        if self._failure_values(checkpoint):
            await asyncio.sleep(self.delay_seconds)
        return InMemorySaver.put(self, config, checkpoint, metadata, new_versions)


class _MissingCitationConfirmationSaver(_CitationProbeSaver):
    """Persist terminal state but hide its exact checkpoint from confirmation."""

    def get_tuple(self, config):  # noqa: ANN001
        result = super().get_tuple(config)
        configurable = config.get("configurable", {})
        if (
            configurable.get("checkpoint_id")
            and result is not None
            and self._failure_values(result.checkpoint)
        ):
            return None
        return result


class _StalledCitationConfirmationSaver(_CitationProbeSaver):
    """Make the exact terminal checkpoint read exceed its deadline."""

    def __init__(self, delay_seconds: float) -> None:
        super().__init__()
        self.delay_seconds = delay_seconds

    def get_tuple(self, config):  # noqa: ANN001
        result = InMemorySaver.get_tuple(self, config)
        configurable = config.get("configurable", {})
        if (
            configurable.get("checkpoint_id")
            and result is not None
            and self._failure_values(result.checkpoint)
        ):
            time.sleep(self.delay_seconds)
        return result

    async def aget_tuple(self, config):  # noqa: ANN001
        result = InMemorySaver.get_tuple(self, config)
        configurable = config.get("configurable", {})
        if (
            configurable.get("checkpoint_id")
            and result is not None
            and self._failure_values(result.checkpoint)
        ):
            await asyncio.sleep(self.delay_seconds)
        return result


def _compiled_graph(
    *,
    responses: list[str],
    middleware: completion_guard.CompletionGuardMiddleware,
) -> Any:
    return create_agent(
        FakeListChatModel(responses=responses),
        tools=[],
        middleware=[middleware],
        checkpointer=InMemorySaver(),
    )


def _invoke_compiled(graph: Any, config: dict[str, Any], *, async_: bool) -> Any:
    input_state = {"messages": [HumanMessage(content="Research privately")]}
    if async_:
        return asyncio.run(graph.ainvoke(input_state, config=config))
    return graph.invoke(input_state, config=config)


def _stream_compiled_values(
    graph: Any,
    config: dict[str, Any],
    *,
    async_: bool,
) -> tuple[list[dict[str, Any]], BaseException | None]:
    input_state = {"messages": [HumanMessage(content="Research privately")]}
    states: list[dict[str, Any]] = []
    caught: BaseException | None = None

    if async_:
        async def consume() -> None:
            nonlocal caught
            try:
                async for state in graph.astream(
                    input_state,
                    config=config,
                    stream_mode="values",
                ):
                    states.append(state)
            except BaseException as error:
                caught = error

        asyncio.run(consume())
    else:
        try:
            states.extend(
                graph.stream(
                    input_state,
                    config=config,
                    stream_mode="values",
                )
            )
        except BaseException as error:
            caught = error
    return states, caught


@pytest.mark.parametrize("async_", [False, True])
def test_compiled_inactive_plan_terminal_passes_through_untouched(
    async_: bool,
) -> None:
    run_id = uuid4()
    graph = _compiled_graph(
        responses=["Clarification response"],
        middleware=_middleware(run_id=run_id),
    )
    config = {
        "run_id": run_id,
        "configurable": {"thread_id": f"inactive-{async_}"},
    }

    result = _invoke_compiled(graph, config, async_=async_)

    assert len(result["messages"]) == 2
    terminal = result["messages"][-1]
    assert terminal.content == "Clarification response"
    assert terminal.response_metadata.get("resume_intermediate") is None
    assert result["completion_attempts"] == 0
    assert result["completion_exhausted_run_id"] is None


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("limit", [1, 2, 3])
def test_compiled_owned_plan_replaces_then_appends_and_checkpoints_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    limit: int,
) -> None:
    monkeypatch.setenv("MAX_COMPLETION_ATTEMPTS", str(limit))
    run_id = uuid4()
    partial_responses = [
        f"Partial {attempt}" for attempt in range(1, limit + 2)
    ]
    graph = _compiled_graph(
        responses=partial_responses,
        middleware=_OwnedPlanCompletionGuard(
            config_getter=lambda: {"run_id": run_id, "configurable": {}}
        ),
    )
    config = {
        "run_id": run_id,
        "configurable": {
            "thread_id": f"exhausted-{limit}-{async_}",
        },
    }

    with pytest.raises(completion_guard.ResearchIncompleteError) as caught:
        _invoke_compiled(graph, config, async_=async_)

    assert f"attempt_limit={limit}" in str(caught.value)
    snapshot = graph.get_state(config)
    values = snapshot.values
    assert values["completion_attempts"] == limit
    assert values["completion_exhausted_run_id"] == str(run_id)
    messages = values["messages"]
    assert [message.content for message in messages] == [
        "Research privately",
        *partial_responses,
    ]
    assert len({message.id for message in messages[1:]}) == limit + 1
    assert all(
        message.response_metadata.get("resume_intermediate") is True
        for message in messages[1:]
    )


@pytest.mark.parametrize("async_", [False, True])
def test_compiled_strict_citation_failure_checkpoints_before_raise_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    import research_agent.agent as agent_module

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 0)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    guard = _OwnedCitationReportGuard()
    saver = _CitationProbeSaver()
    graph = create_agent(
        FakeListChatModel(
            responses=["First invalid", "Still invalid", "Corrected terminal"]
        ),
        tools=[],
        middleware=[guard, agent_module.ResearchStateMiddleware()],
        checkpointer=saver,
    )
    thread_id = f"citation-failure-{async_}"
    first_run = uuid4()
    first_config = {
        "run_id": first_run,
        "configurable": {"thread_id": thread_id},
    }

    with pytest.raises(citation_failure.ReportCitationError) as caught:
        _invoke_compiled(graph, first_config, async_=async_)

    assert "Invalid report" not in str(caught.value)
    assert saver.confirmed_failure_reads >= 1, saver.exact_read_observations
    snapshot = graph.get_state(first_config).values
    failed_report = snapshot["files"]["/final_report.md"]
    failed_fingerprint = _fingerprint(failed_report)
    assert snapshot["citation_failure_run_id"] == str(first_run)
    assert snapshot["citation_failure_report_fingerprint"] == failed_fingerprint
    assert snapshot["citation_failure_defects"] == [
        {"code": "missing_url", "detail": "web"}
    ]
    assert snapshot["citation_corrections_used"] == 1
    assert snapshot["citation_accepted_report_fingerprint"] is None
    assert snapshot["completion_verified_report_fingerprint"] is None
    assert snapshot["completion_verified_report_run_id"] is None
    assert snapshot["completion_accepted_at_limit_report_fingerprint"] is None
    assert snapshot["completion_accepted_at_limit_report_run_id"] is None
    assert snapshot["_streamed_files"] == []
    assert snapshot["_eval_logged"] is False
    assert all(
        "**Final Report:**" not in str(message.content)
        for message in snapshot["messages"]
    )
    assert snapshot["messages"][-1].response_metadata["resume_intermediate"] is True

    second_run = uuid4()
    second_config = {
        "run_id": second_run,
        "configurable": {"thread_id": thread_id},
    }
    corrected = _invoke_compiled(graph, second_config, async_=async_)
    corrected_report = corrected["files"]["/final_report.md"]
    corrected_fingerprint = _fingerprint(corrected_report)

    assert corrected["citation_failure_run_id"] is None
    assert corrected["citation_failure_report_fingerprint"] is None
    assert corrected["citation_failure_defects"] == []
    assert corrected["citation_accepted_report_fingerprint"] == corrected_fingerprint
    assert corrected["citation_corrections_used"] == 0
    assert any(
        "**Final Report:**\n\nCorrected report" in str(message.content)
        for message in corrected["messages"]
    )


@pytest.mark.parametrize("async_", [False, True])
def test_compiled_web_stream_never_renders_pre_acceptance_model_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    async_: bool,
) -> None:
    import research_agent.agent as agent_module
    from research_agent import cli as cli_module

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 0)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    secret = "SECRET MODEL EVENT BEFORE CITATION AFTER_MODEL"
    graph = create_agent(
        FakeListChatModel(
            responses=[secret, f"{secret} AGAIN", "Accepted terminal"]
        ),
        tools=[],
        middleware=[
            _OwnedCitationReportGuard(),
            agent_module.ResearchStateMiddleware(),
        ],
        checkpointer=_CitationProbeSaver(),
    )
    rendered: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "format_messages",
        lambda messages: rendered.extend(
            cli_module.extract_message_content(message) for message in messages
        ),
    )
    thread_id = f"citation-stream-privacy-{async_}"
    failed_config = {
        "run_id": uuid4(),
        "configurable": {"thread_id": thread_id},
    }

    failed_states, failure = _stream_compiled_values(
        graph,
        failed_config,
        async_=async_,
    )
    for state in failed_states:
        messages = state.get("messages", [])
        if messages:
            cli_module._render_live_stream_message(messages[-1], no_web=False)

    assert isinstance(failure, citation_failure.ReportCitationError)
    assert any(
        secret in str(message.content)
        for state in failed_states
        for message in state.get("messages", [])
        if isinstance(message, AIMessage)
    )
    assert rendered == []

    accepted_config = {
        "run_id": uuid4(),
        "configurable": {"thread_id": thread_id},
    }
    accepted_states, accepted_error = _stream_compiled_values(
        graph,
        accepted_config,
        async_=async_,
    )
    for state in accepted_states:
        messages = state.get("messages", [])
        if messages:
            cli_module._render_live_stream_message(messages[-1], no_web=False)
    accepted_result = accepted_states[-1]
    accepted_report = accepted_result["files"]["/final_report.md"]
    cli_module._render_final_result(
        accepted_result,
        agent_module.file_data_to_string(accepted_report),
        no_web=False,
    )

    captured = capsys.readouterr()
    assert accepted_error is None
    assert secret not in captured.out
    assert secret not in captured.err
    assert rendered == [
        "Corrected report https://public.publisher.org/report"
    ]


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("fail_operation", ["put_writes", "put", "get"])
def test_compiled_strict_citation_failure_never_masks_saver_error(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    fail_operation: str,
) -> None:
    import research_agent.agent as agent_module

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 0)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setenv("CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS", "0.05")
    saver = _CitationProbeSaver(fail_operation)
    graph = create_agent(
        FakeListChatModel(responses=["First invalid", "Still invalid"]),
        tools=[],
        middleware=[
            _OwnedCitationReportGuard(),
            agent_module.ResearchStateMiddleware(),
        ],
        checkpointer=saver,
    )
    run_id = uuid4()
    config = {
        "run_id": run_id,
        "configurable": {
            "thread_id": f"citation-saver-{fail_operation}-{async_}",
        },
    }

    with pytest.raises(_CitationPersistenceError) as caught:
        _invoke_compiled(graph, config, async_=async_)

    assert not isinstance(caught.value, citation_failure.ReportCitationError)
    saver.fail_operation = None
    restored_tuple = saver.get_tuple(config)
    assert restored_tuple is not None
    restored = restored_tuple.checkpoint["channel_values"]
    failed_fingerprint = restored.get("completion_report_owned_fingerprint")
    if fail_operation in {"get", "put_writes"}:
        assert citation_failure.citation_failure_is_current(
            restored,
            run_id=str(run_id),
            report_fingerprint=failed_fingerprint,
        )
    else:
        assert not citation_failure.citation_failure_is_current(
            restored,
            run_id=str(run_id),
            report_fingerprint=failed_fingerprint,
        )
    if fail_operation == "put_writes":
        assert restored_tuple.parent_config is not None
        parent_tuple = saver.get_tuple(restored_tuple.parent_config)
        assert parent_tuple is not None
        assert not any(
            channel == "citation_failure_run_id" and value == str(run_id)
            for _task_id, channel, value in parent_tuple.pending_writes
        )


@pytest.mark.parametrize("async_", [False, True])
def test_compiled_strict_citation_waits_for_slow_successful_saver(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    import research_agent.agent as agent_module

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 0)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setenv("CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS", "1.5")
    saver = _DelayedCitationSaver(0.45)
    graph = create_agent(
        FakeListChatModel(responses=["First invalid", "Still invalid"]),
        tools=[],
        middleware=[
            _OwnedCitationReportGuard(),
            agent_module.ResearchStateMiddleware(),
        ],
        checkpointer=saver,
    )
    config = {
        "run_id": uuid4(),
        "configurable": {"thread_id": f"citation-slow-{async_}"},
    }

    started = time.monotonic()
    with pytest.raises(citation_failure.ReportCitationError):
        _invoke_compiled(graph, config, async_=async_)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.4
    assert elapsed < 1.5
    assert saver.confirmed_failure_reads >= 1


@pytest.mark.parametrize("async_", [False, True])
def test_compiled_missing_citation_confirmation_is_bounded_and_blocked(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    import research_agent.agent as agent_module

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 0)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setenv("CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS", "0.05")
    graph = create_agent(
        FakeListChatModel(responses=["First invalid", "Still invalid"]),
        tools=[],
        middleware=[
            _OwnedCitationReportGuard(),
            agent_module.ResearchStateMiddleware(),
        ],
        checkpointer=_MissingCitationConfirmationSaver(),
    )
    run_id = uuid4()
    config = {
        "run_id": run_id,
        "configurable": {"thread_id": f"citation-missing-{async_}"},
    }

    started = time.monotonic()
    result = _invoke_compiled(graph, config, async_=async_)
    elapsed = time.monotonic() - started
    fingerprint = result["completion_report_owned_fingerprint"]

    assert elapsed < 0.5
    assert citation_failure.citation_failure_is_current(
        result,
        run_id=str(run_id),
        report_fingerprint=fingerprint,
    )
    assert result["citation_accepted_report_fingerprint"] is None
    assert result["_streamed_files"] == []


@pytest.mark.parametrize("async_", [False, True])
def test_compiled_stalled_citation_confirmation_read_is_bounded_and_blocked(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    import research_agent.agent as agent_module

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "MAX_VERIFICATION_ROUNDS", 0)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    monkeypatch.setenv("CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS", "0.05")
    graph = create_agent(
        FakeListChatModel(responses=["First invalid", "Still invalid"]),
        tools=[],
        middleware=[
            _OwnedCitationReportGuard(),
            agent_module.ResearchStateMiddleware(),
        ],
        checkpointer=_StalledCitationConfirmationSaver(0.45),
    )
    run_id = uuid4()
    config = {
        "run_id": run_id,
        "configurable": {"thread_id": f"citation-stalled-{async_}"},
    }

    started = time.monotonic()
    result = _invoke_compiled(graph, config, async_=async_)
    elapsed = time.monotonic() - started
    fingerprint = result["completion_report_owned_fingerprint"]

    assert elapsed < 0.3
    assert citation_failure.citation_failure_is_current(
        result,
        run_id=str(run_id),
        report_fingerprint=fingerprint,
    )
    assert result["citation_accepted_report_fingerprint"] is None
    assert result["_streamed_files"] == []


def test_sync_checkpoint_confirmation_stalls_have_fixed_resource_bound() -> None:
    import research_agent.agent as agent_module

    release = threading.Event()
    lock = threading.Lock()
    started = 0
    finished = 0

    def stalled_read(_config: Any) -> None:
        nonlocal started, finished
        with lock:
            started += 1
        release.wait(timeout=2.0)
        with lock:
            finished += 1

    try:
        for _ in range(20):
            result = agent_module._get_checkpoint_tuple_before_deadline(
                stalled_read,
                {},
                time.monotonic() + 0.01,
            )
            assert result is agent_module._CITATION_CHECKPOINT_READ_TIMEOUT

        assert started <= 2
        unrelated = threading.Event()
        threading.Thread(target=unrelated.set).start()
        assert unrelated.wait(timeout=0.2)
    finally:
        release.set()
        deadline = time.monotonic() + 0.5
        while finished < started and time.monotonic() < deadline:
            time.sleep(0.005)
        assert finished == started


def test_async_checkpoint_confirmation_stalls_have_process_wide_bound() -> None:
    import research_agent.agent as agent_module

    async def scenario() -> None:
        release = asyncio.Event()
        started = 0
        finished = 0

        async def cancellation_resistant_read(_config: Any) -> None:
            nonlocal started, finished
            started += 1
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished += 1

        try:
            for _ in range(20):
                result = await agent_module._aget_checkpoint_tuple_before_deadline(
                    cancellation_resistant_read,
                    {},
                    time.monotonic() + 0.01,
                )
                assert result is agent_module._CITATION_CHECKPOINT_READ_TIMEOUT

            assert started <= 4
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        finally:
            release.set()
            deadline = time.monotonic() + 0.5
            while finished < started and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            assert finished == started

    asyncio.run(scenario())


def test_async_checkpoint_confirmation_outer_cancel_cleans_up_boundedly() -> None:
    import research_agent.agent as agent_module

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        cancellation_seen = asyncio.Event()
        finished = asyncio.Event()

        async def cancellation_resistant_read(_config: Any) -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
            finally:
                finished.set()

        confirmation = asyncio.create_task(
            agent_module._aget_checkpoint_tuple_before_deadline(
                cancellation_resistant_read,
                {},
                time.monotonic() + 5.0,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.2)
        before_cancel = time.monotonic()
        confirmation.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await confirmation

            assert time.monotonic() - before_cancel < 0.2
            await asyncio.wait_for(cancellation_seen.wait(), timeout=0.2)
            assert not finished.is_set()
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=0.2)

    asyncio.run(scenario())


class _DeterministicToolModel(FakeMessagesListChatModel):
    """Fake model that supports tool binding and records every model turn."""

    call_count: int = 0

    def bind_tools(self, *args: Any, **kwargs: Any) -> _DeterministicToolModel:
        return self

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        return super()._generate(*args, **kwargs)


class _AfterModelTraceMiddleware(AgentMiddleware):
    """Record compiled after-model hook unwinding order."""

    def __init__(self, label: str, events: list[str]) -> None:
        super().__init__()
        self.label = label
        self.events = events

    @hook_config(can_jump_to=["model", "end"])
    def after_model(self, state: Any, runtime: Any) -> None:
        self.events.append(self.label)

    @hook_config(can_jump_to=["model", "end"])
    async def aafter_model(self, state: Any, runtime: Any) -> None:
        self.events.append(self.label)


class _CompletionTraceMiddleware(_AfterModelTraceMiddleware):
    pass


class _ResumeTraceMiddleware(_AfterModelTraceMiddleware):
    pass


class _ResearchTraceMiddleware(_AfterModelTraceMiddleware):
    pass


@pytest.mark.parametrize("async_", [False, True])
def test_compiled_after_model_hooks_unwind_research_resume_completion(
    async_: bool,
) -> None:
    events: list[str] = []
    graph = create_agent(
        _DeterministicToolModel(responses=[AIMessage(content="Done")]),
        tools=[],
        middleware=[
            TodoListMiddleware(system_prompt=""),
            ClarificationMiddleware(feature_enabled=False),
            _CompletionTraceMiddleware("completion", events),
            _ResumeTraceMiddleware("resume", events),
            _ResearchTraceMiddleware("research", events),
        ],
    )

    _invoke_compiled(
        graph,
        {"configurable": {"thread_id": f"hook-order-{async_}"}},
        async_=async_,
    )

    assert events == ["research", "resume", "completion"]


class _RecordingCompletionGuard(completion_guard.CompletionGuardMiddleware):
    """Record ownership after each real tool-result correlation hook."""

    def __init__(self) -> None:
        super().__init__()
        self.ownership_observations: list[tuple[str | None, str | None, bool]] = []
        self.runtime_run_ids: list[str | None] = []
        self.model_runtime_run_ids: list[str | None] = []

    @staticmethod
    def _runtime_run_id(runtime: Any) -> str | None:
        execution_info = getattr(runtime, "execution_info", None)
        return getattr(execution_info, "run_id", None)

    def before_agent(
        self,
        state: completion_guard.CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        self.runtime_run_ids.append(self._runtime_run_id(runtime))
        return super().before_agent(state, runtime)

    def _record(
        self,
        state: completion_guard.CompletionState,
        update: dict[str, Any] | None,
    ) -> None:
        effective = {**state, **(update or {})}
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        self.ownership_observations.append(
            (
                getattr(last, "name", None),
                getattr(last, "status", None),
                effective.get("completion_report_owned") is True,
            )
        )

    def before_model(
        self,
        state: completion_guard.CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        self.model_runtime_run_ids.append(self._runtime_run_id(runtime))
        update = super().before_model(state, runtime)
        self._record(state, update)
        return update

    async def abefore_model(
        self,
        state: completion_guard.CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        self.model_runtime_run_ids.append(self._runtime_run_id(runtime))
        update = await super().abefore_model(state, runtime)
        self._record(state, update)
        return update


def _tool_call(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str,
    message_id: str,
) -> AIMessage:
    return AIMessage(
        content="",
        id=message_id,
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


def _production_like_graph(
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: list[AIMessage],
    completion: completion_guard.CompletionGuardMiddleware | None = None,
    full_stack: bool = True,
    with_checkpointer: bool = False,
) -> tuple[Any, _DeterministicToolModel]:
    import research_agent.agent as agent_module

    monkeypatch.setattr(agent_module, "ENABLE_VERIFICATION", False)
    monkeypatch.setattr(agent_module, "ENABLE_EVAL_TRACKING", False)
    model = _DeterministicToolModel(responses=responses)
    guard = completion or completion_guard.CompletionGuardMiddleware()
    middleware: list[AgentMiddleware[Any, Any]] = [
        TodoListMiddleware(system_prompt=""),
        guard,
    ]
    if full_stack:
        middleware = [
            TodoListMiddleware(system_prompt=""),
            ClarificationMiddleware(feature_enabled=False),
            guard,
            ResumeMiddleware(),
            agent_module.ResearchStateMiddleware(),
        ]
    graph = create_deep_agent(
        model=model,
        tools=[write_file],
        middleware=middleware,
        checkpointer=InMemorySaver() if with_checkpointer else None,
    )
    return graph, model


def _run_production_like_graph(
    graph: Any,
    *,
    config: dict[str, Any],
    async_: bool,
) -> dict[str, Any]:
    input_state = {"messages": [HumanMessage(content="Research graph systems")]}
    if async_:
        return asyncio.run(graph.ainvoke(input_state, config=config))
    return graph.invoke(input_state, config=config)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize(
    "write_args",
    [
        {"content": "Accepted report"},
        {"file_path": "/final_report.md", "content": "Accepted report"},
    ],
)
def test_compiled_production_stack_owns_only_successful_real_report_write(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    write_args: dict[str, Any],
) -> None:
    run_id = uuid4()
    guard = _RecordingCompletionGuard()
    responses = [
        _tool_call(
            "write_todos",
            {"todos": [{"content": "Write report", "status": "pending"}]},
            call_id="todo-open-call",
            message_id="todo-open-message",
        ),
        AIMessage(content="Stopping early", id="early-terminal"),
        _tool_call(
            "write_file",
            write_args,
            call_id="successful-report-call",
            message_id="successful-report-message",
        ),
        _tool_call(
            "write_todos",
            {"todos": [{"content": "Write report", "status": "completed"}]},
            call_id="todo-done-call",
            message_id="todo-done-message",
        ),
        AIMessage(content="Research complete", id="accepted-terminal"),
    ]
    graph, model = _production_like_graph(
        monkeypatch,
        responses=responses,
        completion=guard,
    )
    config = {
        "run_id": run_id,
        "recursion_limit": 200,
        "configurable": {"thread_id": f"accepted-{async_}-{bool(write_args.get('file_path'))}"},
    }

    result = _run_production_like_graph(graph, config=config, async_=async_)

    assert model.call_count == len(responses)
    assert guard.runtime_run_ids == [str(run_id)]
    assert guard.model_runtime_run_ids == [str(run_id)] * len(responses)
    assert result["completion_current_run_id"] == str(run_id)
    assert result["completion_attempts"] == 1
    assert result["completion_report_owned"] is True
    assert result["files"]["/final_report.md"]["content"] == "Accepted report"
    assert result["todos"] == [{"content": "Write report", "status": "completed"}]
    successful = [
        observed
        for observed in guard.ownership_observations
        if observed[:2] == ("write_file", "success")
    ]
    assert successful == [("write_file", "success", True)]
    successful_index = guard.ownership_observations.index(successful[0])
    assert all(
        not owned
        for _name, _status, owned in guard.ownership_observations[:successful_index]
    )
    messages = result["messages"]
    opening_todo_result = next(
        message
        for message in messages
        if isinstance(message, ToolMessage)
        and message.tool_call_id == "todo-open-call"
    )
    assert opening_todo_result.status == "success"
    assert messages.index(opening_todo_result) < next(
        index for index, message in enumerate(messages) if message.id == "early-terminal"
    )
    assert next(message for message in messages if message.id == "early-terminal").response_metadata[
        "resume_intermediate"
    ] is True
    assert {message.id for message in messages}.issuperset(
        {
            "todo-open-message",
            "early-terminal",
            "successful-report-message",
            "todo-done-message",
            "accepted-terminal",
        }
    )


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("limit", [1, 2, 3])
def test_compiled_real_tools_exhaust_exact_continuation_limit_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    limit: int,
) -> None:
    monkeypatch.setenv("MAX_COMPLETION_ATTEMPTS", str(limit))
    terminal_responses = [
        AIMessage(content=f"Partial {attempt}", id=f"partial-{attempt}")
        for attempt in range(limit + 1)
    ]
    responses = [
        _tool_call(
            "write_file",
            {"file_path": "/notes.md", "content": "Durable notes"},
            call_id="notes-call",
            message_id="notes-message",
        ),
        _tool_call(
            "write_todos",
            {"todos": [{"content": "Finish report", "status": "pending"}]},
            call_id="todo-call",
            message_id="todo-message",
        ),
        *terminal_responses,
    ]
    run_id = uuid4()
    guard = _RecordingCompletionGuard()
    graph, model = _production_like_graph(
        monkeypatch,
        responses=responses,
        completion=guard,
        full_stack=False,
        with_checkpointer=True,
    )
    config = {
        "run_id": run_id,
        "recursion_limit": 200,
        "configurable": {"thread_id": f"real-exhaustion-{limit}-{async_}"},
    }

    with pytest.raises(completion_guard.ResearchIncompleteError):
        _run_production_like_graph(graph, config=config, async_=async_)

    assert model.call_count == limit + 3
    assert guard.runtime_run_ids == [str(run_id)]
    assert guard.model_runtime_run_ids == [str(run_id)] * (limit + 3)
    snapshot = graph.get_state(config)
    values = snapshot.values
    assert values["completion_current_run_id"] == str(run_id)
    assert values["completion_attempts"] == limit
    assert values["completion_exhausted_run_id"] == str(run_id)
    assert values["completion_exhausted_incomplete_todo_count"] == 1
    assert values["completion_exhausted_malformed_todo_count"] == 0
    assert values["completion_exhausted_report_reason"] == "missing"
    assert values["files"]["/notes.md"]["content"] == "Durable notes"
    assert values["todos"] == [{"content": "Finish report", "status": "pending"}]
    messages = values["messages"]
    assert {message.id for message in messages}.issuperset(
        {"notes-message", "todo-message", *(f"partial-{i}" for i in range(limit + 1))}
    )
    assert all(
        next(message for message in messages if message.id == f"partial-{attempt}")
        .response_metadata.get("resume_intermediate")
        is True
        for attempt in range(limit + 1)
    )


@pytest.mark.parametrize("async_", [False, True])
def test_compiled_resume_uses_new_runtime_run_and_clears_stale_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
) -> None:
    monkeypatch.setenv("MAX_COMPLETION_ATTEMPTS", "1")
    first_run_id = uuid4()
    resumed_run_id = uuid4()
    guard = _RecordingCompletionGuard()
    responses = [
        _tool_call(
            "write_todos",
            {"todos": [{"content": "Finish report", "status": "pending"}]},
            call_id="resume-todo-open-call",
            message_id="resume-todo-open-message",
        ),
        AIMessage(content="First partial", id="resume-partial-0"),
        AIMessage(content="Second partial", id="resume-partial-1"),
        _tool_call(
            "write_file",
            {"file_path": "/final_report.md", "content": "Resumed report"},
            call_id="resume-report-call",
            message_id="resume-report-message",
        ),
        _tool_call(
            "write_todos",
            {"todos": [{"content": "Finish report", "status": "completed"}]},
            call_id="resume-todo-done-call",
            message_id="resume-todo-done-message",
        ),
        AIMessage(content="Resumed research complete", id="resume-terminal"),
    ]
    graph, model = _production_like_graph(
        monkeypatch,
        responses=responses,
        completion=guard,
        full_stack=False,
        with_checkpointer=True,
    )
    thread_id = f"stale-exhaustion-resume-{async_}"
    first_config = {
        "run_id": first_run_id,
        "recursion_limit": 200,
        "configurable": {"thread_id": thread_id},
    }

    with pytest.raises(completion_guard.ResearchIncompleteError):
        _run_production_like_graph(graph, config=first_config, async_=async_)

    exhausted = graph.get_state(first_config).values
    original_generation = exhausted["completion_request_generation"]
    assert exhausted["completion_current_run_id"] == str(first_run_id)
    assert exhausted["completion_exhausted_run_id"] == str(first_run_id)

    resumed_config = {
        "run_id": resumed_run_id,
        "recursion_limit": 200,
        "configurable": {
            "thread_id": thread_id,
            "resume_incomplete_todos": True,
        },
    }
    result = _run_production_like_graph(
        graph,
        config=resumed_config,
        async_=async_,
    )

    assert model.call_count == len(responses)
    assert guard.runtime_run_ids == [str(first_run_id), str(resumed_run_id)]
    assert guard.model_runtime_run_ids == [
        *([str(first_run_id)] * 3),
        *([str(resumed_run_id)] * 3),
    ]
    assert result["completion_current_run_id"] == str(resumed_run_id)
    assert result["completion_request_generation"] == original_generation
    assert result["completion_resume_adopted_generation"] == original_generation
    assert result["completion_exhausted_run_id"] is None
    assert result["completion_attempts"] == 0
    assert result["completion_report_owned"] is True
    assert result["files"]["/final_report.md"]["content"] == "Resumed report"
    assert result["todos"] == [{"content": "Finish report", "status": "completed"}]
