"""Tests for agent contract validation."""

from pathlib import Path


def test_agent_source_registers_tools_list() -> None:
    agent_source = Path("agent.py").read_text(encoding="utf-8")

    assert "tools=[" in agent_source
    assert "create_deep_agent" in agent_source
