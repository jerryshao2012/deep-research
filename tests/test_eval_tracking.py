"""Tests for evaluation tracking and metrics collection."""

from __future__ import annotations

from pathlib import Path

from research_agent.utils.cli import build_parser
from research_agent.utils.eval_tracking import (
    append_jsonl,
    build_manifest,
    collect_run_metrics,
    compare_records,
    load_jsonl,
    make_run_record,
    manifest_hash,
)


def test_manifest_hash_changes_when_subject_changes() -> None:
    manifest_5 = build_manifest(
        subject="Generate 5 pairs for policy docs",
        skill="golden-dataset",
        doc_folder="./docs/policy",
        no_web=False,
        model_name="test-model",
        verify_ssl=True,
    )
    manifest_10 = build_manifest(
        subject="Generate 10 pairs for policy docs",
        skill="golden-dataset",
        doc_folder="./docs/policy",
        no_web=False,
        model_name="test-model",
        verify_ssl=True,
    )

    assert manifest_hash(manifest_5) != manifest_hash(manifest_10)


def test_collect_run_metrics_requires_golden_artifacts_for_completeness() -> None:
    result = {
        "messages": [
            {"role": "assistant", "content": "", "tool_calls": [{"name": "task"}]},
            {"role": "tool", "name": "task", "content": "done"},
        ],
        "files": {
            "/golden_dataset_metrics.md": {"value": "ok"},
            "/final_report.md": {"value": "ok"},
        },
    }

    metrics = collect_run_metrics(result, runtime_seconds=3.5, stream_fallback_used=False)

    assert metrics["completeness"]["pass"] is True
    assert metrics["tool_execution"]["total_tool_calls"] == 1
    assert metrics["tool_execution"]["successful_tool_calls"] == 1


def test_compare_records_returns_non_comparable_without_matching_baseline() -> None:
    candidate_manifest = build_manifest(
        subject="Generate 10 pairs",
        skill="golden-dataset",
        doc_folder="./docs/policy",
        no_web=False,
        model_name="test-model",
        verify_ssl=True,
    )

    candidate_record = make_run_record(
        manifest=candidate_manifest,
        run_type="candidate",
        metrics={
            "completeness": {"pass": True},
            "tool_execution": {"total_tool_calls": 10},
            "failure": {"failure_rate": 0.0},
            "token_efficiency": {"available": False},
            "latency": {"runtime_seconds": 10.0},
        },
        runtime_seconds=10.0,
        model_name="test-model",
        stream_fallback_used=False,
        output_file="output.md",
        git_sha="abc123",
    )

    comparison = compare_records(baseline=None, candidate=candidate_record)

    assert comparison["comparable"] is False
    assert comparison["overall_verdict"] == "non-comparable"


def test_compare_records_rejects_5_vs_10_pairs_case_mismatch() -> None:
    baseline_manifest = build_manifest(
        subject="Generate 5 pairs",
        skill="golden-dataset",
        doc_folder="./docs/policy",
        no_web=False,
        model_name="test-model",
        verify_ssl=True,
    )
    candidate_manifest = build_manifest(
        subject="Generate 10 pairs",
        skill="golden-dataset",
        doc_folder="./docs/policy",
        no_web=False,
        model_name="test-model",
        verify_ssl=True,
    )

    baseline_record = make_run_record(
        manifest=baseline_manifest,
        run_type="baseline",
        metrics={
            "completeness": {"pass": True},
            "tool_execution": {"total_tool_calls": 6},
            "failure": {"failure_rate": 0.0},
            "token_efficiency": {"available": False},
            "latency": {"runtime_seconds": 8.0},
        },
        runtime_seconds=8.0,
        model_name="test-model",
        stream_fallback_used=False,
        output_file="out1.md",
        git_sha="abc123",
    )
    candidate_record = make_run_record(
        manifest=candidate_manifest,
        run_type="candidate",
        metrics={
            "completeness": {"pass": True},
            "tool_execution": {"total_tool_calls": 9},
            "failure": {"failure_rate": 0.0},
            "token_efficiency": {"available": False},
            "latency": {"runtime_seconds": 8.5},
        },
        runtime_seconds=8.5,
        model_name="test-model",
        stream_fallback_used=False,
        output_file="out2.md",
        git_sha="abc123",
    )

    comparison = compare_records(baseline=baseline_record, candidate=candidate_record)

    assert comparison["comparable"] is False
    assert comparison["overall_verdict"] == "non-comparable"


def test_jsonl_append_and_load(tmp_path: Path) -> None:
    history_file = tmp_path / "golden_dataset_runs.jsonl"

    record = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "run_type": "baseline",
        "manifest_hash": "abc",
        "metrics": {"completeness": {"pass": True}},
    }

    append_jsonl(history_file, record)
    records = load_jsonl(history_file)

    assert len(records) == 1
    assert records[0]["run_type"] == "baseline"


def test_parser_accepts_eval_arguments() -> None:
    import sys
    from pathlib import Path
    _scripts_dir = Path(__file__).resolve().parent.parent / ".deepagents" / "skills" / "golden-dataset" / "scripts"
    if str(_scripts_dir) not in sys.path:
        sys.path.insert(0, str(_scripts_dir))
    from score_dataset import build_parser as build_score_parser

    parser = build_score_parser()
    args = parser.parse_args(
        [
            "dataset.csv",
            "--eval-mode",
            "baseline",
            "--output-dir",
            "./output",
        ]
    )

    assert args.input_csv == "dataset.csv"
    assert args.eval_mode == "baseline"
    assert args.output_dir == "./output"
