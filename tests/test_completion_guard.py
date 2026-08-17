"""Pure completion-policy tests."""

from __future__ import annotations

from typing import Any

import pytest

from research_agent.completion_guard import (
    CompletionInspection,
    get_max_completion_attempts,
    inspect_completion,
)


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
            {"content": "Write report", "status": " COMPLETED "},
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
        ({"/final_report.md": _file("   ")}, True, "prior", "empty"),
        ({"/final_report.md": {"content": object()}}, True, "prior", "malformed"),
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
