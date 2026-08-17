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
