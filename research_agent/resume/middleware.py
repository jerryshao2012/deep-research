"""LangChain middleware for per-run incomplete-todo resume behavior."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    hook_config,
)
from langchain.agents.middleware.todo import PlanningState
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.config import get_config

from research_agent.resume.policy import inspect_todos

RESUME_INSTRUCTION = """<ResumeIncompleteTodos>
Resume round {round_number} of {max_rounds}. Preserve the original research goal, selected skill, files, and valid existing todo plan. Execute every pending or in-progress item. Do not replace the plan merely because this run resumed. Mark an item completed only after its work is done. Synthesize the requested final output after all items are complete.
</ResumeIncompleteTodos>"""


class ResumeMiddleware(AgentMiddleware):
    """Inject ephemeral resume guidance and tag incomplete terminal outputs."""

    state_schema = PlanningState

    def __init__(
            self,
            *,
            config_getter: Callable[[], dict[str, Any]] = get_config,
    ) -> None:
        """Create middleware with an injectable per-run config source."""
        super().__init__()
        self._config_getter = config_getter

    def _configurable(self) -> dict[str, Any]:
        try:
            config = self._config_getter()
        except RuntimeError:
            return {}
        configurable = config.get("configurable", {})
        return configurable if isinstance(configurable, dict) else {}

    def configure_request(self, request: ModelRequest) -> ModelRequest:
        """Append run-local guidance without changing messages or graph state."""
        configurable = self._configurable()
        if configurable.get("resume_incomplete_todos") is not True:
            return request

        state = request.state or {}
        if not inspect_todos(state.get("todos")).has_incomplete:
            return request

        instruction = RESUME_INSTRUCTION.format(
            round_number=configurable.get("resume_round", 1),
            max_rounds=configurable.get("resume_max_rounds", 3),
        )
        system_message = request.system_message
        if system_message is None:
            content: str | list[str | dict[str, Any]] = instruction
        elif isinstance(system_message.content, str):
            content = f"{system_message.content}\n\n{instruction}".strip()
        else:
            content = [
                *system_message.content,
                {"type": "text", "text": instruction},
            ]

        return request.override(
            system_message=SystemMessage(content=content),
        )

    def wrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], Any],
    ) -> Any:
        """Apply resume guidance to a synchronous model request."""
        return handler(self.configure_request(request))

    async def awrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        """Apply resume guidance to an asynchronous model request."""
        return await handler(self.configure_request(request))

    @hook_config(can_jump_to=["end"])
    def after_model(
            self,
            state: PlanningState,
            runtime: Any,
    ) -> dict[str, Any] | None:
        """Tag terminal output when another hidden resume round is required."""
        configurable = self._configurable()
        if configurable.get("resume_incomplete_todos") is not True:
            return None
        if not inspect_todos(state.get("todos")).has_incomplete:
            return None

        messages = state.get("messages") or []
        final_message = messages[-1] if messages else None
        if not isinstance(final_message, AIMessage) or final_message.tool_calls:
            return None

        response_metadata = {
            **(final_message.response_metadata or {}),
            "resume_intermediate": True,
        }
        tagged_message = final_message.model_copy(
            update={"response_metadata": response_metadata},
        )
        return {"messages": [tagged_message]}
