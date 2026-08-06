"""Contract tests for frontend-used API endpoints and stream event structure."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)

sys.path.append(str(Path(__file__).resolve().parents[1]))

from research_agent import db, server


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    monkeypatch.setenv("ALLOW_ALL_THREADS", "true")
    monkeypatch.setenv("DB_TYPE", "sqlite")
    conn = db._get_sqlite_conn()
    conn.executescript("DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS threads;")
    db._init_sqlite()


@pytest.fixture()
def client():
    return TestClient(server.app)


def _fake_response(*_args, **_kwargs):
    return {
        "messages": [AIMessage(content="streamed answer")],
        "files": {"note.md": "done"},
    }


def _configure_stream_agent(mock_agent):
    async def _events(*_args, **_kwargs):
        yield {
            "event": "on_tool_end",
            "name": "think",
            "run_id": "tool-call",
            "data": {"output": {"type": "tool", "content": "done"}},
        }

    mock_agent.astream_events = _events
    mock_agent.aget_state = AsyncMock(
        return_value=SimpleNamespace(values=_fake_response())
    )


def _parse_sse_frames(frames):
    parsed = []
    for frame in frames:
        event = next(
            line.removeprefix("event: ")
            for line in frame.splitlines()
            if line.startswith("event: ")
        )
        data = next(
            json.loads(line.removeprefix("data: "))
            for line in frame.splitlines()
            if line.startswith("data: ")
        )
        parsed.append((event, data))
    return parsed


class _RoundStreamAgent:
    def __init__(self, states, events):
        self.states = states
        self.events = events
        self.current = states[0]
        self.round = 0
        self.stream_calls = []
        self.update_calls = []

    async def aget_state(self, _config):
        return SimpleNamespace(values=self.current)

    async def astream_events(self, input_state, *, config, version):
        self.stream_calls.append((input_state, config, version))
        for event in self.events[self.round]:
            yield event
        self.round += 1
        self.current = self.states[self.round]

    async def aupdate_state(self, config, values):
        self.update_calls.append((config, values))
        self.current = dict(self.current)
        self.current["messages"] = [
            *self.current.get("messages", []),
            *values.get("messages", []),
        ]


def _seed_stream_resume_run(*, candidate="Please continue!"):
    thread_id = "stream-resume-thread"
    run_id = "stream-resume-run"
    created_at = "2026-07-24T00:00:00+00:00"
    initial_messages = [
        HumanMessage(content="Please continue!", id="resume-request"),
        AIMessage(content="OLD ANSWER", id="old-answer"),
        HumanMessage(content="later unrelated thread message", id="later-message"),
    ]
    pending = [{"content": "SENSITIVE TODO", "status": "pending"}]
    db.create_thread(
        thread_id,
        "test-admin",
        created_at,
        values={
            "messages": [server.serialize_message(message) for message in initial_messages],
            "todos": pending,
            "files": {"/round.txt": "initial"},
            "custom_public": {"kept": True},
        },
    )
    db.create_run(
        run_id,
        thread_id,
        "researcher",
        created_at,
        kwargs={"_resume_candidate": candidate},
    )
    return thread_id, run_id, initial_messages, pending


def test_stream_resume_runs_hidden_rounds_and_emits_only_new_final(caplog):
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    hidden = AIMessage(
        content="SENSITIVE INTERMEDIATE",
        id="hidden-round-1",
        response_metadata={"resume_intermediate": True},
    )
    tool = ToolMessage(
        content="tool progress",
        id="tool-round-1",
        tool_call_id="call-1",
    )
    final = AIMessage(content="FINAL RESULT", id="final-round-2")
    completed = [{"content": "SENSITIVE TODO", "status": "completed"}]
    states = [
        {
            "messages": initial_messages,
            "todos": pending,
            "files": {"/round.txt": "initial"},
            "custom_public": {"kept": True},
        },
        {
            "messages": [*initial_messages, hidden, tool],
            "todos": pending,
            "files": {"/round.txt": "round one"},
            "custom_public": {"kept": True},
        },
        {
            "messages": [*initial_messages, hidden, tool, final],
            "todos": completed,
            "files": {"/round.txt": "round two"},
            "custom_public": {"kept": True},
        },
    ]
    text_events = [
        {
            "event": event_type,
            "name": "researcher",
            "data": {
                "chunk" if event_type.endswith("stream") else "output": AIMessage(
                    content="SENSITIVE INTERMEDIATE",
                    id=f"{event_type}-hidden",
                )
            },
        }
        for event_type in (
            "on_chat_model_stream",
            "on_chat_model_end",
            "on_chain_end",
            "on_llm_stream",
            "on_llm_end",
        )
    ]
    agent = _RoundStreamAgent(
        states,
        [
            [
                *text_events,
                {
                    "event": "on_tool_end",
                    "name": "search",
                    "run_id": "call-1",
                    "data": {"output": tool},
                },
                {
                    "event": "on_chain_stream",
                    "name": "researcher",
                    "data": {
                        "chunk": {
                            "agent": {
                                "messages": [{
                                    "type": "ai",
                                    "content": "NESTED STREAM LEAK",
                                }]
                            }
                        }
                    },
                },
                {
                    "event": "on_custom_event",
                    "name": "progress",
                    "data": {"chunk": {"progress": {"percent": 50}}},
                },
                {
                    "event": "on_custom_event",
                    "name": "progress",
                    "data": {
                        "chunk": {
                            "progress": AIMessage(
                                content="OBJECT MESSAGE LEAK"
                            )
                        }
                    },
                },
                {
                    "event": "on_chain_stream",
                    "name": "researcher",
                    "data": {
                        "chunk": {
                            "progress": [
                                AIMessageChunk(
                                    content="OBJECT CHUNK LEAK"
                                ),
                                {
                                    "nested": AIMessage(
                                        content="SEQUENCE MESSAGE LEAK"
                                    )
                                },
                            ]
                        }
                    },
                },
                {
                    "event": "on_custom_event",
                    "name": "progress",
                    "data": {
                        "chunk": {
                            "progress": {
                                "type": "ai",
                                "content": "SERIALIZED TYPE LEAK",
                            }
                        }
                    },
                },
                {
                    "event": "on_chain_stream",
                    "name": "researcher",
                    "data": {
                        "chunk": {
                            "progress": [
                                {
                                    "role": "assistant",
                                    "content": "SERIALIZED ROLE LEAK",
                                },
                                {
                                    "type": "AIMessageChunk",
                                    "content": "SERIALIZED CHUNK LEAK",
                                },
                            ]
                        }
                    },
                },
                {
                    "event": "on_custom_event",
                    "name": "progress",
                    "data": {
                        "chunk": {
                            "phase": "research",
                            "current": 1,
                            "total": 2,
                        }
                    },
                },
            ],
            [],
        ],
    )
    input_state = {
        "messages": initial_messages,
        "todos": pending,
        "files": {"/round.txt": "initial"},
    }
    persisted = []
    real_update_thread = db.update_thread

    def recording_update_thread(thread, messages, values):
        persisted.append((messages, values))
        return real_update_thread(thread, messages, values)

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                input_state,
            )
        ]

    caplog.set_level("INFO", logger=server.__name__)
    with (
        patch.object(server, "agent", agent),
        patch.object(db, "update_thread", side_effect=recording_update_thread),
    ):
        parsed = _parse_sse_frames(__import__("asyncio").run(collect()))

    initial_metadata = [
        data for event, data in parsed
        if event == "metadata" and "resume_round" not in data
    ]
    progress_metadata = [
        data for event, data in parsed
        if event == "metadata" and "resume_round" in data
    ]
    assert len(initial_metadata) == 1
    assert progress_metadata == [{
        "run_id": run_id,
        "thread_id": thread_id,
        "status": "running",
        "resume_round": 2,
        "resume_max_rounds": 3,
        "incomplete_todo_count": 1,
    }]

    message_payloads = [
        message
        for event, data in parsed
        if event == "messages"
        for message in data
    ]
    assert [message["content"] for message in message_payloads] == ["FINAL RESULT"]
    assert sum(event == "updates" for event, _ in parsed) == 3
    assert "NESTED STREAM LEAK" not in json.dumps(parsed)
    assert "OBJECT MESSAGE LEAK" not in json.dumps(parsed)
    assert "OBJECT CHUNK LEAK" not in json.dumps(parsed)
    assert "SEQUENCE MESSAGE LEAK" not in json.dumps(parsed)
    assert "SERIALIZED TYPE LEAK" not in json.dumps(parsed)
    assert "SERIALIZED ROLE LEAK" not in json.dumps(parsed)
    assert "SERIALIZED CHUNK LEAK" not in json.dumps(parsed)
    assert any(
        event == "updates" and data.get("progress") == {"percent": 50}
        for event, data in parsed
    )
    assert any(
        event == "updates"
        and data == {"phase": "research", "current": 1, "total": 2}
        for event, data in parsed
    )
    final_values = [data for event, data in parsed if event == "values"][-1]
    assert final_values["todos"] == completed
    assert final_values["files"] == {"/round.txt": "round two"}
    assert final_values["custom_public"] == {"kept": True}
    assert "SENSITIVE INTERMEDIATE" not in json.dumps(final_values)
    assert [data["status"] for event, data in parsed if event == "end"] == ["success"]
    assert db.get_run(run_id)["status"] == "success"

    assert len(agent.stream_calls) == 2
    for round_number, (_, config, version) in enumerate(agent.stream_calls, start=1):
        assert version == "v2"
        assert config["configurable"] == {
            "thread_id": thread_id,
            "resume_incomplete_todos": True,
            "resume_round": round_number,
            "resume_max_rounds": 3,
        }
    assert any(
        values["todos"] == pending
        and values["files"] == {"/round.txt": "round one"}
        and "SENSITIVE INTERMEDIATE" not in json.dumps(values)
        for _, values in persisted
    )

    resume_logs = [
        record.getMessage() for record in caplog.records
        if "resume_stream" in record.getMessage()
    ]
    assert resume_logs
    assert all(
        field in resume_logs[-1]
        for field in (
            f"thread_id={thread_id}",
            f"run_id={run_id}",
            "round=",
            "max_rounds=",
            "incomplete_count=",
            "malformed_count=",
            "stop_reason=",
        )
    )
    assert "SENSITIVE TODO" not in "\n".join(resume_logs)
    assert "SENSITIVE INTERMEDIATE" not in "\n".join(resume_logs)
    assert "Please continue!" not in "\n".join(resume_logs)


def test_stream_resume_redacts_nested_messages_from_tool_updates():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    final = AIMessage(content="FINAL RESULT", id="final-after-tools")
    completed = [{"content": "SENSITIVE TODO", "status": "completed"}]
    safe_tool = ToolMessage(
        content="safe tool result",
        id="safe-tool-message",
        name="safe_tool",
        tool_call_id="safe-tool-call",
    )
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": pending, "files": {}},
            {
                "messages": [*initial_messages, final],
                "todos": completed,
                "files": {},
            },
        ],
        [[
            {
                "event": "on_tool_end",
                "name": "serialized_tool",
                "run_id": "serialized-run",
                "data": {
                    "output": {
                        "messages": [{
                            "type": "ai",
                            "content": "SERIALIZED TOOL LEAK",
                        }]
                    }
                },
            },
            {
                "event": "on_tool_end",
                "name": "object_tool",
                "run_id": "object-run",
                "data": {
                    "output": {
                        "result": {
                            "message": AIMessage(content="OBJECT TOOL LEAK")
                        }
                    }
                },
            },
            {
                "event": "on_tool_end",
                "name": "sequence_tool",
                "run_id": "sequence-run",
                "data": {
                    "output": [
                        {
                            "payload": AIMessageChunk(
                                content="CHUNK TOOL LEAK"
                            )
                        },
                        {
                            "role": "assistant",
                            "content": "SEQUENCE TOOL LEAK",
                        },
                    ]
                },
            },
            {
                "event": "on_tool_end",
                "name": "safe_tool",
                "run_id": "safe-tool-run",
                "data": {"output": safe_tool},
            },
            {
                "event": "on_tool_end",
                "name": "scalar_tool",
                "run_id": "scalar-run",
                "data": {"output": "safe scalar result"},
            },
        ]],
    )

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {
                    "messages": initial_messages,
                    "todos": pending,
                    "files": {},
                },
            )
        ]

    with patch.object(server, "agent", agent):
        parsed = _parse_sse_frames(__import__("asyncio").run(collect()))

    updates = [
        data["tools"]["messages"][0]
        for event, data in parsed
        if event == "updates"
    ]
    assert len(updates) == 5
    serialized_updates = json.dumps(updates)
    assert "SERIALIZED TOOL LEAK" not in serialized_updates
    assert "OBJECT TOOL LEAK" not in serialized_updates
    assert "CHUNK TOOL LEAK" not in serialized_updates
    assert "SEQUENCE TOOL LEAK" not in serialized_updates
    for update, tool_name, tool_run_id in zip(
            updates[:3],
            ("serialized_tool", "object_tool", "sequence_tool"),
            ("serialized-run", "object-run", "sequence-run"),
            strict=True,
    ):
        assert update == {
            "type": "tool",
            "id": tool_run_id,
            "name": tool_name,
            "content": "",
            "tool_call_id": tool_run_id,
        }
    assert updates[3]["type"] == "tool"
    assert updates[3]["content"] == "safe tool result"
    assert updates[3]["tool_call_id"] == "safe-tool-call"
    assert updates[4] == {
        "type": "tool",
        "id": "scalar-run",
        "name": "scalar_tool",
        "content": "safe scalar result",
        "tool_call_id": "scalar-run",
    }


def test_stream_resume_uses_stored_candidate_not_latest_thread_message():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    completed = [{"content": "SENSITIVE TODO", "status": "completed"}]
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": pending, "files": {}},
            {
                "messages": [
                    *initial_messages,
                    AIMessage(
                        content="hidden",
                        id="hidden",
                        response_metadata={"resume_intermediate": True},
                    ),
                ],
                "todos": pending,
                "files": {},
            },
            {
                "messages": [
                    *initial_messages,
                    AIMessage(content="finished", id="finished"),
                ],
                "todos": completed,
                "files": {},
            },
        ],
        [[], []],
    )

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {"messages": initial_messages, "todos": pending, "files": {}},
            )
        ]

    with patch.object(server, "agent", agent):
        __import__("asyncio").run(collect())

    assert len(agent.stream_calls) == 2
    assert all(
        call[1]["configurable"]["resume_incomplete_todos"] is True
        for call in agent.stream_calls
    )


def test_stream_resume_rechecks_cancellation_before_next_round():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": pending, "files": {}},
            {"messages": initial_messages, "todos": pending, "files": {}},
            {"messages": initial_messages, "todos": pending, "files": {}},
        ],
        [[], []],
    )

    async def collect_and_cancel():
        frames = []
        async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {"messages": initial_messages, "todos": pending, "files": {}},
        ):
            frames.append(frame)
            if (
                    "event: metadata\n" in frame
                    and '"resume_round": 2' in frame
            ):
                db.update_run_status(run_id, "cancelled")
        return frames

    with patch.object(server, "agent", agent):
        parsed = _parse_sse_frames(
            __import__("asyncio").run(collect_and_cancel())
        )

    assert len(agent.stream_calls) == 1
    assert [data["status"] for event, data in parsed if event == "end"] == [
        "interrupted"
    ]
    assert db.get_run(run_id)["status"] == "cancelled"


def test_stream_resume_does_not_restart_run_cancelled_before_first_round():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    db.update_run_status(run_id, "cancelled")
    agent = _RoundStreamAgent(
        [{"messages": initial_messages, "todos": pending, "files": {}}],
        [],
    )

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {"messages": initial_messages, "todos": pending, "files": {}},
            )
        ]

    with patch.object(server, "agent", agent):
        parsed = _parse_sse_frames(__import__("asyncio").run(collect()))

    assert agent.stream_calls == []
    assert [data["status"] for event, data in parsed if event == "end"] == [
        "interrupted"
    ]
    assert db.get_run(run_id)["status"] == "cancelled"


def test_stream_resume_disconnect_before_first_round_marks_run_cancelled():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    agent = _RoundStreamAgent(
        [{"messages": initial_messages, "todos": pending, "files": {}}],
        [],
    )

    async def start_and_disconnect():
        stream = server._stream_run_events(
            thread_id,
            run_id,
            {"messages": initial_messages, "todos": pending, "files": {}},
        )
        first = await anext(stream)
        await stream.aclose()
        return first

    with patch.object(server, "agent", agent):
        first = __import__("asyncio").run(start_and_disconnect())

    assert "event: metadata\n" in first
    assert agent.stream_calls == []
    assert db.get_run(run_id)["status"] == "cancelled"


def test_stream_resume_close_after_end_preserves_success():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    completed = [{"content": "SENSITIVE TODO", "status": "completed"}]
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": pending, "files": {}},
            {
                "messages": [
                    *initial_messages,
                    AIMessage(content="finished", id="finished"),
                ],
                "todos": completed,
                "files": {},
            },
        ],
        [[]],
    )

    async def consume_through_end_and_close():
        stream = server._stream_run_events(
            thread_id,
            run_id,
            {"messages": initial_messages, "todos": pending, "files": {}},
        )
        end_frame = ""
        async for frame in stream:
            if "event: end\n" in frame:
                end_frame = frame
                assert db.get_run(run_id)["status"] == "success"
                break
        await stream.aclose()
        return end_frame

    with patch.object(server, "agent", agent):
        end_frame = __import__("asyncio").run(
            consume_through_end_and_close()
        )

    assert '"status": "success"' in end_frame
    assert db.get_run(run_id)["status"] == "success"


def test_stream_resume_close_at_final_values_cancels_before_end():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    completed = [{"content": "SENSITIVE TODO", "status": "completed"}]
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": pending, "files": {}},
            {
                "messages": [
                    *initial_messages,
                    AIMessage(content="finished", id="finished"),
                ],
                "todos": completed,
                "files": {},
            },
        ],
        [[]],
    )

    async def consume_final_values_and_close():
        stream = server._stream_run_events(
            thread_id,
            run_id,
            {"messages": initial_messages, "todos": pending, "files": {}},
        )
        final_values = ""
        async for frame in stream:
            if (
                    "event: values\n" in frame
                    and '"status": "completed"' in frame
            ):
                final_values = frame
                break
        status_before_close = db.get_run(run_id)["status"]
        await stream.aclose()
        return final_values, status_before_close

    with patch.object(server, "agent", agent):
        final_values, status_before_close = __import__("asyncio").run(
            consume_final_values_and_close()
        )

    assert '"status": "completed"' in final_values
    assert status_before_close == "running"
    assert db.get_run(run_id)["status"] == "cancelled"


def test_completed_resume_phrase_streams_ordinary_events_without_progress_metadata():
    thread_id, run_id, initial_messages, _ = _seed_stream_resume_run()
    completed = [{"content": "done", "status": "completed"}]
    final = AIMessage(content="ordinary streamed answer", id="ordinary-final")
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": completed, "files": {}},
            {
                "messages": [*initial_messages, final],
                "todos": completed,
                "files": {},
            },
        ],
        [[{
            "event": "on_chat_model_stream",
            "name": "researcher",
            "data": {"chunk": final},
        }]],
    )

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {"messages": initial_messages, "todos": completed, "files": {}},
            )
        ]

    with patch.object(server, "agent", agent):
        parsed = _parse_sse_frames(__import__("asyncio").run(collect()))

    assert len(agent.stream_calls) == 1
    assert agent.stream_calls[0][1]["configurable"] == {"thread_id": thread_id}
    assert not any(
        event == "metadata" and "resume_round" in data
        for event, data in parsed
    )
    assert any(
        event == "messages"
        and any(message["content"] == "ordinary streamed answer" for message in data)
        for event, data in parsed
    )


def test_stream_resume_round_limit_updates_checkpoint_before_summary(monkeypatch):
    monkeypatch.setenv("MAX_RESUME_ROUNDS", "1")
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    hidden = AIMessage(
        content="hidden limit round",
        id="hidden-limit",
        response_metadata={"resume_intermediate": True},
    )
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": pending, "files": {}},
            {
                "messages": [*initial_messages, hidden],
                "todos": pending,
                "files": {"/recovery.txt": "saved"},
            },
        ],
        [[]],
    )

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {"messages": initial_messages, "todos": pending, "files": {}},
            )
        ]

    with patch.object(server, "agent", agent):
        parsed = _parse_sse_frames(__import__("asyncio").run(collect()))

    assert len(agent.update_calls) == 1
    update_config, update_values = agent.update_calls[0]
    assert update_config["configurable"]["resume_round"] == 1
    summary = update_values["messages"][0].content
    assert "safety limit" in summary.lower()
    message_contents = [
        message["content"]
        for event, data in parsed
        if event == "messages"
        for message in data
    ]
    assert message_contents == [summary]
    assert db.get_thread(thread_id)["values"]["files"] == {
        "/recovery.txt": "saved"
    }


def test_stream_resume_round_limit_rejects_stale_identical_summary(monkeypatch):
    monkeypatch.setenv("MAX_RESUME_ROUNDS", "1")
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    old_summary = AIMessage(
        content=server.build_round_limit_message(
            server.inspect_todos(pending),
            1,
        ),
        id="old-summary",
    )
    messages = [*initial_messages, old_summary]
    agent = _RoundStreamAgent(
        [
            {"messages": messages, "todos": pending, "files": {}},
            {"messages": messages, "todos": pending, "files": {}},
        ],
        [[]],
    )

    async def ignore_update(config, values):
        agent.update_calls.append((config, values))

    agent.aupdate_state = ignore_update

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {"messages": messages, "todos": pending, "files": {}},
            )
        ]

    with patch.object(server, "agent", agent):
        parsed = _parse_sse_frames(__import__("asyncio").run(collect()))

    assert len(agent.update_calls) == 1
    assert any(event == "error" for event, _ in parsed)
    assert [data["status"] for event, data in parsed if event == "end"] == [
        "error"
    ]
    assert db.get_run(run_id)["status"] == "error"


def test_stream_resume_no_id_final_has_stable_public_id():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    completed = [{"content": "SENSITIVE TODO", "status": "completed"}]
    final = AIMessage(content="NO ID FINAL")
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": pending, "files": {}},
            {
                "messages": [*initial_messages, final],
                "todos": completed,
                "files": {},
            },
        ],
        [[]],
    )

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {"messages": initial_messages, "todos": pending, "files": {}},
            )
        ]

    with patch.object(server, "agent", agent):
        parsed = _parse_sse_frames(__import__("asyncio").run(collect()))

    message_id = next(
        message["id"]
        for event, data in parsed
        if event == "messages"
        for message in data
        if message["content"] == "NO ID FINAL"
    )
    values_id = next(
        message["id"]
        for event, data in parsed
        if event == "values"
        for message in data["messages"]
        if message["content"] == "NO ID FINAL"
    )
    db_id = next(
        message["id"]
        for message in db.get_thread(thread_id)["values"]["messages"]
        if message["content"] == "NO ID FINAL"
    )
    assert message_id == values_id == db_id
    assert final.id is None


def test_stream_resume_duplicate_no_id_finals_have_unique_stable_ids():
    thread_id, run_id, initial_messages, pending = _seed_stream_resume_run()
    completed = [{"content": "SENSITIVE TODO", "status": "completed"}]
    finals = [
        AIMessage(content="IDENTICAL FINAL"),
        AIMessage(content="IDENTICAL FINAL"),
    ]
    agent = _RoundStreamAgent(
        [
            {"messages": initial_messages, "todos": pending, "files": {}},
            {
                "messages": [*initial_messages, *finals],
                "todos": completed,
                "files": {},
            },
        ],
        [[]],
    )

    async def collect():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {"messages": initial_messages, "todos": pending, "files": {}},
            )
        ]

    with patch.object(server, "agent", agent):
        parsed = _parse_sse_frames(__import__("asyncio").run(collect()))

    message_ids = [
        message["id"]
        for event, data in parsed
        if event == "messages"
        for message in data
        if message["content"] == "IDENTICAL FINAL"
    ]
    values_ids = [
        message["id"]
        for event, data in parsed
        if event == "values"
        for message in data["messages"]
        if message["content"] == "IDENTICAL FINAL"
    ]
    db_ids = [
        message["id"]
        for message in db.get_thread(thread_id)["values"]["messages"]
        if message["content"] == "IDENTICAL FINAL"
    ]
    assert len(message_ids) == 2
    assert len(set(message_ids)) == 2
    assert message_ids == values_ids == db_ids
    assert all(message.id is None for message in finals)


def test_frontend_used_paths_are_present_in_openapi(client):
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json().get("paths", {})

    required = [
        "/ok",
        "/health",
        "/auth/session/validate",
        "/auth/logout",
        "/threads",
        "/threads/search",
        "/threads/{thread_id}",
        "/threads/{thread_id}/state",
        "/threads/{thread_id}/runs",
        "/threads/{thread_id}/runs/stream",
        "/threads/{thread_id}/runs/{run_id}",
        "/threads/{thread_id}/runs/{run_id}/cancel",
    ]

    for p in required:
        assert p in paths, f"missing path in OpenAPI: {p}"


def test_thread_lifecycle_contract(client):
    created = client.post("/threads", json={"metadata": {"assistant_id": "abc"}})
    assert created.status_code == 200
    thread = created.json()

    assert "thread_id" in thread
    assert "created_at" in thread
    assert "updated_at" in thread
    assert "status" in thread
    assert "metadata" in thread
    assert "values" in thread

    thread_id = thread["thread_id"]

    search = client.post(
        "/threads/search",
        json={
            "limit": 20,
            "offset": 0,
            "sort_by": "updated_at",
            "sort_order": "desc",
        },
    )
    assert search.status_code == 200
    payload = search.json()
    assert isinstance(payload, list)
    assert any(t["thread_id"] == thread_id for t in payload)

    patched = client.patch(
        f"/threads/{thread_id}",
        json={"metadata": {"custom_title": "hello", "title_source": "user"}},
    )
    assert patched.status_code == 200
    assert patched.json()["metadata"]["custom_title"] == "hello"

    state_updated = client.post(
        f"/threads/{thread_id}/state",
        json={"values": {"files": {"a.txt": "abc"}}},
    )
    assert state_updated.status_code == 200
    assert "checkpoint" in state_updated.json()

    got = client.get(f"/threads/{thread_id}")
    assert got.status_code == 200
    assert got.json()["values"]["files"]["a.txt"] == "abc"

    deleted = client.delete(f"/threads/{thread_id}")
    assert deleted.status_code == 200

    missing = client.get(f"/threads/{thread_id}")
    assert missing.status_code == 404


def test_run_contract_and_stream_events(client):
    thread_id = client.post("/threads").json()["thread_id"]

    with patch.object(server, "agent") as mock_agent:
        _configure_stream_agent(mock_agent)

        stream_resp = client.post(
            f"/threads/{thread_id}/runs/stream",
            json={
                "assistant_id": "researcher",
                "input": {"messages": [{"role": "user", "content": "hello"}]},
            },
        )

    assert stream_resp.status_code == 200
    assert stream_resp.headers["content-type"].startswith("text/event-stream")

    body = stream_resp.text
    assert "event: metadata" in body
    assert "event: updates" in body
    assert "event: values" in body
    assert "event: end" in body

    # Parse at least one values payload as valid JSON.
    values_lines = [line for line in body.splitlines() if line.startswith("data: ")]
    assert values_lines
    parsed_any = False
    for line in values_lines:
        obj = json.loads(line.removeprefix("data: "))
        if isinstance(obj, dict) and ("messages" in obj or "run_id" in obj):
            parsed_any = True
            break
    assert parsed_any

    runs = client.get(f"/threads/{thread_id}/runs")
    assert runs.status_code == 200
    run_list = runs.json()
    assert isinstance(run_list, list)
    assert run_list
    run_id = run_list[0]["run_id"]

    single = client.get(f"/threads/{thread_id}/runs/{run_id}")
    assert single.status_code == 200
    run_payload = single.json()
    assert run_payload["status"] in {"pending", "running", "success", "error", "timeout", "interrupted"}

    cancel = client.post(f"/threads/{thread_id}/runs/{run_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "interrupted"


def test_stream_run_binds_resume_candidate_from_own_request(client):
    thread_id = client.post("/threads").json()["thread_id"]
    db.update_thread(
        thread_id,
        [{"role": "user", "content": "old thread message"}],
        {},
    )

    with patch.object(server, "agent") as mock_agent:
        _configure_stream_agent(mock_agent)
        response = client.post(
            f"/threads/{thread_id}/runs/stream",
            json={
                "assistant_id": "researcher",
                "input": {
                    "messages": [
                        {"role": "user", "content": "first request message"},
                        {"role": "assistant", "content": "not a candidate"},
                        {"role": "human", "content": "Please continue!"},
                    ]
                },
            },
        )

    assert response.status_code == 200
    stored_run = db.list_runs(thread_id)[0]
    assert stored_run["kwargs"]["_resume_candidate"] == "Please continue!"

    db.update_thread(
        thread_id,
        [{"role": "user", "content": "later thread message"}],
        {},
    )
    assert db.get_run(stored_run["run_id"])["kwargs"]["_resume_candidate"] == "Please continue!"

    listed_run = client.get(f"/threads/{thread_id}/runs").json()[0]
    assert listed_run["kwargs"] == {}


def test_health_and_auth_contract_basics(client):
    ok = client.get("/ok")
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}

    health = client.get("/health")
    assert health.status_code == 200
    h = health.json()
    assert "status" in h
    assert "version" in h

    # Endpoint exists and returns auth-related response when no token is provided.
    validate = client.get("/auth/session/validate")
    assert validate.status_code in {401, 503}

    logout = client.post("/auth/logout")
    assert logout.status_code in {401, 503}


def test_assistants_search_post_contract(client):
    response = client.post(
        "/assistants/search",
        json={
            "graph_id": "researcher",
            "limit": 10,
            "offset": 0,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert payload
    assert payload[0]["id"] == "researcher"


def test_thread_history_contract(client):
    created = client.post("/threads", json={"metadata": {"assistant_id": "researcher"}})
    assert created.status_code == 200
    thread_id = created.json()["thread_id"]

    state_resp = client.post(
        f"/threads/{thread_id}/state",
        json={"values": {"messages": [{"role": "user", "content": "hi"}]}}
    )
    assert state_resp.status_code == 200

    history = client.get(f"/threads/{thread_id}/history")
    assert history.status_code == 200
    payload = history.json()
    assert isinstance(payload, list)
    assert payload
    first = payload[0]
    assert "checkpoint" in first
    assert "values" in first
    assert "metadata" in first
    assert "created_at" in first
