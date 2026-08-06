"""DEPRECATED — Custom LangGraph Platform API server.

⚠️  This file is deprecated.  Use the official LangGraph Platform server instead::

        langgraph dev

    The official server provides the full LangGraph Platform API surface
    (threads, runs, SSE streaming, checkpoint-based persistence, Studio UI)
    that this custom implementation attempted to replicate.  After extensive
    testing we found that the official platform is the only reliable way to
    serve a LangGraph agent to the ``@langchain/langgraph-sdk`` client.

    This file is kept for reference only and will be removed in a future release.

    For documents uploads (port 8000), run::

        uv run python -m webapp
"""

from __future__ import annotations

import os

# ── Before any project imports: ensure MEMORY_TYPE is set ─────────────────
# When running under langgraph dev / LangGraph Platform the env var is NOT
# set and the platform provides its own persistence.  When running via our
# custom server.py we default to InMemorySaver so that state survives within
# the process lifetime.
if not os.environ.get("MEMORY_TYPE", "").strip():
    os.environ["MEMORY_TYPE"] = "memory"

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph_sdk import Auth
from pydantic import BaseModel, Field

# Import DB wrapper
from research_agent import db

# Import the actual deep_research agent
from research_agent.agent import RECURSION_LIMIT, agent

# Import shared authentication logic
from research_agent.auth import authenticate_credential
from research_agent.research_subagent.prompts import RESEARCHER_DESCRIPTION
from research_agent.research_subagent.resume import (
    build_round_limit_message,
    get_max_resume_rounds,
    inspect_todos,
    is_resume_intent,
    visible_messages,
)

# Import the existing app and settings from webapp
from webapp import app

# Track active background tasks to allow cancellation
_active_tasks: dict[str, asyncio.Task] = {}
# Lock to synchronize task modification operations
_task_lock = asyncio.Lock()


def custom_openapi() -> dict[str, Any]:
    """Build an explicit OpenAPI documents for the async subagent server."""
    if app.openapi_schema:
        return app.openapi_schema

    app.openapi_schema = get_openapi(
        title="Deep Research Async Subagent API",
        version=os.environ.get("SERVER_API_VERSION", "1.0.0"),
        description=(
            "Async subagent server for Deep Research. "
            "Includes thread/run lifecycle endpoints, upload API, and auth-protected operations."
        ),
        routes=app.routes,
        tags=[
            {"name": "Health", "description": "Service health endpoints."},
            {"name": "Assistants", "description": "Assistant discovery and metadata endpoints."},
            {"name": "Threads", "description": "Thread lifecycle and state endpoints."},
            {"name": "Runs", "description": "Background run execution and cancellation endpoints."},
            {"name": "Documents", "description": "Document upload and management endpoints."},
            {"name": "Wiki",
             "description": "Thread-level wiki knowledge base management (ingest, query, lint, progress)."},
            {"name": "Auth", "description": "Authentication and authorization endpoints."},
        ],
    )
    return app.openapi_schema


app.openapi = custom_openapi


# ── Pydantic Request/Response Models ──────────────────────────────────────────

class MessagePayload(BaseModel):
    """DEPRECATED. Message payload for the legacy custom server."""

    role: str
    content: str
    name: str | None = None


class RunInputPayload(BaseModel):
    """DEPRECATED. Run input payload for the legacy custom server."""

    messages: list[MessagePayload] = Field(default_factory=list)


class RunCreateRequest(BaseModel):
    """DEPRECATED. Run creation request for the legacy custom server."""

    assistant_id: str = "researcher"
    input: RunInputPayload = Field(default_factory=RunInputPayload)
    multitask_strategy: str | None = None


class ThreadCreateRequest(BaseModel):
    """DEPRECATED. Thread creation request for the legacy custom server."""

    thread_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    if_exists: str = "raise"


class ThreadSearchRequest(BaseModel):
    """DEPRECATED. Thread search request for the legacy custom server."""

    limit: int = 10
    offset: int = 0
    sort_by: str = "updated_at"
    sort_order: str = "desc"
    status: str | None = None
    metadata: dict[str, Any] | None = None


class ThreadPatchRequest(BaseModel):
    """DEPRECATED. Thread patch request for the legacy custom server."""

    metadata: dict[str, Any] = Field(default_factory=dict)


class ThreadStateUpdateRequest(BaseModel):
    """DEPRECATED. Thread state update request for the legacy custom server."""

    values: dict[str, Any] | list[Any] | None = None


class AssistantSearchRequest(BaseModel):
    """DEPRECATED. Assistant search request for the legacy custom server."""

    limit: int = 10
    offset: int = 0
    graph_id: str | None = None
    assistant_id: str | None = None


class ThreadHistoryRequest(BaseModel):
    """DEPRECATED. Thread history request for the legacy custom server."""

    limit: int = 10
    before: str | None = None
    metadata: dict[str, Any] | None = None


class RunStreamRequest(BaseModel):
    """DEPRECATED. Run stream request for the legacy custom server."""

    assistant_id: str = "researcher"
    input: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    config: dict[str, Any] | None = None
    stream_mode: list[str] | None = None
    stream_resumable: bool | None = None
    on_disconnect: str | None = None
    multitask_strategy: str | None = None


class AssistantResponse(BaseModel):
    """DEPRECATED. Assistant response model for the legacy custom server."""

    id: str
    name: str
    description: str
    model: str | None = None
    instructions: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _sse_frame(event: str, data: Any, event_id: int | None = None) -> str:
    payload = json.dumps(data, default=str)
    id_part = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_part}event: {event}\ndata: {payload}\n\n"


def _public_state_values(raw_values: Any) -> dict[str, Any]:
    """Copy state for public output, hiding private keys and messages."""
    if not isinstance(raw_values, Mapping):
        return {}

    values = {
        key: value
        for key, value in raw_values.items()
        if not (isinstance(key, str) and key.startswith("_"))
    }
    if "messages" in values:
        raw_messages = values.get("messages")
        messages = list(raw_messages) if isinstance(raw_messages, (list, tuple)) else []
        values["messages"] = _serialize_visible_messages(messages)
    return values


def _api_thread(thread: dict[str, Any]) -> dict[str, Any]:
    raw_values = thread.get("values")
    return {
        "thread_id": thread.get("thread_id"),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at") or thread.get("created_at"),
        "state_updated_at": thread.get("state_updated_at"),
        "metadata": thread.get("metadata") or {},
        "status": thread.get("status") or "idle",
        "config": thread.get("config") or {},
        "values": (
            _public_state_values(raw_values)
            if isinstance(raw_values, Mapping)
            else None
        ),
    }


def _map_run_status_for_api(status: str | None) -> str:
    # Keep API-compatible enum for clients expecting interrupted rather than cancelled.
    if status == "cancelled":
        return "interrupted"
    return status or "pending"


def _last_user_entry(raw_messages: Any) -> tuple[str, str] | None:
    """Return normalized role and content for the request's triggering message."""
    if not isinstance(raw_messages, list):
        return None

    for raw_message in reversed(raw_messages):
        if isinstance(raw_message, dict):
            role = raw_message.get("role")
            content = raw_message.get("content")
        else:
            role = getattr(raw_message, "role", None)
            content = getattr(raw_message, "content", None)

        if not isinstance(role, str) or role.strip().lower() not in {"user", "human"}:
            continue
        if not isinstance(content, str):
            return None
        return role.strip().lower(), content

    return None


def _last_user_message(raw_messages: Any) -> str:
    """Return last valid user message content from one request message list."""
    entry = _last_user_entry(raw_messages)
    return entry[1] if entry is not None else ""


def _api_run(run: dict[str, Any]) -> dict[str, Any]:
    raw_kwargs = run.get("kwargs") or {}
    public_kwargs = {
        key: value
        for key, value in raw_kwargs.items()
        if not (isinstance(key, str) and key.startswith("_"))
    } if isinstance(raw_kwargs, dict) else {}

    return {
        "run_id": run.get("run_id"),
        "thread_id": run.get("thread_id"),
        "assistant_id": run.get("assistant_id"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at") or run.get("created_at"),
        "status": _map_run_status_for_api(run.get("status")),
        "metadata": run.get("metadata") or {},
        "kwargs": public_kwargs,
        "multitask_strategy": run.get("multitask_strategy") or "enqueue",
        "error": run.get("error"),
    }


def _list_assistants(*, limit: int, offset: int, graph_id: str | None = None, assistant_id: str | None = None) -> list[
    AssistantResponse]:
    assistants = [
        AssistantResponse(
            id="researcher",
            name="Research Assistant",
            description=RESEARCHER_DESCRIPTION or "Deep research agent for comprehensive multi-source information gathering and analysis.",
            model=os.environ.get("MODEL_NAME", "unknown"),
            created_at=None,
            updated_at=None,
            metadata={},
        )
    ]

    selected_id = assistant_id or graph_id
    if selected_id:
        assistants = [a for a in assistants if a.id == selected_id]

    safe_limit = max(1, min(int(limit or 10), 100))
    safe_offset = max(0, int(offset or 0))
    return assistants[safe_offset: safe_offset + safe_limit]


def _build_thread_history_item(thread: dict[str, Any]) -> dict[str, Any]:
    checkpoint_time = thread.get("state_updated_at") or thread.get("updated_at") or thread.get("created_at")
    checkpoint_id = str(checkpoint_time or uuid.uuid4())

    return {
        "config": {
            "configurable": {
                "thread_id": thread.get("thread_id"),
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            },
        },
        "checkpoint": {
            "thread_id": thread.get("thread_id"),
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        },
        "values": _public_state_values(thread.get("values")),
        "metadata": thread.get("metadata") or {},
        "created_at": checkpoint_time,
        "next": [],
        "tasks": [],
    }


async def _resolve_thread_history(
        thread_id: str, *, limit: int, before: str | None = None
) -> list[dict[str, Any]]:
    """Collect checkpoint history from the LangGraph checkpointer.

    Falls back to the single snapshot stored in the custom DB when the
    checkpointer does not support history listing (or has no checkpoints).
    """
    config = {"configurable": {"thread_id": thread_id}}
    items: list[dict[str, Any]] = []

    try:
        cp = getattr(agent, "checkpointer", None)
        if cp is not None and hasattr(cp, "alist"):
            kwargs: dict[str, Any] = {"config": config, "limit": limit}
            if before:
                kwargs["before"] = {"configurable": {"thread_id": thread_id, "checkpoint_id": before}}
            async for checkpoint in cp.alist(**kwargs):
                cpt_config = checkpoint.get("config", {}).get("configurable", {})
                raw_values = (
                        checkpoint.get("values")
                        or checkpoint.get("channel_values")
                )
                values = _public_state_values(raw_values)
                items.append({
                    "config": {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": cpt_config.get("checkpoint_ns", ""),
                            "checkpoint_id": cpt_config.get("checkpoint_id", ""),
                        },
                    },
                    "checkpoint": {
                        "thread_id": thread_id,
                        "checkpoint_ns": cpt_config.get("checkpoint_ns", ""),
                        "checkpoint_id": cpt_config.get("checkpoint_id", ""),
                    },
                    "values": values or {},
                    "metadata": checkpoint.get("metadata", {}),
                    "created_at": checkpoint.get("created_at"),
                    "next": [],
                    "tasks": [],
                })
    except Exception:
        items = []

    if items:
        return items[:limit]

    # Fallback: return single DB snapshot
    thread = db.get_thread(thread_id)
    return [_build_thread_history_item(thread)] if thread else []


# ── Security Authentication ───────────────────────────────────────────────────

async def get_current_user(request: Request) -> Auth.types.MinimalUserDict:
    """Authenticate requests using API key or OAuth session token (matching auth.py logic)."""
    # Check for test mode bypass
    if os.environ.get("ALLOW_ALL_THREADS", "").lower() == "true":
        return {"identity": "test-admin", "display_name": "Test Admin"}

    headers = request.headers
    api_key = headers.get("x-api-key") or headers.get("X-API-Key")

    if not api_key:
        auth_header = headers.get("authorization") or headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            api_key = auth_header[7:]

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing authentication. Please provide 'x-api-key', 'Authorization: Bearer', or OAuth session token."
        )

    return authenticate_credential(api_key)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_thread_with_auth(thread_id: str, current_user: Auth.types.MinimalUserDict) -> dict[str, Any]:
    thread = db.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Access control: threads are only accessible by their owner, admins, or if ALLOW_ALL_THREADS=true
    if os.environ.get("ALLOW_ALL_THREADS", "").lower() == "true":
        pass
    elif current_user["identity"] != "admin" and thread.get("user_id") and thread.get("user_id") != current_user[
        "identity"]:
        raise HTTPException(status_code=403, detail="Forbidden: You do not own this thread")

    return thread


def _stable_message_id(message: Any) -> str:
    """Build a repeatable public ID for a checkpoint message lacking one."""
    if isinstance(message, Mapping):
        message_type = message.get("type") or message.get("role")
        content = message.get("content")
        name = message.get("name")
        tool_call_id = message.get("tool_call_id")
        tool_calls = message.get("tool_calls")
    else:
        message_type = getattr(message, "type", None)
        content = getattr(message, "content", None)
        name = getattr(message, "name", None)
        tool_call_id = getattr(message, "tool_call_id", None)
        tool_calls = getattr(message, "tool_calls", None)
    fingerprint = json.dumps(
        [message_type, content, name, tool_call_id, tool_calls],
        sort_keys=True,
        default=str,
    )
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"deep-research-message:{fingerprint}",
        )
    )


def serialize_message(m: Any) -> dict[str, Any]:
    """Convert a LangChain message object or a dictionary to the standard
    LangGraph Platform serializable format understood by @langchain/langgraph-sdk.

    Matches the serialization produced by ``langgraph dev`` so the SDK's
    ``useStream()`` hook sees identical message shapes whether served from
    the platform or from our custom ``server.py``.

    Includes all fields the platform emits: ``additional_kwargs``,
    ``response_metadata``, ``tool_calls``, ``invalid_tool_calls``,
    ``usage_metadata``, ``tool_call_id``, ``artifact``, ``status``, ``name``.
    """
    # ── Dict-based messages (from DB or client) ──────────────────────────
    if isinstance(m, dict):
        out = dict(m)
        # Normalise type field
        if "role" in out and "type" not in out:
            out["type"] = out.pop("role")
        msg_type = out.get("type", "user")
        if msg_type == "user":
            out["type"] = "human"
        elif msg_type == "assistant" or msg_type == "AIMessage":
            out["type"] = "ai"
        # Ensure unique id
        if "id" not in out or not out["id"]:
            out["id"] = _stable_message_id(m)
        # Fill in missing LangChain serialization fields with defaults
        out.setdefault("name", None)
        out.setdefault("additional_kwargs", {})
        out.setdefault("response_metadata", {})
        if out["type"] == "ai":
            out.setdefault("tool_calls", [])
            out.setdefault("invalid_tool_calls", [])
            out.setdefault("usage_metadata", None)
        elif out["type"] == "tool":
            out.setdefault("tool_call_id", out.get("tool_call_id", ""))
            out.setdefault("artifact", None)
            out.setdefault("status", "success")
        return out

    # ── LangChain message objects ───────────────────────────────────────
    msg_type = getattr(m, "type", "human")
    if msg_type == "human":
        wire_type = "human"
    elif msg_type == "ai":
        wire_type = "ai"
    elif msg_type == "tool":
        wire_type = "tool"
    elif msg_type == "system":
        wire_type = "system"
    else:
        wire_type = "human"

    content = getattr(m, "content", "")
    msg_id = getattr(m, "id", "") if hasattr(m, "id") else ""
    msg_name = getattr(m, "name", None) if hasattr(m, "name") else None

    # Common base for all message types
    res: dict[str, Any] = {
        "type": wire_type,
        "content": content,
        "id": msg_id or _stable_message_id(m),
        "name": msg_name if msg_name else None,
        "additional_kwargs": getattr(m, "additional_kwargs", None) or {},
        "response_metadata": getattr(m, "response_metadata", None) or {},
    }

    if wire_type == "ai":
        tool_calls = getattr(m, "tool_calls", None) or []
        res["tool_calls"] = [
            {
                "id": tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", ""),
                "name": tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", ""),
                "args": tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}),
            }
            for tc in tool_calls
        ]
        res["invalid_tool_calls"] = getattr(m, "invalid_tool_calls", None) or []
        res["usage_metadata"] = getattr(m, "usage_metadata", None)

    elif wire_type == "tool":
        res["tool_call_id"] = getattr(m, "tool_call_id", "")
        res["artifact"] = getattr(m, "artifact", None)
        res["status"] = getattr(m, "status", "success") or "success"

    return res


def _serialize_visible_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Serialize copied public messages without changing checkpoint objects."""
    serialized_messages: list[dict[str, Any]] = []
    fallback_occurrences: dict[str, int] = {}
    for message in visible_messages(list(messages)):
        serialized = serialize_message(message)
        raw_id = (
            message.get("id")
            if isinstance(message, Mapping)
            else getattr(message, "id", None)
        )
        if raw_id is None or not str(raw_id).strip():
            base_id = _stable_message_id(message)
            occurrence = fallback_occurrences.get(base_id, 0)
            fallback_occurrences[base_id] = occurrence + 1
            serialized["id"] = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{base_id}:occurrence:{occurrence}",
                )
            )
        serialized_messages.append(serialized)
    return serialized_messages


# ── Run executor ──────────────────────────────────────────────────────────────

async def _current_agent_values(
        thread_id: str,
        thread: dict[str, Any],
) -> dict[str, Any]:
    """Load checkpoint values, falling back to the public thread snapshot."""
    thread_values = thread.get("values")
    fallback = dict(thread_values) if isinstance(thread_values, Mapping) else {}
    config = {"configurable": {"thread_id": thread_id}}

    try:
        snapshot = await agent.aget_state(config)
        snapshot_values = getattr(snapshot, "values", None)
        values = dict(snapshot_values) if isinstance(snapshot_values, Mapping) else fallback
    except Exception:
        values = fallback

    public_messages = thread.get("messages")
    if isinstance(public_messages, list):
        values["messages"] = list(public_messages)
    return values


def _agent_input_state(values: dict[str, Any]) -> dict[str, Any]:
    """Return only state fields accepted by the research agent."""
    keys = (
        "messages",
        "files",
        "todos",
        "doc_folder",
        "skill",
        "no_web",
        "wiki_query_complete",
        "existing_reports",
    )
    return {
        key: values[key]
        for key in keys
        if key in values and values[key] is not None
    }


def _persistable_public_values(
        values: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Serialize visible messages and retain JSON-safe non-message state."""
    serialized_messages = _serialize_visible_messages(values.get("messages") or [])
    serializable_values: dict[str, Any] = {}
    for key, value in values.items():
        if key == "messages":
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        serializable_values[key] = value
    serializable_values["messages"] = serialized_messages
    return serialized_messages, serializable_values


def _preserve_initial_messages(
        initial_messages: list[Any],
        returned_messages: Any,
) -> list[Any]:
    """Keep public messages present when an adapter returns only new messages."""
    if not isinstance(returned_messages, list):
        return list(initial_messages)

    def identity(message: Any) -> tuple[str, str]:
        if isinstance(message, Mapping):
            message_id = message.get("id")
            message_type = message.get("type") or message.get("role")
            content = message.get("content")
            name = message.get("name")
            tool_call_id = message.get("tool_call_id")
        else:
            message_id = getattr(message, "id", None)
            message_type = getattr(message, "type", None)
            content = getattr(message, "content", None)
            name = getattr(message, "name", None)
            tool_call_id = getattr(message, "tool_call_id", None)

        if message_id is not None and str(message_id).strip():
            return "id", str(message_id)

        normalized_type = str(message_type or "human").strip().casefold()
        normalized_type = {
            "user": "human",
            "assistant": "ai",
            "aimessage": "ai",
        }.get(normalized_type, normalized_type)
        fingerprint = json.dumps(
            [normalized_type, content, name, tool_call_id],
            sort_keys=True,
            default=str,
        )
        return "content", fingerprint

    unmatched_initial: dict[tuple[str, str], int] = {}
    for message in initial_messages:
        key = identity(message)
        unmatched_initial[key] = unmatched_initial.get(key, 0) + 1

    merged = list(initial_messages)
    for message in returned_messages:
        key = identity(message)
        remaining = unmatched_initial.get(key, 0)
        if remaining:
            unmatched_initial[key] = remaining - 1
        else:
            merged.append(message)
    return merged


async def _stream_run_events_ordinary(
        thread_id: str,
        run_id: str,
        input_state: dict[str, Any],
        *,
        recursion_limit: int = RECURSION_LIMIT,
) -> AsyncGenerator[str, None]:
    """Stream agent execution as SSE events for langgraph-sdk useStream().

    Wraps ``agent.astream_events()`` and maps LangGraph v2 events to
    SDK-compatible SSE frames.  Uses multiple fallback strategies to
    ensure AI message content reaches the UI even when the model
    provider does not emit on_chat_model_stream events.

    Strategy (in priority order):
      1. on_chat_model_stream → token-level AIMessageChunk events
      2. on_chat_model_end → full AIMessage events
      3. on_chain_end (agent / model nodes) → full AIMessage events
      4. Post-stream state diff → any remaining new AI messages
    """
    _logger = logging.getLogger(__name__)
    seq = 0
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    _seen_event_types: set[str] = set()
    _emitted_message_ids: set[str] = set()  # track which AI msgs we've sent

    # Record initial message count so we can find new messages post-stream
    _initial_msg_count = len(input_state.get("messages", []))

    # Emit initial metadata
    yield _sse_frame("metadata", {
        "run_id": run_id,
        "thread_id": thread_id,
        "assistant_id": "researcher",
        "status": "running",
    }, event_id=seq)
    seq += 1

    # Emit initial values so the UI shows the user's message immediately
    initial_values = _public_state_values(input_state)
    yield _sse_frame("values", initial_values, event_id=seq)
    seq += 1

    _tool_start_times: dict[str, float] = {}
    _streamed_tool_outputs: list[Any] = []
    _chain_end_count = 0  # debug counter
    _debug_events_logged: set[str] = set()  # track which events we've debug-logged

    try:
        async for event in agent.astream_events(
                input_state,
                config=config,
                version="v2",
        ):
            event_type = event.get("event", "")
            _seen_event_types.add(event_type)

            # Debug: log the first occurrence of key event types with sample data
            if event_type not in _debug_events_logged and event_type in (
                    "on_chat_model_start", "on_chat_model_stream", "on_chat_model_end",
                    "on_chain_start", "on_chain_end", "on_llm_start", "on_llm_end",
                    "on_llm_stream",
            ):
                _debug_events_logged.add(event_type)
                # Log a safe subset of the event data
                event_name = event.get("name", "")
                event_data_keys = list(event.get("data", {}).keys()) if event.get("data") else []
                _logger.info(
                    "[stream %s] Event DEBUG — type=%s name=%s data_keys=%s",
                    run_id, event_type, event_name, event_data_keys,
                )

            # ── Token-level AI message streaming ──
            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk is None:
                    continue
                content = getattr(chunk, "content", None)
                # Also try dict-style access (some providers return dicts)
                if content is None and isinstance(chunk, dict):
                    content = chunk.get("content")
                if content:
                    if isinstance(content, list):
                        text_parts = [
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in content
                            if (isinstance(p, dict) and p.get("type") == "text")
                               or isinstance(p, str)
                        ]
                        text = "".join(text_parts)
                    else:
                        text = str(content)
                    if text and text.strip():
                        chunk_id = (
                            getattr(chunk, "id", "")
                            if not isinstance(chunk, dict)
                            else chunk.get("id", "")
                        )
                        # Use chunk's own id, or generate a unique one — never
                        # reuse run_id across chunks (causes React key warnings).
                        msg_id = chunk_id or f"{run_id}-chunk-{seq}"
                        yield _sse_frame("messages", [{
                            "type": "AIMessageChunk",
                            "id": msg_id,
                            "content": text,
                        }], event_id=seq)
                        seq += 1

            # ── Fallback 1: full message on chat model end ──
            elif event_type == "on_chat_model_end":
                output = event.get("data", {}).get("output")
                if output is None:
                    continue
                content = getattr(output, "content", None)
                if content is None and isinstance(output, dict):
                    content = output.get("content")
                # Handle list-form content (Google Gemini multi-part responses)
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
                msg_id = (
                    getattr(output, "id", "") if not isinstance(output, dict)
                    else output.get("id", "")
                )
                if content and str(content).strip() and msg_id not in _emitted_message_ids:
                    if msg_id:
                        _emitted_message_ids.add(msg_id)
                    else:
                        # Generate a unique fallback id so React keys don't clash
                        msg_id = f"{run_id}-msg-{seq}"
                        _emitted_message_ids.add(msg_id)
                    # Include tool_calls so the SDK renders tool call indicators
                    tc_list = (
                        getattr(output, "tool_calls", None) if not isinstance(output, dict)
                        else output.get("tool_calls")
                    )
                    msg_payload: dict[str, Any] = {
                        "type": "ai",
                        "id": msg_id,
                        "content": str(content),
                    }
                    if tc_list:
                        msg_payload["tool_calls"] = [
                            {
                                "id": tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", ""),
                                "name": tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", ""),
                                "args": tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}),
                            }
                            for tc in tc_list
                        ]
                    yield _sse_frame("messages", [msg_payload], event_id=seq)
                    seq += 1

            # ── Fallback 2: chain end for agent / model nodes ──
            elif event_type == "on_chain_end":
                chain_name = event.get("name", "")
                output = event.get("data", {}).get("output")
                if output is None:
                    continue

                # Extract candidate messages from various output shapes
                candidates: list[Any] = []
                if isinstance(output, list):
                    candidates = output
                elif isinstance(output, dict):
                    # LangGraph node output: {"messages": [...], "todos": [...], ...}
                    candidates = output.get("messages", [])
                    if not candidates:
                        candidates = [output]  # maybe a serialized message dict
                else:
                    candidates = [output]

                for m in candidates:
                    msg_type = getattr(m, "type", None) if not isinstance(m, dict) else m.get("type")
                    if msg_type not in ("ai", "AIMessageChunk"):
                        continue
                    c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
                    if isinstance(c, list):
                        # Google Gemini sometimes returns content as a list of parts
                        c = "".join(
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in c
                        )
                    mid = getattr(m, "id", "") if not isinstance(m, dict) else m.get("id", "")
                    if c and str(c).strip() and mid not in _emitted_message_ids:
                        if mid:
                            _emitted_message_ids.add(mid)
                        else:
                            mid = f"{run_id}-msg-{seq}"
                            _emitted_message_ids.add(mid)
                        yield _sse_frame("messages", [{
                            "type": "ai",
                            "id": mid,
                            "content": str(c),
                        }], event_id=seq)
                        seq += 1

            # ── v1 event names (on_llm_*) — some providers use these ──
            elif event_type == "on_llm_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk is not None:
                    content = getattr(chunk, "content", None) if not isinstance(chunk, dict) else chunk.get("content")
                    text = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in (content if isinstance(content, list) else [content])
                    ) if isinstance(content, list) else str(content) if content else ""
                    if text.strip():
                        yield _sse_frame("messages", [{
                            "type": "AIMessageChunk",
                            "id": f"{run_id}-llm-chunk-{seq}",
                            "content": text,
                        }], event_id=seq)
                        seq += 1

            elif event_type == "on_llm_end":
                output = event.get("data", {}).get("output")
                if output is not None:
                    content = getattr(output, "content", None) if not isinstance(output, dict) else output.get(
                        "content")
                    if isinstance(content, list):
                        content = "".join(
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in content
                        )
                    if content and str(content).strip():
                        yield _sse_frame("messages", [{
                            "type": "ai",
                            "id": f"{run_id}-llm-msg-{seq}",
                            "content": str(content),
                        }], event_id=seq)
                        seq += 1

            # ── Tool execution started ──
            elif event_type == "on_tool_start":
                tool_name = event.get("name", "unknown")
                run_name = event.get("run_id", "")
                _tool_start_times[run_name] = time.time()
                _logger.info("[stream %s] Tool started: %s", run_id, tool_name)
                # Don't emit an updates event here — the SDK would render a
                # perpetually-spinning tool call.  Instead the AI message's
                # tool_calls (emitted in on_chat_model_end) already tells the
                # UI which tools are being invoked.

            # ── Tool execution completed ──
            elif event_type == "on_tool_end":
                tool_name = event.get("name", "unknown")
                elapsed = ""
                run_name = event.get("run_id", "")
                if run_name in _tool_start_times:
                    elapsed = f" ({time.time() - _tool_start_times[run_name]:.1f}s)"
                _logger.info("[stream %s] Tool completed: %s%s", run_id, tool_name, elapsed)
                # Emit the ToolMessage via an updates event in the standard
                # LangGraph node-output format so the SDK renders it as a
                # completed tool result.
                output = event.get("data", {}).get("output")
                if output is not None:
                    serialized = _serialize_visible_messages([
                        *list(input_state.get("messages") or []),
                        *_streamed_tool_outputs,
                        output,
                    ])
                    _streamed_tool_outputs.append(output)
                    tool_msg = serialized[-1] if serialized else {
                        "type": "tool",
                        "id": run_name,
                        "name": tool_name,
                        "content": "",
                        "tool_call_id": run_name,
                    }
                else:
                    tool_msg = {
                        "type": "tool",
                        "id": run_name,
                        "name": tool_name,
                        "content": "",
                        "tool_call_id": run_name,
                    }
                yield _sse_frame("updates", {
                    "tools": {
                        "messages": [tool_msg]
                    }
                }, event_id=seq)
                seq += 1

        _logger.info("[stream %s] astream_events complete. Seen event types: %s",
                     run_id, sorted(_seen_event_types))

        # ── Agent finished — emit final state ──
        snapshot = await agent.aget_state(config)
        values_dict: dict[str, Any] = {}
        if snapshot and snapshot.values:
            values_dict = dict(snapshot.values)

        # Always include messages from the snapshot or input_state
        messages = list(values_dict.get("messages", []))
        if not messages:
            # Fallback: use input_state messages (at least the user will see their own msg)
            messages = input_state.get("messages", [])
        public_messages = visible_messages(list(messages))
        serialized_public_messages = _serialize_visible_messages(messages)

        # ── Fallback 3: emit any new AI messages that weren't captured during stream ──
        for m, serialized_message in zip(
                public_messages[_initial_msg_count:],
                serialized_public_messages[_initial_msg_count:],
        ):
            m_type = getattr(m, "type", None) if not isinstance(m, dict) else m.get("type")
            if m_type not in ("ai", "AIMessageChunk"):
                continue
            c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
            if isinstance(c, list):
                c = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in c
                )
            mid = getattr(m, "id", "") if not isinstance(m, dict) else m.get("id", "")
            if c and str(c).strip() and mid not in _emitted_message_ids:
                if mid:
                    _emitted_message_ids.add(mid)
                else:
                    mid = serialized_message["id"]
                    _emitted_message_ids.add(mid)
                payload = dict(serialized_message)
                payload.update({"type": "ai", "id": mid, "content": str(c)})
                yield _sse_frame("messages", [payload], event_id=seq)
                seq += 1

        values_dict["messages"] = messages
        public_values = _public_state_values(values_dict)
        serialized_messages = public_values.get("messages", [])

        # Persist final state to DB for thread listing/search
        try:
            _, serializable_result = _persistable_public_values(values_dict)
            db.update_thread(
                thread_id,
                serialized_messages,
                serializable_result,
            )
            db.update_run_status(run_id, "success")
        except Exception as _db_err:
            _logger.warning("[stream %s] DB sync failed: %s", run_id, _db_err)

        yield _sse_frame("values", public_values, event_id=seq)
        seq += 1

        yield _sse_frame("end", {
            "run_id": run_id,
            "status": "success",
        }, event_id=seq)

    except asyncio.CancelledError:
        db.update_run_status(run_id, "cancelled")
        yield _sse_frame("end", {
            "run_id": run_id,
            "status": "interrupted",
        }, event_id=seq)
        raise

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        _logger.error("[stream %s] Error: %s\n%s", run_id, exc, tb)
        try:
            db.update_run_status(run_id, "error", error=str(exc))
        except Exception:
            pass
        yield _sse_frame("error", {
            "detail": str(exc),
            "traceback": tb,
        }, event_id=seq)


def _message_id(message: Any) -> str:
    if isinstance(message, Mapping):
        message_id = message.get("id")
    else:
        message_id = getattr(message, "id", None)
    return str(message_id) if message_id is not None and str(message_id).strip() else ""


def _is_final_assistant_message(message: Any) -> bool:
    if isinstance(message, Mapping):
        message_type = message.get("type") or message.get("role")
        tool_calls = message.get("tool_calls")
    else:
        message_type = getattr(message, "type", None)
        tool_calls = getattr(message, "tool_calls", None)
    normalized_type = str(message_type or "").strip().casefold()
    return (
            normalized_type in {"ai", "assistant", "aimessage"}
            and not tool_calls
    )


def _message_content(message: Any) -> Any:
    if isinstance(message, Mapping):
        return message.get("content")
    return getattr(message, "content", None)


def _contains_message_payload(value: Any) -> bool:
    """Detect nested LangChain or serialized chat messages."""
    message_discriminators = {
        "ai",
        "assistant",
        "aimessage",
        "aimessagechunk",
        "human",
        "user",
        "humanmessage",
        "humanmessagechunk",
        "tool",
        "toolmessage",
        "toolmessagechunk",
        "system",
        "systemmessage",
        "systemmessagechunk",
    }
    message_payload_keys = {
        "content",
        "tool_calls",
        "invalid_tool_calls",
        "tool_call_id",
        "additional_kwargs",
        "response_metadata",
    }

    if isinstance(value, BaseMessage):
        return True
    if isinstance(value, Mapping):
        normalized_keys = {str(key).casefold() for key in value}
        discriminator = value.get("type") or value.get("role")
        normalized_discriminator = str(discriminator or "").strip().casefold()
        if (
                normalized_discriminator in message_discriminators
                and normalized_keys.intersection(message_payload_keys)
        ):
            return True
        return any(
            str(key).casefold() == "messages"
            or _contains_message_payload(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_message_payload(item) for item in value)
    return False


def _is_safe_progress_update(value: Any) -> bool:
    """Allow explicit progress fields while rejecting message-bearing chunks."""
    safe_keys = {
        "progress",
        "phase",
        "status",
        "step",
        "percent",
        "current",
        "total",
    }

    return (
            isinstance(value, Mapping)
            and bool(value)
            and set(value).issubset(safe_keys)
            and not _contains_message_payload(value)
    )


async def _load_stream_values(
        thread_id: str,
        input_state: dict[str, Any],
) -> dict[str, Any]:
    """Load latest checkpoint state and merge request messages without mutation."""
    thread = db.get_thread(thread_id) or {}
    raw_thread_values = thread.get("values")
    fallback = (
        dict(raw_thread_values)
        if isinstance(raw_thread_values, Mapping)
        else {}
    )
    try:
        snapshot = await agent.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        snapshot_values = getattr(snapshot, "values", None)
        values = (
            dict(snapshot_values)
            if isinstance(snapshot_values, Mapping)
            else fallback
        )
    except Exception:
        values = fallback

    checkpoint_messages = values.get("messages")
    if not isinstance(checkpoint_messages, list):
        checkpoint_messages = thread.get("messages")
    if not isinstance(checkpoint_messages, list):
        checkpoint_messages = []
    request_messages = input_state.get("messages")
    if not isinstance(request_messages, list):
        request_messages = []
    values["messages"] = _preserve_initial_messages(
        list(checkpoint_messages),
        request_messages,
    )
    for key, value in input_state.items():
        if key != "messages":
            values.setdefault(key, value)
    return values


def _persist_stream_values(
        thread_id: str,
        values: dict[str, Any],
) -> None:
    serialized_messages, serializable_values = _persistable_public_values(values)
    db.update_thread(thread_id, serialized_messages, serializable_values)


def _cancel_stream_run_if_nonterminal(run_id: str) -> None:
    """Mark interrupted stream cancelled without overwriting terminal state."""
    try:
        run = db.get_run(run_id)
        status = run.get("status") if run else None
        if status not in {"success", "error", "cancelled", "timeout"}:
            db.update_run_status(run_id, "cancelled")
    except Exception:
        return


async def _stream_run_events(
        thread_id: str,
        run_id: str,
        input_state: dict[str, Any],
        *,
        recursion_limit: int = RECURSION_LIMIT,
) -> AsyncGenerator[str, None]:
    """Stream one ordinary run or bounded hidden todo-resumption rounds."""
    logger = logging.getLogger(__name__)
    seq = 0
    try:
        latest_values = await _load_stream_values(thread_id, input_state)
        run = db.get_run(run_id) or {}
        run_kwargs = run.get("kwargs")
        candidate = (
            run_kwargs.get("_resume_candidate", "")
            if isinstance(run_kwargs, Mapping)
            else ""
        )
        candidate = candidate if isinstance(candidate, str) else ""
        inspection = inspect_todos(latest_values.get("todos"))
        resume_active = (
                is_resume_intent(candidate)
                and inspection.has_incomplete
        )
    except asyncio.CancelledError:
        db.update_run_status(run_id, "cancelled")
        yield _sse_frame(
            "end",
            {"run_id": run_id, "status": "interrupted"},
            event_id=seq,
        )
        raise
    except Exception as exc:
        try:
            db.update_run_status(run_id, "error", error=str(exc))
        except Exception:
            pass
        yield _sse_frame(
            "error",
            {"detail": str(exc)},
            event_id=seq,
        )
        seq += 1
        yield _sse_frame(
            "end",
            {"run_id": run_id, "status": "error"},
            event_id=seq,
        )
        return

    if not resume_active:
        async for frame in _stream_run_events_ordinary(
                thread_id,
                run_id,
                input_state,
                recursion_limit=recursion_limit,
        ):
            yield frame
        return

    max_rounds = get_max_resume_rounds()
    initial_messages = list(latest_values.get("messages") or [])
    initial_message_ids = {
        message_id
        for message in initial_messages
        if (message_id := _message_id(message))
    }
    initial_message_count = len(initial_messages)
    emitted_message_ids: set[str] = set()
    last_config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    rounds_completed = 0
    end_emitted = False

    try:
        current_run = db.get_run(run_id)
        if current_run and current_run.get("status") == "cancelled":
            end_emitted = True
            yield _sse_frame(
                "end",
                {"run_id": run_id, "status": "interrupted"},
                event_id=seq,
            )
            return
        db.update_run_status(run_id, "running")
        yield _sse_frame(
            "metadata",
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "assistant_id": "researcher",
                "status": "running",
            },
            event_id=seq,
        )
        seq += 1
        yield _sse_frame(
            "values",
            _public_state_values(latest_values),
            event_id=seq,
        )
        seq += 1

        for round_number in range(1, max_rounds + 1):
            current_run = db.get_run(run_id)
            if current_run and current_run.get("status") == "cancelled":
                logger.info(
                    "resume_stream thread_id=%s run_id=%s round=%d "
                    "max_rounds=%d incomplete_count=%d malformed_count=%d "
                    "stop_reason=%s",
                    thread_id,
                    run_id,
                    round_number - 1,
                    max_rounds,
                    len(inspection.incomplete),
                    inspection.malformed_count,
                    "cancelled",
                )
                yield _sse_frame(
                    "values",
                    _public_state_values(latest_values),
                    event_id=seq,
                )
                seq += 1
                end_emitted = True
                yield _sse_frame(
                    "end",
                    {"run_id": run_id, "status": "interrupted"},
                    event_id=seq,
                )
                return
            last_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "resume_incomplete_todos": True,
                    "resume_round": round_number,
                    "resume_max_rounds": max_rounds,
                },
                "recursion_limit": recursion_limit,
            }
            logger.info(
                "resume_stream thread_id=%s run_id=%s round=%d max_rounds=%d "
                "incomplete_count=%d malformed_count=%d stop_reason=%s",
                thread_id,
                run_id,
                round_number,
                max_rounds,
                len(inspection.incomplete),
                inspection.malformed_count,
                "invoke",
            )

            tool_start_times: dict[str, float] = {}
            round_tool_outputs: list[Any] = []
            async for event in agent.astream_events(
                    _agent_input_state(latest_values),
                    config=last_config,
                    version="v2",
            ):
                event_type = event.get("event", "")
                if event_type == "on_tool_start":
                    tool_start_times[str(event.get("run_id", ""))] = time.time()
                    continue
                if event_type == "on_tool_end":
                    tool_name = event.get("name", "unknown")
                    tool_run_id = str(event.get("run_id", ""))
                    output = event.get("data", {}).get("output")
                    tool_message = {
                        "type": "tool",
                        "id": tool_run_id,
                        "name": tool_name,
                        "content": "",
                        "tool_call_id": tool_run_id,
                    }
                    if isinstance(output, ToolMessage):
                        nested_tool_payload = (
                            output.content,
                            output.additional_kwargs,
                            output.response_metadata,
                            output.artifact,
                        )
                        unsafe_output = any(
                            _contains_message_payload(value)
                            for value in nested_tool_payload
                        )
                    else:
                        unsafe_output = _contains_message_payload(output)
                    if output is not None and not unsafe_output:
                        round_tool_outputs.append(output)
                        if isinstance(output, (str, int, float, bool)):
                            tool_message["content"] = str(output)
                        else:
                            serialized = _serialize_visible_messages([
                                *list(latest_values.get("messages") or []),
                                *round_tool_outputs,
                            ])
                            if serialized:
                                tool_message = serialized[-1]
                    yield _sse_frame(
                        "updates",
                        {"tools": {"messages": [tool_message]}},
                        event_id=seq,
                    )
                    seq += 1
                    elapsed = (
                        time.time() - tool_start_times[tool_run_id]
                        if tool_run_id in tool_start_times
                        else None
                    )
                    logger.info(
                        "[stream %s] Tool completed: %s%s",
                        run_id,
                        tool_name,
                        f" ({elapsed:.1f}s)" if elapsed is not None else "",
                    )
                    continue
                if event_type in {"on_custom_event", "on_chain_stream"}:
                    progress = event.get("data", {}).get("chunk")
                    if _is_safe_progress_update(progress):
                        yield _sse_frame(
                            "updates",
                            dict(progress),
                            event_id=seq,
                        )
                        seq += 1
                # All model/chain terminal text stays hidden during resume rounds.

            snapshot = await agent.aget_state(last_config)
            snapshot_values = getattr(snapshot, "values", None)
            if not isinstance(snapshot_values, Mapping):
                raise RuntimeError("resume checkpoint reload returned no state values")
            round_values = dict(snapshot_values)
            if "messages" in round_values:
                round_values["messages"] = _preserve_initial_messages(
                    list(latest_values.get("messages") or []),
                    round_values.get("messages"),
                )
            latest_values.update(round_values)
            rounds_completed = round_number
            inspection = inspect_todos(latest_values.get("todos"))

            # Recovery snapshot after every hidden round.
            _persist_stream_values(thread_id, latest_values)

            current_run = db.get_run(run_id)
            if current_run and current_run.get("status") == "cancelled":
                logger.info(
                    "resume_stream thread_id=%s run_id=%s round=%d max_rounds=%d "
                    "incomplete_count=%d malformed_count=%d stop_reason=%s",
                    thread_id,
                    run_id,
                    round_number,
                    max_rounds,
                    len(inspection.incomplete),
                    inspection.malformed_count,
                    "cancelled",
                )
                yield _sse_frame(
                    "values",
                    _public_state_values(latest_values),
                    event_id=seq,
                )
                seq += 1
                end_emitted = True
                yield _sse_frame(
                    "end",
                    {"run_id": run_id, "status": "interrupted"},
                    event_id=seq,
                )
                return

            if not inspection.has_incomplete:
                logger.info(
                    "resume_stream thread_id=%s run_id=%s round=%d max_rounds=%d "
                    "incomplete_count=%d malformed_count=%d stop_reason=%s",
                    thread_id,
                    run_id,
                    round_number,
                    max_rounds,
                    len(inspection.incomplete),
                    inspection.malformed_count,
                    "complete",
                )
                break

            if round_number < max_rounds:
                logger.info(
                    "resume_stream thread_id=%s run_id=%s round=%d max_rounds=%d "
                    "incomplete_count=%d malformed_count=%d stop_reason=%s",
                    thread_id,
                    run_id,
                    round_number,
                    max_rounds,
                    len(inspection.incomplete),
                    inspection.malformed_count,
                    "continue",
                )
                yield _sse_frame(
                    "metadata",
                    {
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "status": "running",
                        "resume_round": round_number + 1,
                        "resume_max_rounds": max_rounds,
                        "incomplete_todo_count": len(inspection.incomplete),
                    },
                    event_id=seq,
                )
                seq += 1

        if inspection.has_incomplete:
            limit_message = AIMessage(
                content=build_round_limit_message(inspection, rounds_completed),
                id=f"{run_id}-resume-limit-{uuid.uuid4()}",
            )
            limit_content = limit_message.content
            limit_message_id = _message_id(limit_message)
            update_state = getattr(agent, "aupdate_state", None)
            if callable(update_state):
                await update_state(last_config, {"messages": [limit_message]})
                snapshot = await agent.aget_state(last_config)
                snapshot_values = getattr(snapshot, "values", None)
                if not isinstance(snapshot_values, Mapping):
                    raise RuntimeError(
                        "resume checkpoint reload returned no state values"
                    )
                reloaded = dict(snapshot_values)
                reloaded["messages"] = _preserve_initial_messages(
                    list(latest_values.get("messages") or []),
                    reloaded.get("messages"),
                )
                if not any(
                        _message_id(message) == limit_message_id
                        and _message_content(message) == limit_content
                        for message in reloaded["messages"]
                ):
                    raise RuntimeError(
                        "resume checkpoint reload omitted round-limit message"
                    )
                latest_values.update(reloaded)
            else:
                latest_values["messages"] = [
                    *list(latest_values.get("messages") or []),
                    limit_message,
                ]
            _persist_stream_values(thread_id, latest_values)
            logger.info(
                "resume_stream thread_id=%s run_id=%s round=%d max_rounds=%d "
                "incomplete_count=%d malformed_count=%d stop_reason=%s",
                thread_id,
                run_id,
                rounds_completed,
                max_rounds,
                len(inspection.incomplete),
                inspection.malformed_count,
                "round_limit",
            )

        final_messages = list(latest_values.get("messages") or [])
        serialized_final_messages = iter(
            _serialize_visible_messages(final_messages)
        )
        for index, message in enumerate(final_messages):
            if not visible_messages([message]):
                continue
            serialized = next(serialized_final_messages)
            if not _is_final_assistant_message(message):
                continue
            message_id = _message_id(message)
            if message_id:
                is_new = message_id not in initial_message_ids
            else:
                is_new = index >= initial_message_count
            if not is_new or message_id in emitted_message_ids:
                continue
            if message_id:
                emitted_message_ids.add(message_id)
            yield _sse_frame("messages", [serialized], event_id=seq)
            seq += 1

        _persist_stream_values(thread_id, latest_values)
        yield _sse_frame(
            "values",
            _public_state_values(latest_values),
            event_id=seq,
        )
        seq += 1
        db.update_run_status(run_id, "success")
        end_emitted = True
        yield _sse_frame(
            "end",
            {"run_id": run_id, "status": "success"},
            event_id=seq,
        )

    except GeneratorExit:
        _cancel_stream_run_if_nonterminal(run_id)
        raise
    except asyncio.CancelledError:
        if not end_emitted:
            try:
                snapshot = await agent.aget_state(
                    {"configurable": {"thread_id": thread_id}}
                )
                snapshot_values = getattr(snapshot, "values", None)
                if isinstance(snapshot_values, Mapping):
                    recovered = dict(snapshot_values)
                    recovered["messages"] = _preserve_initial_messages(
                        list(latest_values.get("messages") or []),
                        recovered.get("messages"),
                    )
                    latest_values.update(recovered)
                    _persist_stream_values(thread_id, latest_values)
            except Exception:
                pass
        _cancel_stream_run_if_nonterminal(run_id)
        if not end_emitted:
            yield _sse_frame(
                "end",
                {"run_id": run_id, "status": "interrupted"},
                event_id=seq,
            )
        raise
    except Exception as exc:
        import traceback

        traceback_text = traceback.format_exc()
        try:
            snapshot = await agent.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            snapshot_values = getattr(snapshot, "values", None)
            if isinstance(snapshot_values, Mapping):
                recovered = dict(snapshot_values)
                recovered["messages"] = _preserve_initial_messages(
                    list(latest_values.get("messages") or []),
                    recovered.get("messages"),
                )
                latest_values.update(recovered)
                _persist_stream_values(thread_id, latest_values)
        except Exception:
            pass
        logger.error(
            "resume_stream thread_id=%s run_id=%s round=%d max_rounds=%d "
            "incomplete_count=%d malformed_count=%d stop_reason=%s",
            thread_id,
            run_id,
            rounds_completed,
            max_rounds,
            len(inspection.incomplete),
            inspection.malformed_count,
            "error",
        )
        db.update_run_status(run_id, "error", error=str(exc))
        yield _sse_frame(
            "error",
            {"detail": str(exc), "traceback": traceback_text},
            event_id=seq,
        )
        seq += 1
        if not end_emitted:
            yield _sse_frame(
                "end",
                {"run_id": run_id, "status": "error"},
                event_id=seq,
            )


async def _execute_run(run_id: str, thread_id: str) -> None:
    """Invoke the agent and persist the result; called as a fire-and-forget task.

    Wiki context injection and sufficiency evaluation are handled by the agent's
    ``ResearchStateMiddleware`` — this function only loads thread state, invokes
    the agent with the proper ``thread_id`` config, and persists the result.
    """
    _logger = logging.getLogger(__name__)
    db.update_run_status(run_id, "running")
    latest_values: dict[str, Any] = {}
    try:
        run = db.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found during run execution")

        thread = db.get_thread(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found during run execution")

        latest_values = await _current_agent_values(thread_id, thread)
        existing_values = latest_values
        existing_files = existing_values.get("files") or {}
        initial_messages = list(existing_values.get("messages") or [])

        # Initialize per-thread cited_response tracking for the middleware
        existing_reports = [k for k in existing_files if k.startswith("/cited_response")]
        from research_agent.research_subagent.utils.knowledge_filesystem import _thread_existing_cited_responses
        _thread_existing_cited_responses[str(thread_id)] = existing_reports

        latest_values.setdefault("files", existing_files)
        latest_values.setdefault("existing_reports", existing_reports)

        run_kwargs = run.get("kwargs")
        candidate = (
            run_kwargs.get("_resume_candidate", "")
            if isinstance(run_kwargs, dict)
            else ""
        )
        candidate = candidate if isinstance(candidate, str) else ""
        inspection = inspect_todos(latest_values.get("todos"))
        resume_active = is_resume_intent(candidate) and inspection.has_incomplete
        max_rounds = get_max_resume_rounds() if resume_active else 1
        rounds_completed = 0
        config: dict[str, Any] = {}

        for round_number in range(1, max_rounds + 1):
            configurable: dict[str, Any] = {"thread_id": str(thread_id)}
            if resume_active:
                configurable.update(
                    {
                        "resume_incomplete_todos": True,
                        "resume_round": round_number,
                        "resume_max_rounds": max_rounds,
                    }
                )
            config = {
                "configurable": configurable,
                "recursion_limit": RECURSION_LIMIT,
            }
            _logger.info(
                "resume_run thread_id=%s run_id=%s round=%d max_rounds=%d "
                "incomplete_count=%d malformed_count=%d stop_reason=%s",
                thread_id,
                run_id,
                round_number,
                max_rounds,
                len(inspection.incomplete),
                inspection.malformed_count,
                "invoke",
            )

            round_result = await agent.ainvoke(
                _agent_input_state(latest_values),
                config=config,
            )
            if isinstance(round_result, dict):
                round_values = dict(round_result)
                if "messages" in round_values:
                    round_values["messages"] = _preserve_initial_messages(
                        initial_messages,
                        round_values["messages"],
                    )
                latest_values.update(round_values)
                if "todos" not in round_values:
                    current_values = await _current_agent_values(
                        thread_id,
                        {"values": latest_values},
                    )
                    latest_values.update(current_values)
            rounds_completed = round_number
            inspection = inspect_todos(latest_values.get("todos"))

            async with _task_lock:
                run_data = db.get_run(run_id)
                if run_data and run_data.get("status") == "cancelled":
                    _logger.info(
                        "resume_run thread_id=%s run_id=%s round=%d max_rounds=%d "
                        "incomplete_count=%d malformed_count=%d stop_reason=%s",
                        thread_id,
                        run_id,
                        round_number,
                        max_rounds,
                        len(inspection.incomplete),
                        inspection.malformed_count,
                        "cancelled",
                    )
                    return

            if not resume_active or not inspection.has_incomplete:
                stop_reason = "ordinary" if not resume_active else "complete"
                _logger.info(
                    "resume_run thread_id=%s run_id=%s round=%d max_rounds=%d "
                    "incomplete_count=%d malformed_count=%d stop_reason=%s",
                    thread_id,
                    run_id,
                    round_number,
                    max_rounds,
                    len(inspection.incomplete),
                    inspection.malformed_count,
                    stop_reason,
                )
                break

        if resume_active and inspection.has_incomplete:
            limit_message = AIMessage(
                content=build_round_limit_message(inspection, rounds_completed),
                id=f"{run_id}-resume-limit-{uuid.uuid4()}",
            )
            limit_content = limit_message.content
            limit_message_id = _message_id(limit_message)
            update_state = getattr(agent, "aupdate_state", None)
            if callable(update_state):
                await update_state(config, {"messages": [limit_message]})
                snapshot = await agent.aget_state(
                    {"configurable": {"thread_id": thread_id}}
                )
                snapshot_values = getattr(snapshot, "values", None)
                if not isinstance(snapshot_values, Mapping):
                    raise RuntimeError(
                        "resume checkpoint reload returned no state values"
                    )
                reloaded = dict(snapshot_values)
                reloaded["messages"] = _preserve_initial_messages(
                    initial_messages,
                    reloaded.get("messages"),
                )
                if not any(
                        _message_id(message) == limit_message_id
                        and _message_content(message) == limit_content
                        for message in reloaded["messages"]
                ):
                    raise RuntimeError(
                        "resume checkpoint reload omitted round-limit message"
                    )
                latest_values.update(reloaded)
            else:
                latest_messages = list(latest_values.get("messages") or [])
                latest_messages.append(limit_message)
                latest_values["messages"] = latest_messages
            _logger.info(
                "resume_run thread_id=%s run_id=%s round=%d max_rounds=%d "
                "incomplete_count=%d malformed_count=%d stop_reason=%s",
                thread_id,
                run_id,
                rounds_completed,
                max_rounds,
                len(inspection.incomplete),
                inspection.malformed_count,
                "round_limit",
            )

        result = latest_values

        # ── Citation validation (post-execution) ─────────────────────────
        files = result.get("files", {})
        existing_reports_result = result.get("existing_reports")
        if not existing_reports_result:
            existing_reports_result = _thread_existing_cited_responses.get(str(thread_id), [])
        from research_agent.research_subagent.utils.knowledge_filesystem import get_active_cited_response_path
        active_report_path = get_active_cited_response_path(files, existing_reports_result)

        if active_report_path in files:
            from deepagents.backends.utils import file_data_to_string, create_file_data
            report_data = files[active_report_path]
            report_text = file_data_to_string(report_data)
            if os.getenv("DEEP_RESEARCH_VALIDATE_CITATIONS") == "1":
                from thread_wiki.service import _extract_citations
                from research_agent.research_subagent.utils.citation_validator import validate_web_citations

                citations = _extract_citations(report_text)
                web_citations = [c for c in citations if c.kind == "web"]
                if web_citations:
                    try:
                        validation_results = await validate_web_citations(web_citations, report_text)
                        if validation_results and "### Citation Verification" not in report_text:
                            appendix_lines = ["", "### Citation Verification"]
                            for res in validation_results:
                                appendix_lines.append(
                                    f"- **[{res.url}]({res.url})**: Reachable: {'Yes' if res.reachable else 'No'}, "
                                    f"Grounded: {'Yes' if res.grounded else 'No'} ({res.reason})"
                                )
                            appendix = "\n".join(appendix_lines)
                            new_report_text = report_text + "\n" + appendix
                            files[active_report_path] = create_file_data(new_report_text)
                            report_text = new_report_text
                    except Exception as e:
                        _logger.warning("Citation validation failed: %s", e, exc_info=True)

            # Update final message if it matches the unvalidated report
            result_messages = result.get("messages", [])
            if result_messages:
                last_msg = result_messages[-1]
                last_content = (
                    last_msg.get("content", "") if isinstance(last_msg, dict)
                    else getattr(last_msg, "content", "") or ""
                )
                if last_content.strip() == report_text.strip():
                    if isinstance(last_msg, dict):
                        last_msg["content"] = report_text
                    else:
                        setattr(last_msg, "content", report_text)

        # Serialize only public messages; hidden resume-round output remains internal.
        serialized_messages = _serialize_visible_messages(
            result.get("messages", [])
        )

        # Sanitize /raw/ references in the final message
        if serialized_messages:
            last_msg = serialized_messages[-1]
            if last_msg.get("role") == "assistant" and last_msg.get("content"):
                content = last_msg["content"]
                if active_report_path in files:
                    from deepagents.backends.utils import file_data_to_string
                    final_report_text = file_data_to_string(files[active_report_path])
                    # If citation validator appended content, use the updated report
                    if "### Citation Verification" in final_report_text:
                        content = final_report_text

                import re as _re
                sanitized = _re.sub(
                    r'/raw/([A-Za-z0-9._\-]+)\.(pdf|docx|pptx|xlsx)\.(md|txt)\b',
                    r'/\1.\2', content,
                )
                sanitized = _re.sub(
                    r'/raw/([A-Za-z0-9._\-]+\.(?:pdf|docx|pptx|xlsx))\b',
                    r'/\1', sanitized,
                )
                last_msg["content"] = sanitized

        # Collect state metadata
        from research_agent.research_subagent.utils.knowledge_filesystem import (
            _thread_wiki_query_complete,
            _thread_existing_cited_responses,
        )
        wiki_query_complete = result.get("wiki_query_complete")
        if not wiki_query_complete and str(thread_id) in _thread_wiki_query_complete:
            wiki_query_complete = _thread_wiki_query_complete[str(thread_id)]

        existing_reports_db = result.get("existing_reports")
        if not existing_reports_db:
            existing_reports_db = _thread_existing_cited_responses.get(str(thread_id), [])

        # Persist all JSON-safe non-message state for thread listing/search.
        _, serializable_result = _persistable_public_values(result)
        serializable_result["messages"] = serialized_messages
        serializable_result["wiki_query_complete"] = wiki_query_complete
        serializable_result["existing_reports"] = existing_reports_db
        db.update_thread(thread_id, serialized_messages, serializable_result)
        db.update_run_status(run_id, "success")

    except asyncio.CancelledError:
        db.update_run_status(run_id, "cancelled")
        raise
    except Exception as exc:
        if latest_values:
            try:
                serialized_messages, serializable_result = _persistable_public_values(
                    latest_values
                )
                db.update_thread(
                    thread_id,
                    serialized_messages,
                    serializable_result,
                )
            except Exception:
                _logger.warning(
                    "resume_run thread_id=%s run_id=%s round=%d max_rounds=%d "
                    "incomplete_count=%d malformed_count=%d stop_reason=%s",
                    thread_id,
                    run_id,
                    0,
                    0,
                    0,
                    0,
                    "error_state_persist_failed",
                )
        _logger.error(
            "resume_run thread_id=%s run_id=%s round=%d max_rounds=%d "
            "incomplete_count=%d malformed_count=%d stop_reason=%s",
            thread_id,
            run_id,
            0,
            0,
            0,
            0,
            "error",
        )
        db.update_run_status(run_id, "error", error=str(exc))
    finally:
        async with _task_lock:
            _active_tasks.pop(run_id, None)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/ok", tags=["Health"])
async def health() -> dict[str, bool]:
    """Health check."""
    return {"ok": True}


@app.get("/assistants/search", tags=["Assistants"])
async def search_assistants(
        limit: int = Query(default=10, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> list[AssistantResponse]:
    """Search/list available assistants."""
    return _list_assistants(limit=limit, offset=offset)


@app.post("/assistants/search", tags=["Assistants"])
async def search_assistants_post(
        body: AssistantSearchRequest = AssistantSearchRequest(),
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> list[AssistantResponse]:
    """Search/list available assistants (POST compatibility for frontend clients)."""
    return _list_assistants(
        limit=body.limit,
        offset=body.offset,
        graph_id=body.graph_id,
        assistant_id=body.assistant_id,
    )


@app.get("/assistants/{assistant_id}", tags=["Assistants"])
async def get_assistant(
        assistant_id: str,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> AssistantResponse:
    """Get a specific assistant by ID."""
    assistants = _list_assistants(limit=1, offset=0, assistant_id=assistant_id)
    if not assistants:
        raise HTTPException(status_code=404, detail=f"Assistant '{assistant_id}' not found")
    return assistants[0]


@app.post("/threads", tags=["Threads"])
async def create_thread(
        body: ThreadCreateRequest = ThreadCreateRequest(),
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a thread."""
    thread_id = body.thread_id or str(uuid.uuid4())
    existing = db.get_thread(thread_id)
    if existing is not None:
        if body.if_exists == "do_nothing":
            return _api_thread(existing)
        raise HTTPException(status_code=409, detail="Thread already exists")

    now = datetime.now(UTC).isoformat()
    user_id = current_user["identity"]
    db.create_thread(thread_id, user_id, now, metadata=body.metadata or {}, status="idle", values=None)
    created = db.get_thread(thread_id)
    if created is None:
        raise HTTPException(status_code=500, detail="Failed to create thread")
    return _api_thread(created)


@app.post("/threads/search", tags=["Threads"])
async def search_threads(
        body: ThreadSearchRequest,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Search/list threads."""
    user_id = None if current_user.get("identity") == "admin" else current_user.get("identity")
    items = db.search_threads(
        limit=body.limit,
        offset=body.offset,
        sort_by=body.sort_by,
        sort_order=body.sort_order,
        status=body.status,
        metadata=body.metadata or {},
        user_id=user_id,
    )
    return [_api_thread(t) for t in items]


@app.patch("/threads/{thread_id}", tags=["Threads"])
async def patch_thread(
        thread_id: str,
        body: ThreadPatchRequest,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> dict[str, Any]:
    """Patch thread metadata."""
    _get_thread_with_auth(thread_id, current_user)
    ok = db.update_thread_metadata(thread_id, body.metadata or {})
    if not ok:
        raise HTTPException(status_code=404, detail="Thread not found")
    updated = db.get_thread(thread_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return _api_thread(updated)


@app.delete("/threads/{thread_id}", tags=["Threads"])
async def delete_thread(
        thread_id: str,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a thread and associated runs."""
    _get_thread_with_auth(thread_id, current_user)
    ok = db.delete_thread(thread_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Thread not found")
    from research_agent.research_subagent.utils.knowledge_filesystem import (
        clear_thread_cache,
    )

    clear_thread_cache(thread_id)
    return {}


@app.post("/threads/{thread_id}/state", tags=["Threads"])
async def update_thread_state(
        thread_id: str,
        body: ThreadStateUpdateRequest,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> dict[str, Any]:
    """Update thread state values."""
    _get_thread_with_auth(thread_id, current_user)
    values = body.values
    if values is None:
        payload_values: dict[str, Any] = {}
    elif isinstance(values, dict):
        payload_values = values
    else:
        payload_values = {"values": values}

    ok = db.update_thread_state(thread_id, payload_values)
    if not ok:
        raise HTTPException(status_code=404, detail="Thread not found")

    thread = db.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    checkpoint = {
        "thread_id": thread_id,
        "checkpoint_ns": "",
        "checkpoint_id": str(uuid.uuid4()),
    }
    return {"checkpoint": checkpoint}


@app.get("/threads/{thread_id}/state", tags=["Threads"])
async def get_thread_state(
        thread_id: str,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> dict[str, Any]:
    """Get thread state from the LangGraph checkpointer, falling back to DB."""
    _get_thread_with_auth(thread_id, current_user)

    # Try checkpointer first (primary source of truth for agent state)
    try:
        snapshot = await agent.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        if snapshot and snapshot.values:
            values = _public_state_values(snapshot.values)
            return {
                "values": values,
                "next": list(snapshot.next) if snapshot.next else [],
                "tasks": [
                    {"id": t.id, "name": t.name, "error": t.error}
                    for t in (snapshot.tasks or [])
                ],
                "checkpoint": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": snapshot.config.get("configurable", {}).get(
                        "checkpoint_id", ""
                    ),
                },
                "metadata": snapshot.metadata or {},
                "created_at": snapshot.created_at,
                "parent_config": snapshot.parent_config,
            }
    except Exception:
        pass

    # Fallback to DB
    thread = db.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {
        "values": _public_state_values(thread.get("values")),
        "next": [],
        "tasks": [],
    }


@app.post("/threads/{thread_id}/runs", tags=["Runs"])
async def create_run(
        thread_id: str,
        body: RunCreateRequest,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user)
) -> dict[str, Any]:
    """Create a run on an existing thread with request payload validation."""
    thread = _get_thread_with_auth(thread_id, current_user)

    multitask_strategy = body.multitask_strategy

    # If interrupt, cancel all currently active runs on this thread
    if multitask_strategy == "interrupt":
        async with _task_lock:
            # Cancel tasks from memory
            to_cancel = []
            for run_id, task in list(_active_tasks.items()):
                run_data = db.get_run(run_id)
                if run_data and run_data.get("thread_id") == thread_id:
                    task.cancel()
                    to_cancel.append(run_id)

            for run_id in to_cancel:
                _active_tasks.pop(run_id, None)

            db.cancel_running_runs(thread_id)
            db.update_thread(thread_id, [], {"messages": []})

    messages = body.input.messages
    trigger_entry = _last_user_entry(messages)
    resume_candidate = trigger_entry[1] if trigger_entry is not None else ""

    if trigger_entry is not None and resume_candidate:
        trigger_role, trigger_content = trigger_entry
        existing = thread.get("messages") or []
        existing.append({"role": trigger_role, "content": trigger_content})
        db.update_thread(thread_id, existing, thread.get("values") or {})

    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    assistant_id = body.assistant_id or "researcher"
    candidate_kwargs = (
        {"kwargs": {"_resume_candidate": resume_candidate}}
        if resume_candidate
        else {}
    )
    db.create_run(
        run_id,
        thread_id,
        assistant_id,
        now,
        multitask_strategy=multitask_strategy or "enqueue",
        **candidate_kwargs,
    )

    # Spawn background task and register it in _active_tasks
    async with _task_lock:
        task = asyncio.create_task(_execute_run(run_id, thread_id))
        _active_tasks[run_id] = task

    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=500, detail="Failed to create run")
    return _api_run(run)


@app.get("/threads/{thread_id}/runs", tags=["Runs"])
async def list_runs(
        thread_id: str,
        limit: int = Query(default=10),
        offset: int = Query(default=0),
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List runs for a thread."""
    _get_thread_with_auth(thread_id, current_user)
    runs = db.list_runs(thread_id, limit=limit, offset=offset)
    return [_api_run(r) for r in runs]


@app.post("/threads/{thread_id}/runs/stream", tags=["Runs"])
async def stream_run(
        thread_id: str,
        body: RunStreamRequest,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
):
    """Create a run and stream output as SSE event payloads.

    Uses real event-driven streaming via ``agent.astream_events()`` with
    token-level message deltas and tool-call visibility.
    """
    _get_thread_with_auth(thread_id, current_user)
    _logger = logging.getLogger(__name__)

    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    raw_messages: Any = []
    if isinstance(body.input, dict):
        raw_messages = body.input.get("messages", [])
    resume_candidate = _last_user_message(raw_messages)
    candidate_kwargs = (
        {"kwargs": {"_resume_candidate": resume_candidate}}
        if resume_candidate
        else {}
    )

    # Record the run
    db.create_run(
        run_id, thread_id,
        body.assistant_id or "researcher", now,
        multitask_strategy=body.multitask_strategy or "enqueue",
        **candidate_kwargs,
    )

    # Build input state from current thread values
    thread = db.get_thread(thread_id)
    existing_values = (thread or {}).get("values") or {}
    existing_files = existing_values.get("files") or {}

    # Parse incoming messages and append to thread messages
    messages = list(thread.get("messages") or [])
    if isinstance(raw_messages, list):
        for msg in raw_messages:
            if isinstance(msg, dict):
                messages.append({
                    "role": str(msg.get("role", "user")),
                    "content": str(msg.get("content", "")),
                    "name": msg.get("name"),
                })

    # Persist the user message to DB immediately so GET /threads/{id} shows it
    db.update_thread(thread_id, messages, existing_values)

    # Initialize per-thread cited_response tracking for the middleware
    from research_agent.research_subagent.utils.knowledge_filesystem import _thread_existing_cited_responses
    existing_reports = [k for k in existing_files if k.startswith("/cited_response")]
    _thread_existing_cited_responses[str(thread_id)] = existing_reports

    input_state = {
        "messages": messages,
        "files": existing_files,
        "doc_folder": existing_values.get("doc_folder"),
        "skill": existing_values.get("skill"),
        "no_web": existing_values.get("no_web"),
        "wiki_query_complete": existing_values.get("wiki_query_complete", False),
        "existing_reports": existing_reports,
    }
    input_state = {k: v for k, v in input_state.items() if v is not None}

    _logger.info("[stream %s] Starting event-driven stream for thread %s (%d messages)",
                 run_id, thread_id, len(messages))

    # Use client-provided recursion_limit if present, else fall back to env var
    client_recursion_limit = RECURSION_LIMIT
    if body.config and isinstance(body.config, dict):
        client_recursion_limit = body.config.get("recursion_limit", RECURSION_LIMIT)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        _stream_run_events(thread_id, run_id, input_state,
                           recursion_limit=client_recursion_limit),
        media_type="text/event-stream",
    )


@app.get("/threads/{thread_id}/runs/{run_id}", tags=["Runs"])
async def get_run(
        thread_id: str,
        run_id: str,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user)
) -> dict[str, Any]:
    """Get run status."""
    # Ensure thread belongs to authenticated user/is accessible
    _get_thread_with_auth(thread_id, current_user)

    run = db.get_run(run_id)
    if run is None or run["thread_id"] != thread_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return _api_run(run)


@app.get("/threads/{thread_id}/runs/{run_id}/wait", tags=["Runs"])
async def wait_for_run(
        thread_id: str,
        run_id: str,
        timeout: float = Query(default=30.0, ge=0.5, le=300.0),
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> dict[str, Any]:
    """Wait for a run to reach a terminal state (polling)."""
    _get_thread_with_auth(thread_id, current_user)

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        run = db.get_run(run_id)
        if run is None or run.get("thread_id") != thread_id:
            raise HTTPException(status_code=404, detail="Run not found")
        status = run.get("status")
        if status in ("success", "error", "cancelled", "timeout"):
            return _api_run(run)
        await asyncio.sleep(0.1)

    run = db.get_run(run_id)
    return _api_run(run) if run else {"run_id": run_id, "status": "timeout"}


@app.get("/threads/{thread_id}", tags=["Threads"])
async def get_thread(
        thread_id: str,
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user)
) -> dict[str, Any]:
    """Get thread state."""
    thread = _get_thread_with_auth(thread_id, current_user)
    return _api_thread(thread)


@app.get("/threads/{thread_id}/history", tags=["Threads"])
async def get_thread_history(
        thread_id: str,
        limit: int = Query(default=10, ge=1, le=100),
        before: str | None = Query(default=None),
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return thread checkpoint history from the checkpointer (or DB fallback)."""
    _get_thread_with_auth(thread_id, current_user)
    if limit <= 0:
        return []
    return await _resolve_thread_history(thread_id, limit=limit, before=before)


@app.post("/threads/{thread_id}/history", tags=["Threads"])
async def get_thread_history_post(
        thread_id: str,
        body: ThreadHistoryRequest = ThreadHistoryRequest(),
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return thread checkpoint history (POST compatibility for frontend clients)."""
    _get_thread_with_auth(thread_id, current_user)
    if body.limit <= 0:
        return []
    return await _resolve_thread_history(thread_id, limit=body.limit, before=body.before)


@app.post("/threads/{thread_id}/runs/{run_id}/cancel", tags=["Runs"])
async def cancel_run(
        thread_id: str,
        run_id: str,
        wait: bool = Query(default=False),
        action: str = Query(default="interrupt"),
        current_user: Auth.types.MinimalUserDict = Depends(get_current_user)
) -> dict[str, Any]:
    """Cancel a run."""
    # Ensure thread belongs to authenticated user/is accessible
    _get_thread_with_auth(thread_id, current_user)

    run = db.get_run(run_id)
    if run is None or run["thread_id"] != thread_id:
        raise HTTPException(status_code=404, detail="Run not found")

    async with _task_lock:
        task = _active_tasks.pop(run_id, None)
        if task:
            task.cancel()
        db.update_run_status(run_id, "cancelled")

    if wait:
        # Allow cancellation propagation to settle.
        await asyncio.sleep(0.05)

    updated = db.get_run(run_id) or {**run, "status": "cancelled"}
    return _api_run(updated)


if __name__ == "__main__":
    # For development with uvicorn: python run.py
    # For production: uvicorn server:app --port 2024
    from research_agent.run import main

    main()
