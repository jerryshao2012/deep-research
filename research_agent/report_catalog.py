"""Report catalog and archival management for multi-topic research persistence.

Maintains an immutable archive of research reports and requests across multiple
topics in a thread's virtual filesystem, while preserving /final_report.md as the
active canonical report for backward compatibility.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TypedDict

from deepagents.backends.utils import create_file_data, file_data_to_string

from research_agent.completion_guard import artifact_fingerprint

CANONICAL_REPORT_PATH = "/final_report.md"
CANONICAL_REQUEST_PATH = "/research_request.md"
MANIFEST_PATH = "/reports/manifest.json"
README_PATH = "/reports/README.md"


class ReportRecord(TypedDict):
    """Metadata record for an archived research report."""

    index: int
    run_id: str
    timestamp: str
    topic: str
    slug: str
    report_path: str
    request_path: str
    fingerprint: str
    word_count: int


def slugify_topic(text: str, max_words: int = 6, max_length: int = 50) -> str:
    """Normalize a topic query into a clean, filesystem-safe slug.

    Args:
        text: Research query or request text.
        max_words: Maximum number of words in slug.
        max_length: Maximum total length of slug string.

    Returns:
        A lowercase, hyphen-separated alphanumeric slug.
    """
    if not text:
        return "research-topic"
    first_line = text.strip().split("\n")[0]
    cleaned = re.sub(r"[^\w\s-]", "", first_line).strip().lower()
    words = cleaned.split()[:max_words]
    slug = "-".join(words)
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "research-topic"


def get_archived_reports(state: Mapping[str, Any]) -> list[ReportRecord]:
    """Retrieve the list of all archived report records from state.

    Args:
        state: LangGraph state mapping containing 'files'.

    Returns:
        List of ReportRecord dictionaries, or empty list if no manifest exists.
    """
    files = state.get("files")
    if not isinstance(files, Mapping):
        return []
    manifest_data = files.get(MANIFEST_PATH)
    if not isinstance(manifest_data, Mapping):
        return []
    try:
        raw_json = file_data_to_string(manifest_data)  # type: ignore[arg-type]
        parsed = json.loads(raw_json)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return []


def _generate_readme_catalog(records: list[ReportRecord]) -> str:
    """Generate a Markdown summary table of all archived reports."""
    lines = [
        "# Research Reports Archive",
        "",
        f"Total Reports: {len(records)}",
        "",
        "| # | Topic | Date | Report Link | Request Link | Word Count |",
        "|---|---|---|---|---|---|",
    ]
    for r in records:
        date_str = r.get("timestamp", "")[:10]
        word_count = f"{r.get('word_count', 0):,}"
        topic = r.get("topic", "Untitled").replace("|", "\\|")
        rep_path = r.get("report_path", "")
        req_path = r.get("request_path", "")
        lines.append(
            f"| {r.get('index', 0)} | {topic} | {date_str} | [{r.get('slug', 'report')}]({rep_path}) | [request]({req_path}) | {word_count} |"
        )
    lines.append("")
    return "\n".join(lines)


def archive_report_to_state(
    state: Mapping[str, Any],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Archive current /final_report.md and /research_request.md to /reports/.

    Checks the active canonical report and request in state['files']. If a valid,
    non-empty report exists that has not already been archived (checked via its
    content fingerprint), archives it under a versioned path and updates
    /reports/manifest.json and /reports/README.md.

    Args:
        state: Current LangGraph state mapping containing 'files'.
        run_id: Optional run ID to associate with the record.

    Returns:
        State update dictionary with 'files' containing new archived files,
        or empty dictionary if no archival was needed.
    """
    files = state.get("files")
    if not isinstance(files, Mapping):
        return {}

    report_data = files.get(CANONICAL_REPORT_PATH)
    if not isinstance(report_data, Mapping):
        return {}

    try:
        report_content = file_data_to_string(report_data)  # type: ignore[arg-type]
    except Exception:
        return {}

    if not report_content.strip():
        return {}

    current_fingerprint = artifact_fingerprint(report_data)
    if not current_fingerprint:
        return {}

    # Read existing manifest
    existing_records = get_archived_reports(state)

    # Check if this exact report fingerprint is already archived
    if any(r.get("fingerprint") == current_fingerprint for r in existing_records):
        return {}

    # Extract topic / request content
    request_content = ""
    request_data = files.get(CANONICAL_REQUEST_PATH)
    if isinstance(request_data, Mapping):
        try:
            request_content = file_data_to_string(request_data)  # type: ignore[arg-type]
        except Exception:
            request_content = ""

    if not request_content.strip():
        # Fallback to user message from state if available
        messages = state.get("messages", [])
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                request_content = str(m.get("content", ""))
                break
            elif hasattr(m, "content") and getattr(m, "type", None) == "human":
                request_content = str(m.content)
                break

    topic_title = request_content.strip().split("\n")[0][:120] if request_content.strip() else "Untitled Research"
    slug = slugify_topic(topic_title)
    index = len(existing_records) + 1

    folder = f"/reports/topic_{index:03d}_{slug}"
    archived_report_path = f"{folder}/final_report.md"
    archived_request_path = f"{folder}/request.md"

    resolved_run_id = str(
        run_id
        or state.get("completion_current_run_id")
        or state.get("completion_request_generation")
        or "unknown"
    )

    record: ReportRecord = {
        "index": index,
        "run_id": resolved_run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "topic": topic_title,
        "slug": slug,
        "report_path": archived_report_path,
        "request_path": archived_request_path,
        "fingerprint": current_fingerprint,
        "word_count": len(report_content.split()),
    }

    updated_records = list(existing_records) + [record]
    readme_content = _generate_readme_catalog(updated_records)
    manifest_content = json.dumps(updated_records, indent=2)

    return {
        "files": {
            archived_report_path: create_file_data(report_content),
            archived_request_path: create_file_data(request_content or topic_title),
            MANIFEST_PATH: create_file_data(manifest_content),
            README_PATH: create_file_data(readme_content),
        }
    }
