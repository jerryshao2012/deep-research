"""Tests for agent contract validation."""

from pathlib import Path


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
