"""Tests for agent contract validation."""

from pathlib import Path
from typing import Any

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

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

    from research_agent.agent import ResearchStateMiddleware, _agent_kwargs, agent
    from research_agent.completion_guard import CompletionGuardMiddleware
    from research_agent.research_subagent.clarification.middleware import (
        ClarificationMiddleware,
    )
    from research_agent.research_subagent.resume.middleware import ResumeMiddleware

    assert agent.get_graph().nodes["model"]
    assert [type(item) for item in _agent_kwargs["middleware"][:5]] == [
        TodoListMiddleware,
        ClarificationMiddleware,
        CompletionGuardMiddleware,
        ResumeMiddleware,
        ResearchStateMiddleware,
    ]
    assert isinstance(_agent_kwargs["middleware"][5], ModelCallGuardMiddleware)


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
