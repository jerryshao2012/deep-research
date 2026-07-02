"""Contract tests for frontend-used API endpoints and stream event structure."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import sys
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

sys.path.append(str(Path(__file__).resolve().parents[1]))

import db
import server


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
        mock_agent.ainvoke = AsyncMock(side_effect=_fake_response)

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
