"""Pure policy for deciding whether a planned research run is complete."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NotRequired
from uuid import UUID, uuid4

from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.filesystem import FilesystemState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.todo import PlanningState
from langchain.agents.middleware.types import OmitFromInput
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_config

DEFAULT_MAX_COMPLETION_ATTEMPTS = 3
MAX_ALLOWED_COMPLETION_ATTEMPTS = 3
FINAL_REPORT_PATH = "/final_report.md"

ReportFailureReason = Literal["missing", "empty", "malformed", "stale"]


class CompletionState(FilesystemState, PlanningState):
    """Request-scoped state used by completion enforcement middleware."""

    completion_current_run_id: Annotated[NotRequired[str | None], OmitFromInput]
    completion_request_generation: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_plan_owner_generation: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_report_owned: Annotated[NotRequired[bool], OmitFromInput]
    completion_resume_adopted_generation: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_attempts: Annotated[NotRequired[int], OmitFromInput]
    completion_attempt_limit: Annotated[NotRequired[int], OmitFromInput]
    completion_report_baseline_modified_at: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_verified_report_modified_at: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_accepted_at_limit_report_modified_at: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_exhausted_run_id: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_exhausted_incomplete_todo_count: Annotated[
        NotRequired[int], OmitFromInput
    ]
    completion_exhausted_malformed_todo_count: Annotated[
        NotRequired[int], OmitFromInput
    ]
    completion_exhausted_report_reason: Annotated[
        NotRequired[ReportFailureReason | None], OmitFromInput
    ]
    verification_round: Annotated[NotRequired[int], OmitFromInput]
    verification_feedback: Annotated[NotRequired[str | None], OmitFromInput]
    _eval_logged: Annotated[NotRequired[bool], OmitFromInput]
    _streamed_files: Annotated[
        NotRequired[list[str] | None], OmitFromInput
    ]


class CompletionGuardMiddleware(AgentMiddleware):
    """Own completion artifacts and retry state for one visible graph run."""

    state_schema = CompletionState

    def __init__(
        self,
        *,
        config_getter: Callable[[], Mapping[str, Any]] = get_config,
    ) -> None:
        super().__init__()
        self._config_getter = config_getter

    def before_agent(
        self,
        state: CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Start one ordinary generation or adopt it for an explicit resume."""
        config = self._config()
        current_run_id = _normalize_run_id(config.get("run_id"))
        if state.get("completion_current_run_id") == current_run_id:
            return None

        common: dict[str, Any] = {
            "completion_current_run_id": current_run_id,
            "completion_attempts": 0,
            "completion_attempt_limit": get_max_completion_attempts(),
            "completion_exhausted_run_id": None,
            "completion_exhausted_incomplete_todo_count": 0,
            "completion_exhausted_malformed_todo_count": 0,
            "completion_exhausted_report_reason": None,
        }
        configurable = config.get("configurable")
        is_resume = (
            isinstance(configurable, Mapping)
            and configurable.get("resume_incomplete_todos") is True
        )
        if is_resume:
            generation = state.get("completion_request_generation")
            if not isinstance(generation, str) or not generation:
                generation = current_run_id
            return {
                **common,
                "completion_request_generation": generation,
                "completion_resume_adopted_generation": generation,
            }

        return {
            **common,
            "completion_request_generation": current_run_id,
            "completion_plan_owner_generation": None,
            "completion_report_owned": False,
            "completion_resume_adopted_generation": None,
            "completion_report_baseline_modified_at": _report_modified_at(
                state.get("files")
            ),
            "completion_verified_report_modified_at": None,
            "completion_accepted_at_limit_report_modified_at": None,
            "todos": [],
            "verification_round": 0,
            "verification_feedback": None,
            "_eval_logged": False,
            "_streamed_files": [],
        }

    def before_model(
        self,
        state: CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Activate request ownership after strictly correlated tool success."""
        return _correlate_artifact_ownership(state)

    async def abefore_model(
        self,
        state: CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Async equivalent of request artifact activation."""
        return _correlate_artifact_ownership(state)

    def _config(self) -> Mapping[str, Any]:
        try:
            config = self._config_getter()
        except RuntimeError:
            return {}
        return config if isinstance(config, Mapping) else {}


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


def _normalize_run_id(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return str(uuid4())


def _report_modified_at(files: object) -> str | None:
    if not isinstance(files, Mapping):
        return None
    report = files.get(FINAL_REPORT_PATH)
    if not isinstance(report, Mapping):
        return None
    modified_at = report.get("modified_at")
    return modified_at if isinstance(modified_at, str) and modified_at else None


def _correlate_artifact_ownership(
    state: CompletionState,
) -> dict[str, Any] | None:
    exchange = _latest_tool_exchange(state.get("messages"))
    if exchange is None:
        return None
    tool_calls, results = exchange

    generation = state.get("completion_request_generation")
    if not isinstance(generation, str) or not generation:
        return None

    updates: dict[str, Any] = {}
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping):
            continue
        call_id = tool_call.get("id")
        name = tool_call.get("name")
        args = tool_call.get("args")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not isinstance(args, Mapping)
        ):
            continue
        if not _valid_tool_arguments(name, args):
            continue
        result = results[call_id]
        if getattr(result, "status", None) != "success":
            continue

        if name == "write_todos" and _valid_nonempty_todos(state.get("todos")):
            updates["completion_plan_owner_generation"] = generation
        elif name == "write_file" and _owns_changed_final_report(args, state):
            updates["completion_report_owned"] = True

    return updates or None


def _latest_tool_exchange(
    messages: object,
) -> tuple[list[object], dict[str, ToolMessage]] | None:
    if not isinstance(messages, list) or not messages:
        return None

    index = len(messages) - 1
    trailing_results: list[ToolMessage] = []
    while index >= 0 and isinstance(messages[index], ToolMessage):
        trailing_results.append(messages[index])
        index -= 1
    if not trailing_results or index < 0:
        return None

    tool_message = messages[index]
    if not isinstance(tool_message, AIMessage) or not isinstance(
        tool_message.tool_calls, list
    ):
        return None

    call_ids: set[str] = set()
    for tool_call in tool_message.tool_calls:
        if not isinstance(tool_call, Mapping):
            return None
        call_id = tool_call.get("id")
        name = tool_call.get("name")
        args = tool_call.get("args")
        if (
            not isinstance(call_id, str)
            or not call_id
            or call_id in call_ids
            or not isinstance(name, str)
            or not name
            or not isinstance(args, Mapping)
        ):
            return None
        call_ids.add(call_id)

    by_id: dict[str, ToolMessage] = {}
    for result in trailing_results:
        result_id = getattr(result, "tool_call_id", None)
        if (
            not isinstance(result_id, str)
            or not result_id
            or result_id in by_id
        ):
            return None
        by_id[result_id] = result
    if set(by_id) != call_ids:
        return None
    return list(tool_message.tool_calls), by_id


def _valid_tool_arguments(name: str, args: Mapping[str, Any]) -> bool:
    if name == "write_todos":
        return isinstance(args.get("todos"), list)
    if name == "write_file":
        content = args.get("content")
        return isinstance(content, str) and bool(content.strip())
    return True


def _valid_nonempty_todos(todos: object) -> bool:
    return isinstance(todos, list) and bool(todos) and all(
        _is_valid_todo(todo) for todo in todos
    )


def _owns_changed_final_report(
    args: Mapping[str, Any],
    state: CompletionState,
) -> bool:
    file_path = args.get("file_path", FINAL_REPORT_PATH)
    if file_path != FINAL_REPORT_PATH:
        return False
    return (
        _inspect_report(
            state.get("files"),
            report_owned=True,
            baseline_modified_at=state.get(
                "completion_report_baseline_modified_at"
            ),
        )
        is None
    )


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
