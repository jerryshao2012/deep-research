"""Unit and integration tests for report catalog and multi-topic archival."""

import json
from deepagents.backends.utils import create_file_data, file_data_to_string

from research_agent.report_catalog import (
    archive_report_to_state,
    get_archived_reports,
    slugify_topic,
)


def test_slugify_topic_variations() -> None:
    """Validate slugification for various topic strings."""
    assert slugify_topic("What is Quantum Computing?") == "what-is-quantum-computing"
    assert slugify_topic("AI & Robotics: What's next in 2026???") == "ai-robotics-whats-next-in-2026"
    assert slugify_topic("First line\nSecond line is ignored") == "first-line"
    assert slugify_topic("") == "research-topic"
    assert slugify_topic("   ") == "research-topic"
    # Long text truncation
    long_topic = "This is an extremely long research query that has many words and should be truncated properly"
    slug = slugify_topic(long_topic, max_words=4)
    assert slug == "this-is-an-extremely"


def test_archive_report_to_state_empty_or_missing() -> None:
    """Ensure graceful handling when no report exists or report is empty."""
    assert archive_report_to_state({}) == {}
    assert archive_report_to_state({"files": {}}) == {}
    assert archive_report_to_state({"files": {"/final_report.md": create_file_data("")}}) == {}
    assert archive_report_to_state({"files": {"/final_report.md": create_file_data("   \n  ")}}) == {}


def test_archive_single_topic_report() -> None:
    """Test archiving a single topic report into state files."""
    request_text = "What is Quantum Computing?"
    report_text = "# Quantum Computing Overview\n\nQuantum computing uses qubits."

    state = {
        "completion_current_run_id": "run-101",
        "files": {
            "/research_request.md": create_file_data(request_text),
            "/final_report.md": create_file_data(report_text),
        },
    }

    updates = archive_report_to_state(state)
    assert "files" in updates
    archived_files = updates["files"]

    expected_folder = "/reports/topic_001_what-is-quantum-computing"
    expected_report = f"{expected_folder}/final_report.md"
    expected_request = f"{expected_folder}/request.md"

    assert expected_report in archived_files
    assert expected_request in archived_files
    assert "/reports/manifest.json" in archived_files
    assert "/reports/README.md" in archived_files

    assert file_data_to_string(archived_files[expected_report]) == report_text
    assert file_data_to_string(archived_files[expected_request]) == request_text

    # Verify manifest JSON
    manifest = json.loads(file_data_to_string(archived_files["/reports/manifest.json"]))
    assert len(manifest) == 1
    record = manifest[0]
    assert record["index"] == 1
    assert record["topic"] == request_text
    assert record["slug"] == "what-is-quantum-computing"
    assert record["run_id"] == "run-101"
    assert record["report_path"] == expected_report
    assert record["request_path"] == expected_request
    assert record["word_count"] == len(report_text.split())

    # Verify README.md table
    readme = file_data_to_string(archived_files["/reports/README.md"])
    assert "Total Reports: 1" in readme
    assert "[what-is-quantum-computing]" in readme


def test_archive_multi_topic_preserves_all_reports() -> None:
    """Simulate a multi-turn conversation across 2 topics, verifying non-overwriting."""
    # Turn 1: Topic A
    req_a = "Research topic A: Quantum Computing"
    rep_a = "# Report A: Quantum Computing\n\nDetails on qubits and superposition."
    state = {
        "completion_current_run_id": "run-turn-1",
        "files": {
            "/research_request.md": create_file_data(req_a),
            "/final_report.md": create_file_data(rep_a),
        },
    }

    turn_1_archival = archive_report_to_state(state)
    state["files"].update(turn_1_archival["files"])

    assert len(get_archived_reports(state)) == 1
    report_a_path = "/reports/topic_001_research-topic-a-quantum-computing/final_report.md"
    assert report_a_path in state["files"]

    # Turn 2: Topic B (Overwrites /final_report.md and /research_request.md in canonical pointers)
    req_b = "Research topic B: CRISPR Gene Editing"
    rep_b = "# Report B: CRISPR Gene Editing\n\nDetails on Cas9 and genetic scissors."

    state["completion_current_run_id"] = "run-turn-2"
    state["files"]["/research_request.md"] = create_file_data(req_b)
    state["files"]["/final_report.md"] = create_file_data(rep_b)

    turn_2_archival = archive_report_to_state(state)
    state["files"].update(turn_2_archival["files"])

    # Verify BOTH reports are preserved!
    records = get_archived_reports(state)
    assert len(records) == 2

    assert records[0]["index"] == 1
    assert records[0]["topic"] == req_a
    assert records[1]["index"] == 2
    assert records[1]["topic"] == req_b

    report_b_path = "/reports/topic_002_research-topic-b-crispr-gene-editing/final_report.md"
    assert report_a_path in state["files"]
    assert report_b_path in state["files"]

    # Confirm content integrity
    assert file_data_to_string(state["files"][report_a_path]) == rep_a
    assert file_data_to_string(state["files"][report_b_path]) == rep_b

    # Confirm canonical /final_report.md still holds the latest report (Topic B)
    assert file_data_to_string(state["files"]["/final_report.md"]) == rep_b


def test_verification_revisions_do_not_duplicate_archive() -> None:
    """Verify that multiple passes on the same report fingerprint do not duplicate records."""
    req = "Topic: AI Safety"
    rep = "# AI Safety Report\n\nAlignment principles."
    state = {
        "completion_current_run_id": "run-verif",
        "files": {
            "/research_request.md": create_file_data(req),
            "/final_report.md": create_file_data(rep),
        },
    }

    first_archival = archive_report_to_state(state)
    assert first_archival != {}
    state["files"].update(first_archival["files"])

    # Calling again with identical report content
    second_archival = archive_report_to_state(state)
    assert second_archival == {}

    records = get_archived_reports(state)
    assert len(records) == 1
