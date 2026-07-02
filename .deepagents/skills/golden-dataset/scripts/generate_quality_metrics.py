#!/usr/bin/env python3
"""Append judge-model quality metrics to a golden dataset CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import sys

# Add project root to path for retry_utils and utils imports
_sys_path_root = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_sys_path_root) not in sys.path:
    sys.path.insert(0, str(_sys_path_root))

# Local import from same scripts directory
from golden_dataset_metrics import (
    build_missing_context_report,
    score_dataset_file,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the quality metrics generation CLI.

    Returns:
        An ``ArgumentParser`` configured with ``input_csv``, ``--output-csv``,
        ``--report``, and ``--report-file`` arguments.
    """
    parser = argparse.ArgumentParser(
        description="Generate Similarity, Relevance, Coherence, and Groundedness columns for a golden dataset CSV.",
    )
    parser.add_argument("input_csv", help="Path to the input CSV with at least Question and Answer columns.")
    parser.add_argument(
        "--output-csv",
        help="Path to write the enriched CSV. Defaults to <input>-with-metrics.csv.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print a warning report for rows that are missing Content.",
    )
    parser.add_argument(
        "--report-file",
        help="Optional path to also write the missing-Content warning report.",
    )
    return parser


def default_output_path(input_csv: str) -> str:
    """Derive the default output CSV path from the input filename.

    Args:
        input_csv: Path to the input CSV file.

    Returns:
        A path like ``<input>-with-metrics.csv``.
    """
    input_path = Path(input_csv)
    return str(input_path.with_name(f"{input_path.stem}-with-metrics{input_path.suffix}"))


def default_report_path(input_csv: str) -> str:
    """Derive the default content report path from the input filename.

    Args:
        input_csv: Path to the input CSV file.

    Returns:
        A path like ``<input>-content-report.txt``.
    """
    input_path = Path(input_csv)
    return str(input_path.with_name(f"{input_path.stem}-content-report.txt"))


def load_rows(input_csv: str) -> list[dict[str, str]]:
    """Load CSV rows as a list of dictionaries.

    Args:
        input_csv: Path to the CSV file.

    Returns:
        A list of dicts, one per row, with column headers as keys.
    """
    input_path = Path(input_csv)
    with input_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    """Entry point: score a dataset CSV and optionally generate a content report."""
    parser = build_parser()
    args = parser.parse_args()
    output_csv = args.output_csv or default_output_path(args.input_csv)
    result = score_dataset_file(args.input_csv, output_csv)
    print(f"Wrote scored dataset to {result}")
    if args.report:
        report = build_missing_context_report(load_rows(args.input_csv))
        print(report)
        if args.report_file:
            report_path = Path(args.report_file)
        else:
            report_path = Path(default_report_path(args.input_csv))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report + "\n", encoding="utf-8")
        print(f"Wrote content report to {report_path}")


if __name__ == "__main__":
    main()
