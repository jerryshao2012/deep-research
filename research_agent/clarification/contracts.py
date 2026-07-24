"""Framework-neutral contracts for requirement clarification."""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Identifier = str
QuestionType = Literal["single_select", "multi_select"]


class StrictContract(BaseModel):
    """Base model for versioned wire contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class ClarificationOption(StrictContract):
    """One model-suggested answer option."""

    id: Identifier = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=240)


class ClarificationQuestion(StrictContract):
    """A required single-select or multi-select question."""

    id: Identifier = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    prompt: str = Field(min_length=1, max_length=300)
    type: QuestionType
    options: list[ClarificationOption] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def option_ids_are_unique(self) -> ClarificationQuestion:
        """Reject repeated option IDs within this question."""
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("Option IDs must be unique within a question")
        return self


class ClarificationBatch(StrictContract):
    """Questions proposed by the agent in a single clarification batch."""

    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def question_ids_are_unique(self) -> ClarificationBatch:
        """Reject repeated question IDs within this batch."""
        question_ids = [question.id for question in self.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("Question IDs must be unique within a batch")
        return self


class ClarificationAnswer(StrictContract):
    """User answer to one clarification question."""

    question_id: Identifier = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    selected_option_ids: list[Identifier] = Field(
        default_factory=list,
        max_length=5,
    )
    other_text: str | None = Field(default=None, max_length=500)

    @field_validator("selected_option_ids")
    @classmethod
    def selected_ids_are_unique(cls, value: list[str]) -> list[str]:
        """Reject selecting the same option more than once."""
        if len(value) != len(set(value)):
            raise ValueError("Selected option IDs must be unique")
        return value

    @field_validator("other_text")
    @classmethod
    def normalize_other_text(cls, value: str | None) -> str | None:
        """Trim Other text and normalize blank input to null."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ClarificationResponse(StrictContract):
    """Versioned resume value submitted by a capable client."""

    kind: Literal["requirement_clarification_response"] = (
        "requirement_clarification_response"
    )
    version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=256)
    skipped: bool = False
    answers: list[ClarificationAnswer] = Field(
        default_factory=list,
        max_length=3,
    )

    @model_validator(mode="after")
    def skipped_response_has_no_answers(self) -> ClarificationResponse:
        """Require skipped responses to omit all answers."""
        if self.skipped and self.answers:
            raise ValueError("Answers must be empty when clarification is skipped")
        return self


class ClarificationInterrupt(StrictContract):
    """Payload surfaced to the frontend by LangGraph interrupt."""

    kind: Literal["requirement_clarification"] = "requirement_clarification"
    version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=256)
    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=3)


class NormalizedRequirement(StrictContract):
    """Deterministic requirement context returned to the agent."""

    question_id: Identifier
    prompt: str
    selected_option_ids: list[Identifier]
    selected_labels: list[str]
    other_text: str | None = None


class ClarificationResult(StrictContract):
    """Persisted tool result consumed by the agent and transcript UI."""

    kind: Literal["requirement_clarification_result"] = (
        "requirement_clarification_result"
    )
    version: Literal[1] = 1
    request_id: str
    status: Literal["answered", "skipped"]
    requirements: list[NormalizedRequirement]
