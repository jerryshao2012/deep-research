"""Core LangGraph Deep Research agent workflow and orchestrator configuration.

Coordinates multi-agent research tasks, managing state transitions, memory
checkpointers, file reading/writing tools, sub-agent delegation, and custom
skills mapping.
"""

import asyncio
import concurrent.futures
import contextvars
import hashlib
import math
import os
import queue
import re
import threading
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, NotRequired

from deepagents import SubAgent, create_deep_agent
from deepagents.backends import StateBackend
from deepagents.backends.utils import (
    create_file_data,
    file_data_to_string,
)
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from dotenv import load_dotenv
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    TodoListMiddleware,
    hook_config,
)
from langchain.agents.middleware.types import OmitFromInput, OmitFromOutput
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.channels.ephemeral_value import EphemeralValue
from langgraph.config import get_config

from research_agent.citation_failure import (
    CITATION_FAILURE_CLEAR_UPDATE,
    STRUCTURAL_CITATION_REJECTION_CLEAR_UPDATE,
    ReportCitationError,
    build_citation_failure_update,
    citation_acceptance_ready,
    citation_failure_is_current,
    raise_current_citation_failure,
    resolve_citation_run_id,
)
from research_agent.cli_utils import get_ssl_verify_config, str2bool
from research_agent.completion_guard import (
    CompletionGuardMiddleware,
    CompletionState,
    artifact_fingerprint,
    completion_ready_for_finalization,
    finalize_accepted_report,
)
from research_agent.document_context import (
    configure_document_tools,
    has_document_context,
)
from research_agent.logger_utils import setup_logger
from research_agent.model_call_guard import (
    ModelCallGuardMiddleware,
    ModelCallTimeoutError,
)
from research_agent.model_factory import create_memory_saver, get_configured_model
from research_agent.research_subagent import (
    RESEARCH_WORKFLOW_INSTRUCTIONS,
    RESEARCHER_INSTRUCTIONS,
    SUBAGENT_DELEGATION_INSTRUCTIONS,
)
from research_agent.research_subagent.clarification.middleware import (
    ClarificationMiddleware,
)
from research_agent.research_subagent.clarification.tool import clarify_requirements
from research_agent.research_subagent.prompts import RESEARCHER_DESCRIPTION
from research_agent.research_subagent.resume.middleware import ResumeMiddleware
from research_agent.research_subagent.resume.policy import (
    inspect_todos,
    is_resume_intent,
)
from research_agent.research_subagent.tools import (
    fetch_webpage_content,
    glob,
    llm_wiki_query,
    ls,
    read_docs_folder,
    read_file,
    tavily_search,
    think_tool,
    write_file,
)
from research_agent.research_subagent.utils.citation_policy import (
    CitationDefect,
    audit_web_citations,
)
from research_agent.research_subagent.utils.cli import (
    build_instruction,
)
from research_agent.research_subagent.utils.eval_tracking import log_server_metrics
from research_agent.research_subagent.utils.knowledge_filesystem import (
    normalize_path_for_filesystem_tools,
)
from research_agent.research_subagent.utils.skill_registry import get_skill_registry
from research_agent.research_subagent.utils.verification import (
    ENABLE_VERIFICATION,
    MAX_VERIFICATION_ROUNDS,
    VerificationVerdict,
    format_feedback,
    verify_report,
)

try:
    from langgraph._internal._constants import (
        CONFIG_KEY_CHECKPOINT_ID,
        CONFIG_KEY_CHECKPOINT_MAP,
        CONFIG_KEY_CHECKPOINT_NS,
        CONFIG_KEY_CHECKPOINTER,
        CONFIG_KEY_RESUMING,
        NS_SEP,
    )
except ImportError:  # pragma: no cover - compatibility with older LangGraph.
    CONFIG_KEY_CHECKPOINT_ID = "checkpoint_id"
    CONFIG_KEY_CHECKPOINT_MAP = "checkpoint_map"
    CONFIG_KEY_CHECKPOINT_NS = "checkpoint_ns"
    CONFIG_KEY_CHECKPOINTER = "__pregel_checkpointer"
    CONFIG_KEY_RESUMING = "__pregel_resuming"
    NS_SEP = "|"

WEB_MODE_HAS_NEW_HUMAN_INPUT = "web_mode_has_new_human_input"

# Load environment variables
load_dotenv()

logger = setup_logger(__name__)

# Create SSL verification setting - CLI flag takes precedence over env var
verify_ssl = get_ssl_verify_config()

# Limits - configurable via environment variables
MAX_CONCURRENT_RESEARCH_UNITS = int(
    os.environ.get("MAX_CONCURRENT_RESEARCH_UNITS", "3")
)
MAX_RESEARCHER_ITERATIONS = int(os.environ.get("MAX_RESEARCHER_ITERATIONS", "3"))

# Evaluation tracking - configurable via environment variables
ENABLE_EVAL_TRACKING = str2bool(os.environ.get("ENABLE_EVAL_TRACKING"), True)
EVAL_HISTORY_FILE = os.environ.get(
    "EVAL_HISTORY_FILE", "./output/eval_history/server_runs.jsonl"
)
EVAL_LOG_QUESTIONS = str2bool(os.environ.get("EVAL_LOG_QUESTIONS"), False)
SYNC_EVAL_LOG_TIMEOUT_SECONDS = 2.0
_SYNC_AWAIT_TIMEOUT = object()
_DEFAULT_CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS = 5.0
_MAX_CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS = 30.0
_CITATION_CHECKPOINT_INITIAL_POLL_SECONDS = 0.005
_CITATION_CHECKPOINT_MAX_POLL_SECONDS = 0.1
_CITATION_CHECKPOINT_READ_TIMEOUT = object()
_CITATION_CHECKPOINT_SYNC_WORKERS = 2
_CITATION_CHECKPOINT_SYNC_MAX_OUTSTANDING = 4
_CITATION_CHECKPOINT_ASYNC_MAX_OUTSTANDING = 4
_CITATION_CHECKPOINT_ASYNC_CANCEL_CLEANUP_SECONDS = 0.05

# Verification loop — post-generation quality review with iterative revision.
# MAX_VERIFICATION_ROUNDS / ENABLE_VERIFICATION are defined in
# research_agent.research_subagent.utils.verification — re-exported here for convenience.


def _verification_is_enabled() -> bool:
    """Return whether verification has at least one configured pass."""
    return ENABLE_VERIFICATION and _normalized_verification_round_limit() > 0


def _citation_checkpoint_confirm_timeout_seconds() -> float:
    """Return safe finite deadline for terminal checkpoint confirmation."""
    raw = os.environ.get("CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS")
    try:
        configured = float(raw) if raw is not None else float("nan")
    except (TypeError, ValueError):
        configured = float("nan")
    if not math.isfinite(configured) or configured <= 0:
        return _DEFAULT_CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS
    return min(configured, _MAX_CITATION_CHECKPOINT_CONFIRM_TIMEOUT_SECONDS)


def _normalized_verification_round_limit() -> int:
    """Return optional judge rounds as a non-negative integer."""
    if (
        isinstance(MAX_VERIFICATION_ROUNDS, int)
        and not isinstance(MAX_VERIFICATION_ROUNDS, bool)
        and MAX_VERIFICATION_ROUNDS > 0
    ):
        return MAX_VERIFICATION_ROUNDS
    return 0


def _citation_correction_limit() -> int:
    """Structural citation enforcement always permits at least one correction."""
    configured = (
        _normalized_verification_round_limit() if ENABLE_VERIFICATION else 0
    )
    return max(configured, 1)


def _citation_corrections_used(state: Mapping[str, Any]) -> int:
    value = state.get("citation_corrections_used")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _build_citation_correction_todo(*, correction: int, limit: int) -> dict[str, str]:
    """Build exact client-visible progress for a structural correction."""
    return {
        "id": "verification_pass",
        "content": f"Citation correction {correction}/{limit} requested",
        "status": "in_progress",
    }


def _format_citation_correction_feedback(
    defects: tuple[CitationDefect, ...],
) -> str:
    """Build bounded correction guidance from fixed defect categories."""
    verdict = VerificationVerdict(
        status="needs_revision",
        sufficiency_score=0.0,
        sufficiency_reason="Citation structure must be corrected.",
        citation_blocking=True,
        citation_defects=defects,
    )
    return format_feedback(verdict)


def _filtered_verification_todos(state: Mapping[str, Any]) -> list[Any]:
    return [
        todo
        for todo in list(state.get("todos") or [])
        if not (
            isinstance(todo, Mapping)
            and todo.get("id") == "verification_pass"
        )
    ]


def _apply_structural_citation_policy(
    *,
    updates: dict[str, Any],
    state: Mapping[str, Any],
    terminal: AIMessage,
    filtered_todos: list[Any],
    report_text: str,
    report_fingerprint: str,
    run_id: str | None,
) -> bool:
    """Apply mandatory structural audit; return whether later checks may run."""
    if state.get("strict_web_citations") is not True:
        return True
    if citation_acceptance_ready(
        state,
        report_fingerprint=report_fingerprint,
        strict_required=True,
    ):
        return True

    audit = audit_web_citations(report_text)
    if not audit.defects:
        updates.update(CITATION_FAILURE_CLEAR_UPDATE)
        updates["citation_accepted_report_fingerprint"] = report_fingerprint
        updates["citation_corrections_used"] = 0
        if len(filtered_todos) != len(list(state.get("todos") or [])):
            updates["todos"] = filtered_todos
        return True

    used = _citation_corrections_used(state)
    limit = _citation_correction_limit()
    updates.update(STRUCTURAL_CITATION_REJECTION_CLEAR_UPDATE)
    if used < limit:
        correction = used + 1
        updates.update(CITATION_FAILURE_CLEAR_UPDATE)
        updates["completion_report_owned_fingerprint"] = report_fingerprint
        updates["citation_corrections_used"] = correction
        updates["verification_feedback"] = _format_citation_correction_feedback(
            audit.defects
        )
        updates["todos"] = filtered_todos + [
            _build_citation_correction_todo(
                correction=correction,
                limit=limit,
            )
        ]
        updates.setdefault("messages", [])
        if isinstance(updates["messages"], list):
            updates["messages"].append(_tag_verification_intermediate(terminal))
        updates["jump_to"] = "model"
        return False

    if run_id is None:
        return False
    updates.update(
        build_citation_failure_update(
            run_id=run_id,
            report_fingerprint=report_fingerprint,
            defects=audit.defects,
            terminal=terminal,
        )
    )
    return False


def _is_legacy_generated_system_message(message: object) -> bool:
    """Identify generated system messages persisted by older checkpoints."""
    if not isinstance(message, SystemMessage) or not isinstance(
        message.content, str
    ):
        return False
    content = message.content.strip()
    return content.startswith("Task configurations:") or (
        content.startswith("<VerificationFeedback>")
        and content.endswith("</VerificationFeedback>")
    )


def _build_verification_todo(
        *, verdict_status: str, verification_round: int, max_rounds: int
) -> dict[str, str]:
    """Build the verification todo shown after one quality-review attempt."""
    attempt = verification_round + 1
    if verdict_status == "needs_revision":
        if attempt < max_rounds:
            content = (
                f"Verification {attempt}/{max_rounds} complete — revision required"
            )
            status = "in_progress"
        else:
            content = (
                f"Verification {attempt}/{max_rounds} complete — "
                "revision limit reached"
            )
            status = "completed"
    else:
        content = f"Verified report quality (round {attempt}/{max_rounds})"
        status = "completed"

    return {
        "id": "verification_pass",
        "content": content,
        "status": status,
    }


def research_todos_complete(todos: object) -> bool:
    """Return whether every non-verification todo is valid and completed."""
    if not isinstance(todos, list):
        return False
    research_todos = [
        todo
        for todo in todos
        if not (
            isinstance(todo, Mapping)
            and todo.get("id") == "verification_pass"
        )
    ]
    if not research_todos:
        return False
    return all(
        isinstance(todo, Mapping)
        and isinstance(todo.get("content"), str)
        and bool(todo["content"].strip())
        and todo.get("status") == "completed"
        for todo in research_todos
    )


def _owned_report_for_verification(
    state: Mapping[str, Any],
) -> tuple[str, str, str] | None:
    """Return current report text/version when this request owns it."""
    generation = state.get("completion_request_generation")
    if (
        not isinstance(generation, str)
        or not generation
        or state.get("completion_plan_owner_generation") != generation
        or state.get("completion_report_owned") is not True
        or not research_todos_complete(state.get("todos"))
    ):
        return None

    files = state.get("files")
    if not isinstance(files, Mapping):
        return None
    report = files.get("/final_report.md")
    if not isinstance(report, Mapping):
        return None
    modified_at = report.get("modified_at")
    if not isinstance(modified_at, str) or not modified_at:
        return None
    try:
        report_text = file_data_to_string(report)  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None
    if not report_text.strip():
        return None
    fingerprint = artifact_fingerprint(report)
    if fingerprint is None:
        return None
    baseline_fingerprint = state.get("completion_report_baseline_fingerprint")
    if isinstance(baseline_fingerprint, str):
        if fingerprint == baseline_fingerprint:
            return None
    elif baseline_fingerprint is not None:
        return None
    elif modified_at == state.get("completion_report_baseline_modified_at"):
        return None
    owned_fingerprint = state.get("completion_report_owned_fingerprint")
    has_fingerprint_ownership = "completion_report_owned_fingerprint" in state
    if has_fingerprint_ownership and owned_fingerprint != fingerprint:
        return None
    already_verified = (
        state.get("completion_verified_report_modified_at") == modified_at
        and state.get("completion_verified_report_fingerprint") == fingerprint
    )
    already_accepted_at_limit = (
        state.get("completion_accepted_at_limit_report_modified_at")
        == modified_at
        and state.get("completion_accepted_at_limit_report_fingerprint")
        == fingerprint
    )
    if (
        already_verified or already_accepted_at_limit
    ) and citation_acceptance_ready(
        state,
        report_fingerprint=fingerprint,
        strict_required=state.get("strict_web_citations") is True,
    ):
        return None
    return report_text, modified_at, fingerprint


def _current_report_fingerprint(state: Mapping[str, Any]) -> str | None:
    files = state.get("files")
    if not isinstance(files, Mapping):
        return None
    return artifact_fingerprint(files.get("/final_report.md"))


def _execution_info_value(runtime: object, key: str) -> object:
    execution_info = getattr(runtime, "execution_info", None)
    if isinstance(execution_info, Mapping):
        return execution_info.get(key)
    return getattr(execution_info, key, None)


def _citation_checkpoint_reader(
    config: Mapping[str, Any],
    runtime: object,
) -> tuple[object, dict[str, Any]] | None:
    """Return saver and exact parent-checkpoint config for current hook task."""
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    checkpointer = configurable.get(CONFIG_KEY_CHECKPOINTER)
    if checkpointer is None:
        return None

    checkpoint_id = _execution_info_value(runtime, "checkpoint_id")
    task_checkpoint_ns = _execution_info_value(runtime, "checkpoint_ns")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        checkpoint_id = configurable.get(CONFIG_KEY_CHECKPOINT_ID)
    if not isinstance(task_checkpoint_ns, str):
        task_checkpoint_ns = configurable.get(CONFIG_KEY_CHECKPOINT_NS)
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        return None
    if not isinstance(task_checkpoint_ns, str):
        return None

    parent_checkpoint_ns = (
        task_checkpoint_ns.rsplit(NS_SEP, 1)[0]
        if NS_SEP in task_checkpoint_ns
        else ""
    )
    checkpoint_map = configurable.get(CONFIG_KEY_CHECKPOINT_MAP)
    if not isinstance(checkpoint_map, Mapping):
        return None
    if checkpoint_map.get(parent_checkpoint_ns) != checkpoint_id:
        return None

    read_config = {
        **config,
        "configurable": {
            **configurable,
            CONFIG_KEY_CHECKPOINT_ID: checkpoint_id,
            CONFIG_KEY_CHECKPOINT_NS: parent_checkpoint_ns,
        },
    }
    return checkpointer, read_config


def _checkpoint_channel_values(checkpoint_tuple: object) -> Mapping[str, Any] | None:
    checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
    if not isinstance(checkpoint, Mapping):
        return None
    values = checkpoint.get("channel_values")
    return values if isinstance(values, Mapping) else None


def _checkpoint_has_matching_failure_writes(
    checkpoint_tuple: object,
    durable_state: Mapping[str, Any],
) -> bool:
    """Confirm one prior task durably wrote all terminal failure metadata."""
    pending_writes = getattr(checkpoint_tuple, "pending_writes", None)
    if not isinstance(pending_writes, (list, tuple)):
        return False
    expected = {
        "citation_failure_run_id": durable_state.get("citation_failure_run_id"),
        "citation_failure_report_fingerprint": durable_state.get(
            "citation_failure_report_fingerprint"
        ),
        "citation_failure_defects": durable_state.get("citation_failure_defects"),
    }
    if any(value in (None, [], "") for value in expected.values()):
        return False
    writes_by_task: dict[str, dict[str, Any]] = {}
    for pending_write in pending_writes:
        if not isinstance(pending_write, (list, tuple)) or len(pending_write) != 3:
            continue
        task_id, channel, value = pending_write
        if not isinstance(task_id, str) or channel not in expected:
            continue
        writes_by_task.setdefault(task_id, {})[channel] = value
    return any(
        all(task_writes.get(channel) == value for channel, value in expected.items())
        for task_writes in writes_by_task.values()
    )


def _checkpoint_parent_config(checkpoint_tuple: object) -> Mapping[str, Any] | None:
    parent_config = getattr(checkpoint_tuple, "parent_config", None)
    return parent_config if isinstance(parent_config, Mapping) else None


class _CitationCheckpointReadWork:
    """One revocable synchronous saver read owned by bounded runtime."""

    def __init__(
        self,
        get_tuple: Callable[[Mapping[str, Any]], object],
        read_config: Mapping[str, Any],
    ) -> None:
        self.get_tuple = get_tuple
        self.read_config = read_config
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._revoked = False
        self._outcome: tuple[bool, object] | None = None

    def revoke(self) -> None:
        """Discard any result delivered after caller deadline."""
        with self._lock:
            self._revoked = True

    def run(self) -> None:
        """Execute unless already revoked, then publish only live delivery."""
        with self._lock:
            if self._revoked:
                self.done.set()
                return
        try:
            outcome = (True, self.get_tuple(self.read_config))
        except BaseException as error:
            outcome = (False, error)
        with self._lock:
            if not self._revoked:
                self._outcome = outcome
            self.done.set()

    def outcome(self) -> tuple[bool, object] | None:
        """Return published outcome, or none after revocation."""
        with self._lock:
            return self._outcome


class _CitationCheckpointSyncRuntime:
    """Fixed daemon workers and bounded queue for blocking saver reads."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self._queue: queue.Queue[_CitationCheckpointReadWork] = queue.Queue(
            maxsize=_CITATION_CHECKPOINT_SYNC_MAX_OUTSTANDING
        )
        self._admission = threading.BoundedSemaphore(
            _CITATION_CHECKPOINT_SYNC_MAX_OUTSTANDING
        )
        self._workers = tuple(
            threading.Thread(
                target=self._worker,
                daemon=True,
                name=f"citation-checkpoint-confirm-{index}",
            )
            for index in range(_CITATION_CHECKPOINT_SYNC_WORKERS)
        )
        for worker in self._workers:
            worker.start()

    def submit(
        self,
        get_tuple: Callable[[Mapping[str, Any]], object],
        read_config: Mapping[str, Any],
    ) -> _CitationCheckpointReadWork | None:
        """Admit work without blocking when process capacity is exhausted."""
        if not self._admission.acquire(blocking=False):
            return None
        work = _CitationCheckpointReadWork(get_tuple, read_config)
        try:
            self._queue.put_nowait(work)
        except queue.Full:
            self._admission.release()
            return None
        return work

    def _worker(self) -> None:
        while True:
            work = self._queue.get()
            try:
                work.run()
            finally:
                self._admission.release()
                self._queue.task_done()


class _CitationCheckpointAsyncAdmission:
    """Process-wide bound held until asynchronous saver work truly ends."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self._semaphore = threading.BoundedSemaphore(
            _CITATION_CHECKPOINT_ASYNC_MAX_OUTSTANDING
        )

    def acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()


_CITATION_CHECKPOINT_RUNTIME_LOCK = threading.Lock()
_citation_checkpoint_sync_runtime: _CitationCheckpointSyncRuntime | None = None
_citation_checkpoint_async_admission: _CitationCheckpointAsyncAdmission | None = None


def _reset_citation_checkpoint_runtime_after_fork() -> None:
    """Drop inherited locks and thread owners in a forked child."""
    global _CITATION_CHECKPOINT_RUNTIME_LOCK
    global _citation_checkpoint_async_admission
    global _citation_checkpoint_sync_runtime
    _CITATION_CHECKPOINT_RUNTIME_LOCK = threading.Lock()
    _citation_checkpoint_sync_runtime = None
    _citation_checkpoint_async_admission = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_citation_checkpoint_runtime_after_fork)


def _get_citation_checkpoint_sync_runtime() -> _CitationCheckpointSyncRuntime:
    global _citation_checkpoint_sync_runtime
    pid = os.getpid()
    with _CITATION_CHECKPOINT_RUNTIME_LOCK:
        runtime = _citation_checkpoint_sync_runtime
        if runtime is None or runtime.pid != pid:
            runtime = _CitationCheckpointSyncRuntime()
            _citation_checkpoint_sync_runtime = runtime
        return runtime


def _get_citation_checkpoint_async_admission() -> _CitationCheckpointAsyncAdmission:
    global _citation_checkpoint_async_admission
    pid = os.getpid()
    with _CITATION_CHECKPOINT_RUNTIME_LOCK:
        admission = _citation_checkpoint_async_admission
        if admission is None or admission.pid != pid:
            admission = _CitationCheckpointAsyncAdmission()
            _citation_checkpoint_async_admission = admission
        return admission


def _get_checkpoint_tuple_before_deadline(
    get_tuple: Callable[[Mapping[str, Any]], object],
    read_config: Mapping[str, Any],
    deadline: float,
) -> object:
    """Run one blocking saver read without exceeding confirmation deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _CITATION_CHECKPOINT_READ_TIMEOUT
    work = _get_citation_checkpoint_sync_runtime().submit(get_tuple, read_config)
    if work is None:
        return _CITATION_CHECKPOINT_READ_TIMEOUT
    if not work.done.wait(remaining):
        work.revoke()
        return _CITATION_CHECKPOINT_READ_TIMEOUT
    outcome = work.outcome()
    if outcome is None:
        return _CITATION_CHECKPOINT_READ_TIMEOUT
    succeeded, value = outcome
    if not succeeded:
        raise value  # type: ignore[misc]
    return value


def _finish_checkpoint_read_task(
    task: asyncio.Future[object],
    admission: _CitationCheckpointAsyncAdmission,
) -> None:
    """Release admission only when task ends and consume late exceptions."""
    if not task.cancelled():
        task.exception()
    admission.release()


async def _bounded_cancel_checkpoint_read_task(
    task: asyncio.Future[object],
) -> None:
    """Request child cancellation and wait briefly without owning its lifetime."""
    task.cancel()
    try:
        await asyncio.wait(
            {task},
            timeout=_CITATION_CHECKPOINT_ASYNC_CANCEL_CLEANUP_SECONDS,
        )
    except asyncio.CancelledError:
        pass


async def _aget_checkpoint_tuple_before_deadline(
    aget_tuple: Callable[[Mapping[str, Any]], Awaitable[object]],
    read_config: Mapping[str, Any],
    deadline: float,
) -> object:
    """Run one asynchronous saver read without exceeding confirmation deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _CITATION_CHECKPOINT_READ_TIMEOUT
    admission = _get_citation_checkpoint_async_admission()
    if not admission.acquire():
        return _CITATION_CHECKPOINT_READ_TIMEOUT
    try:
        read = aget_tuple(read_config)
        task = asyncio.ensure_future(read)
    except BaseException:
        admission.release()
        raise
    task.add_done_callback(
        lambda completed: _finish_checkpoint_read_task(completed, admission)
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=remaining)
    except asyncio.CancelledError:
        await _bounded_cancel_checkpoint_read_task(task)
        raise
    if task not in done:
        task.cancel()
        return _CITATION_CHECKPOINT_READ_TIMEOUT
    return task.result()


def _read_durable_citation_state(
    config: Mapping[str, Any],
    runtime: object,
) -> Mapping[str, Any] | None:
    reader = _citation_checkpoint_reader(config, runtime)
    if reader is None:
        return None
    checkpointer, read_config = reader
    get_tuple = getattr(checkpointer, "get_tuple", None)
    if not callable(get_tuple):
        return None
    deadline = time.monotonic() + _citation_checkpoint_confirm_timeout_seconds()
    poll_seconds = _CITATION_CHECKPOINT_INITIAL_POLL_SECONDS
    while True:
        checkpoint_tuple = _get_checkpoint_tuple_before_deadline(
            get_tuple,
            read_config,
            deadline,
        )
        if checkpoint_tuple is _CITATION_CHECKPOINT_READ_TIMEOUT:
            return None
        if checkpoint_tuple is not None:
            durable_state = _checkpoint_channel_values(checkpoint_tuple)
            parent_config = _checkpoint_parent_config(checkpoint_tuple)
            if durable_state is not None and parent_config is not None:
                parent_tuple = _get_checkpoint_tuple_before_deadline(
                    get_tuple,
                    parent_config,
                    deadline,
                )
                if parent_tuple is _CITATION_CHECKPOINT_READ_TIMEOUT:
                    return None
                if parent_tuple is not None and _checkpoint_has_matching_failure_writes(
                    parent_tuple,
                    durable_state,
                ):
                    return durable_state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_seconds, remaining))
        poll_seconds = min(
            poll_seconds * 2,
            _CITATION_CHECKPOINT_MAX_POLL_SECONDS,
        )


async def _aread_durable_citation_state(
    config: Mapping[str, Any],
    runtime: object,
) -> Mapping[str, Any] | None:
    reader = _citation_checkpoint_reader(config, runtime)
    if reader is None:
        return None
    checkpointer, read_config = reader
    aget_tuple = getattr(checkpointer, "aget_tuple", None)
    if not callable(aget_tuple):
        return None
    deadline = time.monotonic() + _citation_checkpoint_confirm_timeout_seconds()
    poll_seconds = _CITATION_CHECKPOINT_INITIAL_POLL_SECONDS
    while True:
        checkpoint_tuple = await _aget_checkpoint_tuple_before_deadline(
            aget_tuple,
            read_config,
            deadline,
        )
        if checkpoint_tuple is _CITATION_CHECKPOINT_READ_TIMEOUT:
            return None
        if checkpoint_tuple is not None:
            durable_state = _checkpoint_channel_values(checkpoint_tuple)
            parent_config = _checkpoint_parent_config(checkpoint_tuple)
            if durable_state is not None and parent_config is not None:
                parent_tuple = await _aget_checkpoint_tuple_before_deadline(
                    aget_tuple,
                    parent_config,
                    deadline,
                )
                if parent_tuple is _CITATION_CHECKPOINT_READ_TIMEOUT:
                    return None
                if parent_tuple is not None and _checkpoint_has_matching_failure_writes(
                    parent_tuple,
                    durable_state,
                ):
                    return durable_state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(poll_seconds, remaining))
        poll_seconds = min(
            poll_seconds * 2,
            _CITATION_CHECKPOINT_MAX_POLL_SECONDS,
        )


def _verification_round(state: Mapping[str, Any]) -> int:
    value = state.get("verification_round")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _verification_question(
    state: Mapping[str, Any], files: Mapping[str, Any]
) -> str:
    request_file = files.get("/research_request.md")
    if isinstance(request_file, Mapping):
        try:
            request_text = file_data_to_string(  # type: ignore[arg-type]
                request_file
            )
        except (KeyError, TypeError, ValueError):
            request_text = ""
        if request_text.strip():
            return request_text

    human_questions: list[str] = []
    messages = state.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, HumanMessage):
                question = str(message.content)
            elif isinstance(message, Mapping) and message.get("role") == "user":
                question = str(message.get("content", ""))
            else:
                continue
            if question.strip():
                human_questions.append(question)

    generation = state.get("completion_request_generation")
    is_current_generation_resume = (
        isinstance(generation, str)
        and bool(generation)
        and state.get("completion_resume_adopted_generation") == generation
    )
    if is_current_generation_resume:
        expected_hash = state.get("_last_user_msg_hash")
        for question in reversed(human_questions):
            if is_resume_intent(question):
                continue
            if isinstance(expected_hash, str) and expected_hash:
                if hashlib.md5(question.encode()).hexdigest() != expected_hash:
                    continue
            return question
        return ""
    return human_questions[-1] if human_questions else ""


def _tag_verification_intermediate(message: AIMessage) -> AIMessage:
    metadata = {**message.response_metadata, "resume_intermediate": True}
    return message.model_copy(update={"response_metadata": metadata})


def _apply_verification_verdict(
    *,
    updates: dict[str, Any],
    terminal: AIMessage,
    filtered_todos: list[Any],
    verdict: VerificationVerdict,
    verification_round: int,
    report_modified_at: str,
    report_fingerprint: str,
) -> None:
    """Apply one verdict without consuming completion-guard attempts."""
    updates["completion_report_owned_fingerprint"] = report_fingerprint
    updates["todos"] = filtered_todos + [
        _build_verification_todo(
            verdict_status=verdict.status,
            verification_round=verification_round,
            max_rounds=MAX_VERIFICATION_ROUNDS,
        )
    ]
    if verdict.status != "needs_revision":
        updates["verification_round"] = verification_round
        updates["verification_feedback"] = None
        updates["completion_verified_report_modified_at"] = report_modified_at
        updates["completion_verified_report_fingerprint"] = report_fingerprint
        updates["completion_accepted_at_limit_report_modified_at"] = None
        updates["completion_accepted_at_limit_report_fingerprint"] = None
        return

    next_round = verification_round + 1
    updates["verification_round"] = next_round
    if next_round >= MAX_VERIFICATION_ROUNDS:
        updates["verification_feedback"] = None
        updates["completion_accepted_at_limit_report_modified_at"] = (
            report_modified_at
        )
        updates["completion_accepted_at_limit_report_fingerprint"] = (
            report_fingerprint
        )
        updates["completion_verified_report_modified_at"] = None
        updates["completion_verified_report_fingerprint"] = None
        return

    updates["verification_feedback"] = format_feedback(verdict)
    updates.setdefault("messages", [])
    if isinstance(updates["messages"], list):
        updates["messages"].append(_tag_verification_intermediate(terminal))
    updates["jump_to"] = "model"


def _merge_finalization_update(
    updates: dict[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Append accepted report output without replacing other hook updates."""
    effective_state = {**state, **updates}
    finalization = finalize_accepted_report(
        effective_state,
        verification_enabled=_verification_is_enabled(),
    )
    if finalization is None:
        return effective_state

    final_messages = finalization.get("messages")
    if isinstance(final_messages, list):
        updates.setdefault("messages", [])
        if isinstance(updates["messages"], list):
            updates["messages"].extend(final_messages)
    updates["_streamed_files"] = finalization["_streamed_files"]
    return {**state, **updates}


def _run_async_from_sync(
    coroutine_factory: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float | None,
    propagate_cancel: bool = False,
) -> Any:
    """Run a coroutine with a bounded wait, distinguishing timeout from None."""
    result: concurrent.futures.Future[Any] = concurrent.futures.Future()
    ready = threading.Event()
    cancellation_requested = threading.Event()
    control_lock = threading.Lock()
    control: dict[str, Any] = {}

    def run_in_thread() -> None:
        loop = asyncio.new_event_loop()
        task: asyncio.Task[Any] | None = None
        try:
            asyncio.set_event_loop(loop)
            if cancellation_requested.is_set():
                result.set_result(None)
                return
            task = loop.create_task(coroutine_factory())
            with control_lock:
                control["loop"] = loop
                control["task"] = task
            ready.set()
            if cancellation_requested.is_set():
                task.cancel()
            try:
                value = loop.run_until_complete(task)
            except asyncio.CancelledError:
                if propagate_cancel:
                    raise
                value = None
            if not result.done():
                result.set_result(value)
        except BaseException as exc:
            if not result.done():
                result.set_exception(exc)
        finally:
            ready.set()
            if task is not None and not task.done():
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass
            asyncio.set_event_loop(None)
            loop.close()

    caller_context = contextvars.copy_context()
    worker = threading.Thread(
        target=lambda: caller_context.run(run_in_thread),
        name="research-eval-logger",
        daemon=True,
    )
    worker.start()

    def cancel_worker(*, join_timeout: float) -> None:
        """Cancel the coroutine before its worker can start provider work."""
        cancellation_requested.set()
        with control_lock:
            loop = control.get("loop")
            task = control.get("task")
        if isinstance(loop, asyncio.AbstractEventLoop) and isinstance(
            task, asyncio.Task
        ):
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        worker.join(timeout=join_timeout)

    if timeout_seconds is None:
        try:
            ready.wait()
            return result.result()
        except BaseException:
            cancel_worker(join_timeout=0.05)
            raise

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    try:
        ready.wait(timeout=max(deadline - time.monotonic(), 0.0))
        return result.result(timeout=max(deadline - time.monotonic(), 0.0))
    except concurrent.futures.TimeoutError:
        cancel_worker(join_timeout=min(max(timeout_seconds, 0.0), 0.05))
        return _SYNC_AWAIT_TIMEOUT
    except BaseException:
        cancel_worker(join_timeout=0.05)
        raise


# Get current date
current_date = datetime.now().strftime("%Y-%m-%d")

# Initialize dynamic skill registry (use singleton to avoid duplicate initialization)
skill_registry = get_skill_registry()


class ResearchState(CompletionState):
    """Runtime state for the research agent."""

    # Root-owned scalar state remains visible inside declarative subagents,
    # but must not be merged back by parallel task calls.
    doc_folder: Annotated[str | None, OmitFromOutput]
    has_documents: Annotated[bool | None, OmitFromOutput]
    skill: Annotated[str | None, OmitFromOutput]
    no_web: Annotated[NotRequired[bool | None], EphemeralValue(bool | None)]
    effective_no_web: Annotated[NotRequired[bool], OmitFromOutput]
    strict_web_citations: Annotated[NotRequired[bool], OmitFromInput]
    web_mode_run_id: Annotated[NotRequired[str | None], OmitFromInput]
    web_mode_last_human_id: Annotated[NotRequired[str | None], OmitFromInput]
    web_mode_last_human_count: Annotated[NotRequired[int], OmitFromInput]
    web_mode_last_human_fingerprint: Annotated[NotRequired[str | None], OmitFromInput]
    chat_start_time: Annotated[float | None, OmitFromOutput]
    chat_elapsed_seconds: Annotated[float | None, OmitFromOutput]
    _last_user_msg_hash: Annotated[str | None, OmitFromOutput]
    # Multi-pass research (Wave 2: Plan + Execute)
    research_pass: Annotated[int, OmitFromOutput]


def _extract_no_web(user_message: str) -> bool | None:
    """Extract a web-mode directive from one newly supplied human message."""
    message_lower = user_message.lower()

    disable_patterns = [
        r"without\s+web",
        r"no\s+web",
        r"disable\s+web",
        r"offline",
        r"no\s+internet",
        r"no\s+search",
        r"disable\s+search",
        r"--no-web",
        r"-n(?:\s|$)",
    ]
    if any(re.search(pattern, message_lower) for pattern in disable_patterns):
        return True

    enable_patterns = [
        r"with\s+web",
        r"with\s+search",
        r"enable\s+search",
        r"search\s+the\s+web",
    ]
    if any(re.search(pattern, message_lower) for pattern in enable_patterns):
        return False
    return None


class WebModeMiddleware(SkillsMiddleware):
    """Resolve raw web mode before skills middleware writes its first update."""

    def __init__(
            self,
            *,
            backend: StateBackend | None = None,
            sources: list[str] | None = None,
            config_getter: Callable[[], dict[str, Any]] = get_config,
    ) -> None:
        """Create first-stack mode resolver backed by the skills source set."""
        super().__init__(backend=backend or StateBackend(), sources=sources or [])
        self._config_getter = config_getter

    @property
    def name(self) -> str:
        """Replace deepagents' generated skills middleware at stack head."""
        return "SkillsMiddleware"

    @staticmethod
    def _latest_human_marker(
            messages: list,
    ) -> tuple[str | None, int, str | None, str | None]:
        """Return latest human marker and safe content fingerprint."""
        latest_id: str | None = None
        latest_text: str | None = None
        latest_type: str | None = None
        latest_fingerprint: str | None = None
        human_count = 0
        for message in messages:
            is_human = (
                isinstance(message, dict) and message.get("role") == "user"
            ) or (
                hasattr(message, "type") and getattr(message, "type", None) == "human"
            )
            if not is_human:
                continue
            human_count += 1
            if isinstance(message, dict):
                latest_text = str(message.get("content", ""))
                message_id = message.get("id")
            else:
                latest_text = str(getattr(message, "content", ""))
                message_id = getattr(message, "id", None)
            latest_type = (
                str(message.get("role", "user"))
                if isinstance(message, dict)
                else str(getattr(message, "type", type(message).__name__))
            )
            latest_id = (
                message_id.strip()
                if isinstance(message_id, str) and message_id.strip()
                else None
            )
        if latest_text is not None and latest_type is not None:
            latest_fingerprint = hashlib.sha256(
                f"{latest_type}\0{latest_text}".encode()
            ).hexdigest()
        return latest_id, human_count, latest_text, latest_fingerprint

    def _runtime_config(self) -> dict[str, Any]:
        """Read LangGraph's injected config without coupling call sites to it."""
        try:
            config = self._config_getter()
        except RuntimeError:
            return {}
        return dict(config) if isinstance(config, Mapping) else {}

    @staticmethod
    def _is_resuming(config: Mapping[str, Any]) -> bool:
        """Return Pregel's first-step resume flag, with a safe default."""
        configurable = config.get("configurable")
        return bool(
            isinstance(configurable, Mapping)
            and configurable.get(CONFIG_KEY_RESUMING, False)
        )

    @staticmethod
    def _markerless_has_new_human(
            config: Mapping[str, Any], human_count: int
    ) -> bool:
        """Resolve server-provided input freshness before Pregel's fallback."""
        configurable = config.get("configurable")
        signal = (
            configurable.get(WEB_MODE_HAS_NEW_HUMAN_INPUT)
            if isinstance(configurable, Mapping)
            else None
        )
        if isinstance(signal, bool):
            return human_count > 0 and signal
        return human_count > 0 and not WebModeMiddleware._is_resuming(config)

    def _mode_update(self, state: ResearchState) -> dict[str, Any]:
        """Resolve raw input while it is still available at graph entry."""
        latest_id, human_count, latest_text, latest_fingerprint = self._latest_human_marker(
            state.get("messages", [])
        )
        previous_count = state.get("web_mode_last_human_count")
        previous_id = state.get("web_mode_last_human_id")
        previous_fingerprint = state.get("web_mode_last_human_fingerprint")
        config = self._runtime_config()
        missing_markers = (
            previous_count is None
            and previous_id is None
            and previous_fingerprint is None
        )
        if missing_markers:
            # Markerless pre-migration checkpoints use an explicit server
            # signal when reconstructing state, else Pregel's first-step flag.
            # This handles a human-only checkpoint and fresh preloaded files.
            has_new_human = self._markerless_has_new_human(config, human_count)
        elif not isinstance(previous_count, int) or isinstance(previous_count, bool):
            has_new_human = human_count > 0
        elif human_count > previous_count:
            has_new_human = True
        elif human_count < previous_count:
            has_new_human = False
        elif (
                latest_id is not None
                and isinstance(previous_id, str)
                and latest_id != previous_id
        ):
            has_new_human = True
        elif (
                isinstance(previous_fingerprint, str)
                and latest_fingerprint is not None
                and latest_fingerprint != previous_fingerprint
        ):
            has_new_human = True
        else:
            has_new_human = False

        if "no_web" in state:
            effective_no_web = str2bool(state.get("no_web"), False)
        elif has_new_human and latest_text is not None:
            effective_no_web = _extract_no_web(latest_text) is True
        else:
            effective_no_web = False

        run_id = config.get("run_id")
        return {
            "effective_no_web": effective_no_web,
            "strict_web_citations": not effective_no_web,
            "web_mode_run_id": str(run_id) if run_id is not None else None,
            "web_mode_last_human_id": latest_id,
            "web_mode_last_human_count": human_count,
            "web_mode_last_human_fingerprint": latest_fingerprint,
        }

    def before_agent(
            self, state: ResearchState, runtime: Any, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        """Persist effective mode and human marker before resume handling."""
        skill_update = super().before_agent(state, runtime, config or {}) or {}
        return {**skill_update, **self._mode_update(state)}

    async def abefore_agent(
            self, state: ResearchState, runtime: Any, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        """Async counterpart that preserves same first-step mode resolution."""
        skill_update = await super().abefore_agent(state, runtime, config or {}) or {}
        return {**skill_update, **self._mode_update(state)}


class ResearchStateMiddleware(AgentMiddleware):
    """Configure non-web request state before model execution."""

    # Ensure middleware state update are validated against the standard state schema.
    state_schema = ResearchState

    def __init__(
            self,
            *,
            config_getter: Callable[[], dict[str, Any]] = get_config,
    ) -> None:
        """Create middleware with an injectable per-run config source."""
        super().__init__()
        self._config_getter = config_getter

    def _config(self) -> Mapping[str, Any]:
        try:
            config = self._config_getter()
        except RuntimeError:
            return {}
        return config if isinstance(config, Mapping) else {}

    def _is_resume_round(self, state: ResearchState) -> bool:
        try:
            config = self._config_getter()
        except RuntimeError:
            return False
        configurable = config.get("configurable", {})
        return (
                isinstance(configurable, dict)
                and configurable.get("resume_incomplete_todos") is True
                and inspect_todos(state.get("todos")).has_incomplete
        )

    @staticmethod
    def _get_current_user_message(messages: list) -> str | None:
        """Return the content of the **last** user/human message in the list."""
        last_user_content: str | None = None
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_content = str(m.get("content", ""))
            elif hasattr(m, "type") and getattr(m, "type", None) == "human":
                last_user_content = str(getattr(m, "content", ""))
        return last_user_content

    @staticmethod
    def _seed_research_request_file(
            user_message: str | None, state: ResearchState
    ) -> dict[str, Any]:
        """Make the current request available to subagents before the model decides its next step."""
        if not user_message:
            return {}

        existing_files = state.get("files", {})
        existing_request = existing_files.get("/research_request.md")
        if isinstance(existing_request, dict):
            existing_content = "\n".join(existing_request.get("content", []))
            if existing_content == user_message:
                return {}

        return {
            "files": {
                "/research_request.md": create_file_data(user_message),
            }
        }

    def before_agent(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Pre-process the research state and runtime environment before the agent executes.

        Seeds the research request file, emits progress feedback, initialises
        verification state, extracts parameters, and builds the system instruction.
        """
        messages = state.get("messages", [])
        current_user_message = self._get_current_user_message(messages)
        is_resume_round = self._is_resume_round(state)
        if is_resume_round:
            extracted_updates: dict[str, Any] = {}
        else:
            extracted_updates = self._extract_parameters_from_user_input(
                state,
                messages,
            )
        effective_state = {**state, **extracted_updates}

        updates: dict[str, Any] = {}
        if not is_resume_round:
            # Seed the research request file with the latest user message.
            updates.update(
                self._seed_research_request_file(
                    current_user_message,
                    effective_state,
                )
            )
            updates.update(extracted_updates)

        # ── Instant progress feedback ──────────────────────────────────────
        if not is_resume_round:
            status_text = (
                "Searching your uploaded documents for relevant information…"
                if has_document_context(effective_state)
                else "Starting research…"
            )
            updates.setdefault("messages", [])
            if isinstance(updates["messages"], list):
                updates["messages"] = [
                                          AIMessage(content=status_text)
                                      ] + updates["messages"]
            else:
                updates["messages"] = [AIMessage(content=status_text)]

        if not is_resume_round:
            # ── Verification loop state ────────────────────────────────────
            # Track the last user message to detect fresh questions and reset
            # verification state for follow-up turns.
            msg_hash = (
                hashlib.md5((current_user_message or "").encode()).hexdigest()
                if current_user_message
                else ""
            )
            last_hash = state.get("_last_user_msg_hash") or ""
            is_fresh_message = msg_hash and msg_hash != last_hash

            if (
                    "verification_round" not in state
                    or state.get("verification_round") is None
            ):
                updates["verification_round"] = 0
                updates["verification_feedback"] = None
                updates["research_pass"] = 0

            if is_fresh_message:
                updates["verification_round"] = 0
                updates["verification_feedback"] = None
                updates["research_pass"] = 0
                updates["_last_user_msg_hash"] = msg_hash

        # Configure OUTPUT_FOLDER based on extracted doc_folder
        if updates.get("doc_folder") or (
                state.get("doc_folder") and not extracted_updates
        ):
            doc_folder = updates.get("doc_folder") or state.get("doc_folder")
            self._configure_output_folder(doc_folder)
        else:
            self._configure_output_folder(None)

        return updates if updates else None

    def configure_request(self, request: ModelRequest) -> ModelRequest:
        """Inject task configuration into the leading system prompt.

        Task configuration used to be persisted as a ``SystemMessage`` after
        the user's message. Strict Ollama chat templates reject that ordering,
        so configuration is now ephemeral and model-request scoped. Generated
        messages from older checkpoints are removed while their current value
        is rebuilt from state.
        """
        request_state = request.state or {}
        documents_available = has_document_context(request_state)
        instruction = self._build_system_instruction(
            request_state,
            documents_available=documents_available,
        )
        task_configuration = f"Task configurations: \n{instruction}"
        configured_tools = (
            configure_document_tools(request.tools, documents_available)
            if request.tools is not None
            else None
        )

        filtered_messages = [
            message
            for message in request.messages
            if not _is_legacy_generated_system_message(message)
        ]
        messages = (
            request.messages
            if len(filtered_messages) == len(request.messages)
            else filtered_messages
        )

        system_message = request.system_message
        if system_message is None:
            content: str | list[str | dict[str, Any]] = task_configuration
        elif isinstance(system_message.content, str):
            content = (
                f"{system_message.content}\n\n{task_configuration}"
            ).strip()
        else:
            content = [
                *system_message.content,
                {"type": "text", "text": task_configuration},
            ]

        return request.override(
            messages=messages,
            system_message=SystemMessage(content=content),
            tools=configured_tools,
        )

    def wrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], Any],
    ) -> Any:
        """Apply task configuration to a synchronous model request."""
        return handler(self.configure_request(request))

    async def awrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        """Apply task configuration to an asynchronous model request."""
        return await handler(self.configure_request(request))

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Capture chat_start_time before model calls, initializing once per chat."""
        if isinstance(state.get("chat_start_time"), (int, float)):
            return None

        chat_start_time = time.time()
        return {
            "chat_start_time": chat_start_time,
            "chat_elapsed_seconds": None,
            "_eval_logged": False,
        }

    @hook_config(can_jump_to=["model", "end"])
    def after_model(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Calculate chat_elapsed_seconds after each model response and optionally track eval metrics.

        Also handles:
        - Progress messages: when the model issues tool calls, emit a brief
          status message so the user can see what phase the agent is in.
        - Wiki-complete guard: when the wiki already provided a complete answer,
          strip ALL tool calls and inject the wiki answer text as the final
          AIMessage to prevent infinite write_todos / write_file loops.
        """
        chat_start_time = state.get("chat_start_time")
        updates = {}

        # ── Progress messages ──────────────────────────────────────────────
        messages = state.get("messages", [])
        last_msg = messages[-1] if messages else None
        last_tool_calls = getattr(last_msg, "tool_calls", None) or []

        if isinstance(chat_start_time, (int, float)):
            chat_elapsed_seconds = time.time() - chat_start_time
            updates["chat_elapsed_seconds"] = chat_elapsed_seconds

        state_files = state.get("files") or {}

        # ── Post-generation verification hook ────────────────────────────
        # Verify only a completed current-request plan and its owned report.
        # Non-final feedback remains in state and is injected ephemerally by
        # configure_request so strict Ollama templates keep system-first order.
        owned_report = _owned_report_for_verification(state)
        if (
            isinstance(last_msg, AIMessage)
            and not last_tool_calls
            and isinstance(state_files, dict)
            and owned_report is not None
        ):
            report_text, report_modified_at, report_fingerprint = owned_report
            filtered_todos = _filtered_verification_todos(state)
            fallback_run_id = state.get("completion_current_run_id") or state.get(
                "completion_request_generation"
            )
            structural_accepted = _apply_structural_citation_policy(
                updates=updates,
                state=state,
                terminal=last_msg,
                filtered_todos=filtered_todos,
                report_text=report_text,
                report_fingerprint=report_fingerprint,
                run_id=resolve_citation_run_id(
                    self._config(),
                    runtime,
                    fallback=fallback_run_id,
                ),
            )
            verification_round = _verification_round(state)
            if (
                structural_accepted
                and _verification_is_enabled()
                and verification_round < _normalized_verification_round_limit()
            ):
                user_question = _verification_question(state, state_files)

                logger.info(
                    "Verification round %d/%d — reviewing /final_report.md",
                    verification_round + 1,
                    _normalized_verification_round_limit(),
                )

                try:
                    async def _verify():
                        return await verify_report(
                            question=user_question,
                            report=report_text,
                            strict_web_citations=(
                                state.get("strict_web_citations") is True
                            ),
                        )

                    verdict: VerificationVerdict = _run_async_from_sync(
                        _verify,
                        timeout_seconds=None,
                        propagate_cancel=True,
                    )

                    logger.info(
                        "Verification verdict: %s (score=%.2f, "
                        "grounding_failures=%d, gaps=%d)",
                        verdict.status,
                        verdict.sufficiency_score,
                        sum(
                            1
                            for result in verdict.grounding_results
                            if not result.grounded or not result.reachable
                        ),
                        len(verdict.adversarial_gaps),
                    )
                except (
                    ModelCallTimeoutError,
                    ReportCitationError,
                    asyncio.CancelledError,
                ):
                    raise
                except Exception as exc:
                    logger.warning(
                        "Verification check failed: %s. "
                        "Allowing report through without revision.",
                        exc,
                    )
                    verdict = VerificationVerdict(
                        status="complete",
                        sufficiency_score=1.0,
                        sufficiency_reason="",
                        error_message=str(exc),
                    )

                _apply_verification_verdict(
                    updates=updates,
                    terminal=last_msg,
                    filtered_todos=filtered_todos,
                    verdict=verdict,
                    verification_round=verification_round,
                    report_modified_at=report_modified_at,
                    report_fingerprint=report_fingerprint,
                )

        effective_state = {**state, **updates}
        accepted_report_ready = (
            isinstance(last_msg, AIMessage)
            and not last_tool_calls
            and completion_ready_for_finalization(
                effective_state,
                verification_enabled=_verification_is_enabled(),
            )
        )
        if accepted_report_ready:
            effective_state = _merge_finalization_update(updates, state)

        # Optional: Log eval metrics on completion (when graph is done)
        if ENABLE_EVAL_TRACKING and accepted_report_ready:
            files = state.get("files", {})
            if not isinstance(files, dict):
                return updates if updates else None

            # Check if already logged (use .get() with default False since TypedDict doesn't support defaults)
            if (
                not effective_state.get("_eval_logged", False)
                and not effective_state.get("_eval_pending", False)
            ):
                # Calculate runtime
                runtime_seconds = 0.0
                if isinstance(chat_start_time, (int, float)):
                    runtime_seconds = time.time() - chat_start_time

                # Extract data from state
                messages = state.get("messages", [])
                doc_folder = state.get("doc_folder") or os.environ.get(
                    "DOC_FOLDER", "N/A"
                )
                skill = state.get("skill", "research")
                no_web = state.get("effective_no_web", False)
                model_name = os.environ.get(
                    "MODEL_NAME", os.environ.get("AZURE_OPENAI_DEPLOYMENT", "N/A")
                )

                # Get user message as subject (for reference only, not for comparison)
                user_message = None
                for m in messages:
                    if isinstance(m, dict) and m.get("role") == "user":
                        user_message = m.get("content", "")
                        break
                    elif hasattr(m, "type") and getattr(m, "type", None) == "human":
                        user_message = getattr(m, "content", "")
                        break
                subject = user_message

                # ── Privacy redaction ───────────────────────────────────
                # When EVAL_LOG_QUESTIONS is False, redact the subject to
                # protect user privacy while preserving metric aggregation.
                if not EVAL_LOG_QUESTIONS:
                    subject = "[REDACTED]"

                # Build context
                context = {
                    "subject": subject,
                    "skill": skill,
                    "doc_folder": doc_folder,
                    "no_web": no_web,
                }

                try:
                    async def _log_metrics() -> dict[str, Any] | None:
                        return await log_server_metrics(
                            messages=messages,
                            files=files,
                            runtime_seconds=runtime_seconds,
                            model_name=model_name,
                            context=context,
                            history_file=EVAL_HISTORY_FILE,
                        )

                    log_result = _run_async_from_sync(
                        _log_metrics,
                        timeout_seconds=SYNC_EVAL_LOG_TIMEOUT_SECONDS,
                    )
                except Exception as e:
                    logger.error(f"⚠️  Failed metrics logging: {e}")
                else:
                    if log_result is _SYNC_AWAIT_TIMEOUT:
                        updates["_eval_pending"] = True
                        logger.error("⚠️  Metrics logging timed out; write still pending")
                        return updates if updates else None
                    if log_result is None:
                        logger.error("⚠️  Metrics logging returned no success result")
                        return updates if updates else None
                    updates["_eval_logged"] = True
                    logger.info("✅ Metrics logging completed")

        return updates if updates else None

    @hook_config(can_jump_to=["model", "end"])
    async def aafter_model(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Asynchronous version of after_model that runs verification without blocking the main event loop."""
        chat_start_time = state.get("chat_start_time")
        updates = {}

        # ── Progress messages ──────────────────────────────────────────────
        messages = state.get("messages", [])
        last_msg = messages[-1] if messages else None
        last_tool_calls = getattr(last_msg, "tool_calls", None) or []

        if isinstance(chat_start_time, (int, float)):
            chat_elapsed_seconds = time.time() - chat_start_time
            updates["chat_elapsed_seconds"] = chat_elapsed_seconds

        state_files = state.get("files") or {}

        # ── Post-generation verification hook ────────────────────────────
        owned_report = _owned_report_for_verification(state)
        if (
            isinstance(last_msg, AIMessage)
            and not last_tool_calls
            and isinstance(state_files, dict)
            and owned_report is not None
        ):
            report_text, report_modified_at, report_fingerprint = owned_report
            filtered_todos = _filtered_verification_todos(state)
            fallback_run_id = state.get("completion_current_run_id") or state.get(
                "completion_request_generation"
            )
            structural_accepted = _apply_structural_citation_policy(
                updates=updates,
                state=state,
                terminal=last_msg,
                filtered_todos=filtered_todos,
                report_text=report_text,
                report_fingerprint=report_fingerprint,
                run_id=resolve_citation_run_id(
                    self._config(),
                    runtime,
                    fallback=fallback_run_id,
                ),
            )
            verification_round = _verification_round(state)
            if (
                structural_accepted
                and _verification_is_enabled()
                and verification_round < _normalized_verification_round_limit()
            ):
                user_question = _verification_question(state, state_files)

                logger.info(
                    "Verification round %d/%d — reviewing /final_report.md (async)",
                    verification_round + 1,
                    _normalized_verification_round_limit(),
                )

                try:
                    verdict = await verify_report(
                        question=user_question,
                        report=report_text,
                        strict_web_citations=(
                            state.get("strict_web_citations") is True
                        ),
                    )
                    logger.info(
                        "Verification verdict: %s (score=%.2f, "
                        "grounding_failures=%d, gaps=%d)",
                        verdict.status,
                        verdict.sufficiency_score,
                        sum(
                            1
                            for result in verdict.grounding_results
                            if not result.grounded or not result.reachable
                        ),
                        len(verdict.adversarial_gaps),
                    )
                except (
                    ModelCallTimeoutError,
                    ReportCitationError,
                    asyncio.CancelledError,
                ):
                    raise
                except Exception as exc:
                    logger.warning(
                        "Verification check failed: %s. "
                        "Allowing report through without revision.",
                        exc,
                    )
                    verdict = VerificationVerdict(
                        status="complete",
                        sufficiency_score=1.0,
                        sufficiency_reason="",
                        error_message=str(exc),
                    )

                _apply_verification_verdict(
                    updates=updates,
                    terminal=last_msg,
                    filtered_todos=filtered_todos,
                    verdict=verdict,
                    verification_round=verification_round,
                    report_modified_at=report_modified_at,
                    report_fingerprint=report_fingerprint,
                )

        effective_state = {**state, **updates}
        accepted_report_ready = (
            isinstance(last_msg, AIMessage)
            and not last_tool_calls
            and completion_ready_for_finalization(
                effective_state,
                verification_enabled=_verification_is_enabled(),
            )
        )
        if accepted_report_ready:
            effective_state = _merge_finalization_update(updates, state)

        # Optional: Log eval metrics on completion (when graph is done)
        if ENABLE_EVAL_TRACKING and accepted_report_ready:
            files = state.get("files", {})
            if not isinstance(files, dict):
                return updates if updates else None

            if (
                not effective_state.get("_eval_logged", False)
                and not effective_state.get("_eval_pending", False)
            ):
                runtime_seconds = 0.0
                if isinstance(chat_start_time, (int, float)):
                    runtime_seconds = time.time() - chat_start_time

                messages = state.get("messages", [])
                doc_folder = state.get("doc_folder") or os.environ.get(
                    "DOC_FOLDER", "N/A"
                )
                skill = state.get("skill", "research")
                no_web = state.get("effective_no_web", False)
                model_name = os.environ.get(
                    "MODEL_NAME", os.environ.get("AZURE_OPENAI_DEPLOYMENT", "N/A")
                )

                user_message = None
                for m in messages:
                    if isinstance(m, dict) and m.get("role") == "user":
                        user_message = m.get("content", "")
                        break
                    elif hasattr(m, "type") and getattr(m, "type", None) == "human":
                        user_message = getattr(m, "content", "")
                        break
                subject = user_message

                if not EVAL_LOG_QUESTIONS:
                    subject = "[REDACTED]"

                context = {
                    "subject": subject,
                    "skill": skill,
                    "doc_folder": doc_folder,
                    "no_web": no_web,
                }

                try:
                    log_result = await log_server_metrics(
                        messages=messages,
                        files=files,
                        runtime_seconds=runtime_seconds,
                        model_name=model_name,
                        context=context,
                        history_file=EVAL_HISTORY_FILE,
                    )
                    logger.info("✅ Metrics logging completed (async)")
                except Exception as e:
                    logger.error(f"⚠️  Failed metrics logging: {e}")
                else:
                    if log_result is None:
                        logger.error("⚠️  Metrics logging returned no success result")
                    else:
                        updates["_eval_logged"] = True

        return updates if updates else None

    def after_agent(
        self,
        state: ResearchState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Raise only after saver confirms current failure checkpoint."""
        config = self._config()
        fingerprint = _current_report_fingerprint(state)
        fallback_run_id = state.get("completion_current_run_id") or state.get(
            "completion_request_generation"
        )
        run_id = resolve_citation_run_id(
            config,
            runtime,
            fallback=fallback_run_id,
        )
        if not citation_failure_is_current(
            state,
            run_id=run_id,
            report_fingerprint=fingerprint,
        ):
            return None
        durable_state = _read_durable_citation_state(config, runtime)
        if durable_state is None:
            return None
        durable_fingerprint = durable_state.get(
            "completion_report_owned_fingerprint"
        )
        durable_fallback_run_id = durable_state.get(
            "completion_current_run_id"
        ) or durable_state.get("completion_request_generation")
        raise_current_citation_failure(
            durable_state,
            run_id=resolve_citation_run_id(
                config,
                runtime,
                fallback=durable_fallback_run_id,
            ),
            report_fingerprint=durable_fingerprint,
        )
        return None

    async def aafter_agent(
        self,
        state: ResearchState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Raise only after async saver confirms current failure checkpoint."""
        config = self._config()
        fingerprint = _current_report_fingerprint(state)
        fallback_run_id = state.get("completion_current_run_id") or state.get(
            "completion_request_generation"
        )
        run_id = resolve_citation_run_id(
            config,
            runtime,
            fallback=fallback_run_id,
        )
        if not citation_failure_is_current(
            state,
            run_id=run_id,
            report_fingerprint=fingerprint,
        ):
            return None
        durable_state = await _aread_durable_citation_state(config, runtime)
        if durable_state is None:
            return None
        durable_fingerprint = durable_state.get(
            "completion_report_owned_fingerprint"
        )
        durable_fallback_run_id = durable_state.get(
            "completion_current_run_id"
        ) or durable_state.get("completion_request_generation")
        raise_current_citation_failure(
            durable_state,
            run_id=resolve_citation_run_id(
                config,
                runtime,
                fallback=durable_fallback_run_id,
            ),
            report_fingerprint=durable_fingerprint,
        )
        return None

    def _extract_parameters_from_user_input(
            self, state: ResearchState, messages: list
    ) -> dict[str, Any]:
        """Extract doc_folder and skill from the **latest** user message.

        Parameters are always re-extracted from the most recent user message so
        that follow-up requests (e.g. switching skills mid-conversation) are
        honoured.  If the latest message does not mention a parameter, the
        existing state value is preserved (we simply omit it from ``updates``).
        """
        # Find the LAST user message (not the first) so follow-ups are picked up.
        user_message = None
        for m in messages:
            # Handle dictionary messages
            if isinstance(m, dict):
                if m.get("role") == "user":
                    user_message = m.get("content")
            # Handle LangChain message objects (not SystemMessage)
            elif hasattr(m, "content") and not isinstance(m, SystemMessage):
                if hasattr(m, "type") and m.type == "human":
                    user_message = m.content
                elif not hasattr(m, "type"):
                    user_message = m.content

        if not user_message:
            return {}

        user_message = str(user_message)
        updates = {}

        # Extract doc_folder — only if not already set (doc_folder rarely changes)
        if not state.get("doc_folder"):
            updates["doc_folder"] = self._extract_doc_folder(user_message)

        # Always attempt skill extraction from the latest message so users can
        # switch skills mid-conversation (e.g. "use humanizer skill").
        extracted_skill = self._extract_skill(user_message)
        if extracted_skill:
            updates["skill"] = extracted_skill

        # Remove None values from updates
        return {k: v for k, v in updates.items() if v is not None}

    @staticmethod
    def _configure_output_folder(doc_folder: str | None) -> None:
        """Configure OUTPUT_FOLDER and DOC_FOLDER environment variables.

        DOC_FOLDER is persisted as an env var so that subagent state schemas
        (which may not include ``doc_folder``) can still access it as a
        fallback inside ``read_doc_folder``.
        """
        reports_output_folder = os.environ.get("REPORTS_OUTPUT_FOLDER", "./output")
        if not doc_folder:
            output_folder = reports_output_folder
        else:
            output_folder = str(Path(reports_output_folder) / Path(doc_folder).name)

        # Normalize path for deepagents filesystem tools compatibility (cross-platform)
        normalized_path = normalize_path_for_filesystem_tools(output_folder)
        os.environ["OUTPUT_FOLDER"] = normalized_path

        # Persist doc_folder so read_doc_folder can fall back to it inside
        # subagents whose state schema doesn't carry the key.
        if doc_folder:
            os.environ["DOC_FOLDER"] = doc_folder
        else:
            os.environ.pop("DOC_FOLDER", None)

    @staticmethod
    def _extract_doc_folder(user_message: str) -> str | None:
        """Extract doc_folder from user message patterns and verify it exists."""
        potential_path: str | None = None

        # Look for --doc-folder pattern
        doc_match = re.search(r"--doc-folder\s+['\"]?([^\s'\"]+)['\"]?", user_message)
        if doc_match:
            # Normalize Windows backslashes to forward slashes
            potential_path = doc_match.group(1).replace("\\", "/")

        if not potential_path:
            # Look for path patterns like ./docs/policy/ or .\docs\policy\ or quoted paths
            path_match = re.search(r"['\"](\.[/\\][^'\"]+)['\"]", user_message)
            if path_match:
                p = path_match.group(1).replace("\\", "/")
                if "doc" in p.lower() or "policy" in p.lower() or "folder" in p.lower():
                    potential_path = p

        if not potential_path:
            # Look for unquoted paths that contain common documents folder names
            # Matches ./path/to/dir, /path/to/dir, or path/to/dir
            unquoted_match = re.search(
                r"((?:\.?/)?[\\w/.-]+(?:[/\\][\\w/.-]+)+)", user_message
            )
            if unquoted_match:
                p = unquoted_match.group(1).replace("\\", "/")
                if any(
                        keyword in p.lower()
                        for keyword in ["doc", "policy", "data", "input", "file"]
                ):
                    potential_path = p

        if not potential_path:
            return None

        # Verify the path exists; if not, check if it's inside 'deep_research'
        path = Path(potential_path)
        if not path.exists():
            # Try to prefix with deep_research if not already
            if not potential_path.startswith(
                    "./deep_research/"
            ) and not potential_path.startswith("deep_research/"):
                deep_path = Path("deep_research") / potential_path.lstrip("./")
                if deep_path.exists():
                    return str(deep_path)

        return potential_path

    @staticmethod
    def _extract_skill(user_message: str) -> str | None:
        """Extract skill from user message patterns using dynamic skill registry."""
        # Look for --skill pattern
        skill_match = re.search(r"--skill\s+([^\s]+)", user_message)
        if skill_match:
            return skill_match.group(1)

        message_lower = user_message.lower()

        # Combine legacy and migrated skill IDs for direct matching
        all_skill_ids = list(skill_registry.skill_ids) + list(skill_registry.SKILL_IDS)
        # Direct skill-id match: check if any skill ID appears in the user
        # message (e.g. "use humanizer skill" contains "humanizer").
        # Prefer longer IDs first to avoid partial matches.
        for sid in sorted(all_skill_ids, key=len, reverse=True):
            if sid in message_lower:
                return sid

        # Fallback: use skill registry keyword / description matching
        # (legacy skills only — migrated skills have no keyword lists)
        matches = skill_registry.find_skills_by_keyword(message_lower)
        if matches:
            # Return the first match (most relevant based on keyword priority)
            return matches[0].skill_id

        return None

    @staticmethod
    def _extract_no_web(user_message: str) -> bool | None:
        """Backward-compatible wrapper for shared web-mode extraction."""
        return _extract_no_web(user_message)

    @staticmethod
    def _build_system_instruction(
            state: ResearchState, *, documents_available: bool | None = None
    ) -> str:
        """Build system instruction from ResearchState parameters.

        Appends a *State Context* block so the agent knows what files are
        already available.  This is the general mechanism that lets any skill
        work correctly in follow-up turns — the agent can decide the right
        workflow for any skill (post-process, extend, or start fresh).
        """
        if documents_available is None:
            documents_available = has_document_context(state)
        no_web = str2bool(state.get("effective_no_web"), False)
        instruction = build_instruction(
            subject="",
            doc_folder=state.get("doc_folder") if documents_available else None,
            skill=state.get("skill"),
            no_web=no_web and documents_available,
        )
        instruction = instruction.replace(
            "Research the following subject: ", ""
        ).strip()

        # --- Structured research plan directive ---
        instruction += (
            "\n\n<PlanDirective>"
            "\nBefore delegating to sub-agents, create a structured research plan "
            "using `write_todos`. For each research question include:"
            "\n1. What specific information is needed to answer it."
            "\n2. Success criteria: 3+ credible sources per major claim, coverage of "
            "all sub-questions, specific data points where applicable."
            "\n3. Which sub-agent(s) will address each information need."
            "\n\nAfter receiving sub-agent results, compare findings against the "
            "success criteria. If criteria are not met, identify remaining gaps "
            "and launch targeted follow-up sub-agent tasks to fill them before "
            "synthesizing the final report."
            "\n</PlanDirective>"
        )

        # --- Verification feedback injection ---
        # When a prior verification pass found issues, surface the structured
        # feedback in the system instruction so the model sees it on every
        # iteration of the revision loop.
        verification_feedback = state.get("verification_feedback")
        if verification_feedback:
            instruction += "\n\n" + verification_feedback

        # --- General state context ---
        # Tell the agent what files already exist so it can decide the right
        # workflow for any skill (post-process, extend, or start fresh).
        files = state.get("files") or {}
        if files:
            file_list = ", ".join(f"`{f}`" for f in sorted(files.keys()))
            instruction += (
                "\n\n<State Context>"
                f"\nFiles already available from prior turns: {file_list}"
                "\nIf the user's request refers to existing content (e.g. 'review', "
                "'rewrite', 'improve', 'humanize'), use `read_file` to load the "
                "relevant file first, then apply the requested skill or changes, "
                "then use `write_file` to save the result."
                "\n</State Context>"
            )

        if documents_available:
            instruction += (
                "\n\n<Source Guidance>"
                "\nUploaded sources are available. Use `llm_wiki_query` or "
                "`read_docs_folder` to ground relevant claims in those sources."
                "\n</Source Guidance>"
            )
        elif no_web:
            instruction += (
                "\n\n<Source Guidance>"
                "\nNeither uploaded documents nor web research is available. Do not "
                "invent facts or use `llm_wiki_query`, `read_docs_folder`, "
                "`tavily_search`, or `fetch_webpage_content`. You may still use "
                "workflow and output tools such as `write_todos`, `write_file`, "
                "and applicable `read_file`. Clearly report this source constraint "
                "to the user."
                "\n</Source Guidance>"
            )
        else:
            instruction += (
                "\n\n<Source Guidance>"
                "\nNo uploaded document sources are available. Do not call "
                "`llm_wiki_query` or `read_docs_folder`. Use `task` with "
                '`subagent_type="research-agent"` for web research.'
                "\n</Source Guidance>"
            )

        return instruction


# Combine orchestrator instructions (RESEARCHER_INSTRUCTIONS only for sub-agents)
INSTRUCTIONS = (
        RESEARCH_WORKFLOW_INSTRUCTIONS
        + "\n\n"
        + "=" * 80
        + "\n\n"
        + SUBAGENT_DELEGATION_INSTRUCTIONS.format(
    max_concurrent_research_units=MAX_CONCURRENT_RESEARCH_UNITS,
    max_researcher_iterations=MAX_RESEARCHER_ITERATIONS,
)
)

try:
    model = get_configured_model()
except Exception as e:
    logger.critical(f"CRITICAL ERROR INITIALIZING MODEL: {e}", exc_info=True)
    traceback.print_exc()
    with open("/deps/deep_research/FATAL_ERROR.log", "w") as f:
        f.write("CRITICAL ERROR: get_configured_model() failed!\n")
        f.write(traceback.format_exc())
    time.sleep(
        15
    )  # Give App Runner 15 seconds to flush the logs to CloudWatch before exiting
    raise

# Create explicit guarded subagents. Research stays web-only; general purpose
# inherits the root tools by intentionally omitting its ``tools`` key.
research_sub_agent: SubAgent = {
    "name": "research-agent",
    "description": RESEARCHER_DESCRIPTION,
    "system_prompt": RESEARCHER_INSTRUCTIONS.format(
        date=current_date,
    ),
    "tools": [
        tavily_search,
        fetch_webpage_content,
        think_tool,
    ],
    "model": model,
    "middleware": [
        ModelCallGuardMiddleware(policy=model._model_call_policy),
    ],
}
general_purpose_sub_agent: SubAgent = {
    **GENERAL_PURPOSE_SUBAGENT,
    "model": model,
    "middleware": [
        ModelCallGuardMiddleware(policy=model._model_call_policy),
    ],
}
# Recursion limit - configurable via environment variable (applied at graph compile time)
RECURSION_LIMIT = int(os.environ.get("GRAPH_RECURSION_LIMIT", "200"))

# Create the agent
# Orchestrator owns documents/filesystem tools.
# Web discovery can still be delegated to `research-agent` via task().
# The `skills` parameter auto-creates SkillsMiddleware backed by the agent's
# internal FilesystemBackend — all skills live in .deepagents/skills/.
# The checkpointer provides persistent state per thread_id — configurable via
# MEMORY_TYPE env var (memory|sqlite|postgres|cosmosdb).
# When unset (default for langgraph dev / LangGraph Platform), the graph is
# created without a checkpointer and the platform injects its own persistence.
checkpointer = create_memory_saver()
backend = StateBackend()
SKILL_SOURCES = [
    ".deepagents/skills/",
    "./doc/.deepagents/skills/",
    "./docs/.deepagents/skills/",
]
_agent_kwargs: dict[str, Any] = dict(
    model=model,
    backend=backend,
    state_schema=ResearchState,
    tools=[
        clarify_requirements,
        think_tool,
        read_file,
        write_file,
        ls,
        glob,
        read_docs_folder,
        llm_wiki_query,
    ],
    system_prompt=INSTRUCTIONS,
    subagents=[research_sub_agent, general_purpose_sub_agent],
    middleware=[
        WebModeMiddleware(backend=backend, sources=SKILL_SOURCES),
        TodoListMiddleware(system_prompt=""),
        ClarificationMiddleware(),
        CompletionGuardMiddleware(),
        ResumeMiddleware(),
        ResearchStateMiddleware(),
        ModelCallGuardMiddleware(policy=model._model_call_policy),
    ],
    skills=SKILL_SOURCES,
)
if checkpointer is not None:
    _agent_kwargs["checkpointer"] = checkpointer

agent = create_deep_agent(**_agent_kwargs).with_config(
    RunnableConfig(recursion_limit=RECURSION_LIMIT)
)
