"""Human-in-the-loop requirement clarification.

Domain contracts and policy stay independent from LangGraph. Framework
adapters live in ``tool.py`` and ``middleware.py``.
"""

from research_agent.clarification.contracts import (
    ClarificationAnswer,
    ClarificationBatch,
    ClarificationOption,
    ClarificationQuestion,
    ClarificationResponse,
    ClarificationResult,
    NormalizedRequirement,
)
from research_agent.clarification.policy import (
    ClarificationMode,
    ClarificationPolicyDecision,
    evaluate_clarification_policy,
)
from research_agent.clarification.use_case import complete_clarification

__all__ = [
    "ClarificationAnswer",
    "ClarificationBatch",
    "ClarificationMode",
    "ClarificationOption",
    "ClarificationPolicyDecision",
    "ClarificationQuestion",
    "ClarificationResponse",
    "ClarificationResult",
    "NormalizedRequirement",
    "complete_clarification",
    "evaluate_clarification_policy",
]
