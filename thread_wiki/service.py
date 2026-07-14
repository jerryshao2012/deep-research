"""Core wiki operations service for thread-level RAG.

Ports llm-wiki's init, ingest, query, and lint workflows as direct Python APIs
(no CLI). Each operation runs against a per-thread wiki workspace at
``./docs/threads-wiki/<thread-id>/`` using the ``deepagents`` library.

Cancellation
------------
Long-running ingest operations check ``cancel_event`` between phases.
When the event is set the coroutine raises ``asyncio.CancelledError`` so
the background task terminates promptly.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import os
import re
import shutil
import sys
import threading
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.runnables import RunnableConfig
from networkx.algorithms.community import louvain_communities

from model_factory import get_configured_model
from research_agent.utils.text_search import (
    load_or_build_search_index,
)
from .models import (
    CommunityInfo,
    GraphInsight,
    IngestPhase,
    IngestProgress,
    RelevanceEdge,
    ReviewItem,
    ReviewReport,
    SourceCitation,
    ThreadWikiPaths,
    WikiQueryResult,
)
from .models import parse_frontmatter
from .progress import remove_progress_snapshot, save_progress

if TYPE_CHECKING:
    import networkx as nx  # noqa: F401
    from langchain_core.documents import Document  # noqa: F401

    from .models import WikiPageMetadata  # noqa: F401

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Text-based formats: read directly with read_text().
_ALLOWED_TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}
# Binary formats: require content_extractors for text extraction.
_BINARY_EXTRACT_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx"}
# All supported source types.
_ALLOWED_SOURCE_SUFFIXES = _ALLOWED_TEXT_SUFFIXES | _BINARY_EXTRACT_SUFFIXES

# Context budget: proportional allocation of the LLM's context window.
# Mirrors LLM Wiki's approach: 5% index, 50% wiki pages, 15% response reserve,
# 30% history + system prompt overhead.
CONTEXT_BUDGET: dict[str, float] = {
    "index": 0.05,
    "wiki_pages": 0.45,
    "raw_sources": 0.35,
    "response_reserve": 0.10,
    "history_system": 0.05,
}

# Default context window size in characters when WIKI_CONTEXT_MAX_CHARS is not set.
# 512K chars ≈ 128K tokens at 4 chars/token — safe for all modern LLMs
# (Claude, GPT-4, DeepSeek all support 128K+ token context windows).
_DEFAULT_CONTEXT_MAX_CHARS = 512_000

# Threshold above which a single raw source is considered "large" and should be
# searched via retrieve_wiki_documents rather than read directly.
# Raised to match the larger default context window (was 80K).
_LARGE_SOURCE_CHAR_THRESHOLD = 200_000

# Content chunking: when a staged source exceeds the max chunk size, it is split
# into overlapping chunks for reliable LLM processing.  Chunk boundaries follow
# natural document structure (page, slide, heading sentinels).
_WIKI_MAX_CHUNK_CHARS = int(__import__("os").getenv("WIKI_MAX_CHUNK_CHARS", "40000"))
_WIKI_CHUNK_OVERLAP_CHARS = int(
    __import__("os").getenv("WIKI_CHUNK_OVERLAP_CHARS", "2000")
)


def _get_context_max_chars() -> int:
    """Return the configured context window size in characters."""
    env_val = os.getenv("WIKI_CONTEXT_MAX_CHARS")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            logger.warning(
                "Invalid WIKI_CONTEXT_MAX_CHARS=%r, using default %d",
                env_val,
                _DEFAULT_CONTEXT_MAX_CHARS,
            )
    return _DEFAULT_CONTEXT_MAX_CHARS


def _calculate_context_budget() -> dict[str, int]:
    """Calculate per-category character budgets from the total context window."""
    total = _get_context_max_chars()
    budgets: dict[str, int] = {}
    for category, fraction in CONTEXT_BUDGET.items():
        budgets[category] = max(0, int(total * fraction))
    return budgets


def _build_context_budget_instructions() -> str:
    """Build human-readable context budget guidance for LLM prompts."""
    budgets = _calculate_context_budget()
    lines = [
        "Context budget (MANDATORY — stay within these limits to avoid truncation):",
        f"  - Total available: ~{_get_context_max_chars():,} characters",
        f"  - Index (/wiki/index.md) budget: ~{budgets['index']:,} chars",
        f"  - Wiki pages budget: ~{budgets['wiki_pages']:,} chars total across all pages read",
        f"  - Raw source budget: ~{budgets['raw_sources']:,} chars total across all sources",
        f"  - Response reserve: ~{budgets['response_reserve']:,} chars (leave room for your response)",
        f"  - History/system overhead: ~{budgets['history_system']:,} chars",
        "",
        "Budget enforcement:",
        "- Read `/wiki/index.md` first — it fits within the index budget.",
        "- Prioritize wiki pages by relevance. Stop reading when the wiki page budget is exhausted.",
        "- For large raw sources (>80K chars), use the `retrieve_wiki_documents` tool instead of reading files directly.",
        "- If a source is small, read it directly.",
        "- Do NOT exhaust the response reserve — always leave room for your full answer.",
    ]
    return "\n".join(lines)


def _total_raw_size(raw_dir: Path) -> int:
    """Return the total character count of all staged raw source files."""
    if not raw_dir.exists():
        return 0
    total = 0
    for path in raw_dir.rglob("*"):
        if path.is_file():
            try:
                total += len(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return total


_BASE_SYSTEM_PROMPT = """You are an expert research synthesizer building a long-lived topic knowledge base.

Mission:
- Build an accurate, high-signal, source-grounded topic corpus in `/wiki/`.
- Treat `/raw/` as immutable evidence inputs.
- Convert raw notes into canonical, reusable understanding.
- Read `/purpose.md` before every operation for directional guidance (goals, key questions, scope, evolving thesis).
- You may suggest refinements to `/purpose.md` in your output, but never edit it directly — the human owns it.

Reasoning style:
- Read primary source material before writing.
- Distinguish facts from inferences.
- Prefer compression-by-structure over compression-by-omission.
- Keep uncertainty explicit.
- Resolve contradictions when possible; otherwise record both claims and state what is unresolved.

Context budget awareness:
- Respect the per-category context budgets communicated in the user prompt.
- For large raw sources, prefer the `retrieve_wiki_documents` search tool over reading entire files.
- Prioritize wiki pages by relevance; stop reading when the wiki page budget is exhausted.
- Always leave room for your response within the response reserve.

Page structure (MANDATORY — every wiki page):
- Begin every page with YAML frontmatter delimited by `---`:
  ```yaml
  ---
  title: "Page Title"
  category: entity|concept|source|comparison|synthesis
  summary: "One-sentence summary of this page's content."
  tags: [tag1, tag2]
  sources: [/raw/filename.pdf.md]
  updated: YYYY-MM-DD
  ---
  ```
- The `category` field determines which subdirectory the page belongs in.
- The `summary` field powers the index catalog — keep it to one sentence.
- The `sources` field lists `/raw/` file paths that substantiate this page's claims.

Structured category directories:
- `/wiki/entities/` — named entities: companies, people, products, organizations
- `/wiki/concepts/` — abstract concepts, definitions, frameworks, methodologies
- `/wiki/sources/` — per-document summaries with bibliographic metadata
- `/wiki/comparisons/` — side-by-side analyses, named `<topic-a>-vs-<topic-b>.md`
- `/wiki/synthesis/` — cross-source integration, overviews, theses
- `/wiki/query/` — durable Q&A responses filed for future reference

Contradiction tracking:
- When two sources disagree on a claim, document the conflict using this callout
  in the relevant wiki page(s):
  ```
  > **Contradiction** (unresolved as of YYYY-MM-DD): Source A claims X
  > (/raw/source-a.pdf.md, p. N), while Source B claims Y
  > (/raw/source-b.pdf.md, p. M). Resolution pending.
  ```
- Link both contradicting sources. Mark contradictions as unresolved until
  new evidence resolves them.
- During lint passes, re-evaluate unresolved contradictions against newer
  source material and update resolution status.

Re-ingestion (merge mode):
- When a source document is re-ingested and a `/wiki/sources/<slug>.md` page
  already exists, append a `## Re-ingest YYYY-MM-DD` section at the bottom
  rather than overwriting.  Preserve the original analysis and note what
  changed in the new section.

Writing and organization rules:
- Maintain canonical pages per concept/entity/theme rather than many overlapping fragments.
- Keep pages scannable with clear headings.
- Include concise "What changed" summaries in your responses for runner-managed logging.
- Keep `/wiki/index.md` authoritative for navigation.
- Use recent `/log.md` entries as operational recency context before major synthesis.

Evidence rules:
- Every non-trivial claim should be traceable to the ingested source set.
- Avoid introducing unsupported external facts.
- If evidence is weak or missing, say so directly.
- If raw documents are too large to read in full, you may use the `retrieve_wiki_documents` tool to query them and retrieve relevant snippets.

Filesystem policy:
- Never write to `/raw/`.
- Never edit `/log.md`; the runner maintains append-only interaction entries.
- Write only under `/wiki/`.
"""


# ── Scaffold helpers (ported from llm-wiki helpers.py) ───────────────────────


def _slugify(text: str) -> str:
    """Convert text into a stable URL-friendly slug."""
    slug_chars: list[str] = []
    last_dash = False
    for char in text.strip().lower():
        if char.isalnum():
            slug_chars.append(char)
            last_dash = False
        elif not last_dash:
            slug_chars.append("-")
            last_dash = True
    slug = "".join(slug_chars).strip("-")
    return slug or "topic"


def _empty_index_text(topic: str) -> str:
    """Build default index markdown for an empty wiki."""
    lines = [
        f"# {topic} Wiki",
        "",
        "Content catalog for wiki navigation and retrieval.",
        "Read this page first during query workflows.",
        "",
        "## Other Pages",
        "",
        "- _No pages yet._",
    ]
    return "\n".join(lines) + "\n"


def _agents_md(topic: str) -> str:
    """Build default AGENTS.md guidance content."""
    return (
        f"# {topic} Wiki\n\n"
        "Use this file as the wiki schema/config for agent behavior.\n"
        "Keep it concise and co-evolve it as the wiki and workflow change.\n\n"
        "Rules:\n"
        "- Treat `/raw/` as read-only source material.\n"
        "- Ingest flow should be supervised: review takeaways first, then apply updates.\n"
        "- Ingest updates should prioritize canonical concept/entity/theme pages.\n"
        "- Every wiki page MUST have YAML frontmatter (title, category, summary, tags, sources, updated).\n"
        "- Organize pages into category subdirectories: entities/, concepts/, sources/, comparisons/, synthesis/, query/.\n"
        "- Use `/wiki/index.md` as the authoritative content catalog.\n"
        "- Use `/log.md` as recency context and keep it append-only.\n"
        "- Do not edit `/log.md` directly; the runner appends structured timeline entries.\n"
        "- Document contradictions explicitly using the `> **Contradiction**` callout format.\n"
        "- In merge mode, append `## Re-ingest` sections rather than overwriting.\n"
        "- See `/wiki/.templates/` for page structure templates.\n"
    )


def _purpose_md(topic: str) -> str:
    """Build default purpose.md content — directional intent for the wiki.

    Mirrors LLM Wiki's purpose.md concept: a human-authored (or human-reviewed)
    document that guides the LLM during every ingest and query.  The LLM reads
    it for context and can suggest refinements, but the human owns the final text.
    """
    return (
        f"# Purpose: {topic} Wiki\n\n"
        "This document captures the **directional intent** behind this wiki — why it\n"
        "exists, what questions it should answer, and what constitutes good coverage.\n\n"
        "## Goals\n"
        "- Build a comprehensive, accurate knowledge base about this topic.\n"
        "- Surface key entities, concepts, relationships, and contradictions.\n"
        "- Enable fast, cited answers to research questions.\n\n"
        "## Key Questions\n"
        "- _(Add 2-5 questions this wiki should be able to answer.)_\n\n"
        "## Research Scope\n"
        "- _(What is in scope? What is out of scope?)_\n\n"
        "## Evolving Thesis\n"
        "- _(Summarize the current understanding. Update as the wiki grows.)_\n\n"
        "## Usage Notes\n"
        "- This file is **human-curated**: the LLM may suggest edits, but you decide.\n"
        "- Review and update this document periodically as the wiki matures.\n"
        "- The LLM reads this before every ingest and query for directional guidance.\n"
    )


def _copy_templates(wiki_dir: Path) -> None:
    """Copy page templates from the package into the wiki workspace (if missing)."""
    templates_src = Path(__file__).resolve().parent / "templates"
    if not templates_src.exists():
        return

    templates_dest = wiki_dir / "wiki" / ".templates"
    templates_dest.mkdir(parents=True, exist_ok=True)

    for tmpl_file in templates_src.rglob("*.md"):
        dest = templates_dest / tmpl_file.name
        if not dest.exists():
            shutil.copy2(tmpl_file, dest)


def _write_if_missing(path: Path, content: str) -> None:
    """Write file content only when the target does not already exist."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ensure_scaffold(wiki_dir: Path, topic: str) -> None:
    """Ensure the required wiki workspace files and directories exist."""
    (wiki_dir / "raw").mkdir(parents=True, exist_ok=True)
    (wiki_dir / "wiki").mkdir(parents=True, exist_ok=True)
    _write_if_missing(wiki_dir / "wiki" / "index.md", _empty_index_text(topic))
    _write_if_missing(wiki_dir / "log.md", "# Change Log\n")
    _write_if_missing(wiki_dir / "AGENTS.md", _agents_md(topic))
    _write_if_missing(wiki_dir / "purpose.md", _purpose_md(topic))
    _copy_templates(wiki_dir)


# ── Source staging ────────────────────────────────────────────────────────────


def _extract_binary_source(file_path: Path) -> str:
    """Extract text from a binary document using content_extractors.

    Supports PDF, DOCX, PPTX, and XLSX formats. Falls back to a graceful
    error message if extraction fails rather than raising.
    """
    try:
        from research_agent.utils.content_extractors import extract_supported_document

        return extract_supported_document(file_path)
    except ImportError:
        # content_extractors not available; fall back to minimal PDF extraction.
        if file_path.suffix.lower() == ".pdf":
            return _fallback_pdf_extract(file_path)
        return f"Error: content_extractors module unavailable for {file_path.suffix}"
    except Exception as exc:
        return f"Error extracting {file_path.suffix}: {exc}"


def _fallback_pdf_extract(file_path: Path) -> str:
    """Minimal PDF extraction fallback when content_extractors is unavailable."""
    try:
        import pymupdf4llm

        markdown_content = pymupdf4llm.to_markdown(str(file_path))
        if isinstance(markdown_content, list):
            return "\n\n".join(str(item) for item in markdown_content)
        if markdown_content.strip():
            return markdown_content
    except Exception:
        try:
            import pypdf

            reader = pypdf.PdfReader(file_path)
            page_texts: list[str] = []
            for index, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    page_texts.append(f"## Page {index}:\n\n{text}")
            return "\n\n".join(page_texts)
        except Exception as e:
            return f"Error extracting PDF text: {e}"
    return ""


def _stage_sources(source_paths: list[Path], raw_dir: Path) -> list[Path]:
    """Copy source files into the wiki's raw directory, de-duplicating.

    Text-based formats (.md, .txt, .json, .yaml, .yml, .csv) are read directly.
    Binary formats (.pdf, .docx, .pptx, .xlsx) are extracted to markdown/text
    via ``content_extractors`` and saved as ``.md`` in the raw directory.

    Binary extractions run in parallel via a thread pool to reduce wall-clock
    time when multiple PDFs or office documents are staged together.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []

    # Separate text and binary sources — text is instant, binary is CPU-bound.
    text_sources: list[tuple[Path, Path, str, str, str]] = []
    binary_sources: list[tuple[Path, Path]] = []

    for source in source_paths:
        if not source.exists() or not source.is_file():
            logger.warning("Source file not found, skipping: %s", source)
            continue

        suffix = source.suffix.lower()
        if suffix not in _ALLOWED_SOURCE_SUFFIXES:
            logger.warning("Unsupported source type (%s), skipping: %s", suffix, source)
            continue

        is_binary = suffix in _BINARY_EXTRACT_SUFFIXES
        if is_binary:
            destination = raw_dir / f"{source.name}.md"
            stem = f"{source.stem}.{suffix.lstrip('.')}"
            binary_sources.append((source, destination))
        else:
            destination = raw_dir / source.name
            out_suffix = source.suffix
            stem = source.stem
            text_sources.append((source, destination, stem, out_suffix, ""))

    # Process text sources sequentially (instant I/O).
    for source, destination, stem, out_suffix, _ in text_sources:
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("Non-UTF-8 source, skipping: %s", source)
            continue

        counter = 2
        while destination.exists():
            destination = raw_dir / f"{stem}-{counter}{out_suffix}"
            counter += 1

        destination.write_text(text, encoding="utf-8")
        staged.append(destination)

    # Process binary sources in parallel (CPU-bound extraction).
    if binary_sources:

        def _extract_one(src: Path, dest: Path) -> tuple[Path, str] | None:
            """Extract one binary source; returns (final_dest, stem) or None."""
            text = _extract_binary_source(src)
            stem = f"{src.stem}.{src.suffix.lstrip('.')}"
            counter = 2
            while dest.exists():
                dest = raw_dir / f"{stem}-{counter}.md"
                counter += 1
            dest.write_text(text, encoding="utf-8")
            return (dest, stem)

        max_workers = min(len(binary_sources), 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_extract_one, src, dest): src
                for src, dest in binary_sources
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    if result is not None:
                        staged.append(result[0])
                except Exception:
                    src = futures[future]
                    logger.exception("Failed to extract binary source: %s", src)

    return staged


def _chunk_large_source(raw_dir: Path, filename: str) -> list[dict] | None:
    """Split a large staged source into overlapping chunks for reliable processing.

    Returns a list of chunk dicts (each with ``chunk_index``, ``total_chunks``,
    ``text``, and ``source_path``), or None if the source is small enough to
    process directly.  Writes chunk files as ``<filename>.chunk<N>.md`` next to
    the original in raw_dir.
    """
    source_path = raw_dir / filename
    if not source_path.exists():
        return None

    try:
        content = source_path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Cannot read source for chunking: %s", source_path)
        return None

    if len(content) <= _LARGE_SOURCE_CHAR_THRESHOLD:
        return None  # Small enough — no chunking needed

    try:
        from research_agent.utils.text_search import chunk_markdown_by_boundaries
    except ImportError:
        logger.warning("text_search module unavailable; cannot chunk %s", filename)
        return None

    # Use natural boundary chunking then sub-split by size.
    raw_chunks = chunk_markdown_by_boundaries(content)
    all_chunks: list[dict] = []
    current_texts: list[str] = []
    current_size = 0

    for rc in raw_chunks:
        text = rc.get("text", "")
        if not text.strip():
            continue

        if current_size + len(text) > _WIKI_MAX_CHUNK_CHARS and current_texts:
            # Finish the current chunk.
            chunk_text = "\n\n".join(current_texts)
            all_chunks.append(
                {
                    "text": chunk_text,
                    "source_path": str(source_path),
                    "chunk_index": len(all_chunks) + 1,
                }
            )
            # Start a new chunk with overlap from the last chunk.
            if _WIKI_CHUNK_OVERLAP_CHARS > 0 and len(current_texts) > 1:
                last_section = current_texts[-1]
                if len(last_section) <= _WIKI_CHUNK_OVERLAP_CHARS:
                    current_texts = [last_section]
                    current_size = len(last_section)
                else:
                    current_texts = []
                    current_size = 0
            else:
                current_texts = []
                current_size = 0

        current_texts.append(text)
        current_size += len(text)

    # Final chunk.
    if current_texts:
        chunk_text = "\n\n".join(current_texts)
        all_chunks.append(
            {
                "text": chunk_text,
                "source_path": str(source_path),
                "chunk_index": len(all_chunks) + 1,
            }
        )

    if len(all_chunks) <= 1:
        return None  # Only one chunk — process directly.

    # Write chunk files.
    total = len(all_chunks)
    chunk_paths: list[str] = []
    for ch in all_chunks:
        chunk_name = f"{filename}.chunk{ch['chunk_index']:03d}.md"
        chunk_path = raw_dir / chunk_name
        chunk_header = (
            f"<!-- Chunk {ch['chunk_index']}/{total} of {filename} -->\n"
            f"<!-- Source: {source_path} -->\n\n"
        )
        chunk_path.write_text(chunk_header + ch["text"], encoding="utf-8")
        ch["chunk_path"] = str(chunk_path)
        ch["total_chunks"] = total
        chunk_paths.append(chunk_name)

    logger.info(
        "Chunked %s (%d chars) into %d chunks (threshold: %d, overlap: %d)",
        filename,
        len(content),
        total,
        _WIKI_MAX_CHUNK_CHARS,
        _WIKI_CHUNK_OVERLAP_CHARS,
    )
    return all_chunks


# ── Index refresh ─────────────────────────────────────────────────────────────

_INDEX_CATEGORY_ORDER = (
    "Entities",
    "Concepts",
    "Sources",
    "Comparisons",
    "Syntheses",
    "Queries",
    "Contradictions",
    "Other Pages",
)
_INDEX_DIRECTORY_CATEGORIES = {
    "entities": "Entities",
    "entity": "Entities",
    "concepts": "Concepts",
    "concept": "Concepts",
    "sources": "Sources",
    "source": "Sources",
    "comparisons": "Comparisons",
    "comparison": "Comparisons",
    "syntheses": "Syntheses",
    "synthesis": "Syntheses",
    "query": "Queries",
    "queries": "Queries",
}
_INDEX_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _strip_markdown_inline(text: str) -> str:
    stripped = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", text)
    stripped = re.sub(r"`([^`]+)`", r"\1", stripped)
    stripped = re.sub(r"[*_~]", "", stripped)
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped.strip(" -:")


def _refresh_index(topic: str, wiki_dir: Path) -> None:
    """Rebuild wiki/index.md from current markdown pages, using YAML frontmatter when available."""
    wiki_content_dir = wiki_dir / "wiki"
    pages = [p for p in sorted(wiki_content_dir.rglob("*.md")) if p.name != "index.md"]

    if not pages:
        (wiki_content_dir / "index.md").write_text(
            _empty_index_text(topic), encoding="utf-8"
        )
        return

    section_lines: dict[str, list[str]] = {cat: [] for cat in _INDEX_CATEGORY_ORDER}
    contradiction_pages: list[str] = []

    for page in pages:
        relative = page.relative_to(wiki_content_dir).as_posix()
        content = page.read_text(encoding="utf-8")

        # Try YAML frontmatter first
        metadata, _body = parse_frontmatter(content)

        # Title: frontmatter > first heading > filename stem
        title = metadata.title
        if not title:
            title = page.stem.replace("-", " ").replace("_", " ").strip().title()
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    heading = _strip_markdown_inline(stripped.lstrip("#").strip())
                    if heading:
                        title = heading
                        break

        # Summary: frontmatter > first paragraph heuristic
        summary = metadata.summary
        if not summary:
            summary = "No summary available."
            in_code = False
            for line in content.splitlines():
                s = line.strip()
                if s.startswith("```"):
                    in_code = not in_code
                    continue
                if in_code or not s or s.startswith("#") or s.startswith("---"):
                    continue
                candidate = _strip_markdown_inline(s.lstrip("-*+ ").strip())
                if candidate:
                    summary = (
                        candidate[:147] + "..." if len(candidate) > 150 else candidate
                    )
                    break

        # Tags
        tag_str = f" `#{'` `#'.join(metadata.tags)}`" if metadata.tags else ""

        # Category: frontmatter > directory name > Other Pages
        category = metadata.category
        if category != "uncategorized":
            cat = _INDEX_DIRECTORY_CATEGORIES.get(category, "Other Pages")
        else:
            parts = page.relative_to(wiki_content_dir).parts
            cat = (
                _INDEX_DIRECTORY_CATEGORIES.get(parts[0].lower(), "Other Pages")
                if len(parts) > 1
                else "Other Pages"
            )

        entry = f"- [{title}]({relative}){tag_str} - {summary}"
        section_lines[cat].append(entry)

        # Detect contradictions for the contradictions section
        if "Contradiction" in content:
            contradiction_pages.append(relative)

    lines = [
        f"# {topic} Wiki",
        "",
        "Content catalog for wiki navigation and retrieval.",
        "Read this page first during query workflows.",
        "",
    ]
    for cat in _INDEX_CATEGORY_ORDER:
        if cat == "Contradictions":
            if contradiction_pages:
                lines.extend(
                    [
                        f"## {cat}",
                        "",
                        "_Pages documenting unresolved source conflicts:_",
                        "",
                    ]
                )
                for cp in sorted(contradiction_pages):
                    lines.append(f"- [{cp}]({cp})")
                lines.append("")
            continue
        if section_lines[cat]:
            lines.extend([f"## {cat}", ""])
            lines.extend(section_lines[cat])
            lines.append("")

    (wiki_content_dir / "index.md").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )


def _repair_index(topic: str, wiki_dir: Path, staged_count: int) -> bool:
    """Incrementally update wiki/index.md for pages from the most recent ingest.

    Uses a focused LLM call to add/update only the affected entries while
    preserving all other existing entries.  Falls back to ``_refresh_index``
    on failure.

    Returns True if the repair succeeded, False if a full rebuild is needed.
    """
    prompt = (
        f"Perform a surgical index repair for the '{topic}' wiki.\n\n"
        "Your ONLY task: update `/wiki/index.md` to reflect wiki pages that were "
        "created or modified by the most recent ingest.  Check `/log.md` for the "
        "latest ingest entries to identify what changed.\n\n"
        "Rules:\n"
        "- Read `/wiki/index.md` first to understand the current structure.\n"
        "- Add entries for newly created wiki pages under their correct category sections.\n"
        "- Update entries for modified pages (changed title, summary, or tags).\n"
        "- DO NOT remove or change ANY other existing entries.\n"
        "- DO NOT reorder sections or entries.\n"
        "- Preserve the existing section headings and formatting exactly.\n"
        "- If a page was deleted, remove only its entry line (keep the section).\n"
        "- Never write to `/raw/`, `/log.md`, `/AGENTS.md`, or `/purpose.md`.\n\n"
        "After editing, return a brief report listing which entries were added, "
        "updated, or removed.\n"
    )
    try:
        _run_agent(wiki_dir, prompt, read_only=False)
        return True
    except Exception:
        logger.exception("Index repair via LLM failed, falling back to full rebuild")
        return False


# ── Log helpers ───────────────────────────────────────────────────────────────


def _append_log_entry(
        wiki_dir: Path,
        phase: str,
        outcome: str,
        *,
        summary: str = "",
        sources_count: int = 0,
        pages_affected: str = "",
) -> None:
    """Append a structured entry to the wiki's log.md.

    Format: ``## [YYYY-MM-DD] op | Title`` with metadata list.
    """
    log_path = wiki_dir / "log.md"
    if not log_path.exists():
        log_path.write_text("# Change Log\n", encoding="utf-8")

    now = datetime.now(UTC)
    date_text = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_text = summary[:320] if summary else "No summary provided."

    # Parse phase into op and title: "ingest.review" → op="ingest", title="review"
    parts = phase.split(".", 1)
    op = parts[0] if parts else phase
    title = parts[1] if len(parts) > 1 else phase

    entry = (
        f"\n## [{date_text}] {op} | {title}\n"
        f"- timestamp: {timestamp}\n"
        f"- outcome: {outcome}\n"
        f"- summary: {summary_text}\n"
    )
    if sources_count:
        entry += f"- sources: {sources_count}\n"
    if pages_affected:
        entry += f"- pages_affected: {pages_affected}\n"

    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry)


# ── Agent execution ───────────────────────────────────────────────────────────


def _resolve_model():
    """Return the configured chat model from model_factory."""
    # Import here to avoid circular imports at module load time.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        return get_configured_model()
    finally:
        sys.path.pop(0)


def _run_agent(
        wiki_dir: Path,
        prompt: str,
        *,
        read_only: bool = False,
        search_index: object | None = None,
        total_raw_size: int | None = None,
        progress_callback: object | None = None,
) -> str:
    """Execute one deepagents pass against the wiki workspace.

    Args:
        wiki_dir: Root of the wiki workspace (contains raw/, wiki/, log.md, AGENTS.md).
        prompt: The instruction prompt to send to the agent.
        read_only: If True, deny all write permissions (review-only mode).
        search_index: Pre-built search index for raw documents. If None, built lazily.
        total_raw_size: Pre-computed total size of raw sources in chars. If None,
            computed on demand (reads all raw files).
        progress_callback: Optional callable(str) for sub-phase progress updates.
    """
    # Resolve wiki_dir to its absolute real path so the root_dir stored in
    # FilesystemBackend matches the \\?\ -prefixed paths that Path.resolve()
    # produces on Windows for long paths, avoiding the "outside root" error.
    wiki_dir = wiki_dir.resolve()

    # On Windows, str(Path) of a long path includes \\?\ prefix internally.
    # Pass these string representations to FilesystemBackend for correct
    # path containment validation across subdirectories.
    root_dir_str = str(wiki_dir)
    raw_dir_str = str(wiki_dir / "raw")
    wiki_subdir_str = str(wiki_dir / "wiki")

    # For server environments, we MUST NOT give the wiki synthesizer a shell.
    # It only needs to read/write files in the wiki directory.
    raw_backend = FilesystemBackend(root_dir=raw_dir_str, virtual_mode=True)
    wiki_backend = FilesystemBackend(root_dir=wiki_subdir_str, virtual_mode=True)
    root_backend = FilesystemBackend(root_dir=root_dir_str, virtual_mode=True)
    backend = CompositeBackend(
        default=root_backend,
        routes={
            "/raw/": raw_backend,
            "/wiki/": wiki_backend,
            "/log.md": root_backend,
            "/AGENTS.md": root_backend,
            "/purpose.md": root_backend,
        },
    )

    if read_only:
        permissions = [
            FilesystemPermission(operations=["write"], paths=["/raw/**"], mode="deny"),
            FilesystemPermission(operations=["write"], paths=["/wiki/**"], mode="deny"),
            FilesystemPermission(operations=["write"], paths=["/log.md"], mode="deny"),
            FilesystemPermission(
                operations=["write"], paths=["/AGENTS.md"], mode="deny"
            ),
            FilesystemPermission(
                operations=["write"], paths=["/purpose.md"], mode="deny"
            ),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ]
    else:
        permissions = [
            FilesystemPermission(operations=["write"], paths=["/raw/**"], mode="deny"),
            FilesystemPermission(
                operations=["write"], paths=["/AGENTS.md"], mode="deny"
            ),
            FilesystemPermission(operations=["write"], paths=["/log.md"], mode="deny"),
            FilesystemPermission(
                operations=["write"], paths=["/purpose.md"], mode="deny"
            ),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="allow"),
        ]

    # ── Budget-aware: pre-build search index when raw content is large ─────
    _raw_dir = wiki_dir / "raw"
    _total_raw = (
        total_raw_size if total_raw_size is not None else _total_raw_size(_raw_dir)
    )
    _raw_budget = _calculate_context_budget()["raw_sources"]
    _use_search = _total_raw > _raw_budget
    _cached_index = search_index  # May be None (build on demand)
    if _use_search and _cached_index is not None:
        logger.info("Using cached search index for raw content (%d chars).", _total_raw)
    elif _use_search:
        logger.info(
            "Raw content (%d chars) exceeds raw source budget (%d chars); "
            "pre-building search index and recommending retrieve_wiki_documents tool.",
            _total_raw,
            _raw_budget,
        )
        # Eagerly build the search index so the tool is ready when the LLM calls it.
        try:
            _index_dir = wiki_dir / "index"
            _cached_index = load_or_build_search_index(_raw_dir, _index_dir)
            logger.info("Search index built successfully for %s", _raw_dir)
        except Exception:
            logger.exception(
                "Failed to pre-build search index; tool will build on first call."
            )

    from langchain_core.tools import tool

    @tool
    def retrieve_wiki_documents(query: str, k: int = 5) -> str:
        """Retrieve top-k relevant document snippets from the raw documents text search index.

        Use this tool when you need to search large raw source documents in `/raw/`
        for specific facts.  Returns ranked snippets with source file paths, page
        numbers, and relevance scores.
        """
        nonlocal _cached_index

        try:
            index_dir = wiki_dir / "index"
            raw_dir = wiki_dir / "raw"

            # Use cached index if available, otherwise build on demand.
            if _cached_index is not None:
                search_idx = _cached_index
            else:
                search_idx = load_or_build_search_index(raw_dir, index_dir)
                _cached_index = search_idx  # Cache for subsequent calls.

            # Notify progress callback with the search query.
            if progress_callback is not None:
                try:
                    progress_callback(f"Searching documents for: {query[:120]}...")
                except Exception:
                    pass  # Progress callback failures must not break the tool.

            if not search_idx:
                return (
                    "Error: No search index is available or could be built. "
                    "You must read `/raw/` files directly using the read_file tool. "
                    "Available raw files are under /raw/."
                )

            # Primary search: full query.
            results = search_index(query, search_idx, k=k)

            # Fallback: if full query returns < k results, try individual terms
            # and merge.  This compensates for BM25 being keyword-based (unlike
            # FAISS which does semantic matching).
            if len(results) < k:
                terms = query.split()
                if len(terms) > 1:
                    term_results: dict[int, tuple[Document, float]] = {}
                    for term in terms:
                        if len(term) < 3:
                            continue
                        tr = search_index(term, search_idx, k=k)
                        for doc, score in tr:
                            doc_id = id(doc)
                            if (
                                    doc_id not in term_results
                                    or score > term_results[doc_id][1]
                            ):
                                term_results[doc_id] = (doc, score)
                    # Merge, keeping the best score per document.
                    seen_ids = {id(d) for d, _ in results}
                    for doc_id, (doc, score) in term_results.items():
                        if doc_id not in seen_ids:
                            results.append((doc, score))
                            seen_ids.add(doc_id)
                    # Re-sort by descending score.
                    results.sort(key=lambda x: x[1], reverse=True)

            if not results:
                return (
                    "No matching document snippets found for query. "
                    "Try reading `/raw/` files directly with the read_file tool, "
                    "or refine your search terms."
                )

            output_lines = []
            for idx, (doc, score) in enumerate(results[:k], start=1):
                meta = doc.metadata
                src = meta.get("source_path") or "unknown"
                page = meta.get("page")
                locator = meta.get("locator")
                heading = meta.get("heading")

                # Strip leading /raw/ from source path in snippet output
                if src.startswith("/raw/"):
                    src = src[len("/raw/"):]

                location_parts = []
                if page:
                    location_parts.append(f"p. {page}")
                if locator and locator != f"Page {page}":
                    location_parts.append(locator)
                if heading:
                    location_parts.append(heading)

                loc_str = f" ({', '.join(location_parts)})" if location_parts else ""
                output_lines.append(
                    f"[{idx}] Source: /raw/{src}{loc_str} (Score: {score:.4f})\n"
                    f"{doc.page_content}\n"
                )

            return "\n---\n\n".join(output_lines)
        except Exception as e:
            logger.exception("retrieve_wiki_documents failed")
            return f"Error retrieving raw documents: {e}"

    model = _resolve_model()
    agent = create_deep_agent(
        model=model,
        backend=backend,
        permissions=permissions,
        system_prompt=_BASE_SYSTEM_PROMPT,
        tools=[retrieve_wiki_documents],
    )

    # Apply a conservative recursion limit to prevent infinite tool-calling
    # loops.  Index repair and lint passes should complete in < 30 turns;
    # ingest review + apply may need more for large document sets.
    _WIKI_AGENT_RECURSION_LIMIT = int(
        __import__("os").getenv("WIKI_AGENT_RECURSION_LIMIT", "100")
    )
    # Timeout for the entire agent invocation (seconds).
    _WIKI_AGENT_TIMEOUT = int(
        __import__("os").getenv("WIKI_AGENT_TIMEOUT_SECONDS", "300")
    )
    agent = agent.with_config(
        RunnableConfig(
            recursion_limit=_WIKI_AGENT_RECURSION_LIMIT,
        )
    )

    # When raw content exceeds the budget, append a strong recommendation to
    # use the search tool instead of direct file reads.
    _effective_prompt = prompt
    if _use_search:
        _effective_prompt = (
            f"{prompt}\n\n"
            "IMPORTANT — Large raw source detected:\n"
            f"Total raw content ({_total_raw:,} chars) exceeds the raw source "
            f"budget ({_raw_budget:,} chars).  Use the `retrieve_wiki_documents` "
            "tool to search for specific facts within raw sources.  Do NOT read "
            "entire large raw files directly — you will exceed the context window.\n"
        )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": _effective_prompt}]}
    )

    # Extract the final AI text message.
    messages = result.get("messages", [])
    for message in reversed(messages):
        msg_type = getattr(message, "type", None)
        if msg_type is None and isinstance(message, dict):
            msg_type = message.get("type")
        if msg_type not in {"ai", "assistant"}:
            continue
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            text = "".join(parts).strip()
            if text:
                return text

    return "Completed wiki operation."


# ── Prompt builders ───────────────────────────────────────────────────────────


def _build_ingest_review_prompt(
        topic: str, staged_names: list[str], note: str | None
) -> str:
    """Build the ingest review prompt (read-only analysis pass)."""
    source_block = "\n".join(f"- /raw/{name}" for name in staged_names)
    note_block = note or "(none)"
    budget_instructions = _build_context_budget_instructions()
    return (
            f"Review the staged sources for topic '{topic}' and prepare a deep ingest plan.\n\n"
            "Phase constraint: review-only. Do not create, edit, move, or delete files yet.\n\n"
            f"{budget_instructions}\n\n"
            "Analysis standards:\n"
            "- Read `/purpose.md` first for directional guidance (goals, scope, key questions).\n"
            "- Read every staged source before proposing wiki edits (or use retrieve_wiki_documents for large files).\n"
            "- Distinguish direct evidence from inference.\n"
            "- Prefer canonical page updates over creating fragmented pages.\n"
            "- Preserve uncertainty; do not invent unsupported claims.\n"
            "- Use source filename citations for non-trivial claims.\n"
            + _DOCUMENT_CITATION_RULES
            + "\nRequired output format (markdown):\n"
              "## 1) Source-by-source extraction\n"
              "## 2) Proposed wiki change set\n"
              "## 3) Cross-source synthesis and structure\n"
              "## 4) Contradictions and unresolved claims\n"
              "## 5) Index updates and recency notes\n"
              "## 6) Gaps and follow-up questions\n\n"
              f"Staged sources:\n{source_block}\n\n"
              f"Operator note: {note_block}\n"
    )


def _build_ingest_apply_prompt(
        topic: str,
        staged_names: list[str],
        review_summary: str,
        note: str | None,
        *,
        merge: bool = False,
) -> str:
    """Build the ingest apply prompt (mutating pass)."""
    source_block = "\n".join(f"- /raw/{name}" for name in staged_names)
    note_block = note or "(none)"
    merge_instruction = ""
    if merge:
        merge_instruction = (
            "\nMerge mode (ACTIVE):\n"
            "- For each staged source, check whether a `/wiki/sources/<slug>.md` summary page already exists.\n"
            "- If a source summary page exists: append a `## Re-ingest YYYY-MM-DD` section at the bottom "
            "with new findings, rather than overwriting the existing content.\n"
            "- For entity/concept/synthesis pages: merge new claims into existing sections; "
            "do not delete or replace existing content unless it is demonstrably wrong.\n"
            "- If new evidence resolves a previously documented contradiction, update the "
            "contradiction callout from `(unresolved)` to `(resolved YYYY-MM-DD)` with a resolution note.\n"
        )
    budget_instructions = _build_context_budget_instructions()
    return (
            f"Apply an approved ingest update for topic '{topic}'.\n\n"
            f"{budget_instructions}\n\n"
            "Required workflow:\n"
            "1) Read `/purpose.md` for directional guidance (goals, scope, key questions).\n"
            "2) Read all staged files in `/raw/` before editing wiki content.\n"
            "3) Update canonical concept/entity/theme pages with high-signal evidence.\n"
            "4) Integrate cross-source synthesis, not just per-source summaries.\n"
            "5) Mark contradictions explicitly and preserve unresolved uncertainty.\n"
            "6) Update `/wiki/index.md`.\n"
            "7) Do not edit `/log.md`.\n"
            "8) Never write to `/raw/`.\n" + merge_instruction + "\nWriting standards:\n"
                                                                 "- Begin every wiki page with YAML frontmatter (title, category, summary, tags, sources, updated).\n"
                                                                 "- Keep pages scannable with clear headings and concise prose.\n"
                                                                 "- Use source filename citations for non-trivial claims.\n"
            + _DOCUMENT_CITATION_RULES
            + "- Avoid duplicative pages; merge into canonical pages when possible.\n\n"
              "Return a concise apply report:\n"
              "A) Files created  B) Files updated  C) Key synthesis  D) Remaining uncertainties\n\n"
              f"Approved review plan:\n{review_summary}\n\n"
              f"Staged sources:\n{source_block}\n\n"
              f"Operator note: {note_block}\n"
    )


_DOCUMENT_CITATION_RULES = """\
Document citation format rules (MANDATORY):
- Cite raw document sources as plain text: (/raw/filename.pdf.md, p. N) or (Source: /raw/filename.pdf.md, p. N)
- NEVER use markdown link syntax for document paths. The following is WRONG: ([/filename.pdf](p. N))
  The correct form is: (/raw/filename.pdf.md, p. N)
- For web URLs you MAY use markdown links: [Title](https://example.com)
- Page number formatting (CRITICAL — follow EXACTLY):
  * Single page: p. 14
  * Page range ONLY (consecutive pages): pp. 10-15
  * List of individual pages — each MUST have its own 'p. ' prefix:
    CORRECT:   bmo_ar2025.pdf: p. 9, p. 14, p. 20, p. 37
    WRONG:     bmo_ar2025.pdf: pp. 9, 14, 20, 37
    WRONG:     bmo_ar2025.pdf: 9, 14, 20, 37
  * NEVER use 'pp. ' for a list of non-consecutive pages — 'pp. ' is ONLY for ranges.
"""


def _build_query_prompt(topic: str, question: str) -> str:
    """Build the read-only query prompt."""
    return (
            f"Answer this question about '{topic}': {question}\n\n"
            "This is analysis-only. Do not create, edit, move, or delete files.\n\n"
            "Required workflow:\n"
            "1) Read `/purpose.md` first for directional guidance (goals, scope, key questions).\n"
            "2) Read `/wiki/index.md` and use its categorized summaries to choose candidate pages.\n"
            "3) Read recent `/log.md` entries (latest ~10 `## [` headings) to understand what was ingested recently.\n"
            "4) Prefer checking relevant prior `/wiki/query/*.md` pages first.\n"
            "5) Read the canonical wiki pages before final synthesis.\n"
            "6) CRITICAL: Search `/raw/` documents using the `retrieve_wiki_documents` tool with MULTIPLE query "
            "variations (at least 3 different phrasings of the question). Do NOT rely solely on wiki pages — "
            "raw documents often contain details that wiki pages summarised away.\n"
            "7) Provide a grounded answer with wiki or raw file path citations. "
            "Every factual claim MUST include a source reference.\n"
            "8) Decide whether this answer should be filed as a durable wiki page.\n\n"
            "ANTI-HALLUCINATION RULES (MANDATORY):\n"
            "- NEVER conclude 'no information available' or 'the document does not mention X' without first:\n"
            "  a) Searching `/raw/` documents using `retrieve_wiki_documents` with at least 3 query variations\n"
            "  b) Reading the top-ranked results from each search\n"
            "  c) If still nothing found, searching for related/adjacent terms (e.g. if 'acquisition' "
            "yields nothing, try 'purchase', 'buyout', 'takeover', 'transaction', 'M&A')\n"
            "- If after thorough searching you genuinely cannot find the information, state EXACTLY:\n"
            "  * Which documents you searched\n"
            "  * Which search queries you tried\n"
            "  * Which page ranges/sections you examined\n"
            "  * That the information MAY still exist in unexamined sections\n"
            "- Never present a negative claim with high confidence — always qualify it.\n\n"
            "Contradiction disclosure (MANDATORY):\n"
            "- Check wiki pages for unresolved `> **Contradiction**` callouts relevant to the question.\n"
            "- If conflicting claims exist, clearly distinguish 'established facts' from 'claims with conflicting evidence'.\n"
            "- Surface unresolved contradictions explicitly in the answer; do not silently pick one side.\n\n"
            + _DOCUMENT_CITATION_RULES
            + "\nOutput format (exact keys):\n"
              "ANSWER:\n<markdown answer with citations>\n\n"
              "DOCUMENTS_SEARCHED:\n<list of documents searched. Each page number MUST have its own 'p. ' prefix. Format EXACTLY as: bmo_ar2025.pdf: p. 10, p. 22, p. 30.  NEVER use 'pp. ' for individual pages — 'pp. ' is ONLY for ranges like pp. 10-15. Also list the search queries used.>\n\n"
              "CONTRADICTIONS:\n<unresolved contradictions relevant to this question, or 'None'>\n\n"
              "FILING_DECISION: file|skip\n"
              "FILING_REASON: <one sentence>\n"
    )


# ── Graph analysis ──────────────────────────────────────────────────────────────


def _analyze_graph(wiki_dir: Path) -> dict:
    """Analyze the wiki's internal link graph for structural health.

    Scans all wiki pages for ``[text](page.md)`` links and builds an adjacency
    list.  Returns a dict with:

    - ``hubs``: pages with many outbound links (top 10)
    - ``sinks``: pages with inbound but no outbound links
    - ``orphans``: pages with no inbound links
    - ``disconnected``: groups of pages with no cross-references to other groups
    - ``total_pages``, ``total_links``: summary stats
    """
    wiki_content_dir = wiki_dir / "wiki"
    if not wiki_content_dir.exists():
        return {
            "total_pages": 0,
            "total_links": 0,
            "hubs": [],
            "sinks": [],
            "orphans": [],
            "disconnected": [],
        }

    # link_re matches [text](path.md) and [[wikilinks]]
    link_re = re.compile(r"\[([^]]*?)]\(([^)]+\.md)\)|\[\[([^]]+\.md)]]", re.IGNORECASE)

    pages = [p for p in sorted(wiki_content_dir.rglob("*.md")) if p.name != "index.md"]
    if not pages:
        return {
            "total_pages": 0,
            "total_links": 0,
            "hubs": [],
            "sinks": [],
            "orphans": [],
            "disconnected": [],
        }

    # Build name → relative path lookup
    name_to_rel: dict[str, str] = {}
    for page in pages:
        rel = page.relative_to(wiki_content_dir).as_posix()
        name_to_rel[page.name] = rel

    outlinks: dict[str, set[str]] = defaultdict(set)
    inlinks: dict[str, set[str]] = defaultdict(set)

    for page in pages:
        rel = page.relative_to(wiki_content_dir).as_posix()
        try:
            content = page.read_text(encoding="utf-8")
        except Exception:
            continue

        for match in link_re.finditer(content):
            target = match.group(2) or match.group(3)
            if not target:
                continue
            # Resolve relative paths
            target_normalized = target
            if "/" not in target:
                # Same-directory link — resolve relative to page's directory
                page_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
                target_normalized = f"{page_dir}/{target}" if page_dir else target
            if target_normalized in name_to_rel.values() or target in name_to_rel:
                resolved = name_to_rel.get(target, target_normalized)
                outlinks[rel].add(resolved)
                inlinks[resolved].add(rel)

    total_links = sum(len(v) for v in outlinks.values())

    # Hubs: pages with top outbound links
    hub_list = sorted(outlinks.items(), key=lambda x: len(x[1]), reverse=True)[:10]
    hubs = [{"page": rel, "outlinks": len(links)} for rel, links in hub_list if links]

    # Sinks: inbound links but no outbound
    sinks = sorted(
        rel
        for rel in name_to_rel.values()
        if rel in inlinks and inlinks[rel] and rel not in outlinks
    )

    # Orphans: no inbound links (exclude pages with outlinks listed under hubs)
    orphans = sorted(
        rel for rel in name_to_rel.values() if rel not in inlinks or not inlinks[rel]
    )

    # Disconnected components (simple BFS)
    all_pages = set(name_to_rel.values())
    visited: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(all_pages):
        if start in visited:
            continue
        stack = [start]
        comp: list[str] = []
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            comp.append(node)
            for neighbor in outlinks.get(node, set()) | {
                p for p, ls in outlinks.items() if node in ls
            }:
                if neighbor in all_pages and neighbor not in visited:
                    stack.append(neighbor)
        components.append(comp)

    disconnected = [c for c in components if len(c) > 1 and len(c) < len(all_pages)]

    # Phase 3: Community detection, relevance graph, and insights.
    communities: list[CommunityInfo] = []
    relevance_result: dict = {"edges": [], "page_count": 0, "total_pairs": 0}
    graph_insights: list[GraphInsight] = []

    try:
        communities = _detect_communities(wiki_dir)
    except Exception:
        logger.exception("Community detection failed")

    try:
        relevance_result = _build_relevance_graph(wiki_dir)
    except Exception:
        logger.exception("Relevance graph construction failed")

    try:
        graph_insights = _generate_graph_insights(
            wiki_dir,
            communities,
            relevance_result.get("edges", []),
        )
    except Exception:
        logger.exception("Graph insight generation failed")

    return {
        "total_pages": len(pages),
        "total_links": total_links,
        "hubs": hubs,
        "sinks": sinks,
        "orphans": orphans,
        "disconnected": [{"size": len(c), "pages": c} for c in disconnected],
        "communities": communities,
        "relevance_edges": relevance_result.get("edges", []),
        "graph_insights": graph_insights,
    }


def _build_graph_payload(wiki_dir: Path) -> dict:
    """Build a serializable node+edge graph for frontend visualization.

    Returns a dict with:
    - ``nodes``: list of {id, title, category, tags, community_id}
    - ``edges``: list of {source, target, weight}
    - ``communities``: list of {id, cohesion, size}
    """
    wiki_content_dir = wiki_dir / "wiki"
    G, name_to_rel, outlinks = _build_wiki_graph(wiki_dir)

    communities = _detect_communities(wiki_dir)
    node_to_community: dict[str, int] = {}
    for comm in communities:
        for node in comm.pages:
            node_to_community[node] = comm.id

    nodes: list[dict] = []
    pages = [
        p for p in sorted(wiki_content_dir.rglob("*.md")) if p.name != "index.md"
    ]
    for page in pages:
        rel = page.relative_to(wiki_content_dir).as_posix()
        try:
            content = page.read_text(encoding="utf-8")
        except Exception:
            content = ""
        meta, _ = parse_frontmatter(content)
        nodes.append(
            {
                "id": rel,
                "title": meta.title or page.stem.replace("-", " ").title(),
                "category": meta.category,
                "tags": meta.tags,
                "community_id": node_to_community.get(rel),
            }
        )

    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    for source, targets in outlinks.items():
        for target in targets:
            key = (source, target) if source < target else (target, source)
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({"source": source, "target": target, "weight": 1.0})

    return {
        "nodes": nodes,
        "edges": edges,
        "communities": [
            {"id": c.id, "cohesion": c.cohesion, "size": c.size}
            for c in communities
        ],
    }


# ── Phase 3: Advanced Graph Analysis ──────────────────────────────────────────


# ── 3.1: Louvain Community Detection ──────────────────────────────────────────


def _build_wiki_graph(
        wiki_dir: Path,
) -> tuple[nx.Graph, dict[str, str], dict[str, set[str]]]:
    """Build a NetworkX graph and adjacency info from wiki cross-references.

    Returns (graph, name_to_rel, outlinks) where graph is an undirected
    NetworkX graph with page relative paths as nodes.
    """
    wiki_content_dir = wiki_dir / "wiki"
    link_re = re.compile(r"\[([^]]*?)]\(([^)]+\.md)\)|\[\[([^]]+\.md)]]", re.IGNORECASE)

    pages = [p for p in sorted(wiki_content_dir.rglob("*.md")) if p.name != "index.md"]
    if not pages:
        return nx.Graph(), {}, {}

    name_to_rel: dict[str, str] = {}
    for page in pages:
        rel = page.relative_to(wiki_content_dir).as_posix()
        name_to_rel[page.name] = rel

    outlinks: dict[str, set[str]] = defaultdict(set)
    G = nx.Graph()
    for page in pages:
        rel = page.relative_to(wiki_content_dir).as_posix()
        G.add_node(rel)
        try:
            content = page.read_text(encoding="utf-8")
        except Exception:
            continue
        for match in link_re.finditer(content):
            target = match.group(2) or match.group(3)
            if not target:
                continue
            target_normalized = target
            if "/" not in target:
                page_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
                target_normalized = f"{page_dir}/{target}" if page_dir else target
            if target_normalized in name_to_rel.values() or target in name_to_rel:
                resolved = name_to_rel.get(target, target_normalized)
                outlinks[rel].add(resolved)
                G.add_edge(rel, resolved)

    return G, name_to_rel, outlinks


def _detect_communities(wiki_dir: Path) -> list[CommunityInfo]:
    """Discover knowledge clusters via Louvain community detection.

    Uses NetworkX's Louvain implementation to find groups of heavily
    cross-referenced wiki pages, then computes cohesion scores for each
    community.  Low-cohesion communities are flagged as knowledge gaps.
    """
    G, name_to_rel, _outlinks = _build_wiki_graph(wiki_dir)
    if G.number_of_nodes() < 3:
        return []

    try:
        raw_communities = louvain_communities(G, seed=42)
    except Exception:
        logger.exception("Louvain community detection failed")
        return []

    communities: list[CommunityInfo] = []
    for idx, node_set in enumerate(raw_communities):
        if len(node_set) < 2:
            continue
        pages = sorted(node_set)
        # Cohesion = internal edges / total possible edges within community
        subgraph = G.subgraph(node_set)
        n = subgraph.number_of_nodes()
        internal_edges = subgraph.number_of_edges()
        max_edges = n * (n - 1) / 2 if n > 1 else 1
        cohesion = internal_edges / max_edges if max_edges > 0 else 0.0

        communities.append(
            CommunityInfo(
                id=idx,
                pages=pages,
                cohesion=round(cohesion, 4),
            )
        )

    communities.sort(key=lambda c: c.cohesion, reverse=True)
    return communities


def _find_bridge_nodes(G: nx.Graph, communities: list[CommunityInfo]) -> list[str]:
    """Identify bridge nodes that connect different knowledge communities."""
    if len(communities) < 2:
        return []

    # Build community membership map.
    node_to_community: dict[str, int] = {}
    for comm in communities:
        for node in comm.pages:
            node_to_community[node] = comm.id

    bridges: set[str] = set()
    for u, v in G.edges():
        cu = node_to_community.get(u)
        cv = node_to_community.get(v)
        if cu is not None and cv is not None and cu != cv:
            bridges.add(u)
            bridges.add(v)

    return sorted(bridges)


# ── 3.2: 4-Signal Relevance Model ─────────────────────────────────────────────


def _compute_direct_link_weight(
        outlinks: dict[str, set[str]], page_a: str, page_b: str
) -> float:
    """Signal 1: 1.0 if A links to B or B links to A, else 0.0. (Weight: 3x)."""
    out_a = outlinks.get(page_a, set())
    out_b = outlinks.get(page_b, set())
    return 1.0 if (page_b in out_a or page_a in out_b) else 0.0


def _compute_source_overlap_weight(
        page_metadata: dict[str, WikiPageMetadata], page_a: str, page_b: str
) -> float:
    """Signal 2: Jaccard similarity of frontmatter sources[] lists. (Weight: 4x)."""
    ma = page_metadata.get(page_a)
    mb = page_metadata.get(page_b)
    if not ma or not mb:
        return 0.0
    sa = set(ma.sources)
    sb = set(mb.sources)
    if not sa or not sb:
        return 0.0
    intersection = sa & sb
    union = sa | sb
    return len(intersection) / len(union) if union else 0.0


def _compute_adamic_adar_weight(
        G: nx.Graph, outlinks: dict[str, set[str]], page_a: str, page_b: str
) -> float:
    """Signal 3: Adamic-Adar index (sum of 1/log(degree) for shared neighbors). (Weight: 1.5x)."""
    neighbors_a = set(G.neighbors(page_a))
    neighbors_b = set(G.neighbors(page_b))
    shared = neighbors_a & neighbors_b
    if not shared:
        return 0.0

    score = 0.0
    for neighbor in shared:
        degree = G.degree(neighbor)
        if degree > 1:
            score += 1.0 / math.log(degree)
    return score


def _compute_type_affinity_weight(
        page_metadata: dict[str, WikiPageMetadata], page_a: str, page_b: str
) -> float:
    """Signal 4: 1.0 if same category, 0.5 if complementary, else 0.0. (Weight: 1x)."""
    ma = page_metadata.get(page_a)
    mb = page_metadata.get(page_b)
    if not ma or not mb:
        return 0.0

    ca = ma.category
    cb = mb.category

    if ca == cb and ca != "uncategorized":
        return 1.0

    # Complementary pairs: entity+concept, source+entity, concept+synthesis
    complementary = {
        ("entity", "concept"),
        ("concept", "entity"),
        ("source", "entity"),
        ("entity", "source"),
        ("concept", "synthesis"),
        ("synthesis", "concept"),
        ("synthesis", "entity"),
        ("entity", "synthesis"),
    }
    return 0.5 if (ca, cb) in complementary else 0.0


def _build_relevance_graph(wiki_dir: Path) -> dict:
    """Compute pairwise relevance scores for all wiki pages.

    Returns a dict with:
    - ``edges``: list of RelevanceEdge for the top-scoring pairs
    - ``page_count``: total pages analyzed
    - ``total_pairs``: total number of pairs evaluated
    """
    wiki_content_dir = wiki_dir / "wiki"

    # Build metadata map
    page_metadata: dict[str, WikiPageMetadata] = {}
    pages = [p for p in sorted(wiki_content_dir.rglob("*.md")) if p.name != "index.md"]
    for page in pages:
        rel = page.relative_to(wiki_content_dir).as_posix()
        try:
            content = page.read_text(encoding="utf-8")
        except Exception:
            continue
        meta, _ = parse_frontmatter(content)
        page_metadata[rel] = meta

    if len(page_metadata) < 2:
        return {"edges": [], "page_count": len(page_metadata), "total_pairs": 0}

    G, name_to_rel, outlinks = _build_wiki_graph(wiki_dir)

    edges: list[RelevanceEdge] = []
    page_list = sorted(page_metadata.keys())
    total_pairs = 0

    for i, page_a in enumerate(page_list):
        for page_b in page_list[i + 1:]:
            total_pairs += 1
            direct = _compute_direct_link_weight(outlinks, page_a, page_b)
            overlap = _compute_source_overlap_weight(page_metadata, page_a, page_b)
            adamic = _compute_adamic_adar_weight(G, outlinks, page_a, page_b)
            affinity = _compute_type_affinity_weight(page_metadata, page_a, page_b)

            edge = RelevanceEdge(
                source_page=page_a,
                target_page=page_b,
                direct_links=direct,
                source_overlap=round(overlap, 4),
                common_neighbors=round(adamic, 4),
                type_affinity=affinity,
            )
            if edge.total_score > 0:
                edges.append(edge)

    edges.sort(key=lambda e: e.total_score, reverse=True)
    return {
        "edges": edges,
        "page_count": len(page_metadata),
        "total_pairs": total_pairs,
    }


# ── 3.3: Graph Insights ──────────────────────────────────────────────────────


def _generate_graph_insights(
        wiki_dir: Path,
        communities: list[CommunityInfo],
        relevance_edges: list[RelevanceEdge],
) -> list[GraphInsight]:
    """Generate structured discovery signals from the knowledge graph.

    Combines community structure and relevance scores to surface:
    - Surprising connections: high relevance across community boundaries
    - Knowledge gaps: sparse communities, isolated nodes with high relevance
    - Bridge nodes: pages connecting communities
    - Missing links: high relevance with no existing wikilink
    """
    G, _name_to_rel, _outlinks = _build_wiki_graph(wiki_dir)
    insights: list[GraphInsight] = []

    # Build community membership map.
    node_to_community: dict[str, int] = {}
    for comm in communities:
        for node in comm.pages:
            node_to_community[node] = comm.id

    # 1. Surprising connections: high-score edges across communities.
    cross_community_edges = [
        e
        for e in relevance_edges
        if e.total_score >= 2.0
           and node_to_community.get(e.source_page) != node_to_community.get(e.target_page)
    ]
    for e in sorted(cross_community_edges, key=lambda x: x.total_score, reverse=True)[
        :10
    ]:
        insights.append(
            GraphInsight(
                insight_type="surprising_connection",
                pages=[e.source_page, e.target_page],
                description=(
                    f"Pages '{e.source_page}' and '{e.target_page}' share high relevance "
                    f"(score: {e.total_score:.1f}) but belong to different knowledge communities. "
                    f"Consider creating a cross-reference or synthesis page."
                ),
                suggested_action="Add wikilinks between these pages or create a bridging synthesis page.",
                score=e.total_score,
            )
        )

    # 2. Knowledge gaps: sparse communities.
    for comm in communities:
        if comm.is_sparse:
            insights.append(
                GraphInsight(
                    insight_type="gap",
                    pages=comm.pages[:5],  # First 5 as representative
                    description=(
                        f"Community {comm.id} has low cohesion ({comm.cohesion:.3f}). "
                        f"It contains {comm.size} pages that lack strong cross-references."
                    ),
                    suggested_action=(
                        "Review these pages for missing cross-references or consider "
                        "adding more sources to strengthen this knowledge area."
                    ),
                    score=comm.cohesion,
                )
            )

    # 3. Bridge nodes: pages linking communities.
    bridges = _find_bridge_nodes(G, communities)
    for bridge in bridges[:5]:
        connected_comms = set()
        for neighbor in G.neighbors(bridge):
            cid = node_to_community.get(neighbor)
            if cid is not None:
                connected_comms.add(cid)
        if len(connected_comms) >= 2:
            insights.append(
                GraphInsight(
                    insight_type="bridge",
                    pages=[bridge],
                    description=(
                        f"Page '{bridge}' bridges {len(connected_comms)} knowledge communities. "
                        f"It is well-positioned for a synthesis overview."
                    ),
                    suggested_action="Consider expanding this page into a cross-community synthesis.",
                    score=len(connected_comms) * 2.0,
                )
            )

    # 4. Missing links: high relevance but no direct wikilink.
    for e in relevance_edges:
        if e.total_score >= 2.5 and e.direct_links == 0.0:
            has_edge = G.has_edge(e.source_page, e.target_page)
            if not has_edge:
                insights.append(
                    GraphInsight(
                        insight_type="surprising_connection",
                        pages=[e.source_page, e.target_page],
                        description=(
                            f"High relevance (score: {e.total_score:.1f}) but no direct wikilink "
                            f"between '{e.source_page}' and '{e.target_page}'."
                        ),
                        suggested_action="Add a [[wikilink]] between these pages.",
                        score=e.total_score,
                    )
                )

    # Deduplicate and sort by score descending.
    seen: set[tuple[str, tuple[str, ...]]] = set()
    unique: list[GraphInsight] = []
    for ins in insights:
        key = (ins.insight_type, tuple(sorted(ins.pages)))
        if key not in seen:
            seen.add(key)
            unique.append(ins)
    unique.sort(key=lambda x: x.score, reverse=True)

    return unique[:20]  # Cap at top 20 insights to keep prompts manageable.


def _build_graph_insight_summary(insights: list[GraphInsight]) -> str:
    """Build a human-readable summary of graph insights for prompt injection."""
    if not insights:
        return "No graph insights generated."

    lines = ["Graph Insights (advanced knowledge graph analysis):", ""]
    by_type: dict[str, list[GraphInsight]] = {}
    for ins in insights:
        by_type.setdefault(ins.insight_type, []).append(ins)

    for itype, items in by_type.items():
        label = itype.replace("_", " ").title()
        lines.append(f"### {label} ({len(items)})")
        for item in items[:5]:
            lines.append(f"- {item.description}")
        lines.append("")

    return "\n".join(lines)


def _build_lint_prompt(topic: str, note: str | None) -> str:
    """Build the lint reconciliation prompt, incorporating graph analysis."""
    note_text = note or "(none)"
    return (
        f"Run a single-pass lint reconciliation for the '{topic}' wiki under `/wiki/`.\n\n"
        "Execution mode:\n"
        "- Read recent `/log.md` entries first.\n"
        "- Apply updates immediately (no review/confirm phase).\n"
        "- You may create new canonical wiki pages when required for reconciliation.\n"
        "- Do not edit `/log.md`.\n"
        "- Never write to `/raw/`.\n\n"
        "Required health checks and fixes:\n"
        "- Reconcile contradictions across wiki pages (update resolution status if resolved).\n"
        "- Identify stale claims superseded by newer evidence.\n"
        "- Detect orphan pages with no inbound links and add/repair cross-references.\n"
        "- Repair broken wikilinks / cross-references.\n"
        "- When an important concept lacks a dedicated page, create a canonical page.\n"
        "- Verify YAML frontmatter is present and correct on all pages.\n"
        "- Identify docs gaps and missing evidence.\n"
        "- Review community analysis for sparse knowledge clusters needing attention.\n"
        "- Address missing cross-references flagged by the graph insights.\n\n"
        "After edits, return a concise report:\n"
        "## Reconciled Changes\n## Remaining Gaps\n"
        "## Contradiction Status (resolved / still-unresolved count)\n"
        "## Structural Health (broken links fixed, orphans addressed, new pages created)\n"
        "## Suggested Next Questions and Sources\n\n"
        f"Operator note: {note_text}\n"
    )


def _build_semantic_lint_prompt(topic: str, note: str | None) -> str:
    """Build a read-only semantic contradiction detection prompt.

    This is a *analysis-only* pass that systematically compares wiki pages
    for factual conflicts, stale claims, and internal inconsistencies.
    The findings feed into the mutating lint pass for repair.
    """
    note_text = note or "(none)"
    return (
        f"Run a read-only semantic contradiction scan for the '{topic}' wiki under `/wiki/`.\n\n"
        "Execution mode: read-only. Do not create, edit, move, or delete files.\n\n"
        "Required workflow:\n"
        "1) Read `/wiki/index.md` to understand the full page catalog.\n"
        "2) Read `/purpose.md` for directional guidance.\n"
        "3) Read recent `/log.md` entries to understand what was recently ingested.\n"
        "4) Systematically compare wiki pages for contradictions:\n"
        "   - Cross-reference factual claims between entity and concept pages.\n"
        "   - Compare source summaries against entity pages (do they align?).\n"
        "   - Check synthesis pages against their cited sources.\n"
        "   - Look for internal contradictions within a single page.\n"
        "5) Identify stale claims that are superseded by newer evidence.\n"
        "6) Detect claims with no source backing (frontmatter `sources` is empty).\n\n"
        "Output format (structured):\n"
        "## Contradictions Found\n"
        "For each contradiction:\n"
        "- **Page A**: `<relative-path>` | **Page B**: `<relative-path>`\n"
        "- **Claim A**: ... | **Claim B**: ...\n"
        "- **Severity**: high|medium|low\n"
        "- **Type**: contradiction|stale_claim|inconsistency\n"
        "- **Resolution Hint**: <suggested resolution or 'needs human judgment'>\n\n"
        "## Stale Claims\n"
        "(Claims superseded by newer evidence — list page path, claim, and why stale)\n\n"
        "## Unresolved Contradictions from Previous Passes\n"
        "(Re-evaluate any `> **Contradiction** (unresolved...)` callouts found in pages)\n\n"
        "## Summary\n"
        "- Total contradictions found: N\n"
        "- High severity: N\n"
        "- Previously unresolved now resolvable: N\n\n"
        f"Operator note: {note_text}\n"
    )


# ── Review system ──────────────────────────────────────────────────────────────


def _build_review_prompt(topic: str, staged_names: list[str], note: str | None) -> str:
    """Build a read-only post-ingest review prompt.

    Asks the LLM to flag items needing human judgment after the apply phase:
    missing canonical pages, potential duplicates, research questions, and
    knowledge gaps.  This mirrors LLM Wiki's review suggestion stage.
    """
    note_text = note or "(none)"
    return (
        f"Review the wiki '{topic}' after the most recent ingest and flag items "
        "that need human curation.\n\n"
        "Execution mode: read-only. Do not create, edit, move, or delete files.\n\n"
        "Required workflow:\n"
        "1) Read `/wiki/index.md` to understand the current page catalog.\n"
        "2) Read `/purpose.md` for directional guidance.\n"
        "3) Read recent `/log.md` entries to understand what was just ingested.\n"
        "4) Scan the wiki for curation signals:\n\n"
        "**Missing Pages** — Important concepts, entities, or topics referenced "
        "in wiki pages that lack a dedicated canonical page. For each:\n"
        "  - Title, why it's needed, suggested search query.\n\n"
        "**Duplicate Suggestions** — Pages that cover overlapping ground and "
        "may benefit from merging. For each:\n"
        "  - Both page paths, what overlaps, suggested resolution.\n\n"
        "**Research Questions** — Open questions the wiki raises but doesn't "
        "yet answer. For each:\n"
        "  - The question, why it matters, suggested search query.\n\n"
        "**Knowledge Gaps** — Topics within scope (per purpose.md) that have "
        "little or no coverage. For each:\n"
        "  - The gap, why it's important, suggested direction.\n\n"
        "Output format (structured):\n"
        "## Missing Pages\n"
        "- **Title**: ... | **Why**: ... | **Search**: ...\n"
        "(or 'None identified.')\n\n"
        "## Duplicate Suggestions\n"
        "- **Page A**: ... | **Page B**: ... | **Overlap**: ... | **Suggestion**: ...\n"
        "(or 'None identified.')\n\n"
        "## Research Questions\n"
        "- **Question**: ... | **Importance**: ... | **Search**: ...\n"
        "(or 'None identified.')\n\n"
        "## Knowledge Gaps\n"
        "- **Gap**: ... | **Importance**: ... | **Direction**: ...\n"
        "(or 'None identified.')\n\n"
        f"Operator note: {note_text}\n"
    )


def _parse_review_report(raw: str) -> ReviewReport:
    """Parse the LLM's review output into a structured ReviewReport."""
    report = ReviewReport()

    if not raw:
        return report

    current_section: str | None = None

    for line in raw.splitlines():
        stripped = line.strip()

        # Track section headers
        if stripped.startswith("## Missing Pages"):
            current_section = "missing_pages"
            continue
        elif stripped.startswith("## Duplicate Suggestions"):
            current_section = "duplicate_suggestions"
            continue
        elif stripped.startswith("## Research Questions"):
            current_section = "research_questions"
            continue
        elif stripped.startswith("## Knowledge Gaps"):
            current_section = "gaps"
            continue
        elif stripped.startswith("##"):
            current_section = None
            continue

        # Skip "None identified" and empty lines
        if not current_section or not stripped.startswith("- **"):
            continue

        # Parse item from bold-keyed format: - **Key**: value | **Key2**: value2
        item = _parse_review_item(stripped, current_section)
        if item:
            target_list = getattr(report, current_section)
            target_list.append(item)

    return report


def _parse_review_item(line: str, section: str) -> ReviewItem | None:
    """Parse a single review item line into a ReviewItem.

    Handles the structured format: ``- **Key**: value | **Key2**: value2``
    """
    # Extract bold-keyed values
    pairs: dict[str, str] = {}
    for match in re.finditer(
            r"\*\*([^*]+)\*\*:\s*([^|]+?)(?=\s*\||\s*\*\*|\s*$)", line
    ):
        key = match.group(1).strip().lower().replace(" ", "_")
        value = match.group(2).strip().rstrip("|").strip()
        pairs[key] = value

    if not pairs:
        return None

    # Map keys to ReviewItem fields based on section
    if section in ("missing_pages",):
        title = pairs.get("title", "Untitled")
        item_type = "missing_page"
        description = pairs.get("why", "")
        search_query = pairs.get("search", "")
        suggested_action = f"Create canonical page for '{title}'"
    elif section == "duplicate_suggestions":
        title = f"{pairs.get('page_a', '?')} ↔ {pairs.get('page_b', '?')}"
        item_type = "duplicate"
        description = pairs.get("overlap", "")
        search_query = ""
        suggested_action = pairs.get("suggestion", "Review for potential merge")
    elif section == "research_questions":
        title = pairs.get("question", "Untitled question")
        item_type = "research_question"
        description = pairs.get("importance", "")
        search_query = pairs.get("search", "")
        suggested_action = "Investigate and consider filing answer to wiki/query/"
    elif section == "gaps":
        title = pairs.get("gap", "Untitled gap")
        item_type = "gap"
        description = pairs.get("importance", "")
        search_query = ""
        suggested_action = pairs.get("direction", "Gather sources to fill gap")
    else:
        return None

    return ReviewItem(
        item_type=item_type,
        title=title,
        description=description,
        suggested_action=suggested_action,
        search_query=search_query,
    )


# ── Query decision parsing ────────────────────────────────────────────────────

_DECISION_RE = re.compile(
    r"^FILING_DECISION:\s*(file|skip)\s*$", re.IGNORECASE | re.MULTILINE
)
_REASON_RE = re.compile(r"^FILING_REASON:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _parse_query_decision(raw: str) -> tuple[str, bool, str]:
    """Parse answer, should_file, and reason from raw query response."""
    decision_match = _DECISION_RE.search(raw)
    reason_match = _REASON_RE.search(raw)

    should_file = (
            decision_match is not None and decision_match.group(1).lower() == "file"
    )
    reason = (
        reason_match.group(1).strip() if reason_match else "Decision marker missing."
    )

    answer = raw
    if decision_match:
        answer = raw[: decision_match.start()].strip()
    if answer.upper().startswith("ANSWER:"):
        answer = answer[len("ANSWER:"):].strip()
    if not answer:
        answer = raw or "No answer returned."

    return answer, should_file, reason


# ── Chunked processing helpers ─────────────────────────────────────────────────


def _build_chunked_processing_instructions(
        chunked_sources: dict[str, list[dict]],
) -> str:
    """Build instructions for processing chunked large sources.

    Tells the LLM how to read chunks in order and merge findings.
    """
    lines = [
        "CHUNKED SOURCE PROCESSING — The following sources were too large to "
        "process in a single read and have been split into overlapping chunks:",
        "",
    ]
    for source_name, chunks in chunked_sources.items():
        total = chunks[0].get("total_chunks", len(chunks))
        chunk_names = [Path(ch.get("chunk_path", "")).name for ch in chunks]
        lines.append(
            f"- **{source_name}**: {total} chunks — "
            f"{', '.join(chunk_names[:5])}"
            f"{' ...' if total > 5 else ''}"
        )

    lines.extend(
        [
            "",
            "Chunk processing workflow:",
            "1) Read chunks in order (chunk001, chunk002, ...).",
            "2) Extract key claims, entities, and concepts from each chunk.",
            "3) After reading all chunks, synthesize your analysis as if you had "
            "read the entire document.",
            "4) In your output, cite the source file (e.g., /raw/report.pdf.md) "
            "rather than individual chunk files.",
            "5) Note any information that appears to span chunk boundaries and "
            "may need cross-referencing.",
        ]
    )
    return "\n".join(lines)


# ── Cascade deletion ──────────────────────────────────────────────────────────

_SOURCE_REF_RE = re.compile(r"/raw/([A-Za-z0-9._/\-]+)", re.IGNORECASE)


def _find_source_references(
        wiki_dir: Path, source_filename: str
) -> dict[str, list[str]]:
    """Scan all wiki pages for references to a specific source file.

    Returns a dict mapping page relative path → list of reference contexts
    (snippets from the page body where the source is mentioned).  Also
    identifies pages that list the source in their frontmatter ``sources[]``.
    """
    wiki_content_dir = wiki_dir / "wiki"
    if not wiki_content_dir.exists():
        return {}

    refs: dict[str, list[str]] = {}
    # Normalize the source filename for matching: strip /raw/ prefix if present,
    # and match both the raw path and the extracted .md variant.
    clean_name = source_filename
    if clean_name.startswith("/raw/"):
        clean_name = clean_name[len("/raw/"):]

    # Build search patterns for both exact name and extracted .md variant
    search_patterns = [clean_name]
    if not clean_name.endswith(".md"):
        search_patterns.append(f"{clean_name}.md")

    pages = [p for p in sorted(wiki_content_dir.rglob("*.md")) if p.name != "index.md"]
    for page in pages:
        rel = page.relative_to(wiki_content_dir).as_posix()
        try:
            content = page.read_text(encoding="utf-8")
        except Exception:
            continue

        page_refs: list[str] = []

        # Check frontmatter sources[] field
        metadata, _ = parse_frontmatter(content)
        for src in metadata.sources:
            for pat in search_patterns:
                if pat in src or f"/raw/{pat}" in src:
                    page_refs.append(f"[frontmatter sources] {src}")
                    break

        # Check body mentions of /raw/<path>
        for match in _SOURCE_REF_RE.finditer(content):
            matched_path = match.group(1)
            for pat in search_patterns:
                if pat in matched_path:
                    # Extract surrounding context (up to 60 chars)
                    start = max(0, match.start() - 20)
                    end = min(len(content), match.end() + 40)
                    ctx = content[start:end].replace("\n", " ").strip()
                    page_refs.append(f"(body) ...{ctx}...")
                    break

        if page_refs:
            refs[rel] = page_refs

    return refs


def _cascade_delete_source_references(wiki_dir: Path, source_filename: str) -> dict:
    """Remove references to a deleted source across all wiki pages.

    Returns a report dict with:
    - ``pages_updated``: list of page paths that had frontmatter sources[] updated
    - ``pages_with_body_refs``: list of page paths with inline /raw/ mentions
    - ``source_summary_page``: the source summary page path (if it exists)
    """
    clean_name = source_filename
    if clean_name.startswith("/raw/"):
        clean_name = clean_name[len("/raw/"):]
    if clean_name.endswith(".md"):
        base_stem = clean_name[:-3]  # strip .md
    else:
        base_stem = clean_name

    wiki_content_dir = wiki_dir / "wiki"
    if not wiki_content_dir.exists():
        return {
            "pages_updated": [],
            "pages_with_body_refs": [],
            "source_summary_page": None,
        }

    # Find references
    refs = _find_source_references(wiki_dir, source_filename)
    pages_updated: list[str] = []
    pages_with_body_refs: list[str] = []
    source_summary_page: str | None = None

    # Check for the source summary page
    for slug_variant in (
            base_stem,
            base_stem.replace(".", "-"),
            base_stem.split(".")[0],
    ):
        candidate = wiki_content_dir / "sources" / f"{slug_variant}.md"
        if candidate.exists():
            source_summary_page = candidate.relative_to(wiki_content_dir).as_posix()
            break

    # Update frontmatter for each affected page
    search_patterns = [clean_name, f"/raw/{clean_name}"]
    if not clean_name.endswith(".md"):
        search_patterns.append(f"{clean_name}.md")
        search_patterns.append(f"/raw/{clean_name}.md")

    for rel, ref_contexts in refs.items():
        page_path = wiki_content_dir / rel
        try:
            content = page_path.read_text(encoding="utf-8")
        except Exception:
            continue

        # Only modify frontmatter if the source appears in sources[]
        has_frontmatter_ref = any("frontmatter sources" in ctx for ctx in ref_contexts)
        has_body_ref = any("(body)" in ctx for ctx in ref_contexts)

        if has_body_ref:
            pages_with_body_refs.append(rel)

        if has_frontmatter_ref:
            metadata, body = parse_frontmatter(content)
            # Filter out the deleted source from the sources list
            original_sources = list(metadata.sources)
            filtered_sources = [
                s
                for s in metadata.sources
                if not any(pat in s for pat in search_patterns)
            ]
            if filtered_sources != original_sources:
                metadata.sources = filtered_sources
                # Rewrite the page with updated frontmatter
                new_frontmatter = metadata.to_frontmatter()
                # Find existing frontmatter and replace
                if content.startswith("---"):
                    end_idx = content.find("\n---", 3)
                    if end_idx != -1:
                        new_content = new_frontmatter + content[end_idx + 4:]
                    else:
                        new_content = new_frontmatter + "\n" + body
                else:
                    new_content = new_frontmatter + "\n" + content
                page_path.write_text(new_content, encoding="utf-8")
                pages_updated.append(rel)

    logger.info(
        "Cascade deletion for %r: updated %d pages, %d pages have body refs, "
        "source summary page: %s",
        source_filename,
        len(pages_updated),
        len(pages_with_body_refs),
        source_summary_page or "none",
    )

    return {
        "pages_updated": pages_updated,
        "pages_with_body_refs": pages_with_body_refs,
        "source_summary_page": source_summary_page,
    }


# ── Public service API ────────────────────────────────────────────────────────


async def init_wiki(paths: ThreadWikiPaths, topic: str) -> None:
    """Initialize the wiki workspace scaffold for a thread."""
    await asyncio.to_thread(paths.wiki_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(_ensure_scaffold, paths.wiki_dir, topic)
    logger.info("Initialized wiki workspace at %s", paths.wiki_dir)


def _collect_source_files(docs_dir: Path) -> list[Path]:
    """Collect all ingestable files from a thread's docs directory."""
    if not docs_dir.exists():
        return []
    return [p for p in sorted(docs_dir.rglob("*")) if p.is_file()]


async def run_ingest(
        paths: ThreadWikiPaths,
        topic: str,
        progress: IngestProgress,
        cancel_event: asyncio.Event,
        note: str | None = None,
        *,
        merge: bool = False,
) -> str:
    """Run the full ingest workflow with progress tracking and cancellation.

    Phases:
    1. Initialize scaffold
    2. Stage source files from docs_dir → raw/
    3. LLM review/analysis pass (read-only)
    4. LLM apply pass (mutating; merge mode appends instead of overwriting)
    5. Refresh index

    Args:
        merge: If True, re-ingested sources append ``## Re-ingest <date>``
               sections to existing source summary pages instead of overwriting.
    Returns the apply summary text.
    """
    try:
        # Phase 1: Initialize
        progress.advance(IngestPhase.INITIALIZING, "Creating wiki scaffold...")
        await init_wiki(paths, topic)

        await check_cancellation(cancel_event, phase_name="initializing")
        await save_progress(progress, paths.wiki_dir)

        # Phase 2: Stage sources
        progress.advance(
            IngestPhase.STAGING_SOURCES, "Collecting and staging source files..."
        )
        source_files = await asyncio.to_thread(_collect_source_files, paths.docs_dir)
        progress.source_count = len(source_files)

        if not source_files:
            progress.mark_complete("No source files found to ingest.")
            return "No source files found."

        staged = await asyncio.to_thread(_stage_sources, source_files, paths.raw_dir)
        staged_names = [p.name for p in staged]
        progress.sources_processed = len(staged)
        progress.source_names = [s.name for s in source_files]

        # Detect and chunk large sources (>80K chars by default).
        # Chunk files are written alongside the originals in raw/ and
        # the review prompt includes chunk-aware processing instructions.
        chunked_sources: dict[str, list[dict]] = {}
        for name in staged_names:
            chunks = await asyncio.to_thread(
                _chunk_large_source, paths.raw_dir, name
            )
            if chunks:
                chunked_sources[name] = chunks
                # Add chunk file names to staged_names so the LLM can read them.
                for ch in chunks:
                    chunk_name = ch.get("chunk_path", "")
                    if chunk_name:
                        chunk_name = Path(chunk_name).name
                        if chunk_name not in staged_names:
                            staged_names.append(chunk_name)

        if chunked_sources:
            logger.info(
                "Chunked %d large source(s): %s",
                len(chunked_sources),
                ", ".join(chunked_sources.keys()),
            )

        await check_cancellation(cancel_event, phase_name="staging_sources")
        await save_progress(progress, paths.wiki_dir)

        # ── Pre-build shared resources for all agent passes ─────────────────
        # Build the search index once and cache total raw size so subsequent
        # _run_agent calls don't redundantly rebuild the index or re-read files.
        _total_raw = await asyncio.to_thread(_total_raw_size, paths.raw_dir)
        _raw_budget = _calculate_context_budget()["raw_sources"]
        _use_search = _total_raw > _raw_budget
        _cached_index: object | None = None
        if _use_search:
            try:
                _index_dir = paths.wiki_dir / "index"
                # Clear stale index to ensure renamed/updated files are reflected
                if _index_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, _index_dir)
                _cached_index = await asyncio.to_thread(
                    load_or_build_search_index, paths.raw_dir, _index_dir
                )
                logger.info(
                    "Pre-built search index for raw content (%d chars) — "
                    "will reuse across all ingest phases.",
                    _total_raw,
                )
            except Exception:
                logger.exception(
                    "Failed to pre-build search index; each phase will build on demand."
                )

        # Sub-phase progress callback: updates detail when the LLM searches.
        def _progress_callback(msg: str) -> None:
            progress.detail = msg

        # ── Phase 3: Review (read-only LLM analysis) ────────────────────────
        progress.advance(
            IngestPhase.ANALYZING,
            f"Analyzing {len(staged)} sources: {', '.join(staged_names[:5])}"
            + (
                f" and {len(staged_names) - 5} more..." if len(staged_names) > 5 else ""
            ),
        )
        review_prompt = _build_ingest_review_prompt(topic, staged_names, note)

        # Append chunked-source instructions when large sources were split.
        if chunked_sources:
            chunk_instructions = _build_chunked_processing_instructions(chunked_sources)
            review_prompt = f"{review_prompt}\n\n{chunk_instructions}"
        _INGEST_PHASE_TIMEOUT = int(
            __import__("os").getenv("WIKI_INGEST_PHASE_TIMEOUT_SECONDS", "600")
        )
        try:
            review_summary = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_agent,
                    paths.wiki_dir,
                    review_prompt,
                    read_only=True,
                    search_index=_cached_index,
                    total_raw_size=_total_raw,
                    progress_callback=_progress_callback,
                ),
                timeout=_INGEST_PHASE_TIMEOUT,
            )
        except TimeoutError:
            logger.error(
                "Ingest review phase timed out after %ds", _INGEST_PHASE_TIMEOUT
            )
            raise
        await asyncio.to_thread(
            _append_log_entry,
            paths.wiki_dir,
            "ingest.review",
            "completed",
            summary=f"Reviewed {len(staged)} sources.",
        )

        await check_cancellation(cancel_event, phase_name="analyzing")
        await save_progress(progress, paths.wiki_dir)

        # ── Phase 4: Apply (mutating LLM pass) ──────────────────────────────
        apply_phase = IngestPhase.MERGING if merge else IngestPhase.APPLYING
        progress.advance(
            apply_phase,
            "Merging wiki updates..." if merge else "Applying wiki updates...",
        )
        apply_prompt = _build_ingest_apply_prompt(
            topic, staged_names, review_summary, note, merge=merge
        )
        try:
            apply_result = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_agent,
                    paths.wiki_dir,
                    apply_prompt,
                    read_only=False,
                    search_index=_cached_index,
                    total_raw_size=_total_raw,
                    progress_callback=_progress_callback,
                ),
                timeout=_INGEST_PHASE_TIMEOUT,
            )
        except TimeoutError:
            logger.error(
                "Ingest apply phase timed out after %ds", _INGEST_PHASE_TIMEOUT
            )
            raise
        await asyncio.to_thread(
            _append_log_entry,
            paths.wiki_dir,
            "ingest.apply",
            "applied",
            summary=apply_result[:320],
        )

        await check_cancellation(cancel_event, phase_name="applying")
        await save_progress(progress, paths.wiki_dir)

        # ── Phase 4.5: Post-ingest review (fire-and-forget background) ──────
        # Run the curation review as a background task so it doesn't block
        # the ingest from completing for the frontend.
        _run_review_bg = True  # default: enabled

        def _post_ingest_review_sync() -> None:
            """Run post-ingest review synchronously in a daemon thread."""
            try:
                review_prompt_bg = _build_review_prompt(topic, staged_names, note)
                review_raw = _run_agent(
                    paths.wiki_dir,
                    review_prompt_bg,
                    read_only=True,
                    search_index=_cached_index,
                    total_raw_size=_total_raw,
                )
                report = _parse_review_report(review_raw)
                progress.review_report = report
                _append_log_entry(
                    paths.wiki_dir,
                    "ingest.review",
                    "completed",
                    summary=f"Review found {report.total_items} curation items.",
                )
                logger.info(
                    "Post-ingest review completed: %d curation items for thread %s",
                    report.total_items,
                    paths.thread_id,
                )
            except Exception:
                logger.exception(
                    "Post-ingest review failed for thread %s", paths.thread_id
                )

        if _run_review_bg:
            _review_thread = threading.Thread(
                target=_post_ingest_review_sync,
                daemon=True,
                name=f"wiki-review-{paths.thread_id}",
            )
            _review_thread.start()
            logger.info(
                "Launched background post-ingest review for thread %s",
                paths.thread_id,
            )

        # ── Phase 5: Refresh index (direct rebuild, skip LLM repair) ────────
        progress.advance(IngestPhase.REFRESHING_INDEX, "Rebuilding wiki index...")
        await asyncio.to_thread(_refresh_index, topic, paths.wiki_dir)

        progress.mark_complete(f"Ingested {len(staged)} sources successfully.")
        await remove_progress_snapshot(paths.wiki_dir)
        return apply_result

    except asyncio.CancelledError:
        progress.mark_cancelled()
        await save_progress(progress, paths.wiki_dir)
        await asyncio.to_thread(
            _append_log_entry,
            paths.wiki_dir, "ingest", "cancelled", summary="Ingest cancelled by client.",
        )
        raise
    except Exception as exc:
        progress.mark_error(str(exc))
        await save_progress(progress, paths.wiki_dir)
        await asyncio.to_thread(
            _append_log_entry,
            paths.wiki_dir, "ingest", "error", summary=str(exc)[:320],
        )
        logger.exception("Ingest failed for thread %s", paths.thread_id)
        raise


def _extract_citations(answer: str) -> list[SourceCitation]:
    """Extract cited source paths, pages, locators, and web URLs from the answer text."""
    if not answer:
        return []

    citations: list[SourceCitation] = []
    seen_web_urls: set[str] = set()
    seen_raw_citations: set[tuple[str, int | None, str | None]] = set()

    # 1. Parse Sources block and numbered web citations
    # Example: [1] AI Research: https://example.com/ai
    # We match [index] title/locator: url
    # Note the title/locator is optional, but if present it resides between [index] and :
    numbered_ref_re = re.compile(
        r"\[(\d+)]\s*(.*?):\s*(https?://[^\s)\],;]+)", re.IGNORECASE
    )
    for match in numbered_ref_re.finditer(answer):
        locator = match.group(2).strip()
        url = match.group(3).rstrip(".,;)]")
        if url not in seen_web_urls:
            seen_web_urls.add(url)
            citations.append(
                SourceCitation(kind="web", url=url, locator=locator or None)
            )

    # 2. Parse bare URLs (excluding those already matched as numbered)
    # Match any http/https URL in the text
    url_re = re.compile(r"(https?://[^\s)\],;]+)", re.IGNORECASE)
    for match in url_re.finditer(answer):
        url = match.group(1).rstrip(".,;)]")
        if url not in seen_web_urls:
            seen_web_urls.add(url)
            citations.append(SourceCitation(kind="web", url=url, locator=None))

    # 3. Parse Section References (e.g. policies.md#Risk-Factors)
    # Format: filename.md#Heading-Anchor, making sure it doesn't start with /raw/
    # If Group 1 (/raw/) is present, it's skipped here (will be parsed as raw citation).
    section_re = re.compile(
        r"(/raw/)?\b([A-Za-z0-9._\-]+\.md)#([A-Za-z0-9._\-]+)", re.IGNORECASE
    )
    for match in section_re.finditer(answer):
        if match.group(1):
            continue
        raw_path = match.group(2)
        locator = match.group(3)
        citations.append(
            SourceCitation(kind="section", raw_path=raw_path, locator=locator)
        )

    # 4. Parse Raw citations
    # Match /raw/<path> and look for trailing page or locator metadata
    raw_path_re = re.compile(r"/raw/([A-Za-z0-9._/\-]+)", re.IGNORECASE)
    page_re = re.compile(r"^\s*[,(\[\s]?\s*p(?:age)?\.?\s*(\d+)\b", re.IGNORECASE)
    slide_re = re.compile(r"^\s*[,(\[\s]?\s*(Slide\s*\d+)\b", re.IGNORECASE)
    xlsx_re = re.compile(
        r"^\s*[,(\[\s]?\s*(Sheet:\s*[^,\n)]+?,\s*(?:row|col)\s*\d+)\b", re.IGNORECASE
    )

    for match in raw_path_re.finditer(answer):
        raw_path = "/raw/" + match.group(1)

        # Look at context immediately following the raw path
        start_idx = match.end()
        next_raw = answer.find("/raw/", start_idx)
        next_nl = answer.find("\n", start_idx)
        end_idx = len(answer)
        if next_raw != -1:
            end_idx = min(end_idx, next_raw)
        if next_nl != -1:
            end_idx = min(end_idx, next_nl)
        end_idx = min(end_idx, start_idx + 100)
        tail = answer[start_idx:end_idx]

        page: int | None = None
        locator: str | None = None

        page_match = page_re.search(tail)
        if page_match:
            page = int(page_match.group(1))
        else:
            slide_match = slide_re.search(tail)
            if slide_match:
                locator = slide_match.group(1)
            else:
                xlsx_match = xlsx_re.search(tail)
                if xlsx_match:
                    locator = xlsx_match.group(1)

        key = (raw_path, page, locator)
        if key not in seen_raw_citations:
            seen_raw_citations.add(key)
            citations.append(
                SourceCitation(
                    kind="raw",
                    raw_path=raw_path,
                    page=page,
                    locator=locator,
                )
            )

    return citations


async def run_query(
        paths: ThreadWikiPaths,
        topic: str,
        question: str,
        *,
        file_results: bool = True,
) -> WikiQueryResult:
    """Query the thread's wiki knowledge base.

    Returns a grounded answer with optional filing into wiki/query/.
    """
    query_prompt = _build_query_prompt(topic, question)
    _QUERY_TIMEOUT = int(__import__("os").getenv("WIKI_QUERY_TIMEOUT_SECONDS", "180"))
    try:
        raw_response = await asyncio.wait_for(
            asyncio.to_thread(_run_agent, paths.wiki_dir, query_prompt, read_only=True),
            timeout=_QUERY_TIMEOUT,
        )
    except TimeoutError:
        logger.error("Wiki query timed out after %ds", _QUERY_TIMEOUT)
        return WikiQueryResult(
            answer="The wiki query timed out. The document may be too large to process in the available time.",
            filed_path=None,
            sources_cited=[],
        )
    answer, should_file, reason = _parse_query_decision(raw_response)

    await asyncio.to_thread(
        _append_log_entry,
        paths.wiki_dir,
        "query.review",
        "file" if should_file else "skip",
        summary=answer[:320],
    )

    filed_path: str | None = None
    if should_file and file_results:
        slug = _slugify(question)[:80].rstrip("-") or "query"
        target = f"/wiki/query/{slug}.md"
        file_prompt = (
            f"File a durable query answer for topic '{topic}'.\n\n"
            f"Create or overwrite exactly: `{target}`\n\n"
            "Requirements:\n"
            "1) Write a clean, scannable markdown page.\n"
            "2) Preserve grounded claims and include wiki file path citations.\n"
            "3) Include sections: Question, Answer, and Sources.\n"
            "4) Never write to `/raw/`.\n\n"
            f"Filing reason: {reason}\n\nQuestion: {question}\n\nAnswer draft:\n{answer}\n"
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    _run_agent, paths.wiki_dir, file_prompt, read_only=False
                ),
                timeout=_QUERY_TIMEOUT,
            )
        except TimeoutError:
            logger.warning("Wiki query filing timed out after %ds", _QUERY_TIMEOUT)
        else:
            await asyncio.to_thread(_refresh_index, topic, paths.wiki_dir)
            await asyncio.to_thread(
                _append_log_entry,
                paths.wiki_dir,
                "query.apply",
                "filed",
                summary=f"Filed query answer at {target}.",
            )
            filed_path = target

    citations = _extract_citations(answer)

    return WikiQueryResult(
        answer=answer, filed_path=filed_path, sources_cited=citations
    )


async def run_lint(
        paths: ThreadWikiPaths,
        topic: str,
        note: str | None = None,
) -> str:
    """Run lint reconciliation on the thread's wiki.

    Three-pass lint:
    1. Graph analysis — structural health (hubs, sinks, orphans, disconnected components)
    2. Semantic contradiction detection (read-only) — systematic page comparison
    3. Mutating repair — applies fixes with both structural and semantic findings as context

    Use this after document deletions to reconcile stale references.
    """
    # Pass 1: Run graph analysis for structural health insights.
    graph_report = await asyncio.to_thread(_analyze_graph, paths.wiki_dir)

    # Build graph summary to inject into the lint prompt.
    graph_lines = [
        "Graph Analysis (pre-lint structural health):",
        f"- Total pages: {graph_report['total_pages']}, total links: {graph_report['total_links']}",
    ]
    if graph_report["sinks"]:
        graph_lines.append(
            f"- Sinks (inbound links, no outbound): {', '.join(graph_report['sinks'][:5])}"
            f"{' ...' if len(graph_report['sinks']) > 5 else ''}"
        )
    if graph_report["orphans"]:
        graph_lines.append(
            f"- Orphans (no inbound links): {', '.join(graph_report['orphans'][:5])}"
            f"{' ...' if len(graph_report['orphans']) > 5 else ''}"
        )
    if graph_report["disconnected"]:
        graph_lines.append(
            f"- Disconnected components: {len(graph_report['disconnected'])}"
        )

    # Phase 3: Add community and insight summaries.
    communities: list = graph_report.get("communities", [])
    if communities:
        sparse = [c for c in communities if getattr(c, "is_sparse", False)]
        graph_lines.append(
            f"- Knowledge communities: {len(communities)} "
            f"({len(sparse)} sparse/low-cohesion)"
        )
    insights: list = graph_report.get("graph_insights", [])
    if insights:
        graph_lines.append(f"- Graph insights: {len(insights)} discovery signals")
        # Append inline insight summary for the LLM
        insight_text = _build_graph_insight_summary(insights)
        graph_lines.append(f"\n{insight_text}")

    graph_summary = "\n".join(graph_lines)

    # Pass 2: Read-only semantic contradiction detection.
    semantic_prompt = _build_semantic_lint_prompt(topic, note)
    logger.info("Running read-only semantic contradiction detection pass...")
    _LINT_TIMEOUT = int(__import__("os").getenv("WIKI_LINT_TIMEOUT_SECONDS", "300"))
    try:
        semantic_result = await asyncio.wait_for(
            asyncio.to_thread(
                _run_agent, paths.wiki_dir, semantic_prompt, read_only=True
            ),
            timeout=_LINT_TIMEOUT,
        )
    except TimeoutError:
        logger.error("Semantic lint pass timed out after %ds", _LINT_TIMEOUT)
        semantic_result = (
            "## Contradictions Found\nNone identified (lint timed out).\n\n"
            "## Stale Claims\nNone identified.\n\n"
            "## Unresolved Contradictions from Previous Passes\nNone.\n\n"
            "## Summary\nLint semantic pass timed out.\n"
        )

    # Pass 3: Mutating lint repair with both structural and semantic findings.
    lint_prompt = _build_lint_prompt(topic, note)
    # Inject graph analysis and semantic findings before the operator note.
    lint_prompt = lint_prompt.replace(
        f"Operator note: {note or '(none)'}\n",
        f"{graph_summary}\n\n"
        f"Semantic Contradiction Scan Results:\n{semantic_result[:2000]}\n\n"
        f"Operator note: {note or '(none)'}\n",
    )
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_agent, paths.wiki_dir, lint_prompt, read_only=False),
            timeout=_LINT_TIMEOUT,
        )
    except TimeoutError:
        logger.error("Lint apply pass timed out after %ds", _LINT_TIMEOUT)
        result = "Lint apply timed out."
    await asyncio.to_thread(_refresh_index, topic, paths.wiki_dir)
    await asyncio.to_thread(
        _append_log_entry,
        paths.wiki_dir,
        "lint.apply",
        "applied",
        summary=result[:320],
        sources_count=graph_report["total_pages"],
        pages_affected=f"lint pass on {graph_report['total_pages']} pages",
    )
    return result


async def check_cancellation(
        cancel_event: asyncio.Event, *, phase_name: str = ""
) -> None:
    """Raise CancelledError if cancellation was requested."""
    if cancel_event.is_set():
        raise asyncio.CancelledError(
            f"Ingest cancelled{' during ' + phase_name if phase_name else ''}."
        )
