from __future__ import annotations

import asyncio
import logging
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.todo import PlanningState
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from research_agent.research_subagent.resume import (
    ACCEPTED_RESUME_PHRASES,
    BASE_RESUME_PHRASES,
    DEFAULT_MAX_RESUME_ROUNDS,
    INCOMPLETE_TODO_STATUSES,
    RESUME_INSTRUCTION,
    ResumeMiddleware,
    TodoInspection,
    build_round_limit_message,
    get_max_resume_rounds,
    inspect_todos,
    is_resume_intent,
    is_resume_intermediate_message,
    normalize_resume_text,
    visible_messages,
)

BASE_PHRASES = (
    "continue",
    "go on",
    "keep going",
    "resume",
    "proceed",
    "finish the remaining tasks",
    "complete the remaining tasks",
)


def _model_request(
        *,
        todos: list[dict[str, Any]],
        system_text: str = "base",
) -> ModelRequest:
    messages = [HumanMessage(content="Please continue!")]
    return ModelRequest(
        model=object(),  # type: ignore[arg-type]
        messages=messages,
        state={"messages": messages, "todos": todos},
        system_message=SystemMessage(content=system_text),
    )


@pytest.mark.parametrize("base", BASE_PHRASES)
@pytest.mark.parametrize(
    "template",
    (
            "{base}",
            "please {base}",
            "{base} please",
            "{base}, please",
    ),
)
def test_is_resume_intent_accepts_every_base_and_please_placement(
        base: str,
        template: str,
) -> None:
    assert is_resume_intent(template.format(base=base)) is True


@pytest.mark.parametrize(
    "text",
    (
            " \tＰＬＥＡＳＥ\u3000ＣＯＮＴＩＮＵＥ！ \n",
            "  Go   On  ",
            "KEEP\tGOING.",
            "resume.!",
            "proceed!!!...",
            "FINISH THE REMAINING TASKS",
            "complete the remaining tasks, please!",
    ),
)
def test_is_resume_intent_normalizes_nfkc_case_whitespace_and_trailing_marks(
        text: str,
) -> None:
    assert is_resume_intent(text) is True


@pytest.mark.parametrize(
    "text",
    (
            "continue?",
            "do not continue",
            "should we continue?",
            "continue researching security",
            "go on, but compare vendors",
            "please; continue",
            "continue,",
            "continue;",
            "continue: please",
            "please continue,",
            "please continue now",
            "discontinue",
            "a resume",
            "resume the task",
    ),
)
def test_is_resume_intent_rejects_non_resume_messages(text: str) -> None:
    assert is_resume_intent(text) is False


def test_phrase_constants_are_explicit_frozen_allowlists() -> None:
    assert BASE_RESUME_PHRASES == frozenset(BASE_PHRASES)
    assert len(ACCEPTED_RESUME_PHRASES) == len(BASE_PHRASES) * 4


def test_normalize_resume_text_strips_only_supported_trailing_punctuation() -> None:
    assert normalize_resume_text(" Continue.! ") == "continue"
    assert normalize_resume_text("continue?") == "continue?"
    assert normalize_resume_text("continue,") == "continue,"


def test_inspect_todos_returns_only_known_incomplete_items() -> None:
    pending = {"content": "A", "status": "pending"}
    in_progress = {"content": "B", "status": "in_progress"}
    completed = {"content": "C", "status": "completed"}

    inspection = inspect_todos(
        [
            pending,
            in_progress,
            completed,
            {"content": "missing status"},
            {"content": "unknown", "status": "blocked"},
            "not a todo",
        ]
    )

    assert inspection.incomplete == (pending, in_progress)
    assert inspection.malformed_count == 3
    assert inspection.has_incomplete is True


def test_inspect_todos_normalizes_status_without_mutating_items() -> None:
    pending = {"content": "A", "status": "  PeNdInG "}
    in_progress = {"content": "B", "status": " IN_PROGRESS "}
    completed = {"content": "C", "status": " ComPleted "}

    inspection = inspect_todos([pending, in_progress, completed])

    assert inspection.incomplete == (pending, in_progress)
    assert inspection.malformed_count == 0
    assert pending["status"] == "  PeNdInG "


@pytest.mark.parametrize("value", (None, {}, (), "pending", 3))
def test_inspect_todos_ignores_non_list_containers(value: object) -> None:
    assert inspect_todos(value) == TodoInspection(())


def test_todo_inspection_is_frozen_and_reports_empty_state() -> None:
    inspection = TodoInspection(())

    assert inspection.has_incomplete is False
    with pytest.raises(FrozenInstanceError):
        inspection.malformed_count = 1  # type: ignore[misc]


def test_todo_policy_constants_are_stable() -> None:
    assert INCOMPLETE_TODO_STATUSES == frozenset({"pending", "in_progress"})
    assert DEFAULT_MAX_RESUME_ROUNDS == 3


def test_get_max_resume_rounds_uses_default_when_absent_without_warning(
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("MAX_RESUME_ROUNDS", raising=False)

    with caplog.at_level(logging.WARNING):
        assert get_max_resume_rounds() == DEFAULT_MAX_RESUME_ROUNDS

    assert not [
        record
        for record in caplog.records
        if "MAX_RESUME_ROUNDS" in record.getMessage()
    ]


@pytest.mark.parametrize("raw", ("", "0", "-1", "x", "3.0"))
def test_get_max_resume_rounds_warns_once_for_invalid_values(
        raw: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MAX_RESUME_ROUNDS", raw)
    monkeypatch.setenv("UNRELATED_SECRET", "sensitive-marker")

    with caplog.at_level(logging.WARNING):
        assert get_max_resume_rounds() == DEFAULT_MAX_RESUME_ROUNDS

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "MAX_RESUME_ROUNDS" in warnings[0]
    assert str(DEFAULT_MAX_RESUME_ROUNDS) in warnings[0]
    assert "sensitive-marker" not in warnings[0]


def test_get_max_resume_rounds_accepts_positive_integer(
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MAX_RESUME_ROUNDS", " 5 ")

    with caplog.at_level(logging.WARNING):
        assert get_max_resume_rounds() == 5

    assert caplog.records == []


def test_build_round_limit_message_lists_remaining_tasks() -> None:
    inspection = TodoInspection(
        (
            {"status": "pending", "content": "Collect evidence"},
            {"status": "in_progress", "task": "Compare vendors"},
            {"status": "pending"},
        )
    )

    assert build_round_limit_message(inspection, 3) == (
        "Resume safety limit reached after 3 rounds.\n"
        "Remaining tasks:\n"
        "- [pending] Collect evidence\n"
        "- [in_progress] Compare vendors\n"
        "- [pending] Unnamed task\n"
        "Send another resume phrase to continue."
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    (
            ({"response_metadata": {"resume_intermediate": True}}, True),
            ({"response_metadata": {"resume_intermediate": False}}, False),
            ({"response_metadata": {"resume_intermediate": "true"}}, False),
            ({"response_metadata": {"resume_intermediate": 1}}, False),
            ({"response_metadata": None}, False),
            ({"response_metadata": []}, False),
            ({}, False),
            (None, False),
            ("message", False),
    ),
)
def test_is_resume_intermediate_message_handles_mappings_and_malformed_values(
        message: object,
        expected: bool,
) -> None:
    assert is_resume_intermediate_message(message) is expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    (
            ({"resume_intermediate": True}, True),
            ({"resume_intermediate": False}, False),
            ({"resume_intermediate": "yes"}, False),
            ({"resume_intermediate": 1}, False),
            ({}, False),
    ),
)
def test_is_resume_intermediate_message_handles_langchain_messages(
        metadata: dict[str, object],
        expected: bool,
) -> None:
    message = AIMessage(content="result", response_metadata=metadata)
    assert is_resume_intermediate_message(message) is expected


def test_visible_messages_filters_only_strictly_tagged_messages_in_order() -> None:
    first = {"role": "human", "content": "continue"}
    hidden_dict = {
        "role": "assistant",
        "content": "intermediate",
        "response_metadata": {"resume_intermediate": True},
    }
    truthy_dict = {
        "role": "assistant",
        "content": "visible",
        "response_metadata": {"resume_intermediate": "true"},
    }
    hidden_message = AIMessage(
        content="intermediate object",
        response_metadata={"resume_intermediate": True},
    )
    final = AIMessage(content="final")

    assert visible_messages(
        [first, hidden_dict, truthy_dict, hidden_message, final]
    ) == [first, truthy_dict, final]


@pytest.mark.parametrize(
    "config",
    (
            {},
            {"configurable": {}},
            {"configurable": {"resume_incomplete_todos": False}},
            {"configurable": {"resume_incomplete_todos": 1}},
    ),
)
def test_resume_middleware_leaves_inactive_request_unchanged(
        config: dict[str, Any],
) -> None:
    middleware = ResumeMiddleware(config_getter=lambda: config)
    request = _model_request(
        todos=[{"content": "Finish report", "status": "pending"}]
    )

    assert middleware.configure_request(request) is request


def test_resume_middleware_injects_ephemeral_system_instruction() -> None:
    middleware = ResumeMiddleware(
        config_getter=lambda: {
            "configurable": {
                "resume_incomplete_todos": True,
                "resume_round": 2,
                "resume_max_rounds": 3,
            }
        }
    )
    request = _model_request(
        todos=[{"content": "Finish report", "status": "pending"}]
    )
    original_messages = request.messages
    original_state = request.state

    configured = middleware.configure_request(request)

    assert configured is not request
    assert configured.system_message is not None
    assert configured.system_message.content == f"base\n\n{RESUME_INSTRUCTION.format(round_number=2, max_rounds=3)}"
    assert configured.messages is original_messages
    assert configured.state is original_state
    assert request.system_message is not None
    assert request.system_message.content == "base"
    assert request.state["messages"] == [HumanMessage(content="Please continue!")]


def test_resume_middleware_skips_instruction_when_todos_are_complete() -> None:
    middleware = ResumeMiddleware(
        config_getter=lambda: {
            "configurable": {"resume_incomplete_todos": True}
        }
    )
    request = _model_request(
        todos=[{"content": "Finish report", "status": "completed"}]
    )

    assert middleware.configure_request(request) is request


def test_resume_middleware_uses_planning_state_without_agent_import() -> None:
    assert ResumeMiddleware.state_schema is PlanningState


def test_resume_middleware_wrap_model_call_uses_configured_request() -> None:
    middleware = ResumeMiddleware(
        config_getter=lambda: {
            "configurable": {
                "resume_incomplete_todos": True,
                "resume_round": 1,
                "resume_max_rounds": 3,
            }
        }
    )
    request = _model_request(
        todos=[{"content": "Finish report", "status": "in_progress"}]
    )
    handled: list[ModelRequest] = []

    result = middleware.wrap_model_call(
        request,
        lambda configured: handled.append(configured) or "handled",
    )

    assert result == "handled"
    assert "Resume round 1 of 3" in str(handled[0].system_message.content)


def test_resume_middleware_awrap_model_call_uses_configured_request() -> None:
    middleware = ResumeMiddleware(
        config_getter=lambda: {
            "configurable": {
                "resume_incomplete_todos": True,
                "resume_round": 3,
                "resume_max_rounds": 3,
            }
        }
    )
    request = _model_request(
        todos=[{"content": "Finish report", "status": "pending"}]
    )
    handled: list[ModelRequest] = []

    async def handler(configured: ModelRequest) -> str:
        handled.append(configured)
        return "handled"

    result = asyncio.run(middleware.awrap_model_call(request, handler))

    assert result == "handled"
    assert "Resume round 3 of 3" in str(handled[0].system_message.content)


def _active_resume_middleware() -> ResumeMiddleware:
    return ResumeMiddleware(
        config_getter=lambda: {
            "configurable": {
                "resume_incomplete_todos": True,
                "resume_round": 1,
                "resume_max_rounds": 3,
            }
        }
    )


def test_after_model_tags_incomplete_terminal_message_without_losing_fields() -> None:
    message = AIMessage(
        id="final-1",
        content=[{"type": "text", "text": "I stopped early"}],
        name="researcher",
        additional_kwargs={"provider": "test"},
        response_metadata={"model": "fake"},
    )

    updates = _active_resume_middleware().after_model(
        {
            "messages": [message],
            "todos": [{"content": "A", "status": "pending"}],
        },
        runtime=None,
    )

    assert updates is not None
    tagged = updates["messages"][0]
    assert tagged is not message
    assert tagged.id == message.id
    assert tagged.content == message.content
    assert tagged.name == message.name
    assert tagged.additional_kwargs == message.additional_kwargs
    assert tagged.response_metadata == {
        "model": "fake",
        "resume_intermediate": True,
    }


@pytest.mark.parametrize(
    ("middleware", "todos", "message"),
    (
            (
                    ResumeMiddleware(config_getter=lambda: {"configurable": {}}),
                    [{"content": "A", "status": "pending"}],
                    AIMessage(content="inactive"),
            ),
            (
                    _active_resume_middleware(),
                    [{"content": "A", "status": "completed"}],
                    AIMessage(content="complete"),
            ),
            (
                    _active_resume_middleware(),
                    [{"content": "A", "status": "pending"}],
                    AIMessage(
                        content="calling tool",
                        tool_calls=[
                            {
                                "name": "write_todos",
                                "args": {},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    ),
            ),
            (
                    _active_resume_middleware(),
                    [{"content": "A", "status": "pending"}],
                    HumanMessage(content="not terminal AI"),
            ),
    ),
)
def test_after_model_leaves_non_terminal_cases_untagged(
        middleware: ResumeMiddleware,
        todos: list[dict[str, Any]],
        message: AIMessage | HumanMessage,
) -> None:
    assert middleware.after_model(
        {"messages": [message], "todos": todos},
        runtime=None,
    ) is None


def test_after_model_ignores_empty_message_state() -> None:
    assert _active_resume_middleware().after_model(
        {
            "messages": [],
            "todos": [{"content": "A", "status": "pending"}],
        },
        runtime=None,
    ) is None
