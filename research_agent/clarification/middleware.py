"""LangChain middleware adapter for clarification run policy."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langgraph.config import get_config

from research_agent.clarification.policy import (
    ClarificationPolicyDecision,
    evaluate_clarification_policy,
)


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        return tool.get("name")
    return getattr(tool, "name", None)


def configure_clarification_tools(
        tools: Sequence[Any],
        decision: ClarificationPolicyDecision,
        *,
        current_tool_choice: Any,
) -> tuple[list[Any], Any]:
    """Apply a pure policy decision to model-visible tools."""
    clarification_tools = [
        tool for tool in tools if _tool_name(tool) == "clarify_requirements"
    ]
    non_clarification_tools = [
        tool for tool in tools if _tool_name(tool) != "clarify_requirements"
    ]

    if not decision.tool_available:
        return non_clarification_tools, current_tool_choice
    if decision.force_tool:
        if not clarification_tools:
            raise RuntimeError(
                "Forced clarification requested but tool is not registered"
            )
        return clarification_tools, "required"
    return list(tools), current_tool_choice


def clarification_feature_enabled() -> bool:
    """Return emergency feature switch, enabled by default."""
    return os.getenv(
        "ENABLE_REQUIREMENT_CLARIFICATION",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}


class ClarificationMiddleware(AgentMiddleware):
    """Expose, force, or hide clarification per capable client run."""

    def __init__(
            self,
            *,
            feature_enabled: bool | None = None,
            config_getter: Callable[[], dict[str, Any]] = get_config,
    ) -> None:
        """Create middleware with injectable feature and config sources."""
        super().__init__()
        self._feature_enabled = (
            clarification_feature_enabled()
            if feature_enabled is None
            else feature_enabled
        )
        self._config_getter = config_getter

    def _configured_request(self, request: ModelRequest) -> ModelRequest:
        config = self._config_getter()
        messages = request.state.get("messages") or request.messages
        decision = evaluate_clarification_policy(
            config=config,
            messages=messages,
            feature_enabled=self._feature_enabled,
        )
        tools, tool_choice = configure_clarification_tools(
            request.tools,
            decision,
            current_tool_choice=request.tool_choice,
        )
        return request.override(tools=tools, tool_choice=tool_choice)

    def wrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], Any],
    ) -> Any:
        """Apply clarification policy to a synchronous model request."""
        return handler(self._configured_request(request))

    async def awrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        """Apply clarification policy to an asynchronous model request."""
        return await handler(self._configured_request(request))
