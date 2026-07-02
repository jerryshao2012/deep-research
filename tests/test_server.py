"""Minimal end-to-end tests for the async subagent server in deep_research.

Tests the Agent Protocol HTTP contract without calling a real LLM.
The agent's ainvoke is patched to return a canned response.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import sys
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

# Ensure deep_research is in sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))

import db
import server


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
