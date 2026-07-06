"""Tests for pattern learning from eval history."""

from __future__ import annotations

import json
import tempfile

import pytest
from pathlib import Path

from research_agent.utils.learning import (
    _topic_bucket,
    analyze_eval_trends,
    compute_baseline_from_history,
    generate_improvement_suggestions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_record(
        timestamp: str,
        success_rate: float = 0.9,
        total_tokens: int = 10000,
        runtime_seconds: float = 60.0,
        experiment_id: str | None = None,
        variant: str | None = None,
        subject: str | None = None,
) -> dict:
    """Build a synthetic eval record for testing."""
    return {
        "timestamp_utc": timestamp,
        "model_name": "test-model",
        "context": {"subject": subject or "[REDACTED]"},
        "runtime_seconds": runtime_seconds,
        "summary": {
            "success_rate": success_rate,
            "total_tokens": total_tokens,
        },
        "metrics": {
            "tool_execution": {"success_rate": success_rate},
            "token_efficiency": {"total_tokens": total_tokens},
        },
        "experiment_id": experiment_id,
        "variant": variant,
    }


@pytest.fixture
def history_dir():
    """Create a temporary directory with synthetic eval history."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        records = [
            _make_record("2026-07-01T10:00:00Z", 0.85, 12000, 55.0),
            _make_record("2026-07-02T10:00:00Z", 0.90, 11000, 50.0),
            _make_record("2026-07-03T10:00:00Z", 0.88, 13000, 65.0),
            _make_record("2026-07-04T10:00:00Z", 0.92, 10000, 48.0),
            _make_record("2026-07-05T10:00:00Z", 0.95, 9000, 42.0),
        ]
        jsonl_path = tmp_path / "server_runs.jsonl"
        with jsonl_path.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        yield tmp_path


@pytest.fixture
def history_dir_with_experiments():
    """Synthetic history with A/B experiment data."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        records = [
            _make_record(
                "2026-07-01T10:00:00Z", 0.80, 15000, 70.0,
                experiment_id="prompt-v2", variant="control",
            ),
            _make_record(
                "2026-07-02T10:00:00Z", 0.82, 14800, 68.0,
                experiment_id="prompt-v2", variant="control",
            ),
            _make_record(
                "2026-07-03T10:00:00Z", 0.90, 12000, 55.0,
                experiment_id="prompt-v2", variant="treatment",
            ),
            _make_record(
                "2026-07-04T10:00:00Z", 0.91, 11800, 52.0,
                experiment_id="prompt-v2", variant="treatment",
            ),
        ]
        jsonl_path = tmp_path / "server_runs.jsonl"
        with jsonl_path.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        yield tmp_path


# ---------------------------------------------------------------------------
# _topic_bucket
# ---------------------------------------------------------------------------


class TestTopicBucket:
    def test_short_subject(self):
        assert _topic_bucket("What is AI") == "what is ai"

    def test_long_subject_truncated(self):
        result = _topic_bucket("Compare Python vs JavaScript for web development")
        assert result == "compare python vs"

    def test_empty_subject(self):
        assert _topic_bucket("") == "unknown"


# ---------------------------------------------------------------------------
# analyze_eval_trends
# ---------------------------------------------------------------------------


class TestAnalyzeEvalTrends:
    def test_basic_analysis(self, history_dir):
        analysis = analyze_eval_trends(history_dir, window_days=30)
        assert analysis["record_count"] == 5
        assert analysis["avg_success_rate"] is not None
        assert 0.85 <= analysis["avg_success_rate"] <= 0.95
        assert analysis["avg_total_tokens"] is not None

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis = analyze_eval_trends(Path(tmp), window_days=30)
            assert analysis["record_count"] == 0
            assert "message" in analysis

    def test_respects_window(self, history_dir):
        # Only 1 day window — should only see the last record.
        analysis = analyze_eval_trends(history_dir, window_days=1)
        assert analysis["record_count"] == 1

    def test_experiment_groups(self, history_dir_with_experiments):
        analysis = analyze_eval_trends(
            history_dir_with_experiments, window_days=30
        )
        assert "experiments" in analysis
        assert "prompt-v2" in analysis["experiments"]
        exp = analysis["experiments"]["prompt-v2"]
        assert exp["record_count"] == 4


# ---------------------------------------------------------------------------
# generate_improvement_suggestions
# ---------------------------------------------------------------------------


class TestGenerateImprovementSuggestions:
    def test_empty_analysis(self):
        text = generate_improvement_suggestions({
            "window_days": 30,
            "record_count": 0,
        })
        assert "No eval records found" in text

    def test_with_failure_patterns(self):
        analysis = {
            "window_days": 30,
            "record_count": 50,
            "avg_success_rate": 0.75,
            "avg_total_tokens": 60000,
            "avg_latency_seconds": 150,
            "top_failure_patterns": [
                {"pattern": "retry_same_tool", "count": 12},
            ],
            "topic_failure_rates": {},
            "experiments": {},
        }
        text = generate_improvement_suggestions(analysis)
        assert "Low tool success rate" in text
        assert "High token usage" in text
        assert "High latency" in text
        assert "retry_same_tool" in text

    def test_healthy_metrics(self):
        analysis = {
            "window_days": 30,
            "record_count": 50,
            "avg_success_rate": 0.97,
            "avg_total_tokens": 8000,
            "avg_latency_seconds": 30,
            "top_failure_patterns": [],
            "topic_failure_rates": {},
            "experiments": {},
        }
        text = generate_improvement_suggestions(analysis)
        assert "healthy" in text.lower()


# ---------------------------------------------------------------------------
# compute_baseline_from_history
# ---------------------------------------------------------------------------


class TestComputeBaselineFromHistory:
    def test_median_baseline(self, history_dir):
        result = compute_baseline_from_history(
            history_dir, window_size=5
        )
        assert result is not None
        assert result["record_count"] == 5
        # Median of [0.85, 0.88, 0.90, 0.92, 0.95] = 0.90
        assert result["median_success_rate"] == pytest.approx(0.90)

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compute_baseline_from_history(Path(tmp))
            assert result is None

    def test_smaller_window(self, history_dir):
        result = compute_baseline_from_history(
            history_dir, window_size=3
        )
        assert result is not None
        assert result["record_count"] == 3
        # Median of [0.92, 0.95, 0.88] = 0.92
        assert result["median_success_rate"] == pytest.approx(0.92)
