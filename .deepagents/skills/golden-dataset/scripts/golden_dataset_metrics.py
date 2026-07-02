"""
Golden dataset evaluation helpers.

Use `Evaluation process` in https://github.com/microsoft/promptflow-resource-hub/blob/main/sample_gallery/golden_dataset/copilot-golden-dataset-creation-guidance.md.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import sys
import yaml
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

# Add project root to path for retry_utils and utils imports
_sys_path_root = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_sys_path_root) not in sys.path:
    sys.path.insert(0, str(_sys_path_root))

# Local import from same scripts directory
from skill_model_factory import get_configured_model

load_dotenv()


def load_metrics_config() -> dict:
    """Load metric configurations from YAML file."""
    config_path = Path(__file__).parent / "metrics_config.yaml"
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_CONFIG = load_metrics_config()
METRIC_NAMES = tuple(_CONFIG["metrics"].keys())
REQUIRED_INPUT_COLUMNS = ("Question", "Answer")
JUDGE_PROMPT_TEMPLATE = _CONFIG["judge_prompt"]
METRIC_GUIDANCE = {
    name: f"{details['description']} Score on a scale of {details['scale']} Suggested goal: {details['goal']}."
    for name, details in _CONFIG["metrics"].items()
}
METRIC_RANGES = {
    name: (details["min"], details["max"])
    for name, details in _CONFIG["metrics"].items()
}


def parse_metric_scores(response_text: str) -> dict[str, float]:
    """Extract metric values from judge output."""
    metrics: dict[str, float] = {}
    for metric_name in METRIC_NAMES:
        pattern = rf"{metric_name}\s*:\s*(-?\d+(?:\.\d+)?)"
        match = re.search(pattern, response_text, re.IGNORECASE)
        if not match:
            raise ValueError(f"Missing metric '{metric_name}' in judge response: {response_text}")
        value = float(match.group(1))
        min_value, max_value = METRIC_RANGES[metric_name]
        if value < min_value or value > max_value:
            raise ValueError(
                f"Metric '{metric_name}' value {value} is outside the allowed range "
                f"{min_value}-{max_value}."
            )
        metrics[metric_name] = value
    return metrics


def build_judge_prompt(question: str, answer: str, context: str = "") -> str:
    """Create a deterministic judge prompt for the four dataset metrics."""
    context_block = context.strip() or "No grounding context was provided. Groundedness should be scored conservatively."
    metric_guidance_lines = "\n".join(
        f"- {metric_name}: {METRIC_GUIDANCE[metric_name]}" for metric_name in METRIC_NAMES
    )
    return JUDGE_PROMPT_TEMPLATE.format(
        metric_guidance_lines=metric_guidance_lines,
        question=question.strip(),
        answer=answer.strip(),
        context=context_block,
    )


def score_row(model, row: dict[str, str]) -> dict[str, float]:
    """Evaluate one dataset row with the configured judge model."""
    prompt = build_judge_prompt(
        question=row["Question"],
        answer=row["Answer"],
        context=row.get("Context", ""),
    )
    response = model.invoke([HumanMessage(content=prompt)])
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "\n".join(str(part) for part in content)
    return parse_metric_scores(str(content))


def validate_input_columns(fieldnames: Iterable[str] | None) -> None:
    """Validate that the input dataset contains the expected minimum columns."""
    existing = set(fieldnames or [])
    missing = [column for column in REQUIRED_INPUT_COLUMNS if column not in existing]
    if missing:
        raise ValueError(
            "Input dataset is missing required columns: " + ", ".join(missing)
        )


def build_missing_context_report(rows: Iterable[dict[str, str]]) -> str:
    """Build a small warning report for rows that are missing grounding context."""
    missing_rows: list[str] = []
    for index, row in enumerate(rows, start=1):
        context = (row.get("Context") or row.get("context") or "").strip()
        if context:
            continue
        row_id = (row.get("ID") or row.get("id") or f"row-{index}").strip()
        question = (row.get("Question") or row.get("question") or "").strip()
        question_preview = question[:80] + ("..." if len(question) > 80 else "")
        missing_rows.append(f"- {row_id}: {question_preview}")

    if not missing_rows:
        return "All rows include Context."

    header = f"Warning: {len(missing_rows)} row(s) are missing Context."
    guidance = (
        "Groundedness scoring is less reliable for these rows because the supporting RAG context is absent, "
        "but scoring still runs."
    )
    return "\n".join([header, guidance, *missing_rows])


def score_dataset_file(input_csv: str, output_csv: str) -> Path:
    """Read a dataset CSV, append quality metrics, and write the enriched file."""
    input_path = Path(input_csv)
    output_path = Path(output_csv)

    with input_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        validate_input_columns(reader.fieldnames)
        rows = list(reader)
        base_fieldnames = list(reader.fieldnames or [])

    model = get_configured_model()
    scored_rows: list[dict[str, str]] = []
    for row in rows:
        metrics = score_row(model, row)
        enriched_row = dict(row)
        for metric_name, metric_value in metrics.items():
            enriched_row[metric_name] = (
                str(int(metric_value)) if float(metric_value).is_integer() else str(metric_value)
            )
        scored_rows.append(enriched_row)

    fieldnames = base_fieldnames + [name for name in METRIC_NAMES if name not in base_fieldnames]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scored_rows)

    return output_path


def metrics_to_json(scores: dict[str, float]) -> str:
    """Serialize metrics for logging or downstream tooling."""
    return json.dumps(scores, sort_keys=True)


def convert_csv_to_markdown(csv_path: str) -> str:
    """Convert a CSV file to a Markdown table format.
    
    Args:
        csv_path: Path to the CSV file with metrics columns.
        
    Returns:
        Markdown formatted table string.
    """
    try:
        df = pd.read_csv(csv_path)  # type: ignore[call-overload]
        # Convert to markdown table without index
        markdown_table = df.to_markdown(index=False)
        return markdown_table
    except Exception as e:
        return f"Error converting CSV to markdown: {e}"


def generate_golden_dataset_report(
        csv_path: str,
        metrics_csv_path: str,
        markdown_content: str,
        payload: dict,
        elapsed_seconds: float | None = None
) -> str:
    """Generate a comprehensive final report for the golden dataset generation process.
    
    Args:
        csv_path: Path to the original CSV file.
        metrics_csv_path: Path to the CSV file with quality metrics.
        markdown_content: The markdown table content from metrics CSV.
        payload: The golden dataset payload with metadata.
        elapsed_seconds: Total time spent in agent chat (in seconds).
        
    Returns:
        Complete markdown report content.
    """
    dataset_name = payload.get("dataset_name", "Unknown Dataset")
    domain = payload.get("domain", "General")
    total_items = len(payload.get("items", []))
    coverage_areas = payload.get("coverage_areas", [])

    # Calculate summary statistics from metrics if available
    metrics_summary = ""
    try:
        df = pd.read_csv(metrics_csv_path)  # type: ignore[call-overload]
        metric_columns = [col for col in df.columns if col in METRIC_NAMES]

        if metric_columns:
            metrics_summary = "\n## Quality Metrics Summary\n\n"
            metrics_summary += "| Metric | Mean | Min | Max | Std Dev |\n"
            metrics_summary += "|--------|------|-----|-----|---------|\n"

            for metric in metric_columns:
                mean_val = df[metric].mean()
                min_val = df[metric].min()
                max_val = df[metric].max()
                std_val = df[metric].std()
                metrics_summary += f"| {metric} | {mean_val:.2f} | {min_val:.2f} | {max_val:.2f} | {std_val:.2f} |\n"

            # Add goal achievements
            metrics_summary += "\n### Goal Achievement\n\n"
            for metric in metric_columns:
                goal_str = _CONFIG["metrics"][metric]["goal"]
                # Extract numeric value from goal string (e.g., "3+" -> 3.0)
                min_goal = float(re.search(r'\d+(?:\.\d+)?', str(goal_str)).group())
                mean_val = df[metric].mean()
                achieved = "✅" if mean_val >= min_goal else "⚠️"
                metrics_summary += f"- {achieved} **{metric}**: {mean_val:.2f} (goal: {min_goal}+)\n"
    except Exception as e:
        metrics_summary = f"\n## Quality Metrics Summary\n\nCould not calculate summary statistics: {e}\n"

    # Build the complete report
    report = f"""# Golden Dataset Generation Report: {dataset_name}

**Generated:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

**Time Spent:** {f"{elapsed_seconds:.2f}" if elapsed_seconds is not None else "N/A"} seconds

## Overview

- **Dataset Name:** {dataset_name}
- **Domain:** {domain}
- **Total Items:** {total_items}
- **Coverage Areas:** {', '.join(coverage_areas) if coverage_areas else 'N/A'}

## Process Summary

This report documents the complete golden dataset generation and evaluation process:

1. ✅ Generated {total_items} question-answer pairs based on research findings
2. ✅ Exported dataset to CSV: `{csv_path}`
3. ✅ Evaluated quality metrics using LLM judge model
4. ✅ Generated metrics-enhanced CSV: `{metrics_csv_path}`
5. ✅ Created this comprehensive report

{metrics_summary}

## Detailed Metrics Table

The following table shows all items with their quality metrics:

{markdown_content}

## Recommendations

Based on the quality metrics:

1. **Review Low-Scoring Items:** Focus on items where any metric falls below the suggested goal
2. **Expert Validation:** Have domain experts review and replace draft answers with authoritative responses
3. **Context Enhancement:** For items with low Groundedness scores, consider adding more supporting RAG context
4. **Iterative Improvement:** Use these metrics as a baseline for future dataset refinements

## Files Generated

- Original CSV: `{csv_path}`
- Metrics CSV: `{metrics_csv_path}`
- This Report: `/final_report.md`
- Metrics Table: `/golden_dataset_metrics.md`

---

*Report generated automatically by the Golden Dataset Skill*
"""

    return report
