"""Tests for fail-closed document tool eligibility."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import research_agent.document_context as document_context
from research_agent.document_context import (
    configure_document_tools,
    has_document_context,
    tool_name,
)
from thread_wiki.source_types import SUPPORTED_WIKI_SOURCE_SUFFIXES

EXPECTED_SOURCE_SUFFIXES = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".csv",
    ".pdf", ".docx", ".pptx", ".xlsx",
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx",
    ".ts", ".mts", ".cts", ".tsx", ".java", ".go", ".rs",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx",
    ".cs", ".rb", ".php",
}


@pytest.fixture(autouse=True)
def _isolate_document_context_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIKI_BASE_DIR", raising=False)
    monkeypatch.delenv("DOC_FOLDER", raising=False)
    monkeypatch.setattr(document_context, "MAX_GLOB_DEPTH", 3)


class _NamedTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _state(folder: Path, **extra: object) -> dict[str, object]:
    return {"doc_folder": str(folder), **extra}


def _wiki_raw(base: Path, thread_id: str) -> Path:
    raw = base / "docs" / "threads-wiki" / thread_id / "raw"
    raw.mkdir(parents=True)
    return raw


def test_explicit_false_overrides_real_uploaded_and_raw_sources(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = tmp_path / "thread-1"
    upload.mkdir()
    (upload / "notes.md").write_text("source", encoding="utf-8")
    (_wiki_raw(tmp_path, "thread-1") / "raw.txt").write_text("source", encoding="utf-8")
    monkeypatch.setenv("WIKI_BASE_DIR", str(tmp_path))

    assert not has_document_context(_state(upload, has_documents=False))


@pytest.mark.parametrize("has_documents", [True, None])
def test_true_or_absent_flag_requires_actual_physical_source(
        tmp_path: Path, has_documents: bool | None
) -> None:
    empty = tmp_path / "thread-1"
    empty.mkdir()
    state = _state(empty)
    if has_documents is not None:
        state["has_documents"] = has_documents

    assert not has_document_context(state)


def test_absent_flag_uses_cli_folder_source(tmp_path: Path) -> None:
    upload = tmp_path / "cli-thread"
    upload.mkdir()
    (upload / "notes.md").write_text("source", encoding="utf-8")

    assert has_document_context(_state(upload))


def test_exact_thread_wiki_raw_directory_counts_as_source(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = tmp_path / "thread-1"
    upload.mkdir()
    (_wiki_raw(tmp_path, "thread-1") / "source.pdf").write_bytes(b"pdf")
    monkeypatch.setenv("WIKI_BASE_DIR", str(tmp_path))

    assert has_document_context(_state(upload, has_documents=True))


def test_canonical_thread_folder_uses_project_root_wiki_fallback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = tmp_path / "project" / "docs" / "threads" / "thread-1"
    upload.mkdir(parents=True)
    (_wiki_raw(tmp_path / "project", "thread-1") / "source.txt").write_text(
        "source", encoding="utf-8"
    )
    monkeypatch.delenv("WIKI_BASE_DIR", raising=False)
    monkeypatch.delenv("DOC_FOLDER", raising=False)

    assert has_document_context(_state(upload, has_documents=True))


@pytest.mark.parametrize(
    "files",
    [
        {"/raw/upload.txt": object()},
        {"/docs/upload.txt": object()},
        {"/wiki/index.md": object()},
        {"/final_report.md": object(), "/research_request.md": object()},
    ],
)
def test_virtual_and_generated_graph_state_files_never_count(
        tmp_path: Path, files: dict[str, object]
) -> None:
    upload = tmp_path / "thread-1"
    upload.mkdir()

    assert not has_document_context(_state(upload, has_documents=True, files=files))


@pytest.mark.parametrize(
    "folder",
    ["", "   ", ".", "/", "../outside", "missing"],
)
def test_invalid_doc_folder_values_fail_closed(
        tmp_path: Path, folder: str
) -> None:
    assert not has_document_context({"doc_folder": folder, "has_documents": True})


def test_empty_and_unreadable_sources_fail_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not has_document_context(_state(empty, has_documents=True))

    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    source = unreadable / "notes.md"
    source.write_text("source", encoding="utf-8")
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError))
    assert not has_document_context(_state(unreadable, has_documents=True))


def test_too_deep_readable_source_fails_closed(tmp_path: Path) -> None:
    deep = tmp_path / "deep"
    candidate = deep
    for _ in range(8):
        candidate /= "nested"
    candidate.mkdir(parents=True)
    (candidate / "notes.md").write_text("source", encoding="utf-8")

    assert not has_document_context(_state(deep, has_documents=True))


def test_depth_boundary_accepts_limit_and_rejects_one_beyond(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(document_context, "MAX_GLOB_DEPTH", 2)
    within_limit = tmp_path / "within" / "nested"
    within_limit.mkdir(parents=True)
    (within_limit / "source.md").write_text("source", encoding="utf-8")
    beyond_limit = tmp_path / "beyond" / "one" / "nested"
    beyond_limit.mkdir(parents=True)
    (beyond_limit / "source.md").write_text("source", encoding="utf-8")

    assert has_document_context(_state(tmp_path / "within", has_documents=True))
    assert not has_document_context(_state(tmp_path / "beyond", has_documents=True))


def test_nested_symlink_source_outside_root_does_not_count(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.md").write_text("source", encoding="utf-8")
    (root / "nested").symlink_to(outside, target_is_directory=True)

    assert not has_document_context(_state(root, has_documents=True))


def test_scans_beyond_twenty_unrelated_files_before_valid_source(tmp_path: Path) -> None:
    upload = tmp_path / "thread-1"
    upload.mkdir()
    for index in range(25):
        (upload / f"ignore-{index:02}.png").write_bytes(b"not source")
    (upload / "valid.md").write_text("source", encoding="utf-8")

    assert has_document_context(_state(upload, has_documents=True))


def test_source_suffix_policy_is_exact_complete_union() -> None:
    assert SUPPORTED_WIKI_SOURCE_SUFFIXES == EXPECTED_SOURCE_SUFFIXES


@pytest.mark.parametrize("suffix", sorted(EXPECTED_SOURCE_SUFFIXES))
def test_every_expected_source_suffix_counts_as_physical_evidence(
        tmp_path: Path, suffix: str
) -> None:
    upload = tmp_path / f"thread-{suffix[1:]}"
    upload.mkdir()
    (upload / f"source{suffix}").write_text("source", encoding="utf-8")

    assert has_document_context(_state(upload, has_documents=True))


def test_document_context_import_does_not_load_code_ingestion() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import research_agent.document_context; "
            "assert 'thread_wiki.code_ingestion' not in sys.modules",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_tool_name_and_filtering_preserve_order_and_unknown_objects() -> None:
    tools = [
        {"name": "llm_wiki_query"},
        _NamedTool("web_search"),
        {"name": "read_docs_folder"},
        object(),
        _NamedTool("other"),
    ]

    assert tool_name(tools[0]) == "llm_wiki_query"
    assert tool_name(tools[1]) == "web_search"
    assert tool_name(tools[3]) is None
    assert configure_document_tools(tools, documents_available=False) == [
        tools[1], tools[3], tools[4]
    ]
    assert configure_document_tools(tools, documents_available=True) == tools
