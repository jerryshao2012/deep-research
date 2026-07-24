"""LangGraph adapter for durable requirement clarification."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command, interrupt

from research_agent.clarification.contracts import (
    ClarificationBatch,
    ClarificationInterrupt,
    ClarificationQuestion,
    ClarificationResponse,
)
from research_agent.clarification.use_case import complete_clarification

InterruptFunction = Callable[[dict[str, Any]], Any]


def run_clarification(
        batch: ClarificationBatch,
        *,
        tool_call_id: str | None,
        interrupt_fn: InterruptFunction = interrupt,
) -> Command:
    """Pause for one answer batch and return normalized requirements.

    ``tool_call_id`` is the stable correlation ID. The function deliberately
    performs only deterministic validation before ``interrupt_fn`` because
    LangGraph re-executes interrupted nodes when resuming.
    """
    if not tool_call_id:
        raise ValueError("A stable tool call ID is required for clarification")

    payload = ClarificationInterrupt(
        request_id=tool_call_id,
        questions=batch.questions,
    ).model_dump(mode="json")
    raw_response = interrupt_fn(payload)
    response = ClarificationResponse.model_validate(raw_response)
    if response.request_id != tool_call_id:
        raise ValueError(
            "Clarification response request ID does not match the pending request"
        )

    result = complete_clarification(batch, response)
    content = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content,
                    tool_call_id=tool_call_id,
                    name="clarify_requirements",
                )
            ]
        }
    )


@tool(args_schema=ClarificationBatch)
def clarify_requirements(
        questions: list[ClarificationQuestion],
        runtime: ToolRuntime,
) -> Command:
    """Pause once to clarify materially ambiguous user requirements.

    When to use:
    - Only before planning, file writes, search, or delegation.
    - Only when plausible interpretations materially change scope, audience,
      deliverable, timeframe, or source constraints.
    - Ask 1-3 non-overlapping questions with 2-5 concrete options each.

    Do not use when the request is clear enough, when optional details can be
    inferred safely, or after this tool has already run for the current user
    turn. The interface automatically supports an Other answer.
    """
    return run_clarification(
        ClarificationBatch(questions=questions),
        tool_call_id=runtime.tool_call_id,
    )
