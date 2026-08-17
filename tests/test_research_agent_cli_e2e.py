"""End-to-end tests for the research agent CLI."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from research_agent import cli as research_agent_cli
from research_agent.model_call_guard import ModelCallTimeoutError


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
    monkeypatch.setattr(
        research_agent_cli,
        "generate_research_title",
        lambda _content, *, config: title,
    )
    monkeypatch.setattr(research_agent_cli, "show_prompt", lambda *args, **kwargs: None)
    monkeypatch.setattr(research_agent_cli, "format_messages", lambda *args, **kwargs: None)
    monkeypatch.setattr(research_agent_cli, "file_data_to_string", lambda data: data)
    monkeypatch.setattr(sys, "argv", ["research_agent/cli.py", *argv])

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


@pytest.mark.parametrize(
    ("todos", "expected"),
    [
        ([{"status": "pending"}], True),
        ([{"status": " PENDING "}], True),
        ([{"status": " in_PROGRESS "}], True),
        ([{"status": "completed"}], False),
        ([{"status": "blocked"}], False),
        ([{}], False),
        (["pending"], False),
    ],
)
def test_should_retry_with_invoke_uses_shared_incomplete_todo_policy(
        todos, expected: bool
) -> None:
    result = {
        "todos": todos,
        "messages": [AIMessage(content="Final report ready.")],
    }

    assert research_agent_cli.should_retry_with_invoke(result) is expected


def test_should_retry_with_invoke_delegates_incomplete_todos_to_shared_policy(
        monkeypatch,
) -> None:
    todos = [{"status": "custom_status"}]
    inspected = []

    class Inspection:
        has_incomplete = True

    def fake_inspect_todos(value):
        inspected.append(value)
        return Inspection()

    monkeypatch.setattr(research_agent_cli, "inspect_todos", fake_inspect_todos)

    assert research_agent_cli.should_retry_with_invoke(
        {
            "todos": todos,
            "messages": [AIMessage(content="Final report ready.")],
        }
    )
    assert inspected == [todos]


class _RecordingSpinner:
    instances: list["_RecordingSpinner"] = []

    def __init__(self, message: str = "Working...") -> None:
        self.message = message
        self.starts = 0
        self.stops = 0
        self.instances.append(self)

    def start(self, message: str | None = None) -> None:
        self.starts += 1
        if message is not None:
            self.message = message

    def stop(self) -> None:
        self.stops += 1


def _configure_timeout_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_agent: FakeAgent,
    *,
    argv: list[str],
) -> list[str]:
    cancelled_scopes: list[str] = []
    _RecordingSpinner.instances = []
    monkeypatch.setattr(research_agent_cli, "agent", fake_agent)
    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", str(tmp_path))
    monkeypatch.setattr(research_agent_cli, "Spinner", _RecordingSpinner)
    monkeypatch.setattr(
        research_agent_cli,
        "cancel_model_call_scope",
        cancelled_scopes.append,
    )
    monkeypatch.setattr(research_agent_cli, "show_prompt", lambda *args, **kwargs: None)
    monkeypatch.setattr(research_agent_cli, "format_messages", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["research_agent/cli.py", *argv])
    return cancelled_scopes


@pytest.mark.parametrize(
    "error",
    [ModelCallTimeoutError("ollama", 1.0, False), asyncio.CancelledError()],
)
def test_cli_timeout_during_verbose_stream_stops_spinner_cancels_scope_and_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    fake_agent = FakeAgent()

    def stream(*args, **kwargs):  # noqa: ANN002, ANN003
        raise error
        yield  # pragma: no cover

    fake_agent.stream = stream
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "True"],
    )

    with pytest.raises(type(error)) as raised:
        research_agent_cli.main()

    assert raised.value is error
    assert fake_agent.invoke_calls == 0
    assert len(cancelled_scopes) == 1
    assert all(spinner.stops >= 1 for spinner in _RecordingSpinner.instances)


@pytest.mark.parametrize(
    "error",
    [ModelCallTimeoutError("ollama", 1.0, False), asyncio.CancelledError()],
)
def test_cli_timeout_during_fallback_invoke_stops_spinner_cancels_scope_and_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    class FallbackTimeoutAgent(FakeAgent):
        def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
            self.stream_calls += 1
            raise RuntimeError("stream unsupported")
            yield  # pragma: no cover

        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            raise error

    fake_agent = FallbackTimeoutAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "True"],
    )

    with pytest.raises(type(error)) as raised:
        research_agent_cli.main()

    assert raised.value is error
    assert fake_agent.stream_calls == 1
    assert fake_agent.invoke_calls == 1
    assert len(cancelled_scopes) == 1
    assert all(spinner.stops >= 1 for spinner in _RecordingSpinner.instances)


@pytest.mark.parametrize(
    "error",
    [ModelCallTimeoutError("ollama", 1.0, False), asyncio.CancelledError()],
)
def test_cli_timeout_during_nonverbose_invoke_cancels_scope_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    class TimeoutAgent(FakeAgent):
        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            raise error

    fake_agent = TimeoutAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "False"],
    )

    with pytest.raises(type(error)) as raised:
        research_agent_cli.main()

    assert raised.value is error
    assert fake_agent.invoke_calls == 1
    assert cancelled_scopes == [fake_agent.last_config["configurable"]["model_call_scope_id"]]


@pytest.mark.parametrize(
    "error",
    [ModelCallTimeoutError("ollama", 1.0, False), asyncio.CancelledError()],
)
def test_title_timeout_receives_exact_scope_cancels_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    seen_configs: list[dict] = []
    cancelled_scopes: list[str] = []

    class TimeoutTitleModel:
        def invoke(self, messages, config=None):  # noqa: ANN001
            seen_configs.append(config)
            raise error

    config = {"configurable": {"thread_id": "thread-1", "model_call_scope_id": "scope-1"}}
    monkeypatch.setattr(research_agent_cli, "model", TimeoutTitleModel())
    monkeypatch.setattr(research_agent_cli, "cancel_model_call_scope", cancelled_scopes.append)

    with pytest.raises(type(error)) as raised:
        research_agent_cli.generate_research_title("content", config=config)

    assert raised.value is error
    assert seen_configs == [config]
    assert cancelled_scopes == ["scope-1"]
