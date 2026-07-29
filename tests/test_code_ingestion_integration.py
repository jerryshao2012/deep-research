from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from thread_wiki.models import IngestProgress, ThreadWikiPaths
from thread_wiki.progress import load_progress, save_progress
from thread_wiki.routes import SourceCitationOut, WikiStatusResponse
from thread_wiki.service import (
    _build_code_processing_instructions,
    _extract_citations,
    _stage_sources,
    _total_raw_size,
    run_ingest,
)


def test_staging_preserves_repository_paths_and_code_sources(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    source = docs_dir / "repositories" / "example" / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    raw_dir = tmp_path / "raw"

    staged = _stage_sources([source], raw_dir, source_root=docs_dir)

    expected = raw_dir / "repositories" / "example" / "src" / "app.py"
    assert staged == [expected]
    assert expected.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_raw_size_excludes_derived_code_artifacts(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "app.py").write_text("12345", encoding="utf-8")
    code_dir = raw_dir / "_code"
    code_dir.mkdir()
    (code_dir / "repository-index.md").write_text("x" * 1000, encoding="utf-8")

    assert _total_raw_size(raw_dir) == 5


def test_code_processing_instructions_prioritize_index_and_original_citations() -> None:
    instructions = _build_code_processing_instructions(
        [
            "_code/0002-app-run.md",
            "_code/0001-app-module.md",
        ]
    )

    assert instructions.index("/raw/_code/repository-index.md") < instructions.index(
        "/raw/_code/0001-app-module.md"
    )
    assert "source order" in instructions
    assert "never cite `/raw/_code/`" in instructions
    assert "cite original `/raw/` code files with line ranges" in instructions


def test_progress_snapshot_preserves_code_analysis(tmp_path: Path) -> None:
    summary = {
        "detected_files": 2,
        "parsed_files": 1,
        "partially_parsed_files": 1,
        "fallback_files": 0,
        "symbol_count": 4,
        "internal_import_count": 1,
        "warnings": [],
    }
    progress = IngestProgress(thread_id="thread-1", code_analysis=summary)

    asyncio.run(save_progress(progress, tmp_path))
    loaded = asyncio.run(load_progress(tmp_path))

    assert loaded is not None
    assert loaded.code_analysis == summary
    assert loaded.to_dict()["code_analysis"] == summary


def test_status_and_citation_contracts_are_additive() -> None:
    status = WikiStatusResponse(
        thread_id="thread-1",
        phase="ready",
        progress=100,
        detail="ready",
        source_count=1,
        sources_processed=1,
        error=None,
        started_at=None,
        completed_at=None,
        is_active=False,
        wiki_ready=True,
    )
    citation = SourceCitationOut(raw_path="/raw/app.py")

    assert status.code_analysis is None
    assert citation.line_start is None
    assert citation.line_end is None


def test_code_line_citations_validate_against_original_source(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    source = raw_dir / "pkg" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("\n".join(f"line {index}" for index in range(1, 11)), encoding="utf-8")

    [citation] = _extract_citations(
        "See (/raw/pkg/app.py, lines 3-7).",
        raw_dir=raw_dir,
    )
    assert citation.raw_path == "/raw/pkg/app.py"
    assert citation.line_start == 3
    assert citation.line_end == 7

    [invalid] = _extract_citations(
        "See (/raw/pkg/app.py, lines 30-40).",
        raw_dir=raw_dir,
    )
    assert invalid.line_start is None
    assert invalid.line_end is None


def test_derived_code_citation_maps_back_to_original(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    source = raw_dir / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    artifact = raw_dir / "_code" / "0001-app.md"
    artifact.parent.mkdir()
    artifact.write_text(
        "# run\n\n"
        "- Original source: `/raw/app.py`\n"
        "- Lines: 1-3\n",
        encoding="utf-8",
    )

    [citation] = _extract_citations(
        "See /raw/_code/0001-app.md.",
        raw_dir=raw_dir,
    )

    assert citation.raw_path == "/raw/app.py"
    assert citation.line_start == 1
    assert citation.line_end == 3


def test_mixed_ingest_uses_ast_chunks_without_changing_document_path(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs_dir = tmp_path / "docs"
    (docs_dir / "src").mkdir(parents=True)
    (docs_dir / "src" / "app.py").write_text(
        "def run():\n    return 1\n",
        encoding="utf-8",
    )
    (docs_dir / "notes.md").write_text("# Notes\n\nOrdinary document.\n", encoding="utf-8")
    wiki_dir = tmp_path / "wiki-root"
    paths = ThreadWikiPaths(
        thread_id="thread-1",
        docs_dir=docs_dir,
        wiki_dir=wiki_dir,
        raw_dir=wiki_dir / "raw",
        wiki_content=wiki_dir / "wiki",
    )
    prompts: list[str] = []

    def fake_run_agent(
            _wiki_dir: Path,
            prompt: str,
            *,
            read_only: bool,
            **_: object,
    ) -> str:
        prompts.append(prompt)
        return "## Approved plan" if read_only else "Applied"

    monkeypatch.setattr("thread_wiki.service._run_agent", fake_run_agent)
    progress = IngestProgress(thread_id="thread-1")

    result = asyncio.run(
        run_ingest(
            paths,
            "Mixed sources",
            progress,
            asyncio.Event(),
        )
    )

    assert result == "Applied"
    assert progress.code_analysis is not None
    assert progress.code_analysis["detected_files"] == 1
    assert (paths.raw_dir / "src" / "app.py").is_file()
    assert (paths.raw_dir / "notes.md").is_file()
    assert (wiki_dir / ".code_ingest_manifest.json").is_file()
    review_prompt = prompts[0]
    assert "/raw/notes.md" in review_prompt
    assert review_prompt.index("/raw/_code/repository-index.md") < review_prompt.index(
        "/raw/_code/0001-"
    )


def test_document_with_fenced_code_keeps_document_and_adds_semantic_chunk(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text(
        "# Guide\n\n"
        "Explanation remains document content.\n\n"
        "```python\n"
        "def embedded():\n"
        "    return 1\n"
        "```\n",
        encoding="utf-8",
    )
    wiki_dir = tmp_path / "wiki-root"
    paths = ThreadWikiPaths(
        thread_id="thread-embedded",
        docs_dir=docs_dir,
        wiki_dir=wiki_dir,
        raw_dir=wiki_dir / "raw",
        wiki_content=wiki_dir / "wiki",
    )
    prompts: list[str] = []

    def fake_run_agent(
            _wiki_dir: Path,
            prompt: str,
            *,
            read_only: bool,
            **_: object,
    ) -> str:
        prompts.append(prompt)
        return "Plan" if read_only else "Applied"

    monkeypatch.setattr("thread_wiki.service._run_agent", fake_run_agent)
    progress = IngestProgress(thread_id="thread-embedded")

    asyncio.run(
        run_ingest(paths, "Embedded code", progress, asyncio.Event())
    )

    assert progress.code_analysis is not None
    assert progress.code_analysis["detected_files"] == 0
    assert progress.code_analysis["embedded_blocks"] == 1
    assert "/raw/guide.md" in prompts[0]
    assert "/raw/_code/0001-" in prompts[0]
