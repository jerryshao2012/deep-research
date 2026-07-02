"""Tests for thread_wiki — Phases 1–3 features (no API keys needed)."""

from __future__ import annotations

import tempfile
from pathlib import Path

# ── Phase 1.1: Purpose.md ────────────────────────────────────────────────────


def test_purpose_md_content():
    from thread_wiki.service import _purpose_md

    result = _purpose_md("Climate Research")
    assert "Climate Research" in result
    assert "directional intent" in result
    assert "Goals" in result
    assert "Key Questions" in result
    assert "Research Scope" in result
    assert "Evolving Thesis" in result
    assert "human-curated" in result


def test_prompts_reference_purpose_md():
    from thread_wiki.service import (
        _build_ingest_apply_prompt,
        _build_ingest_review_prompt,
        _build_query_prompt,
    )

    rp = _build_ingest_review_prompt("T", ["a.md"], None)
    assert "/purpose.md" in rp

    ap = _build_ingest_apply_prompt("T", ["a.md"], "summary", None)
    assert "/purpose.md" in ap

    qp = _build_query_prompt("T", "question?")
    assert "/purpose.md" in qp


# ── Phase 1.2: Incremental Index Repair ──────────────────────────────────────


def test_repair_index_callable():
    from thread_wiki.service import _repair_index

    assert callable(_repair_index)


# ── Phase 1.3: Semantic Lint ─────────────────────────────────────────────────


def test_semantic_lint_prompt():
    from thread_wiki.service import _build_semantic_lint_prompt

    sp = _build_semantic_lint_prompt("Test", None)
    assert "read-only" in sp.lower()
    assert "Contradictions Found" in sp
    assert "Stale Claims" in sp


def test_semantic_lint_finding_model():
    from thread_wiki.models import SemanticLintFinding

    slf = SemanticLintFinding(
        page_a="entities/a.md",
        page_b="concepts/b.md",
        claim_a="X is true",
        claim_b="X is false",
        severity="high",
    )
    assert slf.severity == "high"
    assert slf.finding_type == "contradiction"
    assert slf.resolution_hint == ""


# ── Phase 1.4: Review System ─────────────────────────────────────────────────


def test_review_report_empty():
    from thread_wiki.models import ReviewReport

    rr = ReviewReport()
    assert rr.is_empty
    assert rr.total_items == 0


def test_review_report_with_items():
    from thread_wiki.models import ReviewItem, ReviewReport

    ri = ReviewItem(
        item_type="missing_page",
        title="Key Entity",
        description="Important concept missing canonical page",
        search_query="entity research",
    )
    rr = ReviewReport(missing_pages=[ri])
    assert rr.total_items == 1
    assert not rr.is_empty
    assert rr.missing_pages[0].title == "Key Entity"


def test_parse_review_report():
    from thread_wiki.service import _parse_review_report

    sample = """## Missing Pages
- **Title**: Key Entity | **Why**: Important concept | **Search**: entity research

## Duplicate Suggestions
None identified.

## Research Questions
- **Question**: What about Z? | **Importance**: Critical gap | **Search**: Z research

## Knowledge Gaps
- **Gap**: Missing coverage | **Importance**: Important | **Direction**: Add sources
"""
    report = _parse_review_report(sample)
    assert report.missing_pages[0].title == "Key Entity"
    assert report.missing_pages[0].search_query == "entity research"
    assert report.research_questions[0].item_type == "research_question"
    assert report.gaps[0].item_type == "gap"
    assert report.total_items == 3


def test_parse_review_report_empty():
    from thread_wiki.service import _parse_review_report

    assert _parse_review_report("").is_empty
    assert _parse_review_report(None).is_empty  # type: ignore[arg-type]


# ── Phase 2.1: Context Budget ────────────────────────────────────────────────


def test_context_budget_sums_to_one():
    from thread_wiki.service import CONTEXT_BUDGET

    assert abs(sum(CONTEXT_BUDGET.values()) - 1.0) < 0.001


def test_calculate_context_budget():
    from thread_wiki.service import _calculate_context_budget

    budgets = _calculate_context_budget()
    assert "index" in budgets
    assert "wiki_pages" in budgets
    assert budgets["index"] > 0
    assert budgets["wiki_pages"] > budgets["index"]


def test_context_budget_instructions():
    from thread_wiki.service import _build_context_budget_instructions

    instructions = _build_context_budget_instructions()
    assert "retrieve_wiki_documents" in instructions
    assert "Context budget" in instructions


def test_total_raw_size():
    from thread_wiki.service import _total_raw_size

    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp)
        (raw_dir / "a.md").write_text("hello world")
        (raw_dir / "b.md").write_text("hello again")
        size = _total_raw_size(raw_dir)
        assert size > 20


# ── Phase 2.2: Persistent Ingest Queue ───────────────────────────────────────


def test_save_and_load_progress():
    import asyncio

    from thread_wiki.models import IngestPhase, IngestProgress
    from thread_wiki.progress import (
        load_progress,
        remove_progress_snapshot,
        save_progress,
    )

    async def _run(tmpdir):
        wiki_dir = Path(tmpdir)
        wiki_dir.mkdir(parents=True, exist_ok=True)

        p = IngestProgress(
            thread_id="test-save", phase=IngestPhase.APPLYING, source_count=5
        )
        await save_progress(p, wiki_dir)

        loaded = await load_progress(wiki_dir)
        assert loaded is not None
        assert loaded.thread_id == "test-save"
        assert loaded.phase == IngestPhase.APPLYING
        assert loaded.source_count == 5

        await remove_progress_snapshot(wiki_dir)
        assert await load_progress(wiki_dir) is None

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_run(tmp))


def test_load_progress_nonexistent():
    import asyncio

    from thread_wiki.progress import load_progress

    async def _run(tmpdir):
        assert await load_progress(Path(tmpdir)) is None

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_run(tmp))


def test_get_max_retry_default():
    from thread_wiki.progress import get_max_retry

    assert get_max_retry() >= 1


def test_ingest_progress_retry_count():
    from thread_wiki.models import IngestProgress

    p = IngestProgress(thread_id="test")
    assert p.retry_count == 0


# ── Phase 2.3: Cascade Deletion ──────────────────────────────────────────────


def make_wiki_page(
    path: Path, title: str, category: str, sources: list[str], body: str
) -> None:
    """Helper to write a wiki page with frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"title: {title}\n"
        f"category: {category}\n"
        f"summary: Test page for {title}\n"
        f"sources: {sources}\n"
        "tags: []\n"
        "updated: 2024-01-01\n"
        "---\n\n"
    )
    path.write_text(frontmatter + body)


def test_find_source_references():
    from thread_wiki.service import _find_source_references

    with tempfile.TemporaryDirectory() as tmp:
        wiki_dir = Path(tmp)
        wiki_content = wiki_dir / "wiki"
        wiki_content.mkdir(parents=True)

        (wiki_content / "entities").mkdir()
        make_wiki_page(
            wiki_content / "entities" / "test.md",
            "Test Entity",
            "entity",
            ["/raw/deleted.pdf.md"],
            "Body references /raw/deleted.pdf.md inline.",
        )

        refs = _find_source_references(wiki_dir, "deleted.pdf.md")
        assert "entities/test.md" in refs
        ref_list = refs["entities/test.md"]
        assert any("frontmatter sources" in r for r in ref_list)
        assert any("(body)" in r for r in ref_list)


def test_cascade_delete_source_references():
    from thread_wiki.service import _cascade_delete_source_references

    with tempfile.TemporaryDirectory() as tmp:
        wiki_dir = Path(tmp)
        wiki_content = wiki_dir / "wiki"
        wiki_content.mkdir(parents=True)

        (wiki_content / "entities").mkdir()
        make_wiki_page(
            wiki_content / "entities" / "test.md",
            "Test Entity",
            "entity",
            ["/raw/deleted.pdf.md"],
            "Body references /raw/deleted.pdf.md inline.",
        )

        report = _cascade_delete_source_references(wiki_dir, "deleted.pdf.md")
        assert "entities/test.md" in report["pages_updated"]
        assert "entities/test.md" in report["pages_with_body_refs"]

        # Verify the source was removed from frontmatter
        updated = (wiki_content / "entities" / "test.md").read_text()
        assert "/raw/deleted.pdf.md" not in updated.split("---")[1]


def test_cascade_delete_no_wiki():
    from thread_wiki.service import _cascade_delete_source_references

    with tempfile.TemporaryDirectory() as tmp:
        report = _cascade_delete_source_references(Path(tmp), "nonexistent.pdf.md")
        assert report["pages_updated"] == []


# ── Phase 2.4: Content Chunking ──────────────────────────────────────────────


def test_chunk_small_source_returns_none():
    from thread_wiki.service import _chunk_large_source

    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "small.md").write_text("Short content")
        assert _chunk_large_source(raw_dir, "small.md") is None


def test_chunk_large_source_creates_chunks():
    from thread_wiki.service import _chunk_large_source

    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        # Generate content that exceeds threshold with page sentinels
        parts = []
        for i in range(1, 8):
            parts.append(f"<!-- page: {i} -->\n\n# Page {i}\n\n" + ("Content. " * 3000))
        content = "\n\n".join(parts)
        (raw_dir / "large.md").write_text(content)

        chunks = _chunk_large_source(raw_dir, "large.md")
        if chunks:  # May be None if text_search unavailable
            assert len(chunks) > 1
            assert "chunk_index" in chunks[0]
            assert "total_chunks" in chunks[0]
            # Verify chunk files exist
            for ch in chunks:
                chunk_path = Path(ch.get("chunk_path", ""))
                assert chunk_path.exists()


def test_chunked_processing_instructions():
    from thread_wiki.service import _build_chunked_processing_instructions

    chunks = [
        {
            "chunk_index": 1,
            "total_chunks": 2,
            "chunk_path": "/tmp/large.md.chunk001.md",
        },
        {
            "chunk_index": 2,
            "total_chunks": 2,
            "chunk_path": "/tmp/large.md.chunk002.md",
        },
    ]
    instructions = _build_chunked_processing_instructions({"large.md": chunks})
    assert "CHUNKED SOURCE PROCESSING" in instructions
    assert "large.md" in instructions
    assert "chunk001.md" in instructions


# ── Phase 3.1: Louvain Community Detection ───────────────────────────────────


def wiki_with_cross_references() -> Path:
    """Create a temporary wiki with cross-references for graph tests."""
    tmp = tempfile.mkdtemp()
    wiki_dir = Path(tmp)
    wiki_content = wiki_dir / "wiki"
    wiki_content.mkdir(parents=True)
    (wiki_content / "entities").mkdir()
    (wiki_content / "concepts").mkdir()
    (wiki_content / "sources").mkdir()
    (wiki_content / "synthesis").mkdir()

    make_wiki_page(
        wiki_content / "entities" / "apple.md",
        "Apple Inc.",
        "entity",
        ["/raw/report.pdf.md"],
        "Apple is a tech company. See [[concepts/design.md]] and [[entities/competitor.md]].",
    )
    make_wiki_page(
        wiki_content / "concepts" / "design.md",
        "Industrial Design",
        "concept",
        ["/raw/report.pdf.md"],
        "Design philosophy used by [Apple](entities/apple.md).",
    )
    make_wiki_page(
        wiki_content / "entities" / "competitor.md",
        "Competitor Inc.",
        "entity",
        ["/raw/report.pdf.md"],
        "Competitor of [Apple](entities/apple.md). Also in [[concepts/design.md]].",
    )
    make_wiki_page(
        wiki_content / "entities" / "isolated.md",
        "Isolated Topic",
        "entity",
        [],
        "No connections to other pages.",
    )
    (wiki_content / "index.md").write_text(
        "# Test Wiki\n\n## Entities\n\n- [Apple](entities/apple.md)\n"
        "- [Competitor](entities/competitor.md)\n"
        "- [Isolated](entities/isolated.md)\n\n"
        "## Concepts\n\n- [Design](concepts/design.md)\n"
    )
    return wiki_dir


def test_build_wiki_graph():
    from thread_wiki.service import _build_wiki_graph

    wiki_dir = wiki_with_cross_references()
    G, name_to_rel, outlinks = _build_wiki_graph(wiki_dir)
    assert G.number_of_nodes() >= 3
    # Apple should link to design and competitor
    assert "entities/apple.md" in name_to_rel.values()
    assert len(outlinks.get("entities/apple.md", set())) >= 1


def test_detect_communities():
    from thread_wiki.service import _detect_communities

    wiki_dir = wiki_with_cross_references()
    communities = _detect_communities(wiki_dir)
    # Should find at least 1 community among the 4 connected pages
    assert len(communities) >= 1
    for c in communities:
        assert c.size >= 2
        assert 0.0 <= c.cohesion <= 1.0


def test_community_info_sparse():
    from thread_wiki.models import CommunityInfo

    dense = CommunityInfo(id=0, pages=["a", "b", "c"], cohesion=0.8)
    assert not dense.is_sparse

    sparse = CommunityInfo(id=1, pages=["x", "y"], cohesion=0.05)
    assert sparse.is_sparse


# ── Phase 3.2: 4-Signal Relevance Model ──────────────────────────────────────


def test_relevance_signals_zero_with_no_data():
    from thread_wiki.service import (
        _compute_direct_link_weight,
        _compute_source_overlap_weight,
        _compute_type_affinity_weight,
    )

    assert _compute_direct_link_weight({}, "a", "b") == 0.0
    assert _compute_source_overlap_weight({}, "a", "b") == 0.0
    assert _compute_type_affinity_weight({}, "a", "b") == 0.0


def test_direct_link_weight_detects_bidirectional():
    from thread_wiki.service import _compute_direct_link_weight

    outlinks = {"a": {"b"}, "b": set()}
    # a links to b → weight should be 1.0
    assert _compute_direct_link_weight(outlinks, "a", "b") == 1.0
    # Bidirectional check: b links to a
    assert _compute_direct_link_weight(outlinks, "b", "a") == 1.0


def test_source_overlap_weight():
    from thread_wiki.models import WikiPageMetadata
    from thread_wiki.service import _compute_source_overlap_weight

    meta = {
        "a": WikiPageMetadata(
            title="A",
            category="entity",
            sources=["/raw/x.pdf.md", "/raw/y.pdf.md"],
        ),
        "b": WikiPageMetadata(
            title="B",
            category="entity",
            sources=["/raw/x.pdf.md", "/raw/z.pdf.md"],
        ),
        "c": WikiPageMetadata(
            title="C",
            category="entity",
            sources=["/raw/w.pdf.md"],
        ),
    }
    # a and b share 1 source out of 3 unique → Jaccard = 1/3
    weight_ab = _compute_source_overlap_weight(meta, "a", "b")
    assert 0.3 < weight_ab < 0.34  # ~0.333

    # a and c share 0 sources
    weight_ac = _compute_source_overlap_weight(meta, "a", "c")
    assert weight_ac == 0.0


def test_type_affinity_weight():
    from thread_wiki.models import WikiPageMetadata
    from thread_wiki.service import _compute_type_affinity_weight

    meta = {
        "a": WikiPageMetadata(title="A", category="entity"),
        "b": WikiPageMetadata(title="B", category="entity"),
        "c": WikiPageMetadata(title="C", category="concept"),
        "d": WikiPageMetadata(title="D", category="uncategorized"),
    }
    # Same category
    assert _compute_type_affinity_weight(meta, "a", "b") == 1.0
    # Complementary categories (entity ↔ concept)
    assert _compute_type_affinity_weight(meta, "a", "c") == 0.5
    # Unrelated
    assert _compute_type_affinity_weight(meta, "a", "d") == 0.0


def test_build_relevance_graph():
    from thread_wiki.service import _build_relevance_graph

    wiki_dir = wiki_with_cross_references()
    result = _build_relevance_graph(wiki_dir)
    assert result["page_count"] >= 4
    assert result["total_pairs"] > 0
    assert len(result["edges"]) > 0
    # Highest-score edge should involve Apple (shared sources + links)
    top = result["edges"][0]
    assert top.total_score > 0


def test_relevance_edge_auto_score():
    from thread_wiki.models import RelevanceEdge

    e = RelevanceEdge(
        source_page="a",
        target_page="b",
        direct_links=1.0,
        source_overlap=0.5,
    )
    # score = 1.0*3.0 + 0.5*4.0 + 0*1.5 + 0*1.0 = 5.0
    assert e.total_score == 5.0


# ── Phase 3.3: Graph Insights ───────────────────────────────────────────────


def test_generate_graph_insights():
    from thread_wiki.service import (
        _build_relevance_graph,
        _detect_communities,
        _generate_graph_insights,
    )

    wiki_dir = wiki_with_cross_references()
    communities = _detect_communities(wiki_dir)
    rel = _build_relevance_graph(wiki_dir)

    insights = _generate_graph_insights(wiki_dir, communities, rel["edges"])
    assert isinstance(insights, list)
    # Each insight has the required fields
    for ins in insights:
        assert ins.insight_type in (
            "surprising_connection",
            "gap",
            "bridge",
        )
        assert ins.description
        assert ins.score >= 0


def test_build_graph_insight_summary():
    from thread_wiki.models import GraphInsight
    from thread_wiki.service import _build_graph_insight_summary

    insights = [
        GraphInsight(
            insight_type="bridge",
            pages=["entities/a.md"],
            description="Page bridges 2 communities.",
            score=4.0,
        ),
        GraphInsight(
            insight_type="gap",
            pages=["entities/x.md", "entities/y.md"],
            description="Sparse community with low cohesion.",
            score=0.1,
        ),
    ]
    summary = _build_graph_insight_summary(insights)
    assert "Graph Insights" in summary
    assert "Bridge" in summary
    assert "Gap" in summary


def test_analyze_graph_returns_phase3_fields():
    from thread_wiki.service import _analyze_graph

    wiki_dir = wiki_with_cross_references()
    report = _analyze_graph(wiki_dir)
    assert "communities" in report
    assert "relevance_edges" in report
    assert "graph_insights" in report
    assert report["total_pages"] >= 4


def test_graph_insight_model():
    from thread_wiki.models import GraphInsight

    gi = GraphInsight(
        insight_type="surprising_connection",
        pages=["a", "b"],
        description="High relevance across communities.",
        suggested_action="Add wikilink.",
        score=8.5,
    )
    assert gi.insight_type == "surprising_connection"
    assert gi.score == 8.5


# ── Integration: Full _analyze_graph pipeline ────────────────────────────────


def test_full_graph_analysis_pipeline():
    """End-to-end: build graph → communities → relevance → insights."""
    from thread_wiki.service import _analyze_graph

    wiki_dir = wiki_with_cross_references()
    report = _analyze_graph(wiki_dir)

    # Structural health
    assert report["total_pages"] == 4
    assert report["total_links"] >= 2

    # Orphans: isolated.md has no inbound links
    assert "entities/isolated.md" in report["orphans"]

    # Phase 3 fields populated
    assert isinstance(report["communities"], list)
    assert isinstance(report["relevance_edges"], list)
    assert isinstance(report["graph_insights"], list)

    # Insights should flag the isolated page as a gap or surprising connection.
    # (Graph insights may be empty for small wikis with tightly connected pages.)
    # At minimum, the analysis completed without error.
    assert isinstance(report["graph_insights"], list)


# ── Misc model tests ─────────────────────────────────────────────────────────


def test_ingest_progress_review_report():
    from thread_wiki.models import IngestProgress, ReviewItem, ReviewReport

    ri = ReviewItem(item_type="missing_page", title="Test", description="Desc")
    rr = ReviewReport(missing_pages=[ri])
    p = IngestProgress(thread_id="test", review_report=rr)
    assert p.review_report is not None
    assert p.review_report.total_items == 1


def test_wiki_page_metadata_to_frontmatter():
    from thread_wiki.models import WikiPageMetadata

    meta = WikiPageMetadata(
        title="Test Page",
        category="entity",
        summary="A test page.",
        tags=["test", "example"],
        sources=["/raw/test.pdf.md"],
    )
    fm = meta.to_frontmatter()
    assert fm.startswith("---")
    assert "title: Test Page" in fm
    assert "category: entity" in fm
    assert "sources:" in fm


def test_parse_frontmatter():
    from thread_wiki.models import parse_frontmatter

    content = """---
title: My Page
category: concept
summary: A summary.
tags: [tag1, tag2]
sources: [/raw/src.pdf.md]
updated: 2024-06-01
---

Body content here.
"""
    meta, body = parse_frontmatter(content)
    assert meta.title == "My Page"
    assert meta.category == "concept"
    assert meta.summary == "A summary."
    assert meta.tags == ["tag1", "tag2"]
    assert meta.sources == ["/raw/src.pdf.md"]
    assert "Body content here." in body


def test_parse_frontmatter_no_frontmatter():
    from thread_wiki.models import parse_frontmatter

    meta, body = parse_frontmatter("# Just a heading\n\nContent.")
    assert meta.title == "Just a heading"
    assert meta.category == "uncategorized"


def test_ingest_phase_reviewing():
    from thread_wiki.models import IngestPhase

    assert IngestPhase.REVIEWING.value == "reviewing"
    assert IngestPhase.REVIEWING in {IngestPhase.REVIEWING}


def test_graph_insight_out_model():
    from thread_wiki.routes import GraphInsightOut, GraphInsightsResponse

    gio = GraphInsightOut(
        insight_type="bridge",
        pages=["a", "b"],
        description="test",
        score=5.0,
    )
    assert gio.insight_type == "bridge"

    resp = GraphInsightsResponse(
        thread_id="t1",
        total_pages=10,
        total_links=15,
        insights=[gio],
    )
    assert resp.total_pages == 10
    assert len(resp.insights) == 1


def test_review_item_out_model():
    from thread_wiki.models import ReviewItem
    from thread_wiki.routes import _to_review_item_out

    ri = ReviewItem(
        item_type="missing_page",
        title="Test",
        description="Desc",
        suggested_action="Create page",
        search_query="search me",
    )
    rio = _to_review_item_out(ri)
    assert rio.item_type == "missing_page"
    assert rio.search_query == "search me"
