"""Tests for agent contract validation."""

from pathlib import Path
from typing import Any, get_type_hints

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from research_agent.model_call_guard import (
    ModelCallGuardMiddleware,
    ModelCallGuardMixin,
    ModelCallPolicy,
    ModelRuntimeMetadata,
    UnsupportedModelOverrideError,
    guard_model,
)


class _ToolCapableFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any):
        del tools, kwargs
        return self


def _guarded_scripted_model(responses: list[AIMessage]):
    return guard_model(
        _ToolCapableFakeModel(responses=responses),
        metadata=ModelRuntimeMetadata(provider="openai", model_name="fake-agent"),
        policy=ModelCallPolicy(timeout_seconds=2.0, force_ollama_unload=False),
    )


def test_agent_source_registers_tools_list() -> None:
    agent_source = Path("research_agent/agent.py").read_text(encoding="utf-8")

    assert "tools=[" in agent_source
    assert "create_deep_agent" in agent_source


def test_compiled_agent_registers_write_todos_tool() -> None:
    from research_agent.agent import agent

    tool_node = agent.get_graph().nodes["tools"].data

    assert "write_todos" in tool_node.tools_by_name


def test_compiled_agent_registers_completion_middleware_in_required_order() -> None:
    from langchain.agents.middleware import TodoListMiddleware

    from research_agent.agent import (
        ResearchStateMiddleware,
        WebModeMiddleware,
        _agent_kwargs,
        agent,
    )
    from research_agent.completion_guard import CompletionGuardMiddleware
    from research_agent.research_subagent.clarification.middleware import (
        ClarificationMiddleware,
    )
    from research_agent.research_subagent.resume.middleware import ResumeMiddleware

    assert agent.get_graph().nodes["model"]
    assert [type(item) for item in _agent_kwargs["middleware"][:6]] == [
        WebModeMiddleware,
        TodoListMiddleware,
        ClarificationMiddleware,
        CompletionGuardMiddleware,
        ResumeMiddleware,
        ResearchStateMiddleware,
    ]
    assert isinstance(_agent_kwargs["middleware"][6], ModelCallGuardMiddleware)


def test_root_and_explicit_subagents_share_guarded_model_and_guard_middleware() -> None:
    from research_agent.agent import _agent_kwargs, model

    assert isinstance(model, ModelCallGuardMixin)
    assert _agent_kwargs["model"] is model
    subagents = {spec["name"]: spec for spec in _agent_kwargs["subagents"]}
    assert set(subagents) == {"research-agent", "general-purpose"}

    research = subagents["research-agent"]
    general = subagents["general-purpose"]
    for spec in (research, general):
        assert spec["model"] is model
        assert len(
            [item for item in spec["middleware"] if isinstance(item, ModelCallGuardMiddleware)]
        ) == 1
    assert [item.name for item in research["tools"]] == [
        "tavily_search",
        "fetch_webpage_content",
        "think_tool",
    ]
    assert "tools" not in general


def test_compiled_root_preserves_nested_tool_updates_and_message_metadata() -> None:
    from research_agent.agent import _agent_kwargs

    tool_calls = 0

    @tool
    def probe(value: str) -> str:
        """Record a nested subagent tool call."""
        nonlocal tool_calls
        tool_calls += 1
        return f"observed:{value}"

    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "description": "Call probe with value nested, then report it.",
                        "subagent_type": "general-purpose",
                    },
                    "id": "root-task-call",
                    "type": "tool_call",
                }
            ],
            response_metadata={"boundary": "root"},
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "probe",
                    "args": {"value": "nested"},
                    "id": "nested-probe-call",
                    "type": "tool_call",
                }
            ],
            response_metadata={"boundary": "subagent"},
        ),
        AIMessage(content="subagent complete", response_metadata={"step": 3}),
        AIMessage(content="root complete", response_metadata={"step": 4}),
    ]
    scripted = _guarded_scripted_model(responses)
    production_specs = _agent_kwargs["subagents"]
    subagent_specs = [
        {
            **spec,
            "model": scripted,
            **({"tools": [probe]} if spec["name"] == "research-agent" else {}),
        }
        for spec in production_specs
    ]
    graph = create_deep_agent(
        model=scripted,
        tools=[probe],
        subagents=subagent_specs,
        middleware=[
            ModelCallGuardMiddleware(
                policy=ModelCallPolicy(
                    timeout_seconds=2.0,
                    force_ollama_unload=False,
                )
            )
        ],
    )

    events = list(
        graph.stream(
            {"messages": [HumanMessage(content="delegate once")]},
            stream_mode="updates",
            subgraphs=True,
        )
    )

    assert tool_calls == 1
    event_text = repr(events)
    assert "root-task-call" in event_text
    assert "nested-probe-call" in event_text
    assert "'boundary': 'root'" in event_text
    assert "'boundary': 'subagent'" in event_text


def test_all_model_boundaries_guard_known_and_reject_unknown_late_overrides() -> None:
    from langchain_openai import ChatOpenAI

    from research_agent.agent import _agent_kwargs

    root_guard = next(
        item
        for item in _agent_kwargs["middleware"]
        if isinstance(item, ModelCallGuardMiddleware)
    )
    subagent_guards = [
        next(
            item
            for item in spec["middleware"]
            if isinstance(item, ModelCallGuardMiddleware)
        )
        for spec in _agent_kwargs["subagents"]
    ]
    boundaries = [root_guard, *subagent_guards]
    assert len(boundaries) == 3

    for boundary in boundaries:
        raw_known = ChatOpenAI(model="late-known", api_key="secret-test-value")
        seen: list[Any] = []

        def handler(request: ModelRequest) -> AIMessage:
            seen.append(request.model)
            return AIMessage(content="ok")

        result = boundary.wrap_model_call(
            ModelRequest(model=raw_known, messages=[]),
            handler,
        )
        assert result.content == "ok"
        assert len(seen) == 1
        assert isinstance(seen[0], ModelCallGuardMixin)

        raw_unknown = _ToolCapableFakeModel(responses=[AIMessage(content="unused")])
        with pytest.raises(UnsupportedModelOverrideError):
            boundary.wrap_model_call(
                ModelRequest(model=raw_unknown, messages=[]),
                handler,
            )
        assert len(seen) == 1


def test_web_mode_is_scoped_to_current_visible_generation() -> None:
    from research_agent.agent import WebModeMiddleware

    middleware = WebModeMiddleware(
        config_getter=lambda: {"run_id": "first", "configurable": {}}
    )
    first_state = {
        "messages": [HumanMessage(id="first-human", content="Research offline")],
        "no_web": True,
    }

    first = middleware.before_agent(first_state, runtime=None)

    assert first is not None
    assert first["effective_no_web"] is True
    assert first["strict_web_citations"] is False
    assert first["web_mode_last_human_id"] == "first-human"
    assert first["web_mode_last_human_count"] == 1

    next_state = {
        "messages": first_state["messages"],
        **first,
    }
    next_state.pop("no_web", None)
    next_state.pop("messages", None)
    next_state["messages"] = first_state["messages"]
    next_update = middleware.before_agent(next_state, runtime=None)

    assert next_update is not None
    assert next_update["effective_no_web"] is False
    assert next_update["strict_web_citations"] is True


def test_web_mode_does_not_reparse_stale_text_during_explicit_resume() -> None:
    from research_agent.agent import WebModeMiddleware

    message = HumanMessage(id="original", content="Research this with no web")
    middleware = WebModeMiddleware(
        config_getter=lambda: {
            "run_id": "resume", "configurable": {"resume_incomplete_todos": True}
        }
    )
    state = {
        "messages": [message],
        "todos": [{"content": "Finish report", "status": "pending"}],
        "web_mode_last_human_id": "original",
        "web_mode_last_human_count": 1,
        "effective_no_web": True,
        "strict_web_citations": False,
    }

    update = middleware.before_agent(state, runtime=None)

    assert update is not None
    assert update["effective_no_web"] is False
    assert update["strict_web_citations"] is True


def test_web_mode_unit_treats_appended_idless_identical_human_as_new() -> None:
    from research_agent.agent import WebModeMiddleware

    middleware = WebModeMiddleware(
        config_getter=lambda: {"run_id": "second", "configurable": {}}
    )
    message = HumanMessage(content="Research with no web")
    state = {
        "messages": [message, HumanMessage(content="Research with no web")],
        "web_mode_last_human_id": None,
        "web_mode_last_human_count": 1,
    }

    update = middleware.before_agent(state, runtime=None)

    assert update is not None
    assert update["effective_no_web"] is True
    assert update["strict_web_citations"] is False
    assert update["web_mode_last_human_count"] == 2


def test_web_mode_unit_detects_idless_equal_count_content_replacement() -> None:
    from research_agent.agent import WebModeMiddleware

    middleware = WebModeMiddleware()
    initial = middleware.before_agent(
        {"messages": [HumanMessage(content="Research with web")]}, runtime=None
    )

    replacement = middleware.before_agent(
        {
            "messages": [HumanMessage(content="Research with no web")],
            **initial,
        },
        runtime=None,
    )

    assert replacement["effective_no_web"] is True
    assert replacement["strict_web_citations"] is False


def test_web_mode_hashes_only_latest_human_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_agent.agent as agent_module
    from research_agent.agent import WebModeMiddleware

    calls = 0
    original_sha256 = agent_module.hashlib.sha256

    def count_sha256(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original_sha256(*args, **kwargs)

    monkeypatch.setattr(agent_module.hashlib, "sha256", count_sha256)
    messages = [
        HumanMessage(id=str(index), content=f"Research {index}")
        for index in range(100)
    ]

    update = WebModeMiddleware().before_agent({"messages": messages}, runtime=None)

    assert update["web_mode_last_human_count"] == 100
    assert calls == 1


@pytest.mark.parametrize(
    ("messages", "previous_id", "previous_count", "expected_no_web"),
    [
        pytest.param(
            [
                HumanMessage(id="same", content="Research with web"),
                HumanMessage(id="same", content="Research with web"),
                HumanMessage(id="same", content="Research with no web"),
            ],
            "same",
            2,
            True,
            id="count-increase-wins-over-duplicate-id",
        ),
        pytest.param(
            [HumanMessage(id="older", content="Research with no web")],
            "newer",
            2,
            False,
            id="count-decrease-is-not-fresh",
        ),
        pytest.param(
            [HumanMessage(id="replacement", content="Research with no web")],
            "original",
            1,
            True,
            id="equal-count-stable-id-replacement-is-fresh",
        ),
        pytest.param(
            [HumanMessage(content="Research with no web")],
            None,
            1,
            False,
            id="equal-count-idless-is-not-fresh",
        ),
        pytest.param(
            [
                HumanMessage(id="same", content="Research with no web"),
                AIMessage(content="internal progress"),
            ],
            "same",
            1,
            False,
            id="nonhuman-append-is-not-fresh",
        ),
    ],
)
def test_web_mode_human_freshness_uses_count_before_identity(
    messages: list[HumanMessage | AIMessage],
    previous_id: str | None,
    previous_count: int,
    expected_no_web: bool,
) -> None:
    from research_agent.agent import WebModeMiddleware

    update = WebModeMiddleware().before_agent(
        {
            "messages": messages,
            "web_mode_last_human_id": previous_id,
            "web_mode_last_human_count": previous_count,
        },
        runtime=None,
    )

    assert update["effective_no_web"] is expected_no_web


@pytest.mark.parametrize(
    ("raw_no_web", "expected_no_web"),
    [
        pytest.param("true", True, id="accepted-true-string"),
        pytest.param("false", False, id="accepted-false-string"),
        pytest.param("not-a-boolean", False, id="invalid-fails-safe"),
    ],
)
def test_raw_web_mode_uses_shared_boolean_normalization(
    raw_no_web: str, expected_no_web: bool
) -> None:
    from research_agent.agent import WebModeMiddleware

    update = WebModeMiddleware().before_agent(
        {
            "messages": [HumanMessage(content="Research with no web")],
            "no_web": raw_no_web,
        },
        runtime=None,
    )

    assert update["effective_no_web"] is expected_no_web


def test_raw_no_web_channel_is_ephemeral_and_not_checkpointed() -> None:
    from research_agent.agent import ResearchState, WebModeMiddleware

    hints = get_type_hints(ResearchState, include_extras=True)
    assert "EphemeralValue" in repr(hints["no_web"])
    middleware = WebModeMiddleware(
        config_getter=lambda: {"run_id": "checkpointed", "configurable": {}}
    )

    def retain_mode(state: ResearchState) -> dict[str, Any]:
        update = middleware.before_agent(state, runtime=None) or {}
        return {
            field: update[field]
            for field in (
                "effective_no_web",
                "strict_web_citations",
                "web_mode_last_human_id",
                "web_mode_last_human_count",
            )
        }

    graph_builder = StateGraph(ResearchState)
    graph_builder.add_node("retain_mode", retain_mode)
    graph_builder.add_edge(START, "retain_mode")
    graph_builder.add_edge("retain_mode", END)
    graph = graph_builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "ephemeral-web-mode"}}

    graph.invoke(
        {"messages": [HumanMessage(id="checkpoint-human", content="Research")], "no_web": True},
        config=config,
    )

    first_snapshot = graph.get_state(config).values
    assert "no_web" not in first_snapshot
    assert first_snapshot["effective_no_web"] is True

    graph.invoke({}, config=config)

    resumed_snapshot = graph.get_state(config).values
    assert "no_web" not in resumed_snapshot
    assert resumed_snapshot["effective_no_web"] is False
    assert resumed_snapshot["strict_web_citations"] is True


@pytest.mark.parametrize(
    ("raw_no_web", "expected_no_web"),
    [
        pytest.param(True, True, id="raw-true"),
        pytest.param(False, False, id="raw-false"),
        pytest.param(None, False, id="raw-omitted"),
    ],
)
def test_compiled_production_agent_captures_raw_web_mode_before_checkpoint(
    raw_no_web: bool | None, expected_no_web: bool
) -> None:
    from research_agent.agent import _agent_kwargs

    scripted_model = _guarded_scripted_model([AIMessage(content="complete")])
    production_kwargs = {
        **_agent_kwargs,
        "model": scripted_model,
        "checkpointer": InMemorySaver(),
        "subagents": [
            {**spec, "model": scripted_model}
            for spec in _agent_kwargs["subagents"]
        ],
    }
    graph = create_deep_agent(**production_kwargs)
    config = {
        "configurable": {"thread_id": f"production-web-mode-{raw_no_web}"}
    }
    input_state: dict[str, Any] = {
        "messages": [HumanMessage(id="production-human", content="Research topic")]
    }
    if raw_no_web is not None:
        input_state["no_web"] = raw_no_web

    graph.invoke(input_state, config=config)

    checkpointed = graph.get_state(config).values
    assert checkpointed["effective_no_web"] is expected_no_web
    assert checkpointed["strict_web_citations"] is (not expected_no_web)
    assert "no_web" not in checkpointed


def test_compiled_web_mode_detects_same_id_human_replacement_but_not_resume() -> None:
    from research_agent.agent import _agent_kwargs

    scripted_model = _guarded_scripted_model(
        [AIMessage(content="complete") for _ in range(3)]
    )
    production_kwargs = {
        **_agent_kwargs,
        "model": scripted_model,
        "checkpointer": InMemorySaver(),
        "subagents": [
            {**spec, "model": scripted_model}
            for spec in _agent_kwargs["subagents"]
        ],
    }
    graph = create_deep_agent(**production_kwargs)
    config = {"configurable": {"thread_id": "same-id-web-mode"}}

    graph.invoke(
        {"messages": [HumanMessage(id="same", content="Research with web")]},
        config=config,
    )
    first = graph.get_state(config).values
    assert first["effective_no_web"] is False

    graph.invoke(
        {"messages": [HumanMessage(id="same", content="Research with no web")]},
        config=config,
    )
    replaced = graph.get_state(config).values
    assert replaced["effective_no_web"] is True
    assert replaced["strict_web_citations"] is False
    assert isinstance(replaced["web_mode_last_human_fingerprint"], str)
    assert "Research with no web" not in replaced["web_mode_last_human_fingerprint"]

    graph.invoke({}, config=config)
    resumed = graph.get_state(config).values
    assert resumed["effective_no_web"] is False
    assert resumed["strict_web_citations"] is True


@pytest.mark.parametrize("resume_input", [None, Command(resume={})])
def test_markerless_checkpoint_resume_defaults_web_then_accepts_new_human_directive(
    resume_input: Command | None,
) -> None:
    from research_agent.agent import _agent_kwargs

    scripted_model = _guarded_scripted_model(
        [AIMessage(content="complete") for _ in range(3)]
    )
    production_kwargs = {
        **_agent_kwargs,
        "model": scripted_model,
        "checkpointer": InMemorySaver(),
        "subagents": [
            {**spec, "model": scripted_model}
            for spec in _agent_kwargs["subagents"]
        ],
    }
    graph = create_deep_agent(**production_kwargs)
    config = {"configurable": {"thread_id": "legacy-web-mode"}}
    graph.update_state(
        config,
        {
            "messages": [
                HumanMessage(id="legacy", content="Research with no web")
            ],
            "effective_no_web": True,
            "strict_web_citations": False,
        },
    )

    graph.invoke(resume_input, config=config)
    migrated = graph.get_state(config).values
    assert migrated["effective_no_web"] is False
    assert migrated["strict_web_citations"] is True

    graph.invoke(
        {"messages": [HumanMessage(id="new", content="Research with no web")]},
        config=config,
    )
    updated = graph.get_state(config).values
    assert updated["effective_no_web"] is True
    assert updated["strict_web_citations"] is False


def test_compiled_fresh_human_directive_applies_with_preloaded_files() -> None:
    from deepagents.backends.utils import create_file_data

    from research_agent.agent import _agent_kwargs

    scripted_model = _guarded_scripted_model([AIMessage(content="complete")])
    graph = create_deep_agent(
        **{
            **_agent_kwargs,
            "model": scripted_model,
            "checkpointer": InMemorySaver(),
            "subagents": [
                {**spec, "model": scripted_model}
                for spec in _agent_kwargs["subagents"]
            ],
        }
    )
    config = {"configurable": {"thread_id": "fresh-files-web-mode"}}

    graph.invoke(
        {
            "messages": [HumanMessage(content="Research this with no web")],
            "files": {"/context.md": create_file_data("Preloaded context")},
        },
        config=config,
    )

    snapshot = graph.get_state(config).values
    assert snapshot["effective_no_web"] is True
    assert snapshot["strict_web_citations"] is False


def test_markerless_checkpoint_applies_immediate_new_human_directive() -> None:
    from research_agent.agent import _agent_kwargs

    scripted_model = _guarded_scripted_model([AIMessage(content="complete")])
    graph = create_deep_agent(
        **{
            **_agent_kwargs,
            "model": scripted_model,
            "checkpointer": InMemorySaver(),
            "subagents": [
                {**spec, "model": scripted_model}
                for spec in _agent_kwargs["subagents"]
            ],
        }
    )
    config = {"configurable": {"thread_id": "legacy-new-human-web-mode"}}
    graph.update_state(
        config,
        {
            "messages": [HumanMessage(id="legacy", content="Research with web")],
            "effective_no_web": False,
            "strict_web_citations": True,
        },
    )

    graph.invoke(
        {"messages": [HumanMessage(id="new", content="Research with no web")]},
        config=config,
    )

    snapshot = graph.get_state(config).values
    assert snapshot["effective_no_web"] is True
    assert snapshot["strict_web_citations"] is False


@pytest.mark.parametrize(
    ("raw_no_web", "expected_no_web"),
    [
        pytest.param(None, False, id="reconstructed-history-ignores-stale-text"),
        pytest.param(True, True, id="raw-mode-overrides-server-resume-signal"),
    ],
)
def test_compiled_server_reconstructed_state_uses_explicit_human_input_signal(
    raw_no_web: bool | None,
    expected_no_web: bool,
) -> None:
    from research_agent.agent import WEB_MODE_HAS_NEW_HUMAN_INPUT, _agent_kwargs

    scripted_model = _guarded_scripted_model([AIMessage(content="complete")])
    graph = create_deep_agent(
        **{
            **_agent_kwargs,
            "model": scripted_model,
            "checkpointer": InMemorySaver(),
            "subagents": [
                {**spec, "model": scripted_model}
                for spec in _agent_kwargs["subagents"]
            ],
        }
    )
    config = {
        "configurable": {
            "thread_id": f"server-reconstructed-{raw_no_web}",
            WEB_MODE_HAS_NEW_HUMAN_INPUT: False,
        }
    }
    history = [HumanMessage(id="legacy", content="Research with no web")]
    graph.update_state(
        config,
        {
            "messages": history,
            "effective_no_web": True,
            "strict_web_citations": False,
        },
    )
    reconstructed_state: dict[str, Any] = {"messages": history, "files": {}}
    if raw_no_web is not None:
        reconstructed_state["no_web"] = raw_no_web

    graph.invoke(reconstructed_state, config=config)

    snapshot = graph.get_state(config).values
    assert snapshot["effective_no_web"] is expected_no_web
    assert snapshot["strict_web_citations"] is (not expected_no_web)


def test_compiled_first_human_text_directive_applies_without_raw_mode() -> None:
    from research_agent.agent import _agent_kwargs

    scripted_model = _guarded_scripted_model([AIMessage(content="complete")])
    graph = create_deep_agent(
        **{
            **_agent_kwargs,
            "model": scripted_model,
            "checkpointer": InMemorySaver(),
            "subagents": [
                {**spec, "model": scripted_model}
                for spec in _agent_kwargs["subagents"]
            ],
        }
    )
    config = {"configurable": {"thread_id": "initial-text-web-mode"}}

    graph.invoke(
        {"messages": [HumanMessage(content="Research this with no web")]},
        config=config,
    )

    snapshot = graph.get_state(config).values
    assert snapshot["effective_no_web"] is True
    assert snapshot["strict_web_citations"] is False


def test_compiled_raw_mode_overwrites_client_effective_mode() -> None:
    from research_agent.agent import _agent_kwargs

    scripted_model = _guarded_scripted_model([AIMessage(content="complete")])
    graph = create_deep_agent(
        **{
            **_agent_kwargs,
            "model": scripted_model,
            "checkpointer": InMemorySaver(),
            "subagents": [
                {**spec, "model": scripted_model}
                for spec in _agent_kwargs["subagents"]
            ],
        }
    )
    config = {"configurable": {"thread_id": "raw-overwrites-effective"}}

    graph.invoke(
        {
            "messages": [HumanMessage(content="Research with web")],
            "no_web": False,
            "effective_no_web": True,
        },
        config=config,
    )

    snapshot = graph.get_state(config).values
    assert snapshot["effective_no_web"] is False
    assert snapshot["strict_web_citations"] is True


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("raw_no_web", [False, True])
def test_compiled_delegation_propagates_effective_web_mode(
    monkeypatch: pytest.MonkeyPatch,
    async_: bool,
    raw_no_web: bool,
) -> None:
    from research_agent.agent import _agent_kwargs
    from research_agent.research_subagent.utils import web_search

    calls = {"search": 0, "fetch": 0}

    def fake_search(**_kwargs: object) -> dict[str, object]:
        calls["search"] += 1
        return {
            "results": [{"title": "Example", "url": "https://example.com"}]
        }

    def fake_fetch(*_args: object, **_kwargs: object) -> str:
        calls["fetch"] += 1
        return "Example page"

    monkeypatch.setattr(web_search, "_run_tavily_search", fake_search)
    monkeypatch.setattr(web_search, "fetch_webpage_content_impl", fake_fetch)
    scripted_model = _guarded_scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Find one source.",
                            "subagent_type": "research-agent",
                        },
                        "id": "delegate",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "tavily_search",
                        "args": {"query": "effective mode"},
                        "id": "search",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Subagent complete"),
            AIMessage(content="Root complete"),
        ]
    )
    production_kwargs = {
        **_agent_kwargs,
        "model": scripted_model,
        "checkpointer": InMemorySaver(),
        "subagents": [
            {**spec, "model": scripted_model}
            for spec in _agent_kwargs["subagents"]
        ],
    }
    graph = create_deep_agent(**production_kwargs)
    config = {"configurable": {"thread_id": f"delegate-web-{async_}-{raw_no_web}"}}
    input_state = {
        "messages": [HumanMessage(content="Delegate research")],
        "no_web": raw_no_web,
    }

    if async_:
        import asyncio

        asyncio.run(graph.ainvoke(input_state, config=config))
    else:
        graph.invoke(input_state, config=config)

    assert calls == ({"search": 0, "fetch": 0} if raw_no_web else {"search": 1, "fetch": 1})
