import sys
from pathlib import Path

# Add golden-dataset scripts to python path
_scripts_dir = Path(__file__).resolve().parent.parent / ".deepagents" / "skills" / "golden-dataset" / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from golden_dataset_metrics import (
    build_missing_context_report,
    build_judge_prompt,
    parse_metric_scores,
)


def test_parse_metric_scores_extracts_expected_columns() -> None:
    scores = parse_metric_scores(
        """
        Similarity: 4.0
        Relevance: 81
        Coherence: 4
        Groundedness: 3.5
        """
    )

    assert scores == {
        "Similarity": 4.0,
        "Relevance": 81.0,
        "Coherence": 4.0,
        "Groundedness": 3.5,
    }


def test_parse_metric_scores_rejects_missing_metrics() -> None:
    try:
        parse_metric_scores(
            """
            Similarity: 4
            Relevance: 80
            Coherence: 4
            """
        )
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected ValueError for missing metric")

    assert "Groundedness" in message


def test_build_judge_prompt_includes_metric_descriptions_and_goals() -> None:
    prompt = build_judge_prompt(
        question="What is the parental leave policy?",
        answer="Employees should review the handbook and submit a request to HR.",
        context="The handbook explains leave policy and approval steps.",
    )

    assert "Measures how similar the response is to a human expert answer" in prompt
    assert "Suggested goal: 3+" in prompt
    assert "Suggested goal: 60+" in prompt
    assert "Measures the quality of all sentences" in prompt
    assert "Measures how grounded the answer is against the provided context" in prompt


def test_parse_metric_scores_rejects_out_of_range_values() -> None:
    try:
        parse_metric_scores(
            """
            Similarity: 9
            Relevance: 81
            Coherence: 4
            Groundedness: 3
            """
        )
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected ValueError for out-of-range metric")

    assert "Similarity" in message
    assert "range" in message


def test_build_missing_content_report_flags_rows_without_content() -> None:
    report = build_missing_context_report(
        [
            {"ID": "Q1", "Question": "What is parental leave?", "Context": ""},
            {"ID": "Q2", "Question": "How do I enroll in benefits?", "Context": "Benefits guide section 4"},
            {"ID": "Q3", "Question": "How do I request PTO?", "Context": "   "},
        ]
    )

    assert "2 row(s) are missing Context" in report
    assert "Q1" in report
    assert "Q3" in report
    assert "Q2" not in report
    assert "scoring still runs" in report
