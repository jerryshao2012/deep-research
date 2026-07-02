"""Tests for content extraction page markers and citation parsing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from research_agent.utils.content_extractors import _render_page_chunk
from thread_wiki.models import SourceCitation, WikiQueryResult
from thread_wiki.service import _extract_citations


# ── _render_page_chunk ─────────────────────────────────────────────────────


class TestRenderPageChunk:
    """Tests for the page chunk renderer helper."""

    def test_emits_page_comment_and_heading(self) -> None:
        result = _render_page_chunk(3, 3, "Some content here.")
        assert "<!-- page: 3 -->" in result
        assert "## Page 3" in result
        assert "Some content here." in result

    def test_strips_whitespace_from_body(self) -> None:
        result = _render_page_chunk(1, 1, "   padded content   ")
        assert "<!-- page: 1 -->" in result
        assert "padded content" in result


# ── _extract_pdf_text ───────────────────────────────────────────────────


@pytest.fixture()
def mock_extractors_modules(monkeypatch):
    """Mock pymupdf4llm and pypdf so _extract_pdf_text can be tested without
    the actual libraries installed.  Returns (mock_pymupdf, mock_pypdf)."""
    import sys

    mock_pymupdf = MagicMock()
    mock_pypdf = MagicMock()

    # Stash originals so we can restore them.
    orig_pymupdf = sys.modules.get("pymupdf4llm")
    orig_pypdf = sys.modules.get("pypdf")

    sys.modules["pymupdf4llm"] = mock_pymupdf
    sys.modules["pypdf"] = mock_pypdf

    # Reload the module under test so the lazy `import pymupdf4llm` inside
    # _extract_pdf_text picks up our mock.
    import importlib
    import research_agent.utils.content_extractors as mod
    importlib.reload(mod)

    yield mock_pymupdf, mock_pypdf, mod

    # Restore originals.
    if orig_pymupdf is None:
        sys.modules.pop("pymupdf4llm", None)
    else:
        sys.modules["pymupdf4llm"] = orig_pymupdf
    if orig_pypdf is None:
        sys.modules.pop("pypdf", None)
    else:
        sys.modules["pypdf"] = orig_pypdf
    importlib.reload(mod)


class TestExtractPdfTextPageChunks:
    """Tests for PDF extraction with page_chunks=True."""

    def test_page_chunks_list_with_metadata_page(self, mock_extractors_modules) -> None:
        """When pymupdf4llm returns page-chunk dicts with metadata.page,
        each chunk should get a <!-- page: N --> sentinel (enumeration index)
        and a ## Page N heading (actual PDF page number from metadata)."""
        mock_pymupdf, _, mod = mock_extractors_modules
        chunks = [
            {"metadata": {"page": 5, "total_page": 10}, "content": "Page five text."},
            {"metadata": {"page": 6, "total_page": 10}, "content": "Page six text."},
        ]
        mock_pymupdf.to_markdown.return_value = chunks
        result = mod._extract_pdf_text(Path("/tmp/test.pdf"))

        # Sentinel uses enumeration index (1-based), heading uses PDF page number
        assert "<!-- page: 1 -->" in result
        assert "<!-- page: 2 -->" in result
        assert "Page five text." in result
        assert "Page six text." in result
        assert "## Page 5" in result
        assert "## Page 6" in result

    def test_page_chunks_list_without_metadata(self, mock_extractors_modules) -> None:
        """When chunks are dicts but lack metadata.page, fall back to 1-based index."""
        mock_pymupdf, _, mod = mock_extractors_modules
        chunks = [
            {"content": "First chunk"},
            {"content": "Second chunk"},
        ]
        mock_pymupdf.to_markdown.return_value = chunks
        result = mod._extract_pdf_text(Path("/tmp/test.pdf"))

        assert "<!-- page: 1 -->" in result
        assert "<!-- page: 2 -->" in result

    def test_page_chunks_string_items(self, mock_extractors_modules) -> None:
        """When chunks are plain strings (not dicts), enumerate with index."""
        mock_pymupdf, _, mod = mock_extractors_modules
        chunks = ["First page text.", "Second page text."]
        mock_pymupdf.to_markdown.return_value = chunks
        result = mod._extract_pdf_text(Path("/tmp/test.pdf"))

        assert "<!-- page: 1 -->" in result
        assert "<!-- page: 2 -->" in result

    def test_flat_string_return(self, mock_extractors_modules) -> None:
        """When pymupdf4llm returns a flat string (older version),
        the content passes through without page markers."""
        mock_pymupdf, _, mod = mock_extractors_modules
        mock_pymupdf.to_markdown.return_value = "Flat markdown content\nwith no page markers."
        result = mod._extract_pdf_text(Path("/tmp/test.pdf"))

        assert "Flat markdown content" in result
        assert "<!-- page:" not in result  # no page markers from flat string

    def test_empty_result_returns_empty_string(self, mock_extractors_modules) -> None:
        """When pymupdf4llm returns empty string, the result is empty
        (pypdf fallback is only triggered on exception)."""
        mock_pymupdf, _, mod = mock_extractors_modules
        mock_pymupdf.to_markdown.return_value = ""
        result = mod._extract_pdf_text(Path("/tmp/test.pdf"))
        assert result == ""

    def test_exception_falls_through_to_pypdf(self, mock_extractors_modules) -> None:
        """When pymupdf4llm raises, fall through to pypdf fallback."""
        mock_pymupdf, mock_pypdf, mod = mock_extractors_modules
        mock_pymupdf.to_markdown.side_effect = RuntimeError("boom")
        mock_reader = MagicMock()
        mock_page1 = MagicMock()
        mock_page1.extract_text.return_value = "Fallback text"
        mock_reader.pages = [mock_page1]
        mock_pypdf.PdfReader.return_value = mock_reader
        result = mod._extract_pdf_text(Path("/tmp/test.pdf"))

        assert "<!-- page: 1 -->" in result
        assert "Fallback text" in result


# ── _extract_citations ───────────────────────────────────────────────────


class TestExtractCitationsRaw:
    """Tests for raw document citation parsing."""

    def test_raw_path_with_page(self) -> None:
        answer = "Revenue was $42B (Source: /raw/report.pdf.md, p. 12)."
        citations = _extract_citations(answer)
        assert len(citations) >= 1
        raw_cits = [c for c in citations if c.kind == "raw"]
        assert len(raw_cits) >= 1
        cit = raw_cits[0]
        assert "/raw/report.pdf.md" in cit.raw_path
        assert cit.page == 12

    def test_raw_path_without_page(self) -> None:
        answer = "Based on (Source: /raw/summary.md)."
        citations = _extract_citations(answer)
        raw_cits = [c for c in citations if c.kind == "raw"]
        assert len(raw_cits) >= 1
        cit = raw_cits[0]
        assert "/raw/summary.md" in cit.raw_path
        assert cit.page is None

    def test_raw_path_page_variant_formats(self) -> None:
        """Various page annotation formats should all parse."""
        for fmt in [
            "/raw/x.pdf.md, p. 3",
            "/raw/x.pdf.md page 4",
            "/raw/x.pdf.md p5",
        ]:
            answer = f"Claim ({fmt})."
            citations = _extract_citations(answer)
            raw_cits = [c for c in citations if c.kind == "raw"]
            assert any(c.page is not None for c in raw_cits), f"Failed for format: {fmt}"

    def test_dedup_same_raw_path(self) -> None:
        answer = "Claim A (Source: /raw/doc.pdf.md, p. 5). Claim B (Source: /raw/doc.pdf.md, p. 5)."
        citations = _extract_citations(answer)
        raw_cits = [c for c in citations if c.kind == "raw" and c.raw_path == "/raw/doc.pdf.md" and c.page == 5]
        assert len(raw_cits) == 1

    def test_multiple_different_raw_paths(self) -> None:
        answer = "From /raw/a.pdf.md, p. 1 and /raw/b.docx.md, p. 3."
        citations = _extract_citations(answer)
        raw_cits = [c for c in citations if c.kind == "raw"]
        paths = {c.raw_path for c in raw_cits}
        assert len(paths) >= 2

    def test_old_style_citation_still_captured(self) -> None:
        """Old-style bare raw path without page should still work."""
        answer = "(Source: /raw/legacy.pdf.md)"
        citations = _extract_citations(answer)
        raw_cits = [c for c in citations if c.kind == "raw"]
        assert len(raw_cits) >= 1
        assert raw_cits[0].page is None

    def test_pptx_slide_citation(self) -> None:
        answer = "Slide info (Source: /raw/deck.pptx.md, Slide 4)."
        citations = _extract_citations(answer)
        raw_cits = [c for c in citations if c.kind == "raw" and c.raw_path == "/raw/deck.pptx.md"]
        assert len(raw_cits) == 1
        assert raw_cits[0].locator == "Slide 4"
        assert raw_cits[0].page is None

    def test_xlsx_sheet_row_citation(self) -> None:
        answer = "Row info (Source: /raw/data.xlsx.txt, Sheet: Cash, row 42)."
        citations = _extract_citations(answer)
        raw_cits = [c for c in citations if c.kind == "raw" and c.raw_path == "/raw/data.xlsx.txt"]
        assert len(raw_cits) == 1
        assert raw_cits[0].locator == "Sheet: Cash, row 42"
        assert raw_cits[0].page is None


class TestExtractCitationsWeb:
    """Tests for web URL citation parsing."""

    def test_sources_block_with_numbered_urls(self) -> None:
        answer = (
            "Some finding [1]. Another [2].\n\n"
            "### Sources\n"
            "[1] AI Research: https://example.com/ai\n"
            "[2] Industry Report: https://example.com/industry\n"
        )
        citations = _extract_citations(answer)
        web_cits = [c for c in citations if c.kind == "web"]
        assert len(web_cits) >= 2
        urls = {c.url for c in web_cits}
        assert "https://example.com/ai" in urls
        assert "https://example.com/industry" in urls

    def test_sources_block_title_captured_as_locator(self) -> None:
        answer = (
            "Finding [1].\n\n"
            "### Sources\n"
            "[1] Annual Report 2025: https://example.com/ar2025\n"
        )
        citations = _extract_citations(answer)
        web_cits = [c for c in citations if c.kind == "web"]
        assert any(c.locator == "Annual Report 2025" for c in web_cits)

    def test_bare_url_without_sources_block(self) -> None:
        """Bare URLs in text should be captured as web citations."""
        answer = "See https://example.com/data for more details."
        citations = _extract_citations(answer)
        web_cits = [c for c in citations if c.kind == "web"]
        assert len(web_cits) >= 1
        assert any(c.url == "https://example.com/data" for c in web_cits)

    def test_bare_url_not_deduped_with_sources_block(self) -> None:
        """URL in Sources block should not be duplicated as bare URL."""
        answer = (
            "See https://example.com/data.\n\n"
            "### Sources\n"
            "[1] Data Page: https://example.com/data\n"
        )
        citations = _extract_citations(answer)
        web_cits = [c for c in citations if c.kind == "web" and c.url == "https://example.com/data"]
        assert len(web_cits) == 1


class TestExtractCitationsSection:
    """Tests for file.md#Heading section reference parsing."""

    def test_section_ref(self) -> None:
        answer = "See policies.md#Risk-Factors for details."
        citations = _extract_citations(answer)
        section_cits = [c for c in citations if c.kind == "section"]
        assert len(section_cits) >= 1
        assert section_cits[0].raw_path == "policies.md"
        assert section_cits[0].locator == "Risk-Factors"

    def test_raw_section_ref_not_captured_as_section(self) -> None:
        """Section refs to /raw/ files should be skipped (they're raw citations)."""
        answer = "See /raw/report.pdf.md#Page 3 for the data."
        citations = _extract_citations(answer)
        section_cits = [c for c in citations if c.kind == "section"]
        assert len(section_cits) == 0


class TestExtractCitationsMixed:
    """Tests with mixed citation styles."""

    def test_mixed_raw_and_web(self) -> None:
        answer = (
            "BMO earned $42B (Source: /raw/bmo_ar2025.pdf.md, p. 15) [1].\n\n"
            "### Sources\n"
            "[1] BMO Financial Group: https://example.com/bmo\n"
        )
        citations = _extract_citations(answer)
        kinds = {c.kind for c in citations}
        assert "raw" in kinds
        assert "web" in kinds

    def test_empty_answer(self) -> None:
        citations = _extract_citations("")
        assert citations == []

    def test_answer_with_no_citations(self) -> None:
        citations = _extract_citations("This answer has no citations at all.")
        assert citations == []


# ── SourceCitation dataclass ───────────────────────────────────────────────


class TestSourceCitationModel:
    """Tests for the SourceCitation dataclass."""

    def test_raw_citation(self) -> None:
        cit = SourceCitation(kind="raw", raw_path="/raw/doc.pdf.md", page=5)
        assert cit.kind == "raw"
        assert cit.page == 5
        assert cit.url is None
        assert cit.locator is None

    def test_web_citation(self) -> None:
        cit = SourceCitation(kind="web", url="https://example.com", locator="Example Site")
        assert cit.kind == "web"
        assert cit.url == "https://example.com"

    def test_frozen(self) -> None:
        cit = SourceCitation(kind="raw", raw_path="/raw/x.md")
        with pytest.raises(AttributeError):
            cit.kind = "web"  # type: ignore[misc]


# ── WikiQueryResult compatibility ─────────────────────────────────────────


class TestWikiQueryResult:
    """Tests for updated WikiQueryResult model."""

    def test_default_empty_citations(self) -> None:
        result = WikiQueryResult(answer="test")
        assert result.sources_cited == []

    def test_with_citations(self) -> None:
        cit = SourceCitation(kind="raw", raw_path="/raw/doc.pdf.md", page=10)
        result = WikiQueryResult(answer="test", sources_cited=[cit])
        assert len(result.sources_cited) == 1
        assert result.sources_cited[0].page == 10


# ── Turn-Aware Report Naming Tests ─────────────────────────────────────────

class TestTurnAwareReportNaming:
    """Tests for turn-aware report naming and dynamic path resolution."""

    def test_normalize_citations_for_comparison(self) -> None:
        from research_agent.utils.knowledge_filesystem import normalize_citations_for_comparison

        t1 = "This is a report with citation (/raw/bmo_ar2025.pdf.md, p. 3)."
        t2 = "This is a report with citation (/bmo_ar2025.pdf, p. 3)."
        assert normalize_citations_for_comparison(t1) == normalize_citations_for_comparison(t2)

        t3 = "Another one: /raw/some_deck.pptx.md."
        t4 = "Another one: /some_deck.pptx."
        assert normalize_citations_for_comparison(t3) == normalize_citations_for_comparison(t4)

    def test_get_target_report_path_empty_state(self) -> None:
        from research_agent.utils.knowledge_filesystem import get_target_cited_response_path

        content = "Some report content"
        # If no reports exist at the start of the turn, return /cited_response.md
        assert get_target_cited_response_path(content, {}, []) == "/cited_response.md"

    def test_get_target_report_path_inplace_update(self) -> None:
        from research_agent.utils.knowledge_filesystem import get_target_cited_response_path
        from deepagents.backends.utils import create_file_data

        content = "Some report content with citation (/raw/bmo_ar2025.pdf.md, p. 3)"
        sanitized_content = "Some report content with citation (/bmo_ar2025.pdf, p. 3)"

        state_files = {
            "/cited_response.md": create_file_data(content)
        }
        existing_reports = ["/cited_response.md"]

        # Since they are equivalent (only citation differences), return /cited_response.md (in-place sanitization)
        assert get_target_cited_response_path(sanitized_content, state_files, existing_reports) == "/cited_response.md"

    def test_get_target_report_path_new_report(self) -> None:
        from research_agent.utils.knowledge_filesystem import get_target_cited_response_path
        from deepagents.backends.utils import create_file_data

        old_content = "Report 1 content"
        new_content = "Report 2 content"

        state_files = {
            "/cited_response.md": create_file_data(old_content),
            "/cited_response_1.md": create_file_data("some other report")
        }
        existing_reports = ["/cited_response.md", "/cited_response_1.md"]

        # Since it's new content and both exist at start, allocate /cited_response_2.md
        assert get_target_cited_response_path(new_content, state_files, existing_reports) == "/cited_response_2.md"

        # Test max suffix increment with a gap (e.g. /cited_response.md and /cited_response_2.md exist, next should be /cited_response_3.md)
        state_files_gap = {
            "/cited_response.md": create_file_data("a"),
            "/cited_response_2.md": create_file_data("b")
        }
        existing_reports_gap = ["/cited_response.md", "/cited_response_2.md"]
        assert get_target_cited_response_path("new content", state_files_gap,
                                              existing_reports_gap) == "/cited_response_3.md"

    def test_get_target_report_path_reuse_turn_path(self) -> None:
        from research_agent.utils.knowledge_filesystem import get_target_cited_response_path
        from deepagents.backends.utils import create_file_data

        old_content = "Report 1 content"
        new_content_1 = "New Report first draft"
        new_content_2 = "New Report final draft"

        state_files = {
            "/cited_response.md": create_file_data(old_content),
            "/cited_response_1.md": create_file_data(new_content_1)  # created this turn
        }
        existing_reports = ["/cited_response.md"]

        # Since /cited_response_1.md was created in this turn (not in existing_reports),
        # multiple writes in the same turn should reuse it even if content differs
        assert get_target_cited_response_path(new_content_2, state_files, existing_reports) == "/cited_response_1.md"

    def test_get_active_report_path(self) -> None:
        from research_agent.utils.knowledge_filesystem import get_active_cited_response_path
        from deepagents.backends.utils import create_file_data

        state_files = {
            "/cited_response.md": create_file_data("1"),
            "/cited_response_1.md": create_file_data("2"),
            "/cited_response_2.md": create_file_data("3"),  # created this turn
        }
        existing_reports = ["/cited_response.md", "/cited_response_1.md"]

        # Returns the newly created file in this turn
        assert get_active_cited_response_path(state_files, existing_reports) == "/cited_response_2.md"

        # If no new file created this turn, returns the highest index
        assert get_active_cited_response_path(state_files, ["/cited_response.md", "/cited_response_1.md",
                                                            "/cited_response_2.md"]) == "/cited_response_2.md"

    def test_after_model_wiki_query_complete_fallback(self) -> None:
        from agent import ResearchStateMiddleware
        from research_agent.utils.knowledge_filesystem import _thread_wiki_query_complete
        from langchain_core.messages import AIMessage

        middleware = ResearchStateMiddleware()

        # When wiki_query_complete is False and no chat_start_time is set,
        # after_model returns an empty updates dict (no eval tracking to run).
        state = {
            "messages": [AIMessage(content="This is the final response.")],
            "files": {},
            "existing_reports": []
        }
        runtime = {"configurable": {"thread_id": "thread-abc"}}
        _thread_wiki_query_complete["thread-abc"] = False

        updates = middleware.after_model(state, runtime)
        # after_model returns {} (empty dict) when wiki is not complete and
        # there is nothing to persist (no chat_start_time, no eval tracking).
        # The empty dict is falsy so it is returned as None.
        assert updates is None

        # When wiki_query_complete is True, the wiki-complete guard activates
        # only if the model emitted tool calls.  Without tool calls in the
        # last message the guard does not inject a jump_to.
        _thread_wiki_query_complete["thread-abc"] = True
        updates = middleware.after_model(state, runtime)
        # No tool calls → guard not triggered → still no meaningful updates
        assert updates is None

    def test_before_agent_wiki_query_complete_registration(self) -> None:
        from agent import ResearchStateMiddleware
        from research_agent.utils.knowledge_filesystem import _thread_wiki_query_complete
        from langchain_core.messages import HumanMessage

        middleware = ResearchStateMiddleware()
        state = {
            "messages": [HumanMessage(content="test query")],
            "files": {},
            "existing_reports": []
        }
        runtime = {"configurable": {"thread_id": "thread-def"}}

        import unittest.mock as mock
        with mock.patch.object(middleware, "_get_wiki_context_sync", return_value=(None, None)):
            middleware.before_agent(state, runtime)

        # By default, wiki_query_complete is set to False in updates if no wiki context matches
        assert _thread_wiki_query_complete.get("thread-def") is False

    def test_after_model_wiki_complete_strips_write_todos(self) -> None:
        """When wiki_query_complete=True, ALL tool calls (including read_file) must be stripped
        and the wiki answer text must be injected as the final AIMessage to prevent the
        infinite loop described in the bug report."""
        from agent import ResearchStateMiddleware
        from deepagents.backends.utils import create_file_data
        from langchain_core.messages import AIMessage

        middleware = ResearchStateMiddleware()

        # Simulate the model issuing write_todos + write_file while wiki is complete
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "write_todos", "args": {"todos": []}, "id": "tc1"},
                {"name": "write_file", "args": {"file_path": "/cited_response.md", "content": "x"}, "id": "tc2"},
            ],
        )
        wiki_answer = "This is the complete wiki answer."
        state = {
            "messages": [ai_msg],
            "files": {"/cited_response.md": create_file_data(wiki_answer)},
            "wiki_query_complete": True,
            "_wiki_answer_text": wiki_answer,
        }
        runtime = {"configurable": {"thread_id": "thread-wiki-loop"}}

        updates = middleware.after_model(state, runtime)
        # The wiki guard should have injected a final AIMessage with the wiki answer text
        assert updates is not None
        assert "messages" in updates
        override_msgs = updates["messages"]
        assert len(override_msgs) == 1
        final_msg = override_msgs[0]
        # No tool calls should remain (agent will stop)
        final_tool_calls = getattr(final_msg, "tool_calls", None) or []
        assert final_tool_calls == []
        # The wiki answer text must be the message content
        assert final_msg.content == wiki_answer
        # jump_to="end" must be set so the framework routing exits immediately
        assert updates.get("jump_to") == "end"

    def test_after_model_has_can_jump_to_end(self) -> None:
        """after_model must declare can_jump_to=['end'] so the framework
        registers a conditional edge that honors jump_to (otherwise a static
        edge is created and jump_to is silently ignored)."""
        from agent import ResearchStateMiddleware

        can_jump = getattr(ResearchStateMiddleware.after_model, "__can_jump_to__", None)
        assert can_jump == ["end"]

    def test_before_model_wiki_complete_fast_exit(self) -> None:
        """When wiki_query_complete=True, before_model must jump straight to
        END without running the model, so the model never gets a chance to call
        read_doc_folder (the root cause of the infinite loop)."""
        from agent import ResearchStateMiddleware
        from deepagents.backends.utils import create_file_data
        from langchain_core.messages import HumanMessage

        middleware = ResearchStateMiddleware()
        wiki_answer = "The definitive wiki answer."
        state = {
            "messages": [HumanMessage(content="what is in the docs?")],
            "files": {"/cited_response.md": create_file_data(wiki_answer)},
            "wiki_query_complete": True,
            "_wiki_answer_text": wiki_answer,
        }
        runtime = {"configurable": {"thread_id": "thread-fast-exit"}}

        updates = middleware.before_model(state, runtime)
        assert updates is not None
        # Must signal the framework to terminate the loop immediately
        assert updates.get("jump_to") == "end"
        # Must inject the terminal AIMessage so the chat reply is correct
        assert "messages" in updates
        final_msg = updates["messages"][0]
        assert final_msg.content == wiki_answer
        assert (getattr(final_msg, "tool_calls", None) or []) == []

    def test_before_model_has_can_jump_to_end(self) -> None:
        """before_model must declare can_jump_to=['end'] so the framework
        registers a conditional edge that honors jump_to."""
        from agent import ResearchStateMiddleware

        can_jump = getattr(ResearchStateMiddleware.before_model, "__can_jump_to__", None)
        assert can_jump == ["end"]

    def test_before_model_no_wiki_runs_normally(self) -> None:
        """When wiki_query_complete is not True, before_model must behave as
        before (initialize chat_start_time) and NOT jump to end."""
        from agent import ResearchStateMiddleware
        from langchain_core.messages import HumanMessage

        middleware = ResearchStateMiddleware()
        state = {
            "messages": [HumanMessage(content="research topic X")],
            "files": {},
            "wiki_query_complete": False,
        }
        runtime = {"configurable": {"thread_id": "thread-normal"}}

        updates = middleware.before_model(state, runtime)
        assert updates is not None
        assert "jump_to" not in updates
        assert "chat_start_time" in updates

    def test_build_system_instruction_wiki_complete_fastpath(self) -> None:
        """When wiki_query_complete=True, _build_system_instruction must include
        the WikiCompleteAnswer block telling the agent to skip the workflow."""
        from agent import ResearchStateMiddleware
        from deepagents.backends.utils import create_file_data

        state = {
            "files": {"/cited_response.md": create_file_data("wiki answer")},
            "wiki_query_complete": True,
            "no_web": True,
        }
        instruction = ResearchStateMiddleware._build_system_instruction(state)
        assert "<WikiCompleteAnswer>" in instruction
        assert "read_file" in instruction
        assert "/cited_response.md" in instruction
        assert "Do NOT call `write_todos`" in instruction
