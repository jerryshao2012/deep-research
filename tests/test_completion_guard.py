"""Pure completion-policy tests."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_api.serde import default as serialize_default

import research_agent.completion_guard as completion_guard
from research_agent.completion_guard import (
    CompletionInspection,
    get_max_completion_attempts,
    inspect_completion,
)


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

    forged = graph.get_input_schema().model_validate(
        {
            "messages": [],
            "completion_request_generation": "forged-generation",
            "completion_plan_owner_generation": "forged-generation",
            "completion_report_owned": True,
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
    assert update["completion_cited_baseline_fingerprints"] == {
        "/cited_response.md": _fingerprint(cited)
    }


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
        "completion_accepted_at_limit_report_modified_at": None,
        "completion_accepted_at_limit_report_fingerprint": None,
        "completion_cited_baseline_fingerprints": {},
        "completion_exhausted_run_id": None,
        "completion_exhausted_incomplete_todo_count": 0,
        "completion_exhausted_malformed_todo_count": 0,
        "completion_exhausted_report_reason": None,
        "todos": [],
        "verification_round": 0,
        "verification_feedback": None,
        "_eval_logged": False,
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
