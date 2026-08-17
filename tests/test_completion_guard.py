"""Pure completion-policy tests."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

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
        "completion_verified_report_modified_at": None,
        "completion_accepted_at_limit_report_modified_at": None,
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

    assert sync_update == {"completion_report_owned": True}
    assert async_update == {"completion_report_owned": True}


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
