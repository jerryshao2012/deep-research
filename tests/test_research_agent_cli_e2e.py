"""End-to-end tests for the research agent CLI."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables.config import var_child_runnable_config
from pydantic import PrivateAttr

from research_agent import agent as agent_module
from research_agent import cli as research_agent_cli
from research_agent.citation_failure import (
    ReportCitationAggregateError,
    ReportCitationError,
    build_citation_failure_update,
)
from research_agent.model_call_guard import (
    ModelCallPolicy,
    ModelCallTimeoutError,
    ModelRuntimeMetadata,
    cancel_model_call_scope,
    guard_model,
)


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


class _BlockingGuardedChatModel(BaseChatModel):
    """Provider fake exposing real guarded bridge cancellation to CLI tests."""

    _started: threading.Event = PrivateAttr(default_factory=threading.Event)
    _cancelled: threading.Event = PrivateAttr(default_factory=threading.Event)
    _release: threading.Event = PrivateAttr(default_factory=threading.Event)

    @property
    def _llm_type(self) -> str:
        return "blocking-cli-test-model"

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise AssertionError("guard must use async provider path")

    async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
        self._started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._cancelled.set()
            raise
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def _guarded_blocking_model(timeout_seconds: float = 0.5):
    provider = _BlockingGuardedChatModel()
    guarded = guard_model(
        provider,
        metadata=ModelRuntimeMetadata(provider="openai", model_name="cli-test"),
        policy=ModelCallPolicy(
            timeout_seconds=timeout_seconds,
            force_ollama_unload=False,
        ),
    )
    return provider, guarded


def _start_two_guarded_calls(guarded, config: dict) -> tuple[list[threading.Thread], list[BaseException]]:
    scope_id = config["configurable"]["model_call_scope_id"]
    errors: list[BaseException] = []

    def invoke_guarded() -> None:
        try:
            guarded.invoke("research", config=config)
        except BaseException as exc:
            errors.append(exc)

    callers = [threading.Thread(target=invoke_guarded) for _ in range(2)]
    for caller in callers:
        caller.start()
    deadline = time.monotonic() + 0.5
    while guarded._bridge_registry.active_count(scope_id) != 2:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert guarded._started.wait(timeout=0.5)
    return callers, errors


def _assert_guarded_callers_cancelled(
    guarded,
    scope_id: str,
    callers: list[threading.Thread],
    errors: list[BaseException],
) -> None:
    deadline = time.monotonic() + 0.5
    while guarded._bridge_registry.active_count(scope_id):
        assert time.monotonic() < deadline
        time.sleep(0.005)
    for caller in callers:
        caller.join(timeout=0.5)
    assert all(not caller.is_alive() for caller in callers)
    assert guarded._cancelled.wait(timeout=0.5)
    assert len(errors) == 2
    assert all(isinstance(error, asyncio.CancelledError) for error in errors)


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
        [
            "Research Claude Code Memory Management",
            "--skill",
            skill,
            "--verbose",
            "False",
            "--no-web",
        ],
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


def _checkpoint_free_citation_result(config: dict | None = None) -> dict[str, Any]:
    report = agent_module.create_file_data("Invalid report without citations")
    fingerprint = agent_module.artifact_fingerprint(report)
    assert fingerprint is not None
    config = config or {}
    run_id = str(config.get("run_id") or "checkpoint-free-run")
    failure = build_citation_failure_update(
        run_id=run_id,
        report_fingerprint=fingerprint,
        defects=[agent_module.CitationDefect("missing_url", "web")],
        terminal=AIMessage(content="Invalid report without citations"),
    )
    return {
        **failure,
        "files": {"/final_report.md": report},
        "todos": [{"content": "Fix citations", "status": "pending"}],
        "completion_current_run_id": run_id,
        "completion_report_owned_fingerprint": fingerprint,
        "_streamed_files": [],
    }


def test_cli_output_helpers_fail_closed_on_current_citation_failure() -> None:
    result = _checkpoint_free_citation_result()

    assert not research_agent_cli.should_retry_with_invoke(result)
    with pytest.raises(ReportCitationError):
        research_agent_cli.select_output_content(result)


def test_cli_output_helpers_fail_closed_on_malformed_report() -> None:
    report = agent_module.create_file_data("private malformed report")
    report.pop("modified_at")
    result = {
        "files": {"/final_report.md": report},
        "citation_accepted_report_fingerprint": None,
    }

    with pytest.raises(ReportCitationError):
        research_agent_cli.should_retry_with_invoke(result)
    with pytest.raises(ReportCitationError):
        research_agent_cli.select_output_content(result)
    with pytest.raises(ReportCitationError):
        research_agent_cli._render_final_result(
            result,
            "private malformed report",
            no_web=False,
        )


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
    cancel_real_bridges: bool = False,
) -> list[str]:
    cancelled_scopes: list[str] = []
    _RecordingSpinner.instances = []
    monkeypatch.setattr(research_agent_cli, "agent", fake_agent)
    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", str(tmp_path))
    monkeypatch.setattr(research_agent_cli, "Spinner", _RecordingSpinner)
    def record_cancel(scope_id: str) -> None:
        cancelled_scopes.append(scope_id)
        if cancel_real_bridges:
            cancel_model_call_scope(scope_id)

    monkeypatch.setattr(research_agent_cli, "cancel_model_call_scope", record_cancel)
    monkeypatch.setattr(research_agent_cli, "show_prompt", lambda *args, **kwargs: None)
    monkeypatch.setattr(research_agent_cli, "format_messages", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["research_agent/cli.py", *argv])
    return cancelled_scopes


@pytest.mark.parametrize("verbose", [False, True])
def test_cli_checkpoint_free_citation_state_stops_after_one_graph_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verbose: bool,
) -> None:
    class CheckpointFreeFailureAgent(FakeAgent):
        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            return _checkpoint_free_citation_result(config)

        def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
            self.stream_calls += 1
            self.last_config = config
            yield _checkpoint_free_citation_result(config)

    fake_agent = CheckpointFreeFailureAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", str(verbose)],
    )

    with pytest.raises(ReportCitationError) as caught:
        research_agent_cli.main()

    assert "Invalid report" not in str(caught.value)
    assert fake_agent.stream_calls == (1 if verbose else 0)
    assert fake_agent.invoke_calls == (0 if verbose else 1)
    assert len(cancelled_scopes) == 1
    assert list(tmp_path.glob("*.md")) == []


def test_cli_web_stream_renders_only_accepted_final_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "SECRET INVALID MODEL REPORT"
    accepted = "Accepted final https://public.publisher.org/report"
    report = agent_module.create_file_data(accepted)
    fingerprint = agent_module.artifact_fingerprint(report)
    assert fingerprint is not None
    invalid_state = {
        "messages": [AIMessage(content=secret)],
        "todos": [{"content": "Correct citations", "status": "pending"}],
    }
    accepted_state = {
        "messages": [
            AIMessage(content=secret),
            AIMessage(content=accepted),
        ],
        "files": {"/final_report.md": report},
        "todos": [{"content": "Correct citations", "status": "completed"}],
        "citation_accepted_report_fingerprint": fingerprint,
    }
    fake_agent = FakeAgent(stream_states=[invalid_state, accepted_state])
    _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "True", "--title", "accepted"],
    )
    rendered: list[str] = []

    def capture_render(messages) -> None:  # noqa: ANN001
        rendered.extend(
            research_agent_cli.extract_message_content(message)
            for message in messages
        )

    monkeypatch.setattr(research_agent_cli, "format_messages", capture_render)

    research_agent_cli.main()

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert rendered == [accepted]
    assert (tmp_path / "accepted").is_file()


def test_cli_web_stream_error_diagnostics_do_not_echo_unaccepted_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "SECRET UNACCEPTED STREAM DIAGNOSTIC"
    accepted = "Accepted final https://public.publisher.org/report"
    report = agent_module.create_file_data(accepted)
    fingerprint = agent_module.artifact_fingerprint(report)
    assert fingerprint is not None
    accepted_state = {
        "messages": [AIMessage(content=accepted)],
        "files": {"/final_report.md": report},
        "todos": [{"content": "Research", "status": "completed"}],
        "citation_accepted_report_fingerprint": fingerprint,
    }

    class InterruptedStreamAgent(FakeAgent):
        def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
            self.stream_calls += 1
            self.last_config = config
            yield {"messages": [AIMessage(content=secret)]}
            raise RuntimeError(secret)

        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            return accepted_state

    fake_agent = InterruptedStreamAgent()
    _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "True", "--title", "accepted"],
    )
    rendered: list[str] = []
    monkeypatch.setattr(
        research_agent_cli,
        "format_messages",
        lambda messages: rendered.extend(
            research_agent_cli.extract_message_content(message)
            for message in messages
        ),
    )
    research_agent_cli.main()

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert rendered == [accepted]
    assert fake_agent.stream_calls == 1
    assert fake_agent.invoke_calls == 1


@pytest.mark.parametrize("verbose", [False, True])
def test_cli_malformed_report_fingerprint_fails_closed_before_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    verbose: bool,
) -> None:
    secret = "SECRET MALFORMED REPORT CONTENT"
    malformed_report = agent_module.create_file_data(secret)
    malformed_report.pop("modified_at")
    result = {
        "messages": [AIMessage(content=secret)],
        "files": {"/final_report.md": malformed_report},
        "todos": [{"content": "Research", "status": "completed"}],
        "citation_accepted_report_fingerprint": None,
        "citation_failure_run_id": None,
        "citation_failure_report_fingerprint": None,
        "citation_failure_defects": [],
    }
    fake_agent = FakeAgent(invoke_result=result, stream_states=[result])
    _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", str(verbose), "--title", "malformed"],
    )
    rendered: list[str] = []
    title_calls: list[str] = []
    monkeypatch.setattr(
        research_agent_cli,
        "format_messages",
        lambda messages: rendered.extend(
            research_agent_cli.extract_message_content(message)
            for message in messages
        ),
    )
    monkeypatch.setattr(
        research_agent_cli,
        "generate_research_title",
        lambda content, *, config: title_calls.append(str(content)) or "unsafe",
    )

    with pytest.raises(ReportCitationError) as caught:
        research_agent_cli.main()

    captured = capsys.readouterr()
    assert secret not in str(caught.value)
    assert secret not in captured.out
    assert secret not in captured.err
    assert rendered == []
    assert title_calls == []
    assert fake_agent.stream_calls == (1 if verbose else 0)
    assert fake_agent.invoke_calls == (0 if verbose else 1)
    assert list(tmp_path.iterdir()) == []


def test_cli_web_progress_never_prints_model_controlled_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_tool = "SECRET_TOOL_NAME"
    secret_role = "SECRET_ROLE"
    secret_preview = "SECRET_PREVIEW_AND_ARGUMENT"
    accepted = "Accepted final https://public.publisher.org/report"
    report = agent_module.create_file_data(accepted)
    fingerprint = agent_module.artifact_fingerprint(report)
    assert fingerprint is not None
    accepted_state = {
        "messages": [AIMessage(content=accepted)],
        "files": {"/final_report.md": report},
        "todos": [{"content": "Research", "status": "completed"}],
        "citation_accepted_report_fingerprint": fingerprint,
    }

    class MaliciousProgressAgent(FakeAgent):
        def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
            self.stream_calls += 1
            self.last_config = config
            yield {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": secret_tool,
                                "args": {"query": secret_preview},
                                "id": "malicious-tool-call",
                            }
                        ],
                    )
                ]
            }
            yield {
                "messages": [
                    {
                        "role": secret_role,
                        "name": secret_tool,
                        "content": secret_preview,
                    }
                ]
            }
            raise RuntimeError(secret_preview)

        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            return accepted_state

    fake_agent = MaliciousProgressAgent()
    _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "True", "--title", "accepted"],
    )
    monkeypatch.setattr(research_agent_cli, "format_messages", lambda _messages: None)

    research_agent_cli.main()

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert secret_tool not in combined
    assert secret_role not in combined
    assert secret_preview not in combined
    assert fake_agent.stream_calls == 1
    assert fake_agent.invoke_calls == 1


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


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_cli_citation_failure_is_terminal_without_fallback_or_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verbose: bool,
    nested: bool,
) -> None:
    citation_error = ReportCitationError(
        [{"code": "missing_url", "detail": "web"}]
    )
    error: BaseException = (
        ExceptionGroup(
            "outer",
            [RuntimeError("secret report prose do-not-echo"), citation_error],
        )
        if nested
        else citation_error
    )

    class CitationFailureAgent(FakeAgent):
        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            raise error

        def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
            self.stream_calls += 1
            self.last_config = config
            raise error
            yield  # pragma: no cover

    fake_agent = CitationFailureAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", str(verbose)],
    )

    expected_error = ReportCitationAggregateError if nested else ReportCitationError
    with pytest.raises(expected_error) as raised:
        research_agent_cli.main()

    if nested:
        assert raised.value.primary_category == "persistence"
        assert "secret report prose" not in str(raised.value)
    else:
        assert raised.value is error
    assert fake_agent.stream_calls == (1 if verbose else 0)
    assert fake_agent.invoke_calls == (0 if verbose else 1)
    assert len(cancelled_scopes) == 1
    assert list(tmp_path.glob("*.md")) == []
    if not nested:
        assert "secret" not in str(raised.value)
        assert "do-not-echo" not in str(raised.value)
    if verbose:
        assert all(spinner.stops >= 1 for spinner in _RecordingSpinner.instances)


def test_cli_citation_exception_group_is_sanitized_without_masking_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "SECRET REPORT PROSE FROM SAVER"
    error = ExceptionGroup(
        secret,
        [
            ReportCitationError(),
            RuntimeError(f"checkpoint failed: {secret}"),
        ],
    )

    class CitationGroupAgent(FakeAgent):
        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            raise error

    fake_agent = CitationGroupAgent()
    _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "False"],
    )

    with pytest.raises(BaseException) as caught:
        research_agent_cli.main()

    rendered_error = "".join(traceback.format_exception(caught.value))
    assert not isinstance(caught.value, BaseExceptionGroup)
    assert getattr(caught.value, "primary_category", None) == "persistence"
    assert getattr(caught.value, "categories", ()) == (
        "citation",
        "persistence",
    )
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in rendered_error
    assert fake_agent.invoke_calls == 1
    assert list(tmp_path.glob("*.md")) == []


@pytest.mark.parametrize("nested", [False, True])
def test_cli_citation_failure_during_finalization_stops_without_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    nested: bool,
) -> None:
    citation_error = ReportCitationError()
    error: BaseException = (
        ExceptionGroup(
            "finalization",
            [citation_error, RuntimeError("checkpoint persistence failed")],
        )
        if nested
        else citation_error
    )

    class FinalizationFailureAgent(FakeAgent):
        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            if self.invoke_calls == 1:
                return {
                    "messages": [AIMessage(content="Partial")],
                    "todos": [{"content": "Research", "status": "pending"}],
                }
            raise error

    fake_agent = FinalizationFailureAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "False"],
    )

    expected_error = ReportCitationAggregateError if nested else ReportCitationError
    with pytest.raises(expected_error) as raised:
        research_agent_cli.main()

    if nested:
        assert raised.value.primary_category == "persistence"
    else:
        assert raised.value is error
    assert fake_agent.invoke_calls == 2
    assert len(cancelled_scopes) == 1
    assert list(tmp_path.glob("*.md")) == []


@pytest.mark.parametrize("nested", [False, True])
def test_title_citation_failure_cancels_scope_without_default(nested: bool) -> None:
    citation_error = ReportCitationError()
    error: BaseException = (
        BaseExceptionGroup(
            "title",
            [citation_error, KeyboardInterrupt("persistence interrupted")],
        )
        if nested
        else citation_error
    )
    cancelled_scopes: list[str] = []

    class CitationTitleModel:
        def invoke(self, messages, config=None):  # noqa: ANN001
            raise error

    original_model = research_agent_cli.model
    original_cancel = research_agent_cli.cancel_model_call_scope
    try:
        research_agent_cli.model = CitationTitleModel()
        research_agent_cli.cancel_model_call_scope = cancelled_scopes.append
        expected_error = (
            ReportCitationAggregateError if nested else ReportCitationError
        )
        with pytest.raises(expected_error) as raised:
            research_agent_cli.generate_research_title(
                "content",
                config={"configurable": {"model_call_scope_id": "citation-scope"}},
            )
    finally:
        research_agent_cli.model = original_model
        research_agent_cli.cancel_model_call_scope = original_cancel

    if nested:
        assert raised.value.primary_category == "interrupt"
    else:
        assert raised.value is error
    assert cancelled_scopes == ["citation-scope"]


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


@pytest.mark.parametrize(
    ("mode", "argv"),
    [
        ("verbose", ["topic", "--verbose", "True"]),
        ("fallback", ["topic", "--verbose", "True"]),
        ("nonverbose", ["topic", "--verbose", "False"]),
    ],
)
def test_cli_guarded_timeout_cancels_real_bridge_without_continuation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    argv: list[str],
) -> None:
    _provider, guarded = _guarded_blocking_model()

    class GuardedAgent(FakeAgent):
        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            return guarded.invoke(messages["messages"], config=config)

        def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
            self.stream_calls += 1
            self.last_config = config
            if mode == "fallback":
                raise RuntimeError("stream unsupported")
            guarded.invoke(messages["messages"], config=config)
            yield {}

    fake_agent = GuardedAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=argv,
        cancel_real_bridges=True,
    )

    with pytest.raises(ModelCallTimeoutError):
        research_agent_cli.main()

    assert guarded._started.wait(timeout=0.5)
    assert guarded._cancelled.wait(timeout=0.5)
    assert fake_agent.invoke_calls == (1 if mode != "verbose" else 0)
    assert fake_agent.stream_calls == (1 if mode != "nonverbose" else 0)
    assert len(cancelled_scopes) == 1
    scope_id = fake_agent.last_config["configurable"]["model_call_scope_id"]
    assert cancelled_scopes == [scope_id]
    assert guarded._bridge_registry.active_count(scope_id) == 0
    assert all(spinner.stops >= 1 for spinner in _RecordingSpinner.instances)


def test_cli_keyboard_interrupt_cancels_two_real_bridges_in_same_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _provider, guarded = _guarded_blocking_model(timeout_seconds=2.0)
    class InterruptingAgent(FakeAgent):
        def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
            self.stream_calls += 1
            self.last_config = config
            self.callers, self.caller_errors = _start_two_guarded_calls(guarded, config)
            raise KeyboardInterrupt
            yield {}

    fake_agent = InterruptingAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "True"],
        cancel_real_bridges=True,
    )

    with pytest.raises(KeyboardInterrupt):
        research_agent_cli.main()

    scope_id = fake_agent.last_config["configurable"]["model_call_scope_id"]
    assert cancelled_scopes == [scope_id]
    _assert_guarded_callers_cancelled(
        guarded, scope_id, fake_agent.callers, fake_agent.caller_errors
    )
    assert all(spinner.stops >= 1 for spinner in _RecordingSpinner.instances)


@pytest.mark.parametrize(
    ("mode", "argv"),
    [
        ("fallback", ["topic", "--verbose", "True"]),
        ("nonverbose", ["topic", "--verbose", "False"]),
    ],
)
def test_cli_keyboard_interrupt_cancels_guarded_fallback_and_nonverbose_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    argv: list[str],
) -> None:
    _provider, guarded = _guarded_blocking_model(timeout_seconds=2.0)
    class InterruptingAgent(FakeAgent):
        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            self.callers, self.caller_errors = _start_two_guarded_calls(guarded, config)
            raise KeyboardInterrupt

        def stream(self, messages, config=None, stream_mode="values"):  # noqa: ANN001
            self.stream_calls += 1
            self.last_config = config
            if mode == "fallback":
                raise RuntimeError("stream unsupported")
            yield {}

    fake_agent = InterruptingAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=argv,
        cancel_real_bridges=True,
    )

    with pytest.raises(KeyboardInterrupt):
        research_agent_cli.main()

    scope_id = fake_agent.last_config["configurable"]["model_call_scope_id"]
    assert cancelled_scopes == [scope_id]
    assert fake_agent.invoke_calls == 1
    assert fake_agent.stream_calls == (1 if mode == "fallback" else 0)
    _assert_guarded_callers_cancelled(
        guarded, scope_id, fake_agent.callers, fake_agent.caller_errors
    )
    assert all(spinner.stops >= 1 for spinner in _RecordingSpinner.instances)


def test_title_keyboard_interrupt_cancels_two_guarded_bridges_without_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider, guarded = _guarded_blocking_model(timeout_seconds=2.0)
    config = {"configurable": {"thread_id": "thread-1", "model_call_scope_id": "scope-1"}}
    seen_configs: list[dict] = []
    class InterruptingTitleModel:
        def invoke(self, messages, config=None):  # noqa: ANN001
            seen_configs.append(config)
            self.callers, self.caller_errors = _start_two_guarded_calls(guarded, config)
            raise KeyboardInterrupt

    title_model = InterruptingTitleModel()
    monkeypatch.setattr(research_agent_cli, "model", title_model)

    with pytest.raises(KeyboardInterrupt):
        research_agent_cli.generate_research_title("content", config=config)

    assert seen_configs == [config]
    _assert_guarded_callers_cancelled(
        guarded, "scope-1", title_model.callers, title_model.caller_errors
    )


def test_title_timeout_without_config_uses_known_scope_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = ModelCallTimeoutError("ollama", 1.0, False)
    seen_configs: list[dict] = []
    cancelled_scopes: list[str] = []

    class TimeoutTitleModel:
        def invoke(self, messages, config=None):  # noqa: ANN001
            seen_configs.append(config)
            raise timeout

    monkeypatch.setattr(research_agent_cli, "model", TimeoutTitleModel())
    monkeypatch.setattr(research_agent_cli, "cancel_model_call_scope", cancelled_scopes.append)

    with pytest.raises(ModelCallTimeoutError) as raised:
        research_agent_cli.generate_research_title("content", config=None)

    assert raised.value is timeout
    assert len(seen_configs) == 1
    scope_id = seen_configs[0]["configurable"]["model_call_scope_id"]
    assert cancelled_scopes == [scope_id]


def test_title_nested_timeout_group_cancels_scope_without_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = ModelCallTimeoutError("ollama", 1.0, False)
    error = ExceptionGroup("outer", [RuntimeError("other"), ExceptionGroup("inner", [timeout])])
    config = {"configurable": {"model_call_scope_id": "group-scope"}}
    cancelled_scopes: list[str] = []

    class GroupTitleModel:
        def invoke(self, messages, config=None):  # noqa: ANN001
            raise error

    monkeypatch.setattr(research_agent_cli, "model", GroupTitleModel())
    monkeypatch.setattr(research_agent_cli, "cancel_model_call_scope", cancelled_scopes.append)

    with pytest.raises(ExceptionGroup) as raised:
        research_agent_cli.generate_research_title("content", config=config)

    assert raised.value is error
    assert cancelled_scopes == ["group-scope"]


def test_cli_nested_timeout_group_stops_stream_and_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    timeout = ModelCallTimeoutError("ollama", 1.0, False)
    error = ExceptionGroup("outer", [RuntimeError("other"), ExceptionGroup("inner", [timeout])])
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

    with pytest.raises(ExceptionGroup) as raised:
        research_agent_cli.main()

    assert raised.value is error
    assert fake_agent.invoke_calls == 0
    assert len(cancelled_scopes) == 1
    assert all(spinner.stops >= 1 for spinner in _RecordingSpinner.instances)


def test_cli_keyboard_group_cancels_nonverbose_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = BaseExceptionGroup("outer", [KeyboardInterrupt(), RuntimeError("other")])

    class GroupAgent(FakeAgent):
        def invoke(self, messages, config=None):  # noqa: ANN001
            self.invoke_calls += 1
            self.last_config = config
            raise error

    fake_agent = GroupAgent()
    cancelled_scopes = _configure_timeout_cli(
        monkeypatch,
        tmp_path,
        fake_agent,
        argv=["topic", "--verbose", "False"],
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        research_agent_cli.main()

    assert raised.value is error
    assert fake_agent.invoke_calls == 1
    assert cancelled_scopes == [fake_agent.last_config["configurable"]["model_call_scope_id"]]


def test_title_guarded_timeout_preserves_scope_and_never_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider, guarded = _guarded_blocking_model()
    seen_configs: list[dict] = []
    config = {"configurable": {"thread_id": "thread-1", "model_call_scope_id": "scope-1"}}

    class GuardedTitleModel:
        def invoke(self, messages, config=None):  # noqa: ANN001
            seen_configs.append(config)
            return guarded.invoke(messages, config=config)

    monkeypatch.setattr(research_agent_cli, "model", GuardedTitleModel())

    with pytest.raises(ModelCallTimeoutError):
        research_agent_cli.generate_research_title("content", config=config)

    assert seen_configs == [config]
    assert guarded._started.wait(timeout=0.5)
    assert guarded._cancelled.wait(timeout=0.5)
    assert guarded._bridge_registry.active_count("scope-1") == 0


def test_sync_verifier_bridge_preserves_ambient_model_call_scope() -> None:
    _provider, guarded = _guarded_blocking_model(timeout_seconds=2.0)
    scope_id = "cli-scope"
    config = {"configurable": {"model_call_scope_id": scope_id}}
    errors: list[BaseException] = []

    def call_verifier() -> None:
        token = var_child_runnable_config.set(config)
        try:
            agent_module._run_async_from_sync(
                lambda: guarded.invoke("verify", config=None),
                timeout_seconds=None,
                propagate_cancel=True,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            var_child_runnable_config.reset(token)

    caller = threading.Thread(target=call_verifier)
    caller.start()
    deadline = time.monotonic() + 0.5
    while guarded._bridge_registry.active_count(scope_id) != 1:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert guarded._started.wait(timeout=0.5)

    cancel_model_call_scope(scope_id)
    caller.join(timeout=0.5)

    assert not caller.is_alive()
    assert errors and isinstance(errors[0], asyncio.CancelledError)
    assert guarded._cancelled.wait(timeout=0.5)
    assert guarded._bridge_registry.active_count(scope_id) == 0
