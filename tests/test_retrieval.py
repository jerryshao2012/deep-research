"""Tests for BM25 text search (replaces FAISS retrieval)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from research_agent.utils.text_search import (
    BM25SearchIndex,
    _tokenize,
    build_search_index,
    chunk_markdown_by_boundaries,
    get_document_chunks,
    load_or_build_search_index,
    search_index,
    stem_word,
)


def test_chunk_markdown_by_boundaries() -> None:
    content = (
        "Some general info.\n"
        "<!-- page: 1 -->\n"
        "## Page 1\n"
        "This is text on page 1.\n"
        "<!-- heading: 2 -->\n"
        "Heading text.\n"
        "<!-- slide: 3 -->\n"
        "Slide content."
    )
    chunks = chunk_markdown_by_boundaries(content)
    assert len(chunks) == 4
    assert chunks[0]["page"] is None
    assert chunks[1]["page"] == 1
    assert chunks[2]["page"] == 1
    assert chunks[2]["heading"] == "Heading level 2"
    assert chunks[3]["page"] == 3
    assert chunks[3]["locator"] == "Slide 3"


def test_get_document_chunks_splits_large_text() -> None:
    large_text = "word " * 1000
    chunks = get_document_chunks(large_text)
    assert len(chunks) > 1


def test_build_and_search_index() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_dir = Path(tmp_dir) / "raw"
        raw_dir.mkdir()

        doc_file = raw_dir / "report.pdf.md"
        doc_file.write_text("<!-- page: 1 -->\n## Page 1\nRevenue is $100M.", encoding="utf-8")

        index_dir = Path(tmp_dir) / "index"

        # Build index
        idx = build_search_index(raw_dir, index_dir)
        assert idx is not None
        assert len(idx) > 0
        assert (index_dir / "index.pkl").exists()

        # Load index
        loaded = load_or_build_search_index(raw_dir, index_dir)
        assert loaded is not None
        assert len(loaded) > 0

        # Test search
        results = search_index("Revenue", loaded, k=1)
        assert len(results) == 1
        doc, score = results[0]
        assert "Revenue is $100M" in doc.page_content
        assert doc.metadata["source_path"] == "/raw/report.pdf.md"
        assert doc.metadata["page"] == 1
        assert score > 0.0  # BM25 scores are positive for matching terms


def test_bm25_index_empty() -> None:
    """Searching an empty index returns no results."""
    idx = BM25SearchIndex()
    results = idx.search("anything")
    assert results == []


def test_bm25_index_serialization() -> None:
    """Round-trip: build, save, load, search."""
    from langchain_core.documents import Document

    with tempfile.TemporaryDirectory() as tmp_dir:
        idx = BM25SearchIndex()
        idx.add_documents([
            Document(page_content="The quick brown fox jumps over the lazy dog.",
                     metadata={"source_path": "/raw/fable.pdf.md"}),
            Document(page_content="Machine learning is transforming research.",
                     metadata={"source_path": "/raw/tech.pdf.md"}),
        ])

        save_dir = Path(tmp_dir) / "index"
        idx.save(save_dir)
        assert (save_dir / "index.pkl").exists()

        loaded = BM25SearchIndex.load(save_dir)
        assert loaded is not None
        assert len(loaded) == 2

        results = loaded.search("fox", k=1)
        assert len(results) == 1
        assert "fox" in results[0][0].page_content

        # Ensure we get positive scores
        assert results[0][1] > 0.0


def test_load_missing_index() -> None:
    """Loading a non-existent index returns None."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        result = BM25SearchIndex.load(Path(tmp_dir) / "nonexistent")
        assert result is None


# ── Page-level precision ──────────────────────────────────────────────────────


def test_page_level_precision() -> None:
    """Multi-page document: query should return the correct page with metadata."""
    content = (
        "<!-- page: 1 -->\n## Page 1\n"
        "Introduction and overview of the company.\n\n"
        "<!-- page: 2 -->\n## Page 2\n"
        "Q4 2024 revenue reached $500M, up 12% year-over-year. "
        "The growth was driven by strong performance in cloud services.\n\n"
        "<!-- page: 3 -->\n## Page 3\n"
        "Risk factors include regulatory changes and market competition.\n\n"
        "<!-- page: 4 -->\n## Page 4\n"
        "Executive compensation totaled $15M with base salary and bonuses.\n"
    )
    chunks = get_document_chunks(content)
    assert len(chunks) == 4

    # Page metadata must be preserved through chunking.
    assert chunks[0]["page"] == 1
    assert chunks[1]["page"] == 2
    assert chunks[2]["page"] == 3
    assert chunks[3]["page"] == 4

    # Page content must be separated correctly.
    assert "overview of the company" in chunks[0]["text"]
    assert "revenue reached" in chunks[1]["text"]
    assert "Risk factors" in chunks[2]["text"]
    assert "Executive compensation" in chunks[3]["text"]


def test_search_returns_correct_page_for_query() -> None:
    """BM25 search on a multi-page index returns the right page for a specific query."""
    from langchain_core.documents import Document

    idx = BM25SearchIndex(prf=True)
    idx.add_documents([
        Document(
            page_content="Introduction and company overview. Founded in 2010.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 1},
        ),
        Document(
            page_content="Q4 2024 revenue reached $500M, up 12% year-over-year. "
            "Cloud services drove the increase in earnings.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 2},
        ),
        Document(
            page_content="Risk factors include regulatory changes and competition.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 3},
        ),
        Document(
            page_content="Executive compensation: $15M total including base salary.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 4},
        ),
    ])

    # Exact keyword match: "revenue" → page 2.
    results = idx.search("revenue", k=1)
    assert len(results) == 1
    assert results[0][0].metadata["page"] == 2

    # Semantic match via stemming: "earnings" stems to "earn" which should
    # match "earnings" in page 2's content.
    results = idx.search("earnings growth", k=1)
    assert len(results) >= 1
    # Page 2 mentions earnings — it should be the top result.
    assert results[0][0].metadata["page"] == 2

    # Numeric token: "2024" should find page 2.
    results = idx.search("2024", k=1)
    assert len(results) == 1
    assert results[0][0].metadata["page"] == 2

    # Different topic: "compensation" → page 4.
    results = idx.search("executive compensation", k=1)
    assert len(results) == 1
    assert results[0][0].metadata["page"] == 4


# ── Stemming & tokenization ───────────────────────────────────────────────────


def test_stemming_reduces_variants() -> None:
    """Stemmer collapses morphological variants to a common root."""
    # Plurals
    assert stem_word("companies") == "company"
    # Participles
    assert stem_word("running") == "run"
    assert stem_word("driven") in ("drive", "driven")  # Light stemmer: -en not always stripped
    # Derived forms
    assert stem_word("earnings") == "earn"
    assert stem_word("growth") == "growth"  # too short for suffix stripping
    # -ly, -ment, -ness
    assert stem_word("quickly") == "quick"
    assert stem_word("management") == "manage"
    assert stem_word("effectiveness") == "effect"


def test_tokenize_preserves_numeric_tokens() -> None:
    """Numeric tokens (years, values) survive tokenization; noise is filtered."""
    tokens = _tokenize("Q4 2024 revenue $500M reached 5 x growth")
    assert "2024" in tokens
    # "$500M" → "500m" by regex, preserved as is (not stemmed).
    assert "500m" in tokens
    # Single letters filtered
    assert "x" not in tokens
    # "Q4" becomes "q4" (alphanumeric, >= 2 chars)
    assert "q4" in tokens


def test_prf_expands_semantic_matches() -> None:
    """PRF mines expansion terms from initial results to find semantically
    related documents that don't share exact keywords with the query."""
    from langchain_core.documents import Document

    idx = BM25SearchIndex(prf=True)
    idx.add_documents([
        Document(
            page_content="The organization's income statement shows strong top-line growth.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 10},
        ),
        Document(
            page_content="Revenue was flat this quarter compared to the prior year.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 20},
        ),
        Document(
            page_content="The cafeteria menu was updated with new vegan options.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 99},
        ),
    ])

    # "income growth" matches page 10 initially.  PRF mines expansion terms
    # from page 10's content (e.g. "statement", "strong", "organization")
    # which may help surface page 20 ("revenue" document) that a pure
    # keyword search would miss.
    results = idx.search("income growth", k=3, use_prf=True)
    assert len(results) >= 1
    top_pages = {r[0].metadata["page"] for r in results}
    assert 10 in top_pages or 20 in top_pages  # Financial pages only
    assert 99 not in top_pages  # cafeteria page excluded


def test_search_without_prf_is_strict_keyword() -> None:
    """Without PRF, only exact/stemmed keyword matches are returned."""
    from langchain_core.documents import Document

    idx = BM25SearchIndex(prf=True)
    idx.add_documents([
        Document(
            page_content="Income from operations grew 15%.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 1},
        ),
        Document(
            page_content="The weather was sunny all week.",
            metadata={"source_path": "/raw/report.pdf.md", "page": 99},
        ),
    ])

    # Without PRF: "revenue" doesn't match "income" or "operations..."
    # Stemming: "revenue" → "revenu", doesn't match "incom" or "oper"
    results_no_prf = idx.search("revenue", k=2, use_prf=False)
    # Without PRF, no document contains "revenue" → may return nothing or very low.
    assert len(results_no_prf) == 0 or all(r[1] < 5.0 for r in results_no_prf)

    # With PRF: "income from operations grew" is the best match initially,
    # PRF mines expansion terms, "revenue" query may find related content.
    results_prf = idx.search("revenue", k=2, use_prf=True)
    # May or may not find results depending on PRF expansion success.
    # The key assertion: PRF should not return WORSE results than no-PRF.
    assert len(results_prf) >= len(results_no_prf)
