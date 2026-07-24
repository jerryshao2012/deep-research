"""Pure run policy for exposing requirement clarification."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict


class ClarificationMode(StrEnum):
    """Per-run behavior requested by a capable client."""

    AUTO = "auto"
    FORCE = "force"
    BYPASS = "bypass"


class ClarificationPolicyDecision(BaseModel):
    """Decision consumed by the LangChain middleware adapter."""

    model_config = ConfigDict(frozen=True)

    mode: ClarificationMode
    tool_available: bool
    force_tool: bool
    reason: str


def _message_type(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("type") or message.get("role") or "")
    return str(getattr(message, "type", ""))


def _has_clarification_call(message: Any) -> bool:
    if isinstance(message, dict):
        name = message.get("name")
        tool_calls = message.get("tool_calls") or []
    else:
        name = getattr(message, "name", None)
        tool_calls = getattr(message, "tool_calls", None) or []

    if name == "clarify_requirements":
        return True

    return any(
        (
            call.get("name")
            if isinstance(call, dict)
            else getattr(call, "name", None)
        )
        == "clarify_requirements"
        for call in tool_calls
    )


def _clarification_handled_since_latest_human(
        messages: Iterable[Any],
) -> bool:
    current_turn: list[Any] = []
    for message in messages:
        if _message_type(message) in {"human", "user"}:
            current_turn = [message]
        else:
            current_turn.append(message)
    return any(_has_clarification_call(message) for message in current_turn)


def evaluate_clarification_policy(
        *,
        config: dict[str, Any] | None,
        messages: Iterable[Any],
        feature_enabled: bool,
) -> ClarificationPolicyDecision:
    """Resolve feature, client capability, run mode, and turn guard."""
    configurable = (
        config.get("configurable", {})
        if isinstance(config, dict)
        else {}
    )
    raw_mode = configurable.get("clarification_mode", ClarificationMode.AUTO)
    try:
        mode = ClarificationMode(str(raw_mode))
    except ValueError:
        mode = ClarificationMode.AUTO

    capabilities = configurable.get("client_capabilities", {})
    capability_version = (
        capabilities.get("requirement_clarification", 0)
        if isinstance(capabilities, dict)
        else 0
    )

    if not feature_enabled:
        return ClarificationPolicyDecision(
            mode=mode,
            tool_available=False,
            force_tool=False,
            reason="feature_disabled",
        )
    if not isinstance(capability_version, int) or capability_version < 1:
        return ClarificationPolicyDecision(
            mode=mode,
            tool_available=False,
            force_tool=False,
            reason="client_unsupported",
        )
    if mode is ClarificationMode.BYPASS:
        return ClarificationPolicyDecision(
            mode=mode,
            tool_available=False,
            force_tool=False,
            reason="bypassed",
        )
    if _clarification_handled_since_latest_human(messages):
        return ClarificationPolicyDecision(
            mode=mode,
            tool_available=False,
            force_tool=False,
            reason="already_handled",
        )

    return ClarificationPolicyDecision(
        mode=mode,
        tool_available=True,
        force_tool=mode is ClarificationMode.FORCE,
        reason="available",
    )
