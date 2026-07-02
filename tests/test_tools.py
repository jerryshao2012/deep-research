"""Tests for research agent tool functions."""

from pathlib import Path

from langchain_core.messages import HumanMessage

from agent import ResearchStateMiddleware
from research_agent import tools
from research_agent.tools import (
    fetch_webpage_content,
    read_docs_folder,
)


# ── read_docs_folder tests ──

def test_read_docs_folder_reads_text_and_markdown_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", str(tmp_path / "output"))
    (tmp_path / "notes.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "summary.md").write_text("# heading", encoding="utf-8")

    result = read_docs_folder.func(
        folder_path=str(tmp_path),
        state={"doc_folder": str(tmp_path)}
    )

    assert "Content of notes.txt" in result
    assert "alpha" in result
    assert "Content of summary.md" in result
    assert "# heading" in result


def test_read_docs_folder_reports_unsupported_and_empty_cases(tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"png")

    result = read_docs_folder.func(
        folder_path=str(tmp_path),
        state={"doc_folder": str(tmp_path)}
    )

    assert "No supported document files found" in result


# ── read_file tests ──

def test_read_file_impl_returns_structured_preview_for_large_markdown(tmp_path: Path) -> None:
    large_doc = tmp_path / "policy.md"
    repeated_section = (
        "## Liability Coverage\n"
        "This section explains liability coverage, claim handling, deductibles, and exclusions.\n\n"
    )
    large_doc.write_text(
        "# Ontario Automobile Policy\n\n"
        + repeated_section * 600,
        encoding="utf-8",
    )

    result = tools.read_file_impl(str(large_doc), state={})

    assert "returning a structured preview" in result
    assert "Heading outline" in result
    assert "## Liability Coverage" in result
    assert len(result) < 20000
    assert "Section chunks:" in result


def test_read_file_impl_can_target_specific_markdown_section(tmp_path: Path) -> None:
    policy_doc = tmp_path / "policy.md"
    policy_doc.write_text(
        "# Ontario Automobile Policy\n\n"
        "## Section 3 - Liability Coverage\n"
        "Liability coverage protects you when you are at fault.\n\n"
        "## Section 4 - Accident Benefits Coverage\n"
        "Accident benefits may be available regardless of fault.\n",
        encoding="utf-8",
    )

    result = tools.read_file_impl(
        f"{policy_doc}#Section 4 - Accident Benefits Coverage",
        state={},
    )

    assert "Section 4 - Accident Benefits Coverage" in result
    assert "Accident benefits may be available regardless of fault." in result
    assert "Liability coverage protects you when you are at fault." not in result


def test_read_file_impl_reports_unknown_markdown_section(tmp_path: Path) -> None:
    policy_doc = tmp_path / "policy.md"
    policy_doc.write_text(
        "# Ontario Automobile Policy\n\n"
        "## Section 3 - Liability Coverage\n"
        "Liability coverage protects you when you are at fault.\n",
        encoding="utf-8",
    )

    result = tools.read_file_impl(
        f"{policy_doc}#Section 9 - Missing Section",
        state={},
    )

    assert "Section 'Section 9 - Missing Section' not found" in result
    assert "Available sections:" in result


# ── middleware tests ──

def test_research_state_middleware_seeds_research_request_file() -> None:
    middleware = ResearchStateMiddleware()

    result = middleware.before_agent(
        state={"messages": [HumanMessage(content="Generate 5 Q/A pairs from ./docs/policy/")]},
        runtime=None,
    )

    assert result is not None
    assert "/research_request.md" in result["files"]
    assert "Generate 5 Q/A pairs" in "".join(result["files"]["/research_request.md"]["content"])


# ── ls / glob tests ──

def test_ls_lists_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "file1.txt").touch()
    (tmp_path / "dir1").mkdir()
    (tmp_path / "dir1" / "file2.txt").touch()

    result = tools.ls.invoke({"path": str(tmp_path), "state": {}})

    assert "file1.txt" in result
    assert "dir1/" in result
    assert "file2.txt" not in result


def test_ls_handles_nonexistent_path(tmp_path: Path) -> None:
    result = tools.ls.invoke({"path": str(tmp_path / "nonexistent"), "state": {}})
    assert "Error: Path" in result
    assert "not found" in result


def test_glob_finds_files_matching_pattern(tmp_path: Path) -> None:
    (tmp_path / "test1.md").touch()
    (tmp_path / "test2.md").touch()
    (tmp_path / "other.txt").touch()
    subdir = tmp_path / "sub"
    subdir.mkdir()
    (subdir / "test3.md").touch()

    result = tools.glob.invoke({"pattern": f"{tmp_path}/*.md", "state": {}})
    assert "test1.md" in result
    assert "test2.md" in result
    assert "other.txt" not in result
    assert "test3.md" not in result

    result = tools.glob.invoke({"pattern": f"{tmp_path}/**/*.md", "state": {}})
    assert "test1.md" in result
    assert "test2.md" in result
    assert "test3.md" in result


def test_glob_handles_nonexistent_base_path() -> None:
    result = tools.glob.invoke({"pattern": "/nonexistent/path/*.md", "state": {}})
    assert "Error: Base path" in result


# ── fetch_webpage_content tests ──

def test_fetch_webpage_content_returns_markdown_for_valid_url() -> None:
    result = fetch_webpage_content.invoke({"url": "https://example.com", "timeout": 5.0, "state": {}})

    assert not result.startswith("Error fetching content")
    assert len(result) > 0


def test_fetch_webpage_content_handles_invalid_url() -> None:
    result = fetch_webpage_content.invoke({"url": "https://this-domain-does-not-exist-12345.com", "timeout": 2.0, "state": {}})

    assert result.startswith("Error fetching content")


def test_fetch_webpage_content_has_proper_tool_metadata() -> None:
    assert hasattr(fetch_webpage_content, "name")
    assert fetch_webpage_content.name == "fetch_webpage_content"
    assert hasattr(fetch_webpage_content, "description")
    assert "markdown" in fetch_webpage_content.description.lower()
