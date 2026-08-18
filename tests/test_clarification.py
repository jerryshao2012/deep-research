from __future__ import annotations

from copy import deepcopy
import json
import operator
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from research_agent.research_subagent.clarification.contracts import (
    ClarificationAnswer,
    ClarificationBatch,
    ClarificationOption,
    ClarificationQuestion,
    ClarificationResponse,
)
from research_agent.research_subagent.clarification.middleware import (
    configure_clarification_tools,
)
from research_agent.research_subagent.clarification.policy import (
    ClarificationMode,
    evaluate_clarification_policy,
)
from research_agent.research_subagent.clarification.tool import (
    clarify_requirements,
    run_clarification,
)
from research_agent.research_subagent.clarification.use_case import (
    complete_clarification,
)
from research_agent.research_subagent.prompts import RESEARCH_WORKFLOW_INSTRUCTIONS


def _question(
        *,
        question_id: str = "target_audience",
        question_type: str = "single_select",
) -> ClarificationQuestion:
    return ClarificationQuestion(
        id=question_id,
        prompt="Who is this report for?",
        type=question_type,
        options=[
            ClarificationOption(
                id="executives",
                label="Executives",
                description="Focus on decisions and business impact.",
            ),
            ClarificationOption(
                id="engineers",
                label="Engineers",
                description="Include implementation details.",
            ),
        ],
    )


def _capable_config(mode: str = "auto") -> dict[str, Any]:
    return {
        "configurable": {
            "clarification_mode": mode,
            "client_capabilities": {"requirement_clarification": 1},
        }
    }


@pytest.fixture
def gemma_shorthand_batch() -> dict[str, Any]:
    """Complete shorthand clarification call observed from Gemma."""
    return {
        "questions": [
            {
                "question": "Who is this report for?",
                "options": ["Executives", "Engineers", "Other (please specify)"],
            },
            {
                "question": "What is the primary goal?",
                "options": ["Planning", "Other (please specify)"],
            },
            {
                "question": "What depth should the report use?",
                "options": ["Overview", "Implementation", "Other (please specify)"],
            },
        ]
    }


def test_batch_normalizes_gemma_shorthand_deterministically_without_mutation(
    gemma_shorthand_batch: dict[str, Any],
) -> None:
    original = deepcopy(gemma_shorthand_batch)

    first = ClarificationBatch.model_validate(gemma_shorthand_batch)
    second = ClarificationBatch.model_validate(gemma_shorthand_batch)

    assert gemma_shorthand_batch == original
    assert first == second
    assert [question.id for question in first.questions] == [
        "question_1",
        "question_2",
        "question_3",
    ]
    assert [question.type for question in first.questions] == [
        "single_select",
        "single_select",
        "single_select",
    ]
    assert [question.prompt for question in first.questions] == [
        item["question"] for item in gemma_shorthand_batch["questions"]
    ]
    assert [
        [(option.id, option.label) for option in question.options]
        for question in first.questions
    ] == [
        [("option_1", "Executives"), ("option_2", "Engineers")],
        [("option_1", "Planning"), ("option_2", "Other (please specify)")],
        [("option_1", "Overview"), ("option_2", "Implementation")],
    ]


def test_batch_normalizes_only_exact_standalone_other_labels() -> None:
    batch = {
        "questions": [
            {
                "question": "What should this include?",
                "options": [
                    "First",
                    "  OTHER  ",
                    "Second",
                    " Other (PLEASE SPECIFY) ",
                    "Another option",
                ],
            }
        ]
    }

    question = ClarificationBatch.model_validate(batch).questions[0]

    assert [(option.id, option.label) for option in question.options] == [
        ("option_1", "First"),
        ("option_2", "Second"),
        ("option_3", "Another option"),
    ]


@pytest.mark.parametrize(
    "invalid_batch",
    [
        {
            "questions": [
                {
                    "id": "question_1",
                    "prompt": "Canonical",
                    "type": "single_select",
                    "options": [
                        {"id": "option_1", "label": "One"},
                        {"id": "option_2", "label": "Two"},
                    ],
                },
                {"question": "Shorthand", "options": ["One", "Two"]},
            ]
        },
        {
            "questions": [
                {
                    "question": "Mixed",
                    "prompt": "Unexpected canonical key",
                    "options": ["One", "Two"],
                }
            ]
        },
        {"questions": [{"question": "Bad option", "options": ["One", 2]}]},
        {
            "questions": [
                {"question": "Extra", "options": ["One", "Two"], "extra": True}
            ]
        },
        {
            "questions": [{"question": "Valid", "options": ["One", "Two"]}],
            "unexpected": True,
        },
    ],
    ids=[
        "hybrid_batch",
        "mixed_item_keys",
        "non_string_option",
        "shorthand_item_extra",
        "unexpected_top_level_field",
    ],
)
def test_batch_rejects_non_exact_shorthand_shapes(
    invalid_batch: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ClarificationBatch.model_validate(invalid_batch)


def test_batch_requires_one_to_three_questions() -> None:
    with pytest.raises(ValidationError):
        ClarificationBatch(questions=[])

    with pytest.raises(ValidationError):
        ClarificationBatch(
            questions=[
                _question(question_id=f"question_{index}")
                for index in range(4)
            ]
        )


def test_question_rejects_duplicate_option_ids_and_oversized_text() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ClarificationQuestion(
            id="audience",
            prompt="Who is this for?",
            type="single_select",
            options=[
                ClarificationOption(id="same", label="One"),
                ClarificationOption(id="same", label="Two"),
            ],
        )

    with pytest.raises(ValidationError):
        ClarificationQuestion(
            id="audience",
            prompt="x" * 301,
            type="single_select",
            options=[
                ClarificationOption(id="one", label="One"),
                ClarificationOption(id="two", label="Two"),
            ],
        )


def test_response_rejects_answers_when_skipped() -> None:
    with pytest.raises(ValidationError, match="empty"):
        ClarificationResponse(
            request_id="tool-call-1",
            skipped=True,
            answers=[
                ClarificationAnswer(
                    question_id="target_audience",
                    selected_option_ids=["executives"],
                )
            ],
        )


def test_single_select_accepts_one_option_or_other_but_not_both() -> None:
    batch = ClarificationBatch(questions=[_question()])

    selected = complete_clarification(
        batch,
        ClarificationResponse(
            request_id="tool-call-1",
            answers=[
                ClarificationAnswer(
                    question_id="target_audience",
                    selected_option_ids=["executives"],
                )
            ],
        ),
    )
    assert selected.status == "answered"
    assert selected.requirements[0].selected_labels == ["Executives"]

    other = complete_clarification(
        batch,
        ClarificationResponse(
            request_id="tool-call-2",
            answers=[
                ClarificationAnswer(
                    question_id="target_audience",
                    other_text="Board audit committee",
                )
            ],
        ),
    )
    assert other.requirements[0].other_text == "Board audit committee"

    with pytest.raises(ValueError, match="exactly one"):
        complete_clarification(
            batch,
            ClarificationResponse(
                request_id="tool-call-3",
                answers=[
                    ClarificationAnswer(
                        question_id="target_audience",
                        selected_option_ids=["executives"],
                        other_text="Board audit committee",
                    )
                ],
            ),
        )


def test_multi_select_accepts_options_plus_other() -> None:
    batch = ClarificationBatch(
        questions=[_question(question_type="multi_select")]
    )
    result = complete_clarification(
        batch,
        ClarificationResponse(
            request_id="tool-call-1",
            answers=[
                ClarificationAnswer(
                    question_id="target_audience",
                    selected_option_ids=["executives", "engineers"],
                    other_text="Security reviewers",
                )
            ],
        ),
    )

    requirement = result.requirements[0]
    assert requirement.selected_labels == ["Executives", "Engineers"]
    assert requirement.other_text == "Security reviewers"


def test_complete_clarification_rejects_unknown_missing_and_duplicate_answers() -> None:
    batch = ClarificationBatch(questions=[_question()])

    with pytest.raises(ValueError, match="Unknown question"):
        complete_clarification(
            batch,
            ClarificationResponse(
                request_id="tool-call-1",
                answers=[
                    ClarificationAnswer(
                        question_id="unknown",
                        selected_option_ids=["executives"],
                    )
                ],
            ),
        )

    with pytest.raises(ValueError, match="Missing answer"):
        complete_clarification(
            batch,
            ClarificationResponse(request_id="tool-call-1", answers=[]),
        )

    with pytest.raises(ValueError, match="Duplicate answer"):
        complete_clarification(
            batch,
            ClarificationResponse(
                request_id="tool-call-1",
                answers=[
                    ClarificationAnswer(
                        question_id="target_audience",
                        selected_option_ids=["executives"],
                    ),
                    ClarificationAnswer(
                        question_id="target_audience",
                        selected_option_ids=["engineers"],
                    ),
                ],
            ),
        )


def test_skip_produces_deterministic_empty_requirement_result() -> None:
    result = complete_clarification(
        ClarificationBatch(questions=[_question()]),
        ClarificationResponse(
            request_id="tool-call-1",
            skipped=True,
            answers=[],
        ),
    )

    assert json.loads(result.model_dump_json()) == {
        "kind": "requirement_clarification_result",
        "version": 1,
        "request_id": "tool-call-1",
        "status": "skipped",
        "requirements": [],
    }


@pytest.mark.parametrize(
    ("feature_enabled", "config", "expected_mode", "expected_available"),
    [
        (False, _capable_config(), ClarificationMode.AUTO, False),
        (True, {}, ClarificationMode.AUTO, False),
        (True, _capable_config("auto"), ClarificationMode.AUTO, True),
        (True, _capable_config("force"), ClarificationMode.FORCE, True),
        (True, _capable_config("bypass"), ClarificationMode.BYPASS, False),
    ],
)
def test_policy_enforces_feature_capability_and_mode(
        feature_enabled: bool,
        config: dict[str, Any],
        expected_mode: ClarificationMode,
        expected_available: bool,
) -> None:
    decision = evaluate_clarification_policy(
        config=config,
        messages=[HumanMessage(content="Research quantum computing")],
        feature_enabled=feature_enabled,
    )

    assert decision.mode is expected_mode
    assert decision.tool_available is expected_available
    assert decision.force_tool is (
            expected_mode is ClarificationMode.FORCE and expected_available
    )


def test_policy_prevents_second_batch_after_latest_human_message() -> None:
    messages = [
        HumanMessage(content="Research quantum computing"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "clarify_requirements",
                    "args": {"questions": []},
                }
            ],
        ),
        ToolMessage(
            content='{"status":"answered"}',
            tool_call_id="call-1",
            name="clarify_requirements",
        ),
    ]

    decision = evaluate_clarification_policy(
        config=_capable_config("force"),
        messages=messages,
        feature_enabled=True,
    )

    assert decision.tool_available is False
    assert decision.force_tool is False
    assert decision.reason == "already_handled"


def test_new_human_message_reopens_auto_clarification_policy() -> None:
    messages = [
        HumanMessage(content="Research quantum computing"),
        ToolMessage(
            content='{"status":"answered"}',
            tool_call_id="call-1",
            name="clarify_requirements",
        ),
        HumanMessage(content="Now compare vendors"),
    ]

    decision = evaluate_clarification_policy(
        config=_capable_config("auto"),
        messages=messages,
        feature_enabled=True,
    )

    assert decision.tool_available is True
    assert decision.reason == "available"


def test_workflow_prompt_defines_material_ambiguity_and_skip_path() -> None:
    prompt = RESEARCH_WORKFLOW_INSTRUCTIONS.lower()

    assert "clarify_requirements" in prompt
    assert "material ambiguity" in prompt
    assert "clear enough" in prompt
    assert "continue immediately" in prompt
    assert "1-3" in prompt
    assert "before" in prompt and "delegat" in prompt


def test_tool_configuration_filters_or_forces_clarification() -> None:
    tools = [
        SimpleNamespace(name="clarify_requirements"),
        SimpleNamespace(name="tavily_search"),
    ]

    disabled = evaluate_clarification_policy(
        config={},
        messages=[HumanMessage(content="Research quantum computing")],
        feature_enabled=True,
    )
    disabled_tools, disabled_choice = configure_clarification_tools(
        tools,
        disabled,
        current_tool_choice="auto",
    )
    assert [tool.name for tool in disabled_tools] == ["tavily_search"]
    assert disabled_choice == "auto"

    forced = evaluate_clarification_policy(
        config=_capable_config("force"),
        messages=[HumanMessage(content="Research quantum computing")],
        feature_enabled=True,
    )
    forced_tools, forced_choice = configure_clarification_tools(
        tools,
        forced,
        current_tool_choice=None,
    )
    assert [tool.name for tool in forced_tools] == ["clarify_requirements"]
    assert forced_choice == "required"


def test_run_clarification_uses_stable_tool_call_id_and_returns_tool_message() -> None:
    captured: dict[str, Any] = {}

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {
            "kind": "requirement_clarification_response",
            "version": 1,
            "request_id": "tool-call-1",
            "skipped": False,
            "answers": [
                {
                    "question_id": "target_audience",
                    "selected_option_ids": ["executives"],
                    "other_text": None,
                }
            ],
        }

    command = run_clarification(
        ClarificationBatch(questions=[_question()]),
        tool_call_id="tool-call-1",
        interrupt_fn=fake_interrupt,
    )

    assert captured["request_id"] == "tool-call-1"
    assert captured["kind"] == "requirement_clarification"
    message = command.update["messages"][0]
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "tool-call-1"
    result = json.loads(message.content)
    assert result["status"] == "answered"
    assert result["requirements"][0]["selected_labels"] == ["Executives"]


def test_run_clarification_rejects_missing_or_stale_request_id() -> None:
    with pytest.raises(ValueError, match="tool call ID"):
        run_clarification(
            ClarificationBatch(questions=[_question()]),
            tool_call_id=None,
            interrupt_fn=lambda payload: {},
        )

    with pytest.raises(ValueError, match="does not match"):
        run_clarification(
            ClarificationBatch(questions=[_question()]),
            tool_call_id="tool-call-1",
            interrupt_fn=lambda payload: {
                "kind": "requirement_clarification_response",
                "version": 1,
                "request_id": "stale-tool-call",
                "skipped": True,
                "answers": [],
            },
        )


def test_clarification_tool_description_documents_canonical_schema() -> None:
    description = re.sub(r"\s+", "", clarify_requirements.description)

    assert (
        '{"questions":[{"id":"target_audience","prompt":"Whoisthisfor?",'
        '"type":"single_select","options":[{"id":"executives",'
        '"label":"Executives"},{"id":"engineers","label":"Engineers"}]}]}'
        in description
    )
    assert (
        "DonotaddanOtheroption;theinterfaceprovidesitautomatically."
        in description
    )


class _InterruptState(TypedDict):
    messages: Annotated[list[Any], operator.add]


def test_interrupt_pauses_and_resumes_same_checkpoint() -> None:
    batch = ClarificationBatch(questions=[_question()])

    def clarify_node(state: _InterruptState) -> Command:
        return run_clarification(batch, tool_call_id="tool-call-1")

    graph = (
        StateGraph(_InterruptState)
        .add_node("clarify", clarify_node)
        .add_edge(START, "clarify")
        .add_edge("clarify", END)
        .compile(checkpointer=InMemorySaver())
    )
    config = {"configurable": {"thread_id": "clarification-thread"}}

    interrupted = graph.invoke({"messages": []}, config)
    assert interrupted["__interrupt__"][0].value["request_id"] == "tool-call-1"

    resumed = graph.invoke(
        Command(
            resume={
                "kind": "requirement_clarification_response",
                "version": 1,
                "request_id": "tool-call-1",
                "skipped": False,
                "answers": [
                    {
                        "question_id": "target_audience",
                        "selected_option_ids": ["executives"],
                        "other_text": None,
                    }
                ],
            }
        ),
        config,
    )
    tool_message = resumed["messages"][-1]
    assert isinstance(tool_message, ToolMessage)
    assert json.loads(tool_message.content)["status"] == "answered"


def test_real_checkpointed_clarification_replays_node_and_preserves_shorthand(
    gemma_shorthand_batch: dict[str, Any],
) -> None:
    raw = deepcopy(gemma_shorthand_batch)
    execution_count = 0

    def clarify_node(state: _InterruptState) -> Command:
        nonlocal execution_count
        execution_count += 1
        batch = clarify_requirements.args_schema.model_validate(raw)
        return run_clarification(batch, tool_call_id="tool-call-1")

    graph = (
        StateGraph(_InterruptState)
        .add_node("clarify", clarify_node)
        .add_edge(START, "clarify")
        .add_edge("clarify", END)
        .compile(checkpointer=InMemorySaver())
    )
    config = {"configurable": {"thread_id": "clarification-shorthand-thread"}}

    interrupted = graph.invoke({"messages": []}, config)
    interrupt_payload = interrupted["__interrupt__"][0].value
    assert interrupt_payload == {
        "kind": "requirement_clarification",
        "version": 1,
        "request_id": "tool-call-1",
        "questions": [
            {
                "id": "question_1",
                "prompt": "Who is this report for?",
                "type": "single_select",
                "options": [
                    {"id": "option_1", "label": "Executives", "description": None},
                    {"id": "option_2", "label": "Engineers", "description": None},
                ],
            },
            {
                "id": "question_2",
                "prompt": "What is the primary goal?",
                "type": "single_select",
                "options": [
                    {"id": "option_1", "label": "Planning", "description": None},
                    {
                        "id": "option_2",
                        "label": "Other (please specify)",
                        "description": None,
                    },
                ],
            },
            {
                "id": "question_3",
                "prompt": "What depth should the report use?",
                "type": "single_select",
                "options": [
                    {"id": "option_1", "label": "Overview", "description": None},
                    {
                        "id": "option_2",
                        "label": "Implementation",
                        "description": None,
                    },
                ],
            },
        ],
    }

    resumed = graph.invoke(
        Command(
            resume={
                "kind": "requirement_clarification_response",
                "version": 1,
                "request_id": "tool-call-1",
                "skipped": False,
                "answers": [
                    {
                        "question_id": question["id"],
                        "selected_option_ids": [question["options"][0]["id"]],
                        "other_text": None,
                    }
                    for question in interrupt_payload["questions"]
                ],
            }
        ),
        config,
    )

    tool_message = resumed["messages"][-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "tool-call-1"
    assert tool_message.name == "clarify_requirements"
    result = json.loads(tool_message.content)
    assert result["kind"] == "requirement_clarification_result"
    assert result["version"] == 1
    assert result["request_id"] == "tool-call-1"
    assert result["status"] == "answered"
    selected_labels = [
        requirement["selected_labels"] for requirement in result["requirements"]
    ]
    assert selected_labels == [
        ["Executives"],
        ["Planning"],
        ["Overview"],
    ]
    assert execution_count == 2
    assert raw == gemma_shorthand_batch


def test_agent_registers_clarification_tool_and_middleware() -> None:
    source = Path("research_agent/agent.py").read_text(encoding="utf-8")

    assert "clarify_requirements," in source
    middleware_block = re.search(
        r"middleware=\[(?P<body>.*?)\],\n    skills=",
        source,
        re.DOTALL,
    )
    assert middleware_block is not None
    assert re.findall(
        r"(\w+Middleware)\(\)",
        middleware_block.group("body"),
    ) == [
               "ClarificationMiddleware",
               "CompletionGuardMiddleware",
               "ResumeMiddleware",
               "ResearchStateMiddleware",
           ]
