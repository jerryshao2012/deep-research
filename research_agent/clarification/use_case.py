"""Application use case for validating and normalizing clarification."""

from __future__ import annotations

from research_agent.clarification.contracts import (
    ClarificationBatch,
    ClarificationResponse,
    ClarificationResult,
    NormalizedRequirement,
)


def complete_clarification(
        batch: ClarificationBatch,
        response: ClarificationResponse,
) -> ClarificationResult:
    """Validate answers against their questions and normalize labels."""
    if response.skipped:
        return ClarificationResult(
            request_id=response.request_id,
            status="skipped",
            requirements=[],
        )

    questions = {question.id: question for question in batch.questions}
    seen_question_ids: set[str] = set()
    requirements: list[NormalizedRequirement] = []

    for answer in response.answers:
        if answer.question_id in seen_question_ids:
            raise ValueError(
                f"Duplicate answer for question '{answer.question_id}'"
            )
        seen_question_ids.add(answer.question_id)

        question = questions.get(answer.question_id)
        if question is None:
            raise ValueError(f"Unknown question '{answer.question_id}'")

        options = {option.id: option for option in question.options}
        unknown_options = [
            option_id
            for option_id in answer.selected_option_ids
            if option_id not in options
        ]
        if unknown_options:
            raise ValueError(
                f"Unknown option IDs for question '{question.id}': "
                + ", ".join(unknown_options)
            )

        has_selection = bool(answer.selected_option_ids)
        has_other = bool(answer.other_text)
        if question.type == "single_select":
            if len(answer.selected_option_ids) > 1 or has_selection == has_other:
                raise ValueError(
                    f"Question '{question.id}' requires exactly one option or Other"
                )
        elif not has_selection and not has_other:
            raise ValueError(
                f"Question '{question.id}' requires at least one option or Other"
            )

        requirements.append(
            NormalizedRequirement(
                question_id=question.id,
                prompt=question.prompt,
                selected_option_ids=answer.selected_option_ids,
                selected_labels=[
                    options[option_id].label
                    for option_id in answer.selected_option_ids
                ],
                other_text=answer.other_text,
            )
        )

    missing = [
        question.id
        for question in batch.questions
        if question.id not in seen_question_ids
    ]
    if missing:
        raise ValueError("Missing answer for question(s): " + ", ".join(missing))

    return ClarificationResult(
        request_id=response.request_id,
        status="answered",
        requirements=requirements,
    )
