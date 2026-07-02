"""End-to-end tests for the research agent CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import sys
from langchain_core.messages import AIMessage, ToolMessage

import research_agent_cli


class FakeAgent:
    def __init__(self, *, invoke_result=None, stream_states=None):
        self.invoke_result = invoke_result
        self.stream_states = stream_states or []
        self.invoke_calls = 0
        self.stream_calls = 0

    def invoke(self, messages, config=None):  # noqa: ANN001
        self.invoke_calls += 1
        self.last_config = config
        return self.invoke_result

    def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
        self.stream_calls += 1
        self.last_config = config
        yield from self.stream_states


def _run_cli(monkeypatch, tmp_path: Path, argv: list[str], fake_agent: FakeAgent, title: str) -> Path:
    monkeypatch.setattr(research_agent_cli, "agent", fake_agent)
    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", str(tmp_path))
    monkeypatch.setattr(research_agent_cli, "generate_research_title", lambda _content: title)
    monkeypatch.setattr(research_agent_cli, "show_prompt", lambda *args, **kwargs: None)
    monkeypatch.setattr(research_agent_cli, "format_messages", lambda *args, **kwargs: None)
    monkeypatch.setattr(research_agent_cli, "file_data_to_string", lambda data: data)
    monkeypatch.setattr(sys, "argv", ["research_agent_cli.py", *argv])

    research_agent_cli.main()

    output_files = list(tmp_path.glob("*.md"))
    assert len(output_files) == 1
    return output_files[0]


@pytest.mark.parametrize(
    ("skill", "result", "expected_content"),
    [
        (
                "study-slides",
                {
                    "messages": [
                        ToolMessage(
                            content=(
                                    "# Presentation: Claude Code Memory Management\n\n"
                                    "## Slide 1: Memory Hierarchy\n\n"
                                    "- Claude Code Memory Management uses layered memory scopes.\n\n"
                                    "### Speaking Notes\n\n"
                                    "Explain how persistent memory differs from working context."
                            ),
                            tool_call_id="tool-1",
                            name="render_skill_output",
                        ),
                        AIMessage(content="I will synthesize later."),
                    ]
                },
                "# Presentation: Claude Code Memory Management",
        ),
        (
                "interview",
                {
                    "messages": [
                        ToolMessage(
                            content=(
                                    "# Interview Kit: Claude Code Memory Management\n\n"
                                    "## 45-minute interview objective\n\n"
                                    "Assess practical memory-management judgment.\n\n"
                                    "Potential Answer: A strong answer would distinguish memory and context."
                            ),
                            tool_call_id="tool-2",
                            name="render_skill_output",
                        ),
                        AIMessage(content="I will synthesize later."),
                    ]
                },
                "# Interview Kit: Claude Code Memory Management",
        ),
        (
                "golden-dataset",
                {
                    "messages": [
                        ToolMessage(
                            content=(
                                    "# Golden Dataset Starter: Claude Code Memory Q&A Draft Set\n\n"
                                    "Question: What is project memory?\n\n"
                                    "Answer: Project memory stores repository-specific guidance.\n\n"
                                    "Content: Repository guidance belongs in project-scoped memory."
                            ),
                            tool_call_id="tool-3",
                            name="render_skill_output",
                        ),
                        AIMessage(content="I will synthesize later."),
                    ]
                },
                "# Golden Dataset Starter: Claude Code Memory Q&A Draft Set",
        ),
        (
                "code-generator",
                {
                    "files": {
                        "/final_report.md": (
                                "```python\n"
                                "def load_memory(path: str) -> str:\n"
                                "    return path\n"
                                "```"
                        )
                    },
                    "messages": [AIMessage(content="done")],
                },
                "```python",
        ),
        (
                "interview-coach-pro",
                {
                    "files": {
                        "/final_report.md": (
                                "| # | Competency | Behavioral Question | Suggested STAR Answer (based on resume) |\n"
                                "|---|---|---|---|\n"
                                "| 1 | Leadership | Tell me about a time you led a project. | **S:** ... **T:** ... **A:** ... **R:** ... |"
                        )
                    },
                    "messages": [AIMessage(content="done")],
                },
                "Suggested STAR Answer",
        ),
        (
                "autoresearch-universal",
                {
                    "files": {
                        "/final_report.md": (
                                "Repo: deep_research\n"
                                "Here is your optimization template:\n"
                                "Eval criteria for prompt quality:"
                        )
                    },
                    "messages": [AIMessage(content="done")],
                },
                "Here is your optimization template:",
        ),
    ],
)
def test_cli_main_saves_expected_report_for_every_skill(
        monkeypatch, tmp_path: Path, skill: str, result: dict, expected_content: str
) -> None:
    output_file = _run_cli(
        monkeypatch,
        tmp_path,
        ["Research Claude Code Memory Management", "--skill", skill, "--verbose", "False"],
        FakeAgent(invoke_result=result),
        f"{skill.replace('-', '_')}_report",
    )

    assert output_file.parent == tmp_path
    assert expected_content in output_file.read_text(encoding="utf-8")


def test_cli_main_uses_task_tool_output_when_structured_result_is_returned_by_subagent(
        monkeypatch, tmp_path: Path
) -> None:
    result = {
        "messages": [
            ToolMessage(
                content=(
                    "# Presentation: Claude Code Memory Management\n\n"
                    "## Slide 1: Context Management\n\n"
                    "- Use `/compact` to summarize active context."
                ),
                tool_call_id="tool-9",
                name="task",
            ),
            AIMessage(content="I have delegated the research and will synthesize later."),
        ]
    }

    output_file = _run_cli(
        monkeypatch,
        tmp_path,
        ["Research Claude Code Memory Management", "--skill", "study-slides", "--verbose", "False"],
        FakeAgent(invoke_result=result),
        "study_slides_task_result",
    )

    content = output_file.read_text(encoding="utf-8")
    assert content.startswith("# Presentation: Claude Code Memory Management")
    assert "delegated the research" not in content.lower()


def test_cli_main_retries_with_invoke_when_stream_ends_with_placeholder(
        monkeypatch, tmp_path: Path
) -> None:
    stream_result = {
        "messages": [
            AIMessage(
                content=(
                    'I have delegated the research on "Claude Code Memory Management" '
                    "to a specialized research agent. Once the agent returns its findings, "
                    "I will synthesize the information into a quick-learning presentation format."
                )
            )
        ]
    }
    invoke_result = {
        "messages": [
            ToolMessage(
                content=(
                    "# Presentation: Claude Code Memory Management\n\n"
                    "## Slide 1: Memory Hierarchy\n\n"
                    "- Project memory stores repository-specific guidance."
                ),
                tool_call_id="tool-10",
                name="render_skill_output",
            )
        ]
    }
    fake_agent = FakeAgent(invoke_result=invoke_result, stream_states=[stream_result])

    output_file = _run_cli(
        monkeypatch,
        tmp_path,
        ["Research Claude Code Memory Management", "--skill", "study-slides"],
        fake_agent,
        "study_slides_retry_result",
    )

    content = output_file.read_text(encoding="utf-8")
    assert fake_agent.stream_calls == 1
    assert fake_agent.invoke_calls == 1
    assert content.startswith("# Presentation: Claude Code Memory Management")
