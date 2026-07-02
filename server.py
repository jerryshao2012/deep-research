"""DEPRECATED — Custom LangGraph Platform API server.

⚠️  This file is deprecated.  Use the official LangGraph Platform server instead::

        langgraph dev

    The official server provides the full LangGraph Platform API surface
    (threads, runs, SSE streaming, checkpoint-based persistence, Studio UI)
    that this custom implementation attempted to replicate.  After extensive
    testing we found that the official platform is the only reliable way to
    serve a LangGraph agent to the ``@langchain/langgraph-sdk`` client.

    This file is kept for reference only and will be removed in a future release.

    For document uploads (port 8000), run::

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
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import time
from fastapi import Depends, HTTPException, Request, Query
from fastapi.openapi.utils import get_openapi
from langgraph_sdk import Auth
from pydantic import BaseModel, Field

# Import DB wrapper
import db
# Import the actual deep_research agent
from agent import agent, RECURSION_LIMIT
# Import shared authentication logic
from auth import authenticate_credential
from research_agent.prompts import RESEARCHER_DESCRIPTION
# Import the existing app and settings from webapp
from webapp import app

# Track active background tasks to allow cancellation
_active_tasks: dict[str, asyncio.Task] = {}
# Lock to synchronize task modification operations
_task_lock = asyncio.Lock()


def custom_openapi() -> dict[str, Any]:
    """Build an explicit OpenAPI document for the async subagent server."""
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


def _api_thread(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread_id": thread.get("thread_id"),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at") or thread.get("created_at"),
        "state_updated_at": thread.get("state_updated_at"),
        "metadata": thread.get("metadata") or {},
        "status": thread.get("status") or "idle",
        "config": thread.get("config") or {},
        "values": thread.get("values") or None,
    }


def _map_run_status_for_api(status: str | None) -> str:
    # Keep API-compatible enum for clients expecting interrupted rather than cancelled.
    if status == "cancelled":
        return "interrupted"
    return status or "pending"


def _api_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "thread_id": run.get("thread_id"),
        "assistant_id": run.get("assistant_id"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at") or run.get("created_at"),
        "status": _map_run_status_for_api(run.get("status")),
        "metadata": run.get("metadata") or {},
        "kwargs": run.get("kwargs") or {},
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
        "values": thread.get("values") or {},
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
                values = checkpoint.get("values") or checkpoint.get("channel_values")
                if values:
                    # Serialize message objects to make the response JSON-safe
                    msgs = values.get("messages", [])
                    values["messages"] = [serialize_message(m) for m in msgs]
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
            out["id"] = str(uuid.uuid4())
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
        "id": msg_id or str(uuid.uuid4()),
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


# ── Run executor ──────────────────────────────────────────────────────────────

async def _stream_run_events(
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
    initial_values = dict(input_state)
    initial_values["messages"] = [
        serialize_message(m) for m in input_state.get("messages", [])
    ]
    yield _sse_frame("values", initial_values, event_id=seq)
    seq += 1

    _tool_start_times: dict[str, float] = {}
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
                    tool_msg = serialize_message(output)
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

        # ── Fallback 3: emit any new AI messages that weren't captured during stream ──
        for m in messages[_initial_msg_count:]:
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
                    mid = f"{run_id}-msg-{seq}"
                    _emitted_message_ids.add(mid)
                yield _sse_frame("messages", [{
                    "type": "ai",
                    "id": mid,
                    "content": str(c),
                }], event_id=seq)
                seq += 1

        serialized_messages = [serialize_message(m) for m in messages]
        values_dict["messages"] = serialized_messages

        # Persist final state to DB for thread listing/search
        try:
            db.update_thread(thread_id, serialized_messages, {
                "messages": serialized_messages,
                "files": values_dict.get("files", {}),
                "doc_folder": values_dict.get("doc_folder"),
                "skill": values_dict.get("skill"),
                "no_web": values_dict.get("no_web"),
                "wiki_query_complete": values_dict.get("wiki_query_complete", False),
            })
            db.update_run_status(run_id, "success")
        except Exception as _db_err:
            _logger.warning("[stream %s] DB sync failed: %s", run_id, _db_err)

        yield _sse_frame("values", values_dict, event_id=seq)
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


async def _execute_run(run_id: str, thread_id: str) -> None:
    """Invoke the agent and persist the result; called as a fire-and-forget task.

    Wiki context injection and sufficiency evaluation are handled by the agent's
    ``ResearchStateMiddleware`` — this function only loads thread state, invokes
    the agent with the proper ``thread_id`` config, and persists the result.
    """
    _logger = logging.getLogger(__name__)
    db.update_run_status(run_id, "running")
    try:
        # Load all existing messages and state values on the thread
        thread = db.get_thread(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found during run execution")

        existing_values = thread.get("values") or {}
        existing_files = existing_values.get("files") or {}
        messages = thread.get("messages") or []

        # Initialize per-thread cited_response tracking for the middleware
        existing_reports = [k for k in existing_files if k.startswith("/cited_response")]
        from research_agent.utils.knowledge_filesystem import _thread_existing_cited_responses
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

        # Pass thread_id through config so ResearchStateMiddleware can access it
        # for wiki lookups, cited_response tracking, and eval logging.
        config = {
            "configurable": {"thread_id": str(thread_id)},
            "recursion_limit": RECURSION_LIMIT,
        }
        result = await agent.ainvoke(input_state, config=config)

        # Check if this run has been cancelled while executing
        async with _task_lock:
            run_data = db.get_run(run_id)
            if run_data and run_data.get("status") == "cancelled":
                return

        # ── Citation validation (post-execution) ─────────────────────────
        files = result.get("files", {})
        existing_reports_result = result.get("existing_reports")
        if not existing_reports_result:
            existing_reports_result = _thread_existing_cited_responses.get(str(thread_id), [])
        from research_agent.utils.knowledge_filesystem import get_active_cited_response_path
        active_report_path = get_active_cited_response_path(files, existing_reports_result)

        if active_report_path in files:
            from deepagents.backends.utils import file_data_to_string, create_file_data
            report_data = files[active_report_path]
            report_text = file_data_to_string(report_data)
            if os.getenv("DEEP_RESEARCH_VALIDATE_CITATIONS") == "1":
                from thread_wiki.service import _extract_citations
                from research_agent.utils.citation_validator import validate_web_citations

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

        # Serialize messages
        serialized_messages = [serialize_message(m) for m in result.get("messages", [])]

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
        from research_agent.utils.knowledge_filesystem import (
            _thread_wiki_query_complete,
            _thread_existing_cited_responses,
        )
        wiki_query_complete = result.get("wiki_query_complete")
        if not wiki_query_complete and str(thread_id) in _thread_wiki_query_complete:
            wiki_query_complete = _thread_wiki_query_complete[str(thread_id)]

        existing_reports_db = result.get("existing_reports")
        if not existing_reports_db:
            existing_reports_db = _thread_existing_cited_responses.get(str(thread_id), [])

        # Persist to DB (for thread listing/search — checkpointer handles state)
        serializable_result = {
            "messages": serialized_messages,
            "files": result.get("files", {}),
            "doc_folder": result.get("doc_folder"),
            "skill": result.get("skill"),
            "no_web": result.get("no_web"),
            "wiki_query_complete": wiki_query_complete,
            "existing_reports": existing_reports_db,
        }
        db.update_thread(thread_id, serialized_messages, serializable_result)
        db.update_run_status(run_id, "success")

    except asyncio.CancelledError:
        db.update_run_status(run_id, "cancelled")
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
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
    from research_agent.utils.knowledge_filesystem import clear_thread_cache
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
            serialized_messages = [
                serialize_message(m) for m in snapshot.values.get("messages", [])
            ]
            values = dict(snapshot.values)
            values["messages"] = serialized_messages
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
        "values": thread.get("values") or {},
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
    user_message = next((m.content for m in messages if m.role == "user"), "")

    if user_message:
        existing = thread.get("messages") or []
        existing.append({"role": "user", "content": user_message})
        db.update_thread(thread_id, existing, thread.get("values") or {})

    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    assistant_id = body.assistant_id or "researcher"
    db.create_run(run_id, thread_id, assistant_id, now, multitask_strategy=multitask_strategy or "enqueue")

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

    # Record the run
    db.create_run(
        run_id, thread_id,
        body.assistant_id or "researcher", now,
        multitask_strategy=body.multitask_strategy or "enqueue",
    )

    # Build input state from current thread values
    thread = db.get_thread(thread_id)
    existing_values = (thread or {}).get("values") or {}
    existing_files = existing_values.get("files") or {}

    # Parse incoming messages and append to thread messages
    messages = list(thread.get("messages") or [])
    if isinstance(body.input, dict):
        raw_messages = body.input.get("messages", [])
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
    from research_agent.utils.knowledge_filesystem import _thread_existing_cited_responses
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
    from run import main

    main()
