"""Minimal end-to-end tests for the async subagent server in deep_research.

Tests the Agent Protocol HTTP contract without calling a real LLM.
The agent's ainvoke is patched to return a canned response.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# Ensure deep_research is in sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from research_agent import db, server


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    """Re-initialize the in-memory database before each test and mock test mode bypass."""
    monkeypatch.setenv("ALLOW_ALL_THREADS", "true")
    monkeypatch.setenv("DB_TYPE", "sqlite")
    conn = db._get_sqlite_conn()
    conn.executescript("DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS threads;")
    db._init_sqlite()


FAKE_RESPONSE = {"messages": [AIMessage(content="Here are the research results.")]}


def _make_ainvoke_mock():
    mock = AsyncMock(return_value=FAKE_RESPONSE)
    return mock


def _seed_executor_run(
        *,
        candidate: str | None,
        messages: list[dict],
        values: dict,
) -> tuple[str, str]:
    thread_id = f"thread-{len(messages)}-{id(values)}"
    run_id = f"run-{len(messages)}-{id(values)}"
    created_at = "2026-07-24T00:00:00+00:00"
    db.create_thread(
        thread_id,
        "user",
        created_at,
        values={**values, "messages": messages},
    )
    db.create_run(
        run_id,
        thread_id,
        "researcher",
        created_at,
        kwargs={"_resume_candidate": candidate} if candidate is not None else {},
    )
    return thread_id, run_id


def _stateful_agent(initial_values: dict, results: list[dict | Exception]) -> MagicMock:
    mock_agent = MagicMock()
    current = dict(initial_values)
    queued = list(results)

    async def aget_state(config):
        return SimpleNamespace(
            values=dict(current),
            next=(),
            tasks=(),
            config=config,
            metadata={},
            created_at=None,
            parent_config=None,
        )

    async def ainvoke(input_state, config):
        outcome = queued.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        current.update(outcome)
        return outcome

    async def aupdate_state(config, update):
        current_messages = list(current.get("messages") or [])
        current_messages.extend(update.get("messages") or [])
        current["messages"] = current_messages
        return config

    mock_agent.aget_state = AsyncMock(side_effect=aget_state)
    mock_agent.ainvoke = AsyncMock(side_effect=ainvoke)
    mock_agent.aupdate_state = AsyncMock(side_effect=aupdate_state)
    mock_agent.checkpointer = None
    return mock_agent


@pytest.fixture()
def client():
    return TestClient(server.app)


def test_health(client):
    resp = client.get("/ok")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_create_thread(client):
    resp = client.post("/threads")
    assert resp.status_code == 200
    data = resp.json()
    assert "thread_id" in data
    assert data["values"]["messages"] == []


def test_create_run_starts_agent(client):
    thread = client.post("/threads").json()
    thread_id = thread["thread_id"]

    with patch.object(server, "agent") as mock_agent:
        mock_agent.ainvoke = _make_ainvoke_mock()
        resp = client.post(
            f"/threads/{thread_id}/runs",
            json={
                "assistant_id": "researcher",
                "input": {"messages": [{"role": "user", "content": "test query"}]},
            },
        )

    assert resp.status_code == 200
    run = resp.json()
    assert run["thread_id"] == thread_id
    assert "run_id" in run
    assert run["status"] == "pending"


def test_create_run_persists_metadata_and_resume_candidate_in_sqlite():
    created_at = "2026-07-24T00:00:00+00:00"
    db.create_thread("thread-sqlite", "user", created_at)

    db.create_run(
        "run-sqlite",
        "thread-sqlite",
        "researcher",
        created_at,
        metadata={"source": "test"},
        kwargs={"_resume_candidate": "Please continue!"},
    )

    run = db.get_run("run-sqlite")
    assert run is not None
    assert run["metadata"] == {"source": "test"}
    assert run["kwargs"] == {"_resume_candidate": "Please continue!"}


def test_create_run_serializes_metadata_and_kwargs_for_postgres(monkeypatch):
    pool = MagicMock()
    cursor = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    monkeypatch.setenv("DB_TYPE", "postgres")
    monkeypatch.setattr(db, "_postgres_pool", pool)

    db.create_run(
        "run-postgres",
        "thread-postgres",
        "researcher",
        "2026-07-24T00:00:00+00:00",
        metadata={"source": "test"},
        kwargs={"_resume_candidate": "Please continue!"},
    )

    statement, parameters = cursor.execute.call_args.args
    assert statement == db.db_sql.INSERT_RUN_POSTGRES
    assert parameters[6] == '{"source": "test"}'
    assert parameters[7] == '{"_resume_candidate": "Please continue!"}'


def test_create_run_stores_metadata_and_kwargs_for_cosmos(monkeypatch):
    runs_container = MagicMock()
    monkeypatch.setenv("DB_TYPE", "cosmos")
    monkeypatch.setattr(db, "_cosmos_runs_container", runs_container)

    db.create_run(
        "run-cosmos",
        "thread-cosmos",
        "researcher",
        "2026-07-24T00:00:00+00:00",
        metadata={"source": "test"},
        kwargs={"_resume_candidate": "Please continue!"},
    )

    body = runs_container.create_item.call_args.kwargs["body"]
    assert body["metadata"] == {"source": "test"}
    assert body["kwargs"] == {"_resume_candidate": "Please continue!"}


def test_last_user_message_returns_last_request_message_only():
    raw_messages = [
        {"role": "user", "content": "older request message"},
        None,
        {"role": "assistant", "content": "response"},
        {"role": "human", "content": "Please continue!"},
    ]

    assert server._last_user_message(raw_messages) == "Please continue!"
    assert server._last_user_message({"messages": raw_messages}) == ""
    assert server._last_user_message(
        [{"role": "user", "content": {"malformed": True}}]
    ) == ""


def test_background_run_binds_resume_candidate_from_own_request(client):
    thread_id = client.post("/threads").json()["thread_id"]
    db.update_thread(
        thread_id,
        [{"role": "user", "content": "old thread message"}],
        {},
    )

    with patch.object(server, "agent") as mock_agent:
        mock_agent.ainvoke = _make_ainvoke_mock()
        response = client.post(
            f"/threads/{thread_id}/runs",
            json={
                "assistant_id": "researcher",
                "input": {
                    "messages": [
                        {"role": "user", "content": "first request message"},
                        {"role": "human", "content": "Please continue!"},
                    ]
                },
            },
        )

    run_id = response.json()["run_id"]
    db.update_thread(
        thread_id,
        [{"role": "user", "content": "later thread message"}],
        {},
    )

    stored_run = db.get_run(run_id)
    assert stored_run is not None
    assert stored_run["kwargs"]["_resume_candidate"] == "Please continue!"
    assert response.json()["kwargs"] == {}


@pytest.mark.parametrize(
    ("request_messages", "expected_role", "expected_content"),
    (
            (
                    [
                        {"role": "user", "content": "older request message"},
                        {"role": "human", "content": "Please continue!"},
                    ],
                    "human",
                    "Please continue!",
            ),
            (
                    [{"role": "human", "content": "Please continue!"}],
                    "human",
                    "Please continue!",
            ),
            (
                    [{"role": "user", "content": "ordinary research request"}],
                    "user",
                    "ordinary research request",
            ),
    ),
)
def test_background_run_persists_exact_selected_trigger_message(
        client,
        request_messages,
        expected_role,
        expected_content,
):
    thread_id = client.post("/threads").json()["thread_id"]

    with patch.object(server, "_execute_run", new=AsyncMock()):
        response = client.post(
            f"/threads/{thread_id}/runs",
            json={
                "assistant_id": "researcher",
                "input": {"messages": request_messages},
            },
        )

    assert response.status_code == 200
    run = db.get_run(response.json()["run_id"])
    thread = db.get_thread(thread_id)
    assert run["kwargs"]["_resume_candidate"] == expected_content
    assert thread["messages"] == [
        {"role": expected_role, "content": expected_content}
    ]


def test_background_run_omits_empty_resume_candidate_kwargs(client):
    thread_id = client.post("/threads").json()["thread_id"]

    with (
        patch.object(db, "create_run", wraps=db.create_run) as create_run_mock,
        patch.object(server, "agent") as mock_agent,
    ):
        mock_agent.ainvoke = _make_ainvoke_mock()
        response = client.post(
            f"/threads/{thread_id}/runs",
            json={"assistant_id": "researcher", "input": {"messages": []}},
        )

    assert response.status_code == 200
    assert "kwargs" not in create_run_mock.call_args.kwargs


def test_api_run_omits_private_kwargs_and_preserves_public_kwargs():
    payload = server._api_run(
        {
            "run_id": "run-private",
            "kwargs": {
                "_resume_candidate": "Please continue!",
                "_internal": "hidden",
                "public": "visible",
                7: "non-string key",
            },
        }
    )

    assert payload["kwargs"] == {"public": "visible", 7: "non-string key"}


def test_preserve_initial_messages_deduplicates_cross_representation_messages():
    initial_by_id = {
        "type": "human",
        "content": "continue",
        "id": "shared-message-id",
    }
    initial_without_id = {
        "type": "human",
        "content": "older no-id message",
    }
    new_without_id = HumanMessage(content="genuinely new message")

    merged = server._preserve_initial_messages(
        [initial_by_id, initial_without_id],
        [
            HumanMessage(content="continue", id="shared-message-id"),
            HumanMessage(content="older no-id message"),
            new_without_id,
        ],
    )

    assert merged == [initial_by_id, initial_without_id, new_without_id]


def test_public_thread_state_and_checkpoint_history_hide_internal_state_and_messages(client):
    thread_id = "thread-public-state"
    created_at = "2026-07-24T00:00:00+00:00"
    private_values = {
        "_eval_logged": True,
        "_streamed_files": ["/private.md"],
        "_last_user_msg_hash": "secret-hash",
    }
    db.create_thread(
        thread_id,
        "user",
        created_at,
        values={
            "messages": [{"role": "user", "content": "stored"}],
            "custom_public": {"kept": True},
            **private_values,
        },
    )

    hidden = AIMessage(
        content="hidden intermediate",
        id="hidden",
        response_metadata={"resume_intermediate": True},
    )
    visible = AIMessage(content="visible final", id="visible")
    summary = AIMessage(
        content="Resume safety limit reached after 3 rounds.",
        id="summary",
    )
    tool = ToolMessage(
        content="tool result",
        id="tool",
        tool_call_id="call-1",
    )
    checkpoint_messages = [hidden, visible, tool, summary]
    checkpoint_values = {
        "messages": checkpoint_messages,
        "custom_public": {"kept": True},
        **private_values,
    }
    snapshot = SimpleNamespace(
        values=checkpoint_values,
        next=(),
        tasks=(),
        config={"configurable": {"checkpoint_id": "state-checkpoint"}},
        metadata={},
        created_at=created_at,
        parent_config=None,
    )
    checkpoint = {
        "config": {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_id": "history-checkpoint",
            }
        },
        "values": checkpoint_values,
        "metadata": {},
        "created_at": created_at,
    }

    async def alist(**kwargs):
        yield checkpoint

    mock_agent = MagicMock()
    mock_agent.aget_state = AsyncMock(return_value=snapshot)
    mock_agent.checkpointer = SimpleNamespace(alist=alist)

    with patch.object(server, "agent", mock_agent):
        thread_values = client.get(f"/threads/{thread_id}").json()["values"]
        state_values = client.get(f"/threads/{thread_id}/state").json()["values"]
        get_history_values = client.get(
            f"/threads/{thread_id}/history"
        ).json()[0]["values"]
        post_history_values = client.post(
            f"/threads/{thread_id}/history",
            json={"limit": 10},
        ).json()[0]["values"]

    for values in (
            thread_values,
            state_values,
            get_history_values,
            post_history_values,
    ):
        assert values["custom_public"] == {"kept": True}
        assert set(private_values).isdisjoint(values)

    for values in (state_values, get_history_values, post_history_values):
        contents = [message["content"] for message in values["messages"]]
        assert contents == [
            "visible final",
            "tool result",
            "Resume safety limit reached after 3 rounds.",
        ]

    assert checkpoint_values["messages"] is checkpoint_messages
    assert checkpoint_values["messages"] == [hidden, visible, tool, summary]
    raw_db_values = db.get_thread(thread_id)["values"]
    assert all(raw_db_values[key] == value for key, value in private_values.items())


def test_stream_final_public_values_hide_private_state_and_intermediate_messages():
    thread_id = "thread-stream-public"
    run_id = "run-stream-public"
    created_at = "2026-07-24T00:00:00+00:00"
    human = HumanMessage(content="continue", id="human")
    hidden = AIMessage(
        content="hidden intermediate",
        id="hidden",
        response_metadata={"resume_intermediate": True},
    )
    visible = AIMessage(content="visible final", id="visible")
    db.create_thread(
        thread_id,
        "user",
        created_at,
        values={"messages": [], "_eval_logged": "stored"},
    )
    db.create_run(run_id, thread_id, "researcher", created_at)
    snapshot = SimpleNamespace(
        values={
            "messages": [human, hidden, visible],
            "_eval_logged": True,
            "_streamed_files": ["/private.md"],
            "_last_user_msg_hash": "secret-hash",
            "custom_public": "kept",
        }
    )

    async def astream_events(*args, **kwargs):
        if False:
            yield {}

    mock_agent = MagicMock()
    mock_agent.astream_events = astream_events
    mock_agent.aget_state = AsyncMock(return_value=snapshot)

    async def collect_frames():
        return [
            frame
            async for frame in server._stream_run_events(
                thread_id,
                run_id,
                {
                    "messages": [human],
                    "_eval_logged": "initial-private",
                    "custom_public": "kept",
                },
            )
        ]

    with patch.object(server, "agent", mock_agent):
        frames = asyncio.run(collect_frames())

    values_payloads = []
    for frame in frames:
        if "event: values\n" not in frame:
            continue
        data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
        values_payloads.append(json.loads(data_line.removeprefix("data: ")))

    assert len(values_payloads) == 2
    for values in values_payloads:
        assert "_eval_logged" not in values
        assert "_streamed_files" not in values
        assert "_last_user_msg_hash" not in values
        assert values["custom_public"] == "kept"
    assert [
               message["content"] for message in values_payloads[-1]["messages"]
           ] == ["continue", "visible final"]
    assert db.get_thread(thread_id)["values"]["_eval_logged"] is True


def test_background_resume_runs_until_todos_complete_and_hides_intermediate():
    human = {"role": "human", "content": "Please continue!"}
    pending = [{"content": "Finish report", "status": "pending"}]
    completed = [{"content": "Finish report", "status": "completed"}]
    hidden = {
        "role": "assistant",
        "content": "intermediate",
        "response_metadata": {"resume_intermediate": True},
    }
    final = {"role": "assistant", "content": "done"}
    thread_id, run_id = _seed_executor_run(
        candidate="Please continue!",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [
            {"messages": [human, hidden], "todos": pending, "files": {}},
            {"messages": [human, hidden, final], "todos": completed, "files": {}},
        ],
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    assert mock_agent.ainvoke.await_count == 2
    for round_number, call in enumerate(mock_agent.ainvoke.await_args_list, start=1):
        input_state, = call.args
        assert input_state["messages"][0] == human
        configurable = call.kwargs["config"]["configurable"]
        assert configurable["resume_incomplete_todos"] is True
        assert configurable["resume_round"] == round_number
        assert configurable["resume_max_rounds"] == 3
        assert configurable["web_mode_has_new_human_input"] is False

    thread = db.get_thread(thread_id)
    assert thread["values"]["todos"] == completed
    assistant_messages = [
        message for message in thread["values"]["messages"]
        if message["type"] == "ai"
    ]
    assert [message["content"] for message in assistant_messages] == ["done"]


@pytest.mark.parametrize(
    "todos",
    (
            None,
            [],
            [{"content": "Done", "status": "completed"}],
    ),
)
def test_background_completed_or_missing_todos_invokes_once_without_resume_config(todos):
    human = {"role": "human", "content": "continue"}
    values = {"files": {}}
    if todos is not None:
        values["todos"] = todos
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values=values,
    )
    mock_agent = _stateful_agent(
        {"messages": [human], **values},
        [{"messages": [human, {"role": "assistant", "content": "ordinary"}], **values}],
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    assert mock_agent.ainvoke.await_count == 1
    configurable = mock_agent.ainvoke.await_args.kwargs["config"]["configurable"]
    assert configurable == {"thread_id": thread_id}


def test_background_malformed_todos_invokes_once_without_resume_config():
    human = {"role": "human", "content": "continue"}
    malformed = [{"content": "Unknown", "status": "blocked"}, "not-a-todo"]
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": malformed, "files": {}},
    )
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": malformed, "files": {}},
        [{"messages": [human], "todos": malformed, "files": {}}],
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    assert mock_agent.ainvoke.await_count == 1
    assert mock_agent.ainvoke.await_args.kwargs["config"]["configurable"] == {
        "thread_id": thread_id
    }


def test_background_non_resume_message_with_pending_todos_invokes_once():
    human = {"role": "human", "content": "continue researching quantum chips"}
    pending = [{"content": "Compare vendors", "status": "pending"}]
    thread_id, run_id = _seed_executor_run(
        candidate=human["content"],
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [{"messages": [human], "todos": pending, "files": {}}],
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    assert mock_agent.ainvoke.await_count == 1
    assert mock_agent.ainvoke.await_args.kwargs["config"]["configurable"] == {
        "thread_id": thread_id
    }


def test_background_resume_limit_persists_visible_summary_in_thread_state_and_history(client):
    human = {"role": "human", "content": "continue"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    hidden = {
        "role": "assistant",
        "content": "still working",
        "response_metadata": {"resume_intermediate": True},
    }
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    unchanged = {"messages": [human, hidden], "todos": pending, "files": {}}
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [unchanged, unchanged, unchanged],
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))
        state = client.get(f"/threads/{thread_id}/state").json()
        history = client.get(f"/threads/{thread_id}/history").json()

    assert mock_agent.ainvoke.await_count == 3
    mock_agent.aupdate_state.assert_awaited_once()
    limit_message = mock_agent.aupdate_state.await_args.args[1]["messages"][0]
    assert limit_message.id
    assert limit_message.id.startswith(f"{run_id}-resume-limit-")
    summary = state["values"]["messages"][-1]["content"]
    assert "Resume safety limit reached after 3 rounds." in summary
    assert "- [pending] Collect evidence" in summary
    assert state["values"]["messages"][-1]["id"] == limit_message.id
    assert history[0]["values"]["messages"][-1]["content"] == summary
    assert db.get_thread(thread_id)["values"]["messages"][-1]["content"] == summary


def test_background_resume_round_limit_rejects_stale_identical_summary(
        monkeypatch,
):
    monkeypatch.setenv("MAX_RESUME_ROUNDS", "1")
    human = {"role": "human", "content": "continue"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    stale_summary = AIMessage(
        content=server.build_round_limit_message(
            server.inspect_todos(pending),
            1,
        ),
        id="stale-summary",
    )
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    unchanged = {
        "messages": [human, stale_summary],
        "todos": pending,
        "files": {},
    }
    mock_agent = _stateful_agent(
        {
            "messages": [human, stale_summary],
            "todos": pending,
            "files": {},
        },
        [unchanged],
    )
    mock_agent.aupdate_state = AsyncMock(return_value=None)

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    run = db.get_run(run_id)
    limit_message = mock_agent.aupdate_state.await_args.args[1]["messages"][0]
    assert limit_message.id
    assert limit_message.id != stale_summary.id
    assert run["status"] == "error"
    assert "omitted round-limit message" in run["error"]


def test_background_resume_checkpoint_update_error_marks_run_error_without_local_summary():
    human = {"role": "human", "content": "continue"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    unchanged = {"messages": [human], "todos": pending, "files": {}}
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [unchanged, unchanged, unchanged],
    )
    mock_agent.aupdate_state = AsyncMock(
        side_effect=RuntimeError("checkpoint update failed")
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    run = db.get_run(run_id)
    messages = db.get_thread(thread_id)["values"]["messages"]
    assert run["status"] == "error"
    assert "checkpoint update failed" in run["error"]
    assert not any(
        "Resume safety limit reached" in message["content"]
        for message in messages
    )


def test_background_resume_checkpoint_reload_error_marks_run_error_without_local_summary():
    human = {"role": "human", "content": "continue"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    unchanged = {"messages": [human], "todos": pending, "files": {}}
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [unchanged, unchanged, unchanged],
    )
    initial_snapshot = SimpleNamespace(
        values={"messages": [human], "todos": pending, "files": {}},
    )
    mock_agent.aget_state = AsyncMock(
        side_effect=[
            initial_snapshot,
            RuntimeError("checkpoint reload failed"),
        ]
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    run = db.get_run(run_id)
    messages = db.get_thread(thread_id)["values"]["messages"]
    assert run["status"] == "error"
    assert "checkpoint reload failed" in run["error"]
    assert not any(
        "Resume safety limit reached" in message["content"]
        for message in messages
    )


def test_background_resume_missing_update_support_keeps_local_visible_summary():
    human = {"role": "human", "content": "continue"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    unchanged = {"messages": [human], "todos": pending, "files": {}}
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [unchanged, unchanged, unchanged],
    )
    mock_agent.aupdate_state = None

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    run = db.get_run(run_id)
    messages = db.get_thread(thread_id)["values"]["messages"]
    assert run["status"] == "success"
    assert "Resume safety limit reached after 3 rounds." in messages[-1]["content"]
    assert "- [pending] Collect evidence" in messages[-1]["content"]


def test_background_resume_honors_configured_round_limit(monkeypatch):
    monkeypatch.setenv("MAX_RESUME_ROUNDS", "2")
    human = {"role": "human", "content": "continue"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    unchanged = {"messages": [human], "todos": pending, "files": {}}
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [unchanged, unchanged],
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    assert mock_agent.ainvoke.await_count == 2
    assert [
               call.kwargs["config"]["configurable"]["resume_max_rounds"]
               for call in mock_agent.ainvoke.await_args_list
           ] == [2, 2]


def test_background_resume_cancellation_after_first_round_prevents_second():
    human = {"role": "human", "content": "continue"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [{"messages": [human], "todos": pending, "files": {}}],
    )
    original_ainvoke = mock_agent.ainvoke.side_effect

    async def cancel_after_first(input_state, config):
        result = await original_ainvoke(input_state, config)
        db.update_run_status(run_id, "cancelled")
        return result

    mock_agent.ainvoke.side_effect = cancel_after_first

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    assert mock_agent.ainvoke.await_count == 1
    assert db.get_run(run_id)["status"] == "cancelled"


def test_background_resume_exception_retains_latest_state_and_marks_error():
    human = {"role": "human", "content": "continue"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    first_files = {"/notes.md": {"content": ["evidence"]}}
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[human],
        values={"todos": pending, "files": {}},
    )
    mock_agent = _stateful_agent(
        {"messages": [human], "todos": pending, "files": {}},
        [
            {"messages": [human], "todos": pending, "files": first_files, "custom": "kept"},
            RuntimeError("round failed"),
        ],
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    run = db.get_run(run_id)
    thread = db.get_thread(thread_id)
    assert run["status"] == "error"
    assert "round failed" in run["error"]
    assert thread["values"]["todos"] == pending
    assert thread["values"]["files"] == first_files
    assert thread["values"]["custom"] == "kept"


def test_background_resume_uses_stored_candidate_after_later_message_append():
    original = {"role": "human", "content": "continue"}
    later = {"role": "human", "content": "Do something unrelated now"}
    pending = [{"content": "Collect evidence", "status": "pending"}]
    completed = [{"content": "Collect evidence", "status": "completed"}]
    thread_id, run_id = _seed_executor_run(
        candidate="continue",
        messages=[original],
        values={"todos": pending, "files": {}},
    )
    db.update_thread(
        thread_id,
        [original, later],
        {"messages": [original, later], "todos": pending, "files": {}},
    )
    mock_agent = _stateful_agent(
        {"messages": [original], "todos": pending, "files": {}},
        [{"messages": [original, later], "todos": completed, "files": {}}],
    )

    with patch.object(server, "agent", mock_agent):
        asyncio.run(server._execute_run(run_id, thread_id))

    configurable = mock_agent.ainvoke.await_args.kwargs["config"]["configurable"]
    assert configurable["resume_incomplete_todos"] is True
    assert configurable["resume_round"] == 1
    assert configurable["web_mode_has_new_human_input"] is False


def test_full_lifecycle(client):
    """Create thread → create run → wait for completion → check status → get thread."""
    thread = client.post("/threads").json()
    thread_id = thread["thread_id"]

    with patch.object(server, "agent") as mock_agent:
        mock_agent.ainvoke = _make_ainvoke_mock()
        run = client.post(
            f"/threads/{thread_id}/runs",
            json={
                "assistant_id": "researcher",
                "input": {"messages": [{"role": "user", "content": "quantum computing"}]},
            },
        ).json()
        run_id = run["run_id"]

        # Let the background task finish.
        asyncio.run(asyncio.sleep(0.5))

    # Check run status — should be success.
    status_resp = client.get(f"/threads/{thread_id}/runs/{run_id}")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "success"

    # Get thread — should have messages with the assistant response.
    thread_resp = client.get(f"/threads/{thread_id}")
    assert thread_resp.status_code == 200
    thread_data = thread_resp.json()
    values_messages = thread_data["values"]["messages"]
    assert any(m["content"] == "Here are the research results." for m in values_messages)


def test_cancel_run(client):
    thread = client.post("/threads").json()
    thread_id = thread["thread_id"]

    # Create a run with a slow agent so we can cancel it.
    async def slow_ainvoke(*args, **kwargs):
        await asyncio.sleep(10)
        return FAKE_RESPONSE

    with patch.object(server, "agent") as mock_agent:
        mock_agent.ainvoke = AsyncMock(side_effect=slow_ainvoke)
        run = client.post(
            f"/threads/{thread_id}/runs",
            json={
                "assistant_id": "researcher",
                "input": {"messages": [{"role": "user", "content": "something"}]},
            },
        ).json()
        run_id = run["run_id"]

    cancel_resp = client.post(f"/threads/{thread_id}/runs/{run_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "interrupted"

    # Verify the run is cancelled.
    status_resp = client.get(f"/threads/{thread_id}/runs/{run_id}")
    assert status_resp.json()["status"] == "interrupted"


def test_interrupt_strategy(client):
    """Creating a run with multitask_strategy='interrupt' cancels running runs."""
    thread = client.post("/threads").json()
    thread_id = thread["thread_id"]

    async def slow_ainvoke(*args, **kwargs):
        await asyncio.sleep(10)
        return FAKE_RESPONSE

    with patch.object(server, "agent") as mock_agent:
        mock_agent.ainvoke = AsyncMock(side_effect=slow_ainvoke)
        first_run = client.post(
            f"/threads/{thread_id}/runs",
            json={
                "assistant_id": "researcher",
                "input": {"messages": [{"role": "user", "content": "first task"}]},
            },
        ).json()

        # Let the first run start.
        asyncio.run(asyncio.sleep(0.1))

    with patch.object(server, "agent") as mock_agent:
        mock_agent.ainvoke = _make_ainvoke_mock()
        second_run = client.post(
            f"/threads/{thread_id}/runs",
            json={
                "assistant_id": "researcher",
                "input": {"messages": [{"role": "user", "content": "new task"}]},
                "multitask_strategy": "interrupt",
            },
        ).json()

    # First run should be cancelled.
    first_status = client.get(f"/threads/{thread_id}/runs/{first_run['run_id']}").json()
    assert first_status["status"] == "interrupted"


def test_404_for_missing_thread(client):
    resp = client.get("/threads/nonexistent")
    assert resp.status_code == 404


def test_404_for_missing_run(client):
    thread = client.post("/threads").json()
    resp = client.get(f"/threads/{thread['thread_id']}/runs/nonexistent")
    assert resp.status_code == 404


def test_authentication_required(client, monkeypatch):
    monkeypatch.setenv("ALLOW_ALL_THREADS", "false")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "secret-key")

    # Missing headers
    resp = client.post("/threads")
    assert resp.status_code == 401
    assert "Missing authentication" in resp.json()["detail"]

    # Invalid header key
    resp = client.post("/threads", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401
    assert "Invalid API key" in resp.json()["detail"]

    # Valid header key
    resp = client.post("/threads", headers={"X-API-Key": "secret-key"})
    assert resp.status_code == 200
    assert "thread_id" in resp.json()


def test_thread_ownership(client, monkeypatch):
    monkeypatch.setenv("ALLOW_ALL_THREADS", "false")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "secret-key")

    # Set up mock OAuth session validation
    from webapp.oauth_handler import user_manager
    session_store = {"token-user-1": {"identity": "user-1", "name": "User One"},
                     "token-user-2": {"identity": "user-2", "name": "User Two"}}

    with patch.object(user_manager, "validate_session", side_effect=session_store.get):
        # User 1 creates thread
        resp1 = client.post("/threads", headers={"Authorization": "Bearer token-user-1"})
        assert resp1.status_code == 200
        thread_id = resp1.json()["thread_id"]

        # User 1 can view it
        resp = client.get(f"/threads/{thread_id}", headers={"Authorization": "Bearer token-user-1"})
        assert resp.status_code == 200

        # User 2 cannot view it (Forbidden)
        resp = client.get(f"/threads/{thread_id}", headers={"Authorization": "Bearer token-user-2"})
        assert resp.status_code == 403
        assert "Forbidden" in resp.json()["detail"]

        # Admin can view it
        resp = client.get(f"/threads/{thread_id}", headers={"X-API-Key": "secret-key"})
        assert resp.status_code == 200
