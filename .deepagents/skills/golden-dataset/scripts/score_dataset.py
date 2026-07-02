#!/usr/bin/env python3
"""Full golden-dataset evaluation pipeline with built-in regression tracking.

Usage:
    python score_dataset.py <input.csv> [--output-dir ./output] [--eval-mode baseline|candidate]

The pipeline bundles scoring, markdown conversion, report generation, and
humanization into a single command.  When ``--eval-mode`` is set the run
is recorded to a JSONL history file and, for candidate runs, compared
against the most recent baseline with the same manifest.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import sys
import time

# Add project root to path so we can import the eval-tracking module and
# the retry_utils / utils helpers that the scoring scripts need.
_project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Local imports from same scripts directory
from golden_dataset_metrics import (
    convert_csv_to_markdown,
    generate_golden_dataset_report,
    score_dataset_file,
)
from humanize_report import humanize_report


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _build_manifest(input_csv: str, eval_mode: str) -> dict:
    """Build a lightweight manifest for run identification."""
    abs_path = Path(input_csv).resolve()
    return {
        "input_csv": str(abs_path),
        "input_size_bytes": abs_path.stat().st_size if abs_path.is_file() else 0,
        "eval_mode": eval_mode,
        "timestamp_utc": _now_iso(),
    }


def _manifest_hash(manifest: dict) -> str:
    """Stable hash of manifest fields that determine comparability."""
    import hashlib
    keys = sorted(k for k in manifest if k not in ("timestamp_utc", "eval_mode"))
    payload = json.dumps({k: manifest[k] for k in keys}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _collect_metrics(metrics_csv_path: Path, report_path: Path, elapsed: float) -> dict:
    """Collect quality and completeness metrics from pipeline outputs."""
    metrics: dict[str, object] = {"runtime_seconds": round(elapsed, 2)}

    if metrics_csv_path.is_file():
        metrics["has_metrics_csv"] = True
        metrics["metrics_csv_size_bytes"] = metrics_csv_path.stat().st_size
    else:
        metrics["has_metrics_csv"] = False

    if report_path.is_file():
        metrics["has_final_report"] = True
        metrics["report_size_bytes"] = report_path.stat().st_size
    else:
        metrics["has_final_report"] = False

    return metrics


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Score a golden-dataset CSV, generate a full report, "
                    "and optionally track runs for regression testing."
    )
    p.add_argument("input_csv", type=str, help="Path to the input CSV file")
    p.add_argument(
        "--output-dir",
        type=str,
        default=os.environ.get("OUTPUT_FOLDER", "./output"),
        help="Output directory (default: ./output or $OUTPUT_FOLDER)",
    )
    p.add_argument(
        "--no-humanize",
        action="store_true",
        help="Skip the humanizer post-processing step",
    )
    p.add_argument(
        "--eval-mode",
        choices=["baseline", "candidate"],
        default=None,
        help="Enable regression tracking.  'baseline' records a reference; "
             "'candidate' also compares against the latest matching baseline.",
    )
    return p


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: run the full golden-dataset scoring pipeline.

    Executes a 6-step pipeline: (1) score with LLM judge, (2) convert to
    Markdown table, (3) generate comprehensive report, (4) optionally
    humanize, (5) write report/metrics files, (6) optionally record eval
    tracking for regression comparison.
    """
    args = build_parser().parse_args()
    start_time = time.time()

    input_path = Path(args.input_csv)
    if not input_path.is_file():
        print(f"Error: Input CSV not found: {args.input_csv}")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = input_path.stem
    metrics_csv_path = output_dir / f"{stem}-with-metrics.csv"

    # ── Pipeline ──────────────────────────────────────────────────────────

    # Step 1: Score the dataset (LLM judge)
    print(f"Scoring dataset: {input_path}")
    score_dataset_file(str(input_path), str(metrics_csv_path))
    print(f"  → Metrics CSV: {metrics_csv_path}")

    # Step 2: Convert scored CSV to markdown table
    print("Converting to markdown...")
    markdown_table = convert_csv_to_markdown(str(metrics_csv_path))

    # Step 3: Generate comprehensive report
    print("Generating report...")
    payload = {}
    final_report = generate_golden_dataset_report(
        csv_path=str(input_path),
        metrics_csv_path=str(metrics_csv_path),
        markdown_content=markdown_table,
        payload=payload,
        elapsed_seconds=None,
    )

    # Step 4: Humanize the report (unless --no-humanize)
    if not args.no_humanize:
        print("Humanizing report...")
        humanized = humanize_report(final_report)
        if humanized and len(humanized) >= len(final_report) * 0.3:
            final_report = humanized
            print("  → Report humanized")
        else:
            print("  → Humanizer skipped (output too short or failed)")

    # Step 5: Write final report
    report_path = output_dir / f"{stem}_report.md"
    report_path.write_text(final_report, encoding="utf-8")

    # Step 6: Write metrics markdown
    metrics_md_path = output_dir / f"{stem}_metrics.md"
    metrics_md_path.write_text(markdown_table, encoding="utf-8")

    elapsed = time.time() - start_time

    print(f"\nDone in {elapsed:.1f}s. Files written to {output_dir}/:")
    print(f"  {metrics_csv_path.name}")
    print(f"  {metrics_md_path.name}")
    print(f"  {report_path.name}")

    # ── Eval tracking (built-in) ──────────────────────────────────────────

    if args.eval_mode is None:
        return

    history_dir = output_dir / "eval_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / "golden_dataset_runs.jsonl"

    manifest = _build_manifest(args.input_csv, args.eval_mode)
    manifest["manifest_hash"] = _manifest_hash(manifest)

    run_metrics = _collect_metrics(metrics_csv_path, report_path, elapsed)

    record = {
        "manifest": manifest,
        "run_type": args.eval_mode,
        "metrics": run_metrics,
        "timestamp_utc": _now_iso(),
    }

    # Load history, find baseline, compare
    records: list[dict] = []
    if history_path.is_file():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    baseline = None
    for r in reversed(records):
        if r.get("run_type") == "baseline" and r.get("manifest", {}).get("manifest_hash") == manifest["manifest_hash"]:
            baseline = r
            break

    if args.eval_mode == "candidate" and baseline:
        base_m = baseline.get("metrics", {})
        cand_m = run_metrics
        comparison: dict[str, object] = {"comparable": True, "diffs": {}}
        for key in sorted(set(list(base_m.keys()) + list(cand_m.keys()))):
            if key == "runtime_seconds":
                continue
            bv = base_m.get(key)
            cv = cand_m.get(key)
            if bv != cv:
                comparison["diffs"][key] = {"baseline": bv, "candidate": cv}
        if comparison.get("diffs"):
            comparison["verdict"] = "regression_detected"
        else:
            comparison["verdict"] = "unchanged"
        record["comparison"] = comparison

        print(f"\nEvaluation — {args.eval_mode} vs baseline")
        print(f"  Manifest hash: {manifest['manifest_hash']}")
        print(f"  Verdict: {comparison['verdict']}")
        diffs = comparison.get("diffs", {})
        if isinstance(diffs, dict) and diffs:
            for key, d in diffs.items():  # type: ignore[assignment]
                print(f"  - {key}: baseline={d['baseline']}, candidate={d['candidate']}")  # type: ignore[index]

    # Append record
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")

    print(f"  Run recorded: {history_path}")


if __name__ == "__main__":
    main()
