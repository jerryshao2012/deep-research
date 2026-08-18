"""Pure policy for deciding whether a planned research run is complete."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NotRequired
from uuid import UUID, uuid4

from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.filesystem import FilesystemState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.todo import PlanningState
from langchain.agents.middleware.types import (
    ModelRequest,
    OmitFromInput,
    hook_config,
)
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.config import get_config

from research_agent.citation_failure import (
    CITATION_FAILURE_CLEAR_UPDATE,
    citation_acceptance_ready,
    citation_failure_blocks_finalization,
    clear_stale_citation_failure,
)

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
    completion_report_baseline_fingerprint: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_report_owned_fingerprint: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_verified_report_modified_at: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_verified_report_fingerprint: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_accepted_at_limit_report_modified_at: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_accepted_at_limit_report_fingerprint: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    completion_cited_baseline_fingerprints: Annotated[
        NotRequired[dict[str, str]], OmitFromInput
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
    citation_failure_run_id: Annotated[NotRequired[str | None], OmitFromInput]
    citation_failure_report_fingerprint: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    citation_failure_defects: Annotated[
        NotRequired[list[dict[str, str]]], OmitFromInput
    ]
    citation_accepted_report_fingerprint: Annotated[
        NotRequired[str | None], OmitFromInput
    ]
    citation_corrections_used: Annotated[NotRequired[int], OmitFromInput]
    verification_round: Annotated[NotRequired[int], OmitFromInput]
    verification_feedback: Annotated[NotRequired[str | None], OmitFromInput]
    _eval_logged: Annotated[NotRequired[bool], OmitFromInput]
    _eval_pending: Annotated[NotRequired[bool], OmitFromInput]
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
        """Create middleware with an injectable per-run config source."""
        super().__init__()
        self._config_getter = config_getter

    def before_agent(
        self,
        state: CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Start one ordinary generation or adopt it for an explicit resume."""
        config = self._config()
        current_run_id = _resolve_run_id(
            config,
            runtime,
            generate_fallback=True,
        )
        assert current_run_id is not None
        stale_failure_update = clear_stale_citation_failure(
            state,
            run_id=current_run_id,
            report_fingerprint=_report_fingerprint(state.get("files")),
        )
        if state.get("completion_current_run_id") == current_run_id:
            return stale_failure_update

        common: dict[str, Any] = {
            "completion_current_run_id": current_run_id,
            "completion_attempts": 0,
            "completion_attempt_limit": get_max_completion_attempts(),
            "completion_exhausted_run_id": None,
            "completion_exhausted_incomplete_todo_count": 0,
            "completion_exhausted_malformed_todo_count": 0,
            "completion_exhausted_report_reason": None,
            **CITATION_FAILURE_CLEAR_UPDATE,
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
            "completion_report_baseline_fingerprint": _report_fingerprint(
                state.get("files")
            ),
            "completion_report_owned_fingerprint": None,
            "completion_verified_report_modified_at": None,
            "completion_verified_report_fingerprint": None,
            "completion_accepted_at_limit_report_modified_at": None,
            "completion_accepted_at_limit_report_fingerprint": None,
            "citation_accepted_report_fingerprint": None,
            "citation_corrections_used": 0,
            "completion_cited_baseline_fingerprints": (
                _snapshot_cited_fingerprints(state.get("files"))
            ),
            "todos": [],
            "verification_round": 0,
            "verification_feedback": None,
            "_eval_logged": False,
            "_eval_pending": False,
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

    @hook_config(can_jump_to=["model", "end"])
    def after_model(
        self,
        state: CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Continue an incomplete terminal response or checkpoint exhaustion."""
        return _completion_update(state)

    @hook_config(can_jump_to=["model", "end"])
    async def aafter_model(
        self,
        state: CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Async equivalent of completion continuation enforcement."""
        return _completion_update(state)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        """Append retry guidance to the ephemeral leading system message."""
        return handler(_configure_continuation_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        """Async equivalent of ephemeral continuation guidance."""
        return await handler(_configure_continuation_request(request))

    def after_agent(
        self,
        state: CompletionState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Fail a run only after its matching exhaustion checkpoint is saved."""
        if not _plan_is_active(state):
            return None
        exhausted_run_id = state.get("completion_exhausted_run_id")
        current_run_id = state.get("completion_current_run_id")
        actual_run_id = _resolve_run_id(
            self._config(),
            runtime,
            fallback=current_run_id,
        )
        if (
            not isinstance(exhausted_run_id, str)
            or not exhausted_run_id
            or exhausted_run_id != current_run_id
            or exhausted_run_id != actual_run_id
        ):
            return None
        raise ResearchIncompleteError(
            attempt_limit=_completion_attempt_limit(
                state.get("completion_attempt_limit")
            ),
            incomplete_todo_count=_safe_count(
                state.get("completion_exhausted_incomplete_todo_count")
            ),
            malformed_todo_count=_safe_count(
                state.get("completion_exhausted_malformed_todo_count")
            ),
            report_reason=_safe_report_reason(
                state.get("completion_exhausted_report_reason")
            ),
        )

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


class ResearchIncompleteError(RuntimeError):
    """Safe terminal error raised after completion retry exhaustion."""

    def __init__(
        self,
        *,
        attempt_limit: int,
        incomplete_todo_count: int,
        malformed_todo_count: int,
        report_reason: ReportFailureReason | None,
    ) -> None:
        """Build a privacy-safe completion-exhaustion error."""
        report_summary = report_reason or "complete"
        super().__init__(
            "Research incomplete after automatic continuation limit "
            f"(attempt_limit={attempt_limit}, "
            f"incomplete_todos={incomplete_todo_count}, "
            f"malformed_todos={malformed_todo_count}, "
            f"report={report_summary})."
        )


def completion_ready_for_finalization(
    state: Mapping[str, Any],
    *,
    verification_enabled: bool,
) -> bool:
    """Return whether this exact owned report version may be exposed."""
    if not _inspect_state_completion(state).ready:
        return False
    fingerprint = _report_fingerprint(state.get("files"))
    if not citation_acceptance_ready(
        state,
        report_fingerprint=fingerprint,
        strict_required=state.get("strict_web_citations") is True,
    ):
        return False
    if not verification_enabled:
        return True

    modified_at = _report_modified_at(state.get("files"))
    owned_fingerprint = state.get("completion_report_owned_fingerprint")
    if (
        modified_at is None
        or fingerprint is None
        or not isinstance(owned_fingerprint, str)
        or owned_fingerprint != fingerprint
    ):
        return False
    verified = (
        state.get("completion_verified_report_modified_at") == modified_at
        and state.get("completion_verified_report_fingerprint") == fingerprint
    )
    accepted_at_limit = (
        state.get("completion_accepted_at_limit_report_modified_at")
        == modified_at
        and state.get("completion_accepted_at_limit_report_fingerprint")
        == fingerprint
    )
    return verified or accepted_at_limit


def finalize_accepted_report(
    state: Mapping[str, Any],
    *,
    verification_enabled: bool,
) -> dict[str, Any] | None:
    """Build cited findings and final-report messages once after acceptance."""
    if not completion_ready_for_finalization(
        state,
        verification_enabled=verification_enabled,
    ):
        return None

    files = state.get("files")
    if not isinstance(files, Mapping):
        return None
    streamed = {
        path
        for path in (state.get("_streamed_files") or [])
        if isinstance(path, str)
    }
    messages: list[AIMessage] = []

    baseline = state.get("completion_cited_baseline_fingerprints")
    baseline_is_valid = isinstance(baseline, Mapping) and all(
        isinstance(path, str) and isinstance(fingerprint, str)
        for path, fingerprint in baseline.items()
    )
    cited_files = sorted(
        (
            path
            for path in files
            if isinstance(path, str)
            and path.lstrip("/").startswith("cited_response")
            and path.endswith(".md")
            and path not in streamed
            and baseline_is_valid
            and artifact_fingerprint(files[path]) != baseline.get(path)
        ),
        key=_cited_report_sort_key,
    )
    for file_path in cited_files:
        file_data = files[file_path]
        if not isinstance(file_data, Mapping):
            continue
        try:
            content = file_data_to_string(file_data)  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        if not content.strip():
            continue
        messages.append(
            AIMessage(
                content=f"**LLM Wiki Query Findings:**\n\n{content.strip()}"
            )
        )
        streamed.add(file_path)

    if messages and FINAL_REPORT_PATH not in streamed:
        messages.append(AIMessage(content="---"))

    if FINAL_REPORT_PATH not in streamed:
        report = files.get(FINAL_REPORT_PATH)
        if isinstance(report, Mapping):
            try:
                content = file_data_to_string(report)  # type: ignore[arg-type]
            except (KeyError, TypeError, ValueError):
                content = ""
            if content.strip():
                messages.append(
                    AIMessage(content=f"**Final Report:**\n\n{content.strip()}")
                )
                streamed.add(FINAL_REPORT_PATH)

    if not messages:
        return None
    return {
        "messages": messages,
        "_streamed_files": sorted(streamed),
    }


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


def _completion_update(state: CompletionState) -> dict[str, Any] | None:
    messages = state.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    terminal = messages[-1]
    if not isinstance(terminal, AIMessage) or terminal.tool_calls:
        return None

    inspection = _inspect_state_completion(state)
    if not inspection.plan_active or inspection.ready:
        return None

    attempts = _safe_count(state.get("completion_attempts"))
    limit = _completion_attempt_limit(state.get("completion_attempt_limit"))
    tagged = _tag_intermediate(terminal)
    if attempts < limit:
        return {
            "messages": [tagged],
            "completion_attempts": attempts + 1,
            "jump_to": "model",
        }

    current_run_id = state.get("completion_current_run_id")
    exhausted_run_id = (
        current_run_id
        if isinstance(current_run_id, str) and current_run_id
        else None
    )
    return {
        "messages": [tagged],
        "completion_attempts": attempts,
        "completion_exhausted_run_id": exhausted_run_id,
        "completion_exhausted_incomplete_todo_count": (
            inspection.incomplete_todo_count
        ),
        "completion_exhausted_malformed_todo_count": (
            inspection.malformed_todo_count
        ),
        "completion_exhausted_report_reason": inspection.report_reason,
        "jump_to": "end",
    }


def _inspect_state_completion(
    state: Mapping[str, Any],
) -> CompletionInspection:
    inspection = inspect_completion(
        todos=state.get("todos"),
        files=state.get("files"),
        plan_active=_plan_is_active(state),
        report_owned=state.get("completion_report_owned") is True,
        report_baseline_modified_at=state.get(
            "completion_report_baseline_modified_at"
        ),
        report_baseline_fingerprint=state.get(
            "completion_report_baseline_fingerprint"
        ),
    )
    if inspection.report_reason is not None:
        return inspection

    report_fingerprint = _report_fingerprint(state.get("files"))
    if citation_failure_blocks_finalization(
        state,
        report_fingerprint=report_fingerprint,
    ):
        return CompletionInspection(
            plan_active=inspection.plan_active,
            incomplete_todo_count=inspection.incomplete_todo_count,
            malformed_todo_count=inspection.malformed_todo_count,
            report_reason="malformed",
        )
    owned_fingerprint = state.get("completion_report_owned_fingerprint")
    if (
        report_fingerprint is not None
        and isinstance(owned_fingerprint, str)
        and owned_fingerprint == report_fingerprint
    ):
        return inspection
    return CompletionInspection(
        plan_active=inspection.plan_active,
        incomplete_todo_count=inspection.incomplete_todo_count,
        malformed_todo_count=inspection.malformed_todo_count,
        report_reason="stale",
    )


def _tag_intermediate(message: AIMessage) -> AIMessage:
    metadata = {**message.response_metadata, "resume_intermediate": True}
    return message.model_copy(update={"response_metadata": metadata})


def _configure_continuation_request(request: ModelRequest) -> ModelRequest:
    state = request.state
    if not isinstance(state, Mapping) or not _plan_is_active(state):
        return request
    attempts = _safe_count(state.get("completion_attempts"))
    if attempts <= 0 or state.get("completion_exhausted_run_id") is not None:
        return request
    limit = _completion_attempt_limit(state.get("completion_attempt_limit"))
    guidance = _completion_guidance(
        inspection=_inspect_state_completion(state),
        attempt=min(attempts, limit),
        limit=limit,
    )

    system_message = request.system_message
    if system_message is None:
        configured_system = SystemMessage(content=guidance)
    elif isinstance(system_message.content, str):
        configured_system = system_message.model_copy(
            update={"content": f"{system_message.content}\n\n{guidance}"}
        )
    else:
        configured_system = system_message.model_copy(
            update={
                "content": [
                    *system_message.content,
                    {"type": "text", "text": guidance},
                ]
            }
        )
    return request.override(system_message=configured_system)


def _plan_is_active(state: Mapping[str, Any]) -> bool:
    generation = state.get("completion_request_generation")
    return (
        isinstance(generation, str)
        and bool(generation)
        and state.get("completion_plan_owner_generation") == generation
    )


def _completion_guidance(
    *,
    inspection: CompletionInspection,
    attempt: int,
    limit: int,
) -> str:
    plan_active = "true" if inspection.plan_active else "false"
    report_reason = inspection.report_reason or "complete"
    blockers = (
        f"plan_active={plan_active}, "
        f"incomplete_todos={inspection.incomplete_todo_count}, "
        f"malformed_todos={inspection.malformed_todo_count}, "
        f"report={report_reason}"
    )
    return f"""<CompletionGuard>
The prior response was not terminal because required research artifacts remain
incomplete. Continuation attempt {attempt} of {limit}. Completion blockers:
{blockers}.
Continue the existing research workflow now. Finish every active task and write a
changed, non-empty /final_report.md before giving a terminal answer. Do not restart
completed work.
</CompletionGuard>"""


def _safe_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _safe_report_reason(value: object) -> ReportFailureReason | None:
    if value == "missing":
        return "missing"
    if value == "empty":
        return "empty"
    if value == "malformed":
        return "malformed"
    if value == "stale":
        return "stale"
    return None


def _completion_attempt_limit(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return min(max(value, 1), MAX_ALLOWED_COMPLETION_ATTEMPTS)
    return DEFAULT_MAX_COMPLETION_ATTEMPTS


def _normalize_optional_run_id(value: object) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return None


def _runtime_run_id(runtime: object) -> str | None:
    execution_info = getattr(runtime, "execution_info", None)
    if isinstance(execution_info, Mapping):
        value = execution_info.get("run_id")
    else:
        value = getattr(execution_info, "run_id", None)
    return _normalize_optional_run_id(value)


def _resolve_run_id(
    config: Mapping[str, Any],
    runtime: object,
    *,
    fallback: object = None,
    generate_fallback: bool = False,
) -> str | None:
    """Resolve run ownership, preferring actual runtime identity on conflict."""
    configured = _normalize_optional_run_id(config.get("run_id"))
    actual = _runtime_run_id(runtime)
    if actual is not None:
        return actual
    if configured is not None:
        return configured
    normalized_fallback = _normalize_optional_run_id(fallback)
    if normalized_fallback is not None:
        return normalized_fallback
    return str(uuid4()) if generate_fallback else None


def _report_modified_at(files: object) -> str | None:
    if not isinstance(files, Mapping):
        return None
    report = files.get(FINAL_REPORT_PATH)
    if not isinstance(report, Mapping):
        return None
    modified_at = report.get("modified_at")
    return modified_at if isinstance(modified_at, str) and modified_at else None


def _cited_report_sort_key(path: str) -> tuple[int, int]:
    stem = path.removesuffix(".md").rstrip("/")
    if stem in {"/cited_response", "cited_response"}:
        return 0, 0
    suffix = stem.rsplit("_", 1)[-1]
    return 1, int(suffix) if suffix.isdigit() else 99


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
        elif name == "write_file":
            fingerprint = _changed_final_report_fingerprint(args, state)
            if fingerprint is not None:
                updates["completion_report_owned"] = True
                updates["completion_report_owned_fingerprint"] = fingerprint

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


def _changed_final_report_fingerprint(
    args: Mapping[str, Any],
    state: CompletionState,
) -> str | None:
    file_path = args.get("file_path", FINAL_REPORT_PATH)
    if file_path != FINAL_REPORT_PATH:
        return None
    fingerprint = _report_fingerprint(state.get("files"))
    if fingerprint is None:
        return None
    baseline_fingerprint = state.get("completion_report_baseline_fingerprint")
    if isinstance(baseline_fingerprint, str):
        return fingerprint if fingerprint != baseline_fingerprint else None
    if baseline_fingerprint is not None:
        return None
    if (
        _inspect_report(
            state.get("files"),
            report_owned=True,
            baseline_modified_at=state.get(
                "completion_report_baseline_modified_at"
            ),
        )
        is not None
    ):
        return None
    return fingerprint


def inspect_completion(
    *,
    todos: object,
    files: object,
    plan_active: bool,
    report_owned: bool,
    report_baseline_modified_at: str | None,
    report_baseline_fingerprint: object = None,
) -> CompletionInspection:
    """Inspect current-request completion without mutating graph state."""
    incomplete_count, malformed_count = _inspect_todos(todos)
    report_reason = _inspect_report(
        files,
        report_owned=report_owned,
        baseline_modified_at=report_baseline_modified_at,
        baseline_fingerprint=report_baseline_fingerprint,
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
    baseline_fingerprint: object = None,
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
    if not report_owned:
        return "stale"
    if isinstance(baseline_fingerprint, str):
        if artifact_fingerprint(file_data) == baseline_fingerprint:
            return "stale"
    elif baseline_fingerprint is not None:
        return "stale"
    elif modified_at == baseline_modified_at:
        return "stale"
    return None


def artifact_fingerprint(file_data: object) -> str | None:
    """Return stable content/version identity for valid filesystem data."""
    if not isinstance(file_data, Mapping):
        return None
    modified_at = file_data.get("modified_at")
    if not isinstance(modified_at, str) or not modified_at:
        return None
    try:
        content = file_data_to_string(file_data)  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None
    digest = hashlib.sha256()
    for part in (modified_at, content):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _report_fingerprint(files: object) -> str | None:
    if not isinstance(files, Mapping):
        return None
    return artifact_fingerprint(files.get(FINAL_REPORT_PATH))


def _snapshot_cited_fingerprints(files: object) -> dict[str, str]:
    if not isinstance(files, Mapping):
        return {}
    fingerprints: dict[str, str] = {}
    for path, file_data in files.items():
        if (
            not isinstance(path, str)
            or not path.lstrip("/").startswith("cited_response")
            or not path.endswith(".md")
        ):
            continue
        fingerprint = artifact_fingerprint(file_data)
        if fingerprint is not None:
            fingerprints[path] = fingerprint
    return fingerprints
