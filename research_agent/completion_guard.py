"""Pure policy for deciding whether a planned research run is complete."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from deepagents.backends.utils import file_data_to_string

DEFAULT_MAX_COMPLETION_ATTEMPTS = 3
MAX_ALLOWED_COMPLETION_ATTEMPTS = 3
FINAL_REPORT_PATH = "/final_report.md"

ReportFailureReason = Literal["missing", "empty", "malformed", "stale"]


@dataclass(frozen=True, slots=True)
class CompletionInspection:
    """Result of inspecting current-request plan and report artifacts."""

    plan_active: bool
    incomplete_todo_count: int
    malformed_todo_count: int
    report_reason: ReportFailureReason | None

    @property
    def ready(self) -> bool:
        """Return whether current request has a complete plan and owned report."""
        return (
            self.plan_active
            and self.incomplete_todo_count == 0
            and self.malformed_todo_count == 0
            and self.report_reason is None
        )


def get_max_completion_attempts() -> int:
    """Resolve automatic continuation budget, bounded to supported limits."""
    raw = os.getenv("MAX_COMPLETION_ATTEMPTS")
    try:
        parsed = int(raw) if raw is not None else DEFAULT_MAX_COMPLETION_ATTEMPTS
    except (TypeError, ValueError):
        return DEFAULT_MAX_COMPLETION_ATTEMPTS
    if parsed <= 0:
        return DEFAULT_MAX_COMPLETION_ATTEMPTS
    return min(parsed, MAX_ALLOWED_COMPLETION_ATTEMPTS)


def inspect_completion(
    *,
    todos: object,
    files: object,
    plan_active: bool,
    report_owned: bool,
    report_baseline_modified_at: str | None,
) -> CompletionInspection:
    """Inspect current-request completion without mutating graph state."""
    incomplete_count, malformed_count = _inspect_todos(todos)
    report_reason = _inspect_report(
        files,
        report_owned=report_owned,
        baseline_modified_at=report_baseline_modified_at,
    )
    return CompletionInspection(
        plan_active=plan_active,
        incomplete_todo_count=incomplete_count,
        malformed_todo_count=malformed_count,
        report_reason=report_reason,
    )


def _inspect_todos(todos: object) -> tuple[int, int]:
    if not isinstance(todos, list) or not todos:
        return 1, 1

    incomplete_count = 0
    malformed_count = 0
    for todo in todos:
        if not _is_valid_todo(todo):
            incomplete_count += 1
            malformed_count += 1
            continue
        if todo["status"] != "completed":
            incomplete_count += 1

    return incomplete_count, malformed_count


def _is_valid_todo(todo: object) -> bool:
    if not isinstance(todo, Mapping):
        return False
    content = todo.get("content")
    status = todo.get("status")
    if not isinstance(content, str) or not content.strip():
        return False
    if not isinstance(status, str):
        return False
    return status in {"pending", "in_progress", "completed"}


def _inspect_report(
    files: object,
    *,
    report_owned: bool,
    baseline_modified_at: str | None,
) -> ReportFailureReason | None:
    if not isinstance(files, Mapping):
        return "malformed"
    if FINAL_REPORT_PATH not in files:
        return "missing"

    file_data = files[FINAL_REPORT_PATH]
    if not isinstance(file_data, Mapping):
        return "malformed"
    modified_at = file_data.get("modified_at")
    if not isinstance(modified_at, str) or not modified_at:
        return "malformed"

    try:
        content = file_data_to_string(file_data)  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return "malformed"
    if not content.strip():
        return "empty"
    if not report_owned or modified_at == baseline_modified_at:
        return "stale"
    return None
