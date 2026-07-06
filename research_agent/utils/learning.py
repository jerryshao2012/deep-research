"""Pattern learning from eval history for data-driven improvement.

Read-only analysis module.  Reads JSONL eval history, identifies trends,
generates actionable improvement suggestions, and computes robust baselines
from historical data (improving on the single-run baseline in eval_tracking.py).

None of these functions auto-apply changes — they produce reports for
developer review.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from pathlib import Path

from research_agent.utils.eval_tracking import load_jsonl

logger = logging.getLogger(__name__)

# Default window for trend analysis.
_DEFAULT_WINDOW_DAYS = 30

# Number of recent runs to aggregate for a robust baseline.
_BASELINE_WINDOW_SIZE = 10


# ---------------------------------------------------------------------------
# Trend analysis
# ---------------------------------------------------------------------------


def analyze_eval_trends(
        history_dir: Path,
        window_days: int = _DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Analyze eval history for quality trends over the given time window.

    Args:
        history_dir: Directory containing ``*.jsonl`` eval history files.
        window_days: How many days back to analyze.

    Returns:
        A dict with keys: ``window_days``, ``record_count``,
        ``avg_success_rate``, ``avg_token_efficiency``, ``avg_latency``,
        ``top_failure_patterns``, ``topic_failure_rates`` (only when
        ``EVAL_LOG_QUESTIONS`` was enabled), ``experiments`` (A/B test results
        when experiment_id is present).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # Collect all records within the window.
    records: list[dict[str, Any]] = []
    for jsonl_path in sorted(history_dir.glob("*.jsonl")):
        for rec in load_jsonl(jsonl_path):
            ts = rec.get("timestamp_utc", "")
            try:
                rec_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if rec_time >= cutoff:
                    records.append(rec)
            except (ValueError, TypeError):
                # Records without valid timestamps are included (best effort).
                records.append(rec)

    if not records:
        return {
            "window_days": window_days,
            "record_count": 0,
            "message": "No records found in the analysis window.",
        }

    # ── Aggregate metrics ──────────────────────────────────────────────
    success_rates: list[float] = []
    total_tokens_list: list[int] = []
    latencies: list[float] = []
    correction_types: dict[str, int] = {}
    topic_failures: dict[str, list[float]] = {}  # topic → list of success rates
    experiment_groups: dict[str, list[dict[str, Any]]] = {}

    for rec in records:
        metrics = rec.get("metrics", {})
        summary = rec.get("summary", {})

        # Success rate
        sr = summary.get(
            "success_rate",
            metrics.get("tool_execution", {}).get("success_rate", 0),
        )
        if isinstance(sr, (int, float)):
            success_rates.append(float(sr))

        # Token efficiency
        tt = summary.get(
            "total_tokens",
            metrics.get("token_efficiency", {}).get("total_tokens", 0),
        )
        if isinstance(tt, (int, float)) and tt > 0:
            total_tokens_list.append(int(tt))

        # Latency
        rt = rec.get("runtime_seconds", 0)
        if isinstance(rt, (int, float)) and rt > 0:
            latencies.append(float(rt))

        # Correction types (failure patterns)
        corrections = metrics.get("self_correction", {})
        for ctype in corrections.get("correction_types", []):
            correction_types[ctype] = correction_types.get(ctype, 0) + 1

        # Topic failure rates (only when questions were logged).
        ctx = rec.get("context", {})
        subject = ctx.get("subject", "")
        if subject and subject != "[REDACTED]":
            topic = _topic_bucket(subject)
            topic_failures.setdefault(topic, []).append(
                float(sr) if isinstance(sr, (int, float)) else 0.0
            )

        # Experiment groups.
        exp_id = rec.get("experiment_id")
        if exp_id:
            experiment_groups.setdefault(exp_id, []).append(rec)

    # ── Top failure patterns ──────────────────────────────────────────
    sorted_patterns = sorted(
        correction_types.items(), key=lambda x: x[1], reverse=True
    )
    top_failure_patterns = [
        {"pattern": p, "count": c} for p, c in sorted_patterns[:3]
    ]

    # ── Topic failure rates ───────────────────────────────────────────
    topic_failure_rates: dict[str, float] = {}
    for topic, rates in topic_failures.items():
        if rates:
            topic_failure_rates[topic] = 1.0 - (sum(rates) / len(rates))

    # ── Experiment comparisons ────────────────────────────────────────
    experiment_summaries: dict[str, dict[str, Any]] = {}
    for exp_id, exp_recs in experiment_groups.items():
        exp_srs = []
        exp_tokens = []
        for r in exp_recs:
            s = r.get("summary", {})
            sr_v = s.get("success_rate", 0)
            tt_v = s.get("total_tokens", 0)
            if isinstance(sr_v, (int, float)):
                exp_srs.append(float(sr_v))
            if isinstance(tt_v, (int, float)) and tt_v > 0:
                exp_tokens.append(int(tt_v))
        variants = set(
            r.get("variant", "default") for r in exp_recs
        )
        experiment_summaries[exp_id] = {
            "record_count": len(exp_recs),
            "variants": sorted(variants),
            "avg_success_rate": (
                sum(exp_srs) / len(exp_srs) if exp_srs else None
            ),
            "avg_tokens": (
                sum(exp_tokens) / len(exp_tokens) if exp_tokens else None
            ),
        }

    return {
        "window_days": window_days,
        "record_count": len(records),
        "avg_success_rate": (
            sum(success_rates) / len(success_rates) if success_rates else None
        ),
        "avg_total_tokens": (
            sum(total_tokens_list) / len(total_tokens_list)
            if total_tokens_list
            else None
        ),
        "avg_latency_seconds": (
            sum(latencies) / len(latencies) if latencies else None
        ),
        "top_failure_patterns": top_failure_patterns,
        "topic_failure_rates": topic_failure_rates,
        "experiments": experiment_summaries,
    }


def _topic_bucket(subject: str) -> str:
    """Heuristic topic bucketing for aggregation.

    Uses the first 3 words as a coarse bucket — sufficient for trend
    spotting without storing full questions.
    """
    words = subject.strip().split()[:3]
    return " ".join(words).lower() if words else "unknown"


# ---------------------------------------------------------------------------
# Improvement suggestions
# ---------------------------------------------------------------------------


def generate_improvement_suggestions(analysis: dict[str, Any]) -> str:
    """Generate human-readable improvement suggestions from trend analysis.

    Args:
        analysis: Result dict from ``analyze_eval_trends``.

    Returns:
        Markdown string with actionable suggestions.
    """
    if analysis.get("record_count", 0) == 0:
        return (
            "# Improvement Suggestions\n\n"
            "No eval records found. Enable eval tracking to collect data.\n"
        )

    lines: list[str] = [
        "# Improvement Suggestions",
        "",
        f"Based on {analysis['record_count']} runs over "
        f"the last {analysis['window_days']} days.",
        "",
    ]

    # Success rate check.
    avg_sr = analysis.get("avg_success_rate")
    if avg_sr is not None:
        if avg_sr < 0.80:
            lines.append(
                f"- **Low tool success rate ({avg_sr:.1%})**: "
                "Investigate tool failure logs. Consider increasing retry "
                "budget or adding fallback search providers."
            )
        elif avg_sr > 0.95:
            lines.append(
                f"- Tool success rate is healthy ({avg_sr:.1%})."
            )

    # Token efficiency.
    avg_tokens = analysis.get("avg_total_tokens")
    if avg_tokens is not None and avg_tokens > 50000:
        lines.append(
            f"- **High token usage ({avg_tokens:,.0f} avg)**: "
            "Review prompt length and sub-agent iteration limits. "
            "Consider reducing MAX_RESEARCHER_ITERATIONS."
        )

    # Latency.
    avg_lat = analysis.get("avg_latency_seconds")
    if avg_lat is not None and avg_lat > 120:
        lines.append(
            f"- **High latency ({avg_lat:.0f}s avg)**: "
            "Check for blocking operations in the research pipeline. "
            "Consider reducing wiki ingest timeout or citation spot-checks."
        )

    # Failure patterns.
    top_patterns = analysis.get("top_failure_patterns", [])
    if top_patterns:
        lines.append("")
        lines.append("## Top Failure Patterns")
        for p in top_patterns:
            lines.append(f"- `{p['pattern']}`: {p['count']} occurrences")

    # Topic failure rates.
    topic_rates = analysis.get("topic_failure_rates", {})
    if topic_rates:
        high_failure_topics = [
            (t, r) for t, r in topic_rates.items() if r > 0.3
        ]
        if high_failure_topics:
            lines.append("")
            lines.append("## High-Failure Topics")
            for topic, rate in sorted(
                    high_failure_topics, key=lambda x: x[1], reverse=True
            ):
                lines.append(f"- `{topic}`: {rate:.1%} failure rate")

    # Experiment comparisons.
    experiments = analysis.get("experiments", {})
    if len(experiments) > 1:
        lines.append("")
        lines.append("## Experiment Comparisons")
        for exp_id, summary in experiments.items():
            lines.append(
                f"- **{exp_id}** ({', '.join(summary['variants'])}): "
                f"SR={summary['avg_success_rate']:.1%}, "
                f"tokens={summary['avg_tokens']:,.0f}"
                if summary["avg_success_rate"] is not None
                else f"- **{exp_id}**: insufficient data"
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Robust baseline computation
# ---------------------------------------------------------------------------


def compute_baseline_from_history(
        history_dir: Path,
        manifest_hash_value: str | None = None,
        window_size: int = _BASELINE_WINDOW_SIZE,
) -> dict[str, Any] | None:
    """Compute a statistically robust baseline from the most recent N runs.

    Improves on ``latest_baseline`` (eval_tracking.py) which uses only a
    single run — median aggregation is more stable against outliers.

    Args:
        history_dir: Directory containing ``*.jsonl`` eval history files.
        manifest_hash_value: Optional filter — only aggregate runs with
            this manifest hash.  If None, aggregates ALL recent runs.
        window_size: Number of most recent matching runs to include.

    Returns:
        Dict with ``record_count``, ``median_success_rate``,
        ``median_total_tokens``, ``median_latency_seconds``, and
        ``record_count`` — or None if no matching records found.
    """
    records: list[dict[str, Any]] = []
    for jsonl_path in sorted(history_dir.glob("*.jsonl"), reverse=True):
        for rec in load_jsonl(jsonl_path):
            if manifest_hash_value is not None:
                if rec.get("manifest_hash") != manifest_hash_value:
                    continue
            records.append(rec)
            if len(records) >= window_size * 3:
                break
        if len(records) >= window_size * 3:
            break

    if not records:
        return None

    # Sort by timestamp descending, take the most recent N.
    records.sort(
        key=lambda r: r.get("timestamp_utc", ""), reverse=True
    )
    recent = records[:window_size]

    success_rates: list[float] = []
    total_tokens: list[int] = []
    latencies: list[float] = []

    for rec in recent:
        metrics = rec.get("metrics", {})
        summary = rec.get("summary", {})

        sr = summary.get(
            "success_rate",
            metrics.get("tool_execution", {}).get("success_rate", 0),
        )
        if isinstance(sr, (int, float)):
            success_rates.append(float(sr))

        tt = summary.get(
            "total_tokens",
            metrics.get("token_efficiency", {}).get("total_tokens", 0),
        )
        if isinstance(tt, (int, float)) and tt > 0:
            total_tokens.append(int(tt))

        rt = rec.get("runtime_seconds", 0)
        if isinstance(rt, (int, float)) and rt > 0:
            latencies.append(float(rt))

    return {
        "record_count": len(recent),
        "manifest_hash": manifest_hash_value,
        "median_success_rate": (
            statistics.median(success_rates) if success_rates else None
        ),
        "median_total_tokens": (
            statistics.median(total_tokens) if total_tokens else None
        ),
        "median_latency_seconds": (
            statistics.median(latencies) if latencies else None
        ),
    }
