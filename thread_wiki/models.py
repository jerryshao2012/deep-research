"""Data models and Enums for thread-level LLM Wiki integrations.

Defines schemas and phases of the wiki ingest lifecycle, configuration objects,
and citation models to trace source information across threads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class IngestPhase(StrEnum):
    """Phases of the wiki ingest lifecycle."""

    IDLE = "idle"
    INITIALIZING = "initializing"
    STAGING_SOURCES = "staging_sources"
    ANALYZING = "analyzing"
    APPLYING = "applying"
    REVIEWING = "reviewing"
    REFRESHING_INDEX = "refreshing_index"
    MERGING = "merging"
    READY = "ready"
    ERROR = "error"
    CANCELLED = "cancelled"


# Phases considered "in progress" (not terminal).
ACTIVE_PHASES = frozenset(
    {
        IngestPhase.INITIALIZING,
        IngestPhase.STAGING_SOURCES,
        IngestPhase.ANALYZING,
        IngestPhase.APPLYING,
        IngestPhase.REVIEWING,
        IngestPhase.REFRESHING_INDEX,
    }
)

# Terminal phases indicating the ingest is no longer running.
TERMINAL_PHASES = frozenset(
    {
        IngestPhase.READY,
        IngestPhase.ERROR,
        IngestPhase.CANCELLED,
        IngestPhase.IDLE,
    }
)

# Phase → approximate progress percentage mapping.
PHASE_PROGRESS = {
    IngestPhase.IDLE: 0,
    IngestPhase.INITIALIZING: 5,
    IngestPhase.STAGING_SOURCES: 15,
    IngestPhase.ANALYZING: 40,
    IngestPhase.APPLYING: 70,
    IngestPhase.REVIEWING: 80,
    IngestPhase.REFRESHING_INDEX: 90,
    IngestPhase.READY: 100,
    IngestPhase.ERROR: -1,
    IngestPhase.CANCELLED: -1,
}


@dataclass
class IngestProgress:
    """Thread-safe progress tracker for a single wiki ingest operation."""

    thread_id: str
    phase: IngestPhase = IngestPhase.IDLE
    progress: int = 0
    detail: str = ""
    source_count: int = 0
    sources_processed: int = 0
    source_names: list[str] = field(default_factory=list)
    current_source: str = ""
    error: str | None = None
    review_report: ReviewReport | None = None
    retry_count: int = 0
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    phase_started_at: str | None = None

    def advance(
        self, phase: IngestPhase, detail: str = "", *, extra_progress: int = 0
    ) -> None:
        """Advance to a new phase, updating progress percentage."""
        self.phase = phase
        self.detail = detail
        self.phase_started_at = datetime.now(UTC).isoformat()
        base = PHASE_PROGRESS.get(phase, 0)
        self.progress = max(0, min(100, base + extra_progress))

    def mark_complete(self, detail: str = "Ingest completed successfully.") -> None:
        """Mark the ingest as successfully finished."""
        self.phase = IngestPhase.READY
        self.progress = 100
        self.detail = detail
        self.completed_at = datetime.now(UTC).isoformat()
        self.phase_started_at = self.phase_started_at or self.started_at

    def mark_error(self, error: str) -> None:
        """Mark the ingest as failed."""
        self.phase = IngestPhase.ERROR
        self.progress = -1
        self.detail = ""
        self.error = error
        self.completed_at = datetime.now(UTC).isoformat()

    def mark_cancelled(self, reason: str = "Cancelled by client.") -> None:
        """Mark the ingest as cancelled."""
        self.phase = IngestPhase.CANCELLED
        self.progress = -1
        self.detail = reason
        self.completed_at = datetime.now(UTC).isoformat()

    def is_active(self) -> bool:
        """Return True if the ingest is still running."""
        return self.phase in ACTIVE_PHASES

    def is_terminal(self) -> bool:
        """Return True if the ingest has reached a terminal state."""
        return self.phase in TERMINAL_PHASES

    def _compute_elapsed_eta(self) -> tuple[float | None, float | None]:
        """Compute elapsed seconds since ingest start.

        Returns (elapsed_seconds, None).  ETA is always None — phase-based
        progress percentages are milestones, not linear work completion, so
        linear extrapolation gives misleading estimates.
        """
        elapsed: float | None = None
        if self.started_at:
            try:
                start = datetime.fromisoformat(self.started_at)
                elapsed = (datetime.now(UTC) - start).total_seconds()
            except (ValueError, TypeError):
                pass
        return elapsed, None

    def to_dict(self) -> dict:
        """Serialize progress state for API responses."""
        elapsed, eta = self._compute_elapsed_eta()
        result: dict = {
            "thread_id": self.thread_id,
            "phase": self.phase.value,
            "progress": self.progress,
            "detail": self.detail,
            "source_count": self.source_count,
            "sources_processed": self.sources_processed,
            "source_names": self.source_names,
            "current_source": self.current_source,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "is_active": self.is_active(),
            "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
            "eta_seconds": round(eta, 1) if eta is not None else None,
        }
        # Include review report when available (for SSE and polling consumers).
        if self.review_report is not None:
            result["review_report"] = {
                "missing_pages": [
                    {
                        "item_type": ri.item_type,
                        "title": ri.title,
                        "description": ri.description,
                        "suggested_action": ri.suggested_action,
                        "search_query": ri.search_query,
                    }
                    for ri in self.review_report.missing_pages
                ],
                "duplicate_suggestions": [
                    {
                        "item_type": ri.item_type,
                        "title": ri.title,
                        "description": ri.description,
                        "suggested_action": ri.suggested_action,
                        "search_query": ri.search_query,
                    }
                    for ri in self.review_report.duplicate_suggestions
                ],
                "research_questions": [
                    {
                        "item_type": ri.item_type,
                        "title": ri.title,
                        "description": ri.description,
                        "suggested_action": ri.suggested_action,
                        "search_query": ri.search_query,
                    }
                    for ri in self.review_report.research_questions
                ],
                "gaps": [
                    {
                        "item_type": ri.item_type,
                        "title": ri.title,
                        "description": ri.description,
                        "suggested_action": ri.suggested_action,
                        "search_query": ri.search_query,
                    }
                    for ri in self.review_report.gaps
                ],
                "total_items": self.review_report.total_items,
                "is_empty": self.review_report.is_empty,
            }
        return result


@dataclass(frozen=True)
class WikiQueryRequest:
    """Request payload for a wiki query operation."""

    question: str
    thread_id: str
    file_results: bool = True


@dataclass(frozen=True)
class SourceCitation:
    """A single source reference parsed from an answer.

    Citation kinds:
    - ``raw``: an uploaded document raw path, e.g. ``/raw/report.pdf.md``.
      ``page`` carries the PDF page number when derivable.
    - ``web``: a web URL, with ``url`` set and ``locator`` holding the title.
    - ``section``: a ``file#Heading`` reference, with ``locator`` holding the
      heading text.
    """

    kind: str = "raw"
    raw_path: str | None = None
    page: int | None = None
    locator: str | None = None
    url: str | None = None


def _resolve_wiki_base_dir(fallback_dir: Path) -> Path:
    """Return the effective base directory for wiki/docs path resolution.

    Checks (in order):
    1. ``WIKI_BASE_DIR`` env var — explicit override for the wiki base directory.
    2. ``DOC_FOLDER`` env var — the docs folder used by the research agent;
       walks up until we find the directory that *contains* ``docs/``, so that
       ``docs/threads-wiki/`` is resolved at the same level as ``docs/threads/``.
    3. *fallback_dir* — the caller's default (usually ``Path(__file__).resolve().parent``).
    """
    import os as _os

    wiki_base = _os.environ.get("WIKI_BASE_DIR")
    if wiki_base:
        return Path(wiki_base)

    doc_folder = _os.environ.get("DOC_FOLDER")
    if doc_folder:
        doc_path = Path(doc_folder).resolve()
        # Walk up from DOC_FOLDER until we find the parent of a "docs"
        # directory, so that docs/threads-wiki/ resolves at the same level
        # as docs/threads/.  This handles cases where DOC_FOLDER points
        # deep into docs/threads/<thread_id>/... without double-nesting.
        for parent in doc_path.parents:
            if parent.name == "docs":
                # parent is .../docs — go one level up to the project root
                return parent.parent
        # Fallback: if no "docs" ancestor found, use the immediate parent.
        return doc_path.parent

    return fallback_dir


@dataclass(frozen=True)
class ThreadWikiPaths:
    """Resolved filesystem paths for a thread's wiki workspace."""

    thread_id: str
    docs_dir: Path  # ./docs/threads/<thread-id>/
    wiki_dir: Path  # ./docs/threads-wiki/<thread-id>/
    raw_dir: Path  # ./docs/threads-wiki/<thread-id>/raw/
    wiki_content: Path  # ./docs/threads-wiki/<thread-id>/wiki/

    @classmethod
    def resolve(
        cls, thread_id: str, base_dir: Path, *, docs_base: Path | None = None
    ) -> ThreadWikiPaths:
        """Resolve paths for a given thread ID relative to a base directory.

        If *docs_base* is provided, it is used as the parent of the ``docs/``
        and ``docs/threads-wiki/`` directories instead of *base_dir*.
        """
        effective = docs_base if docs_base is not None else base_dir
        docs_dir = effective / "docs" / "threads" / thread_id
        wiki_dir = effective / "docs" / "threads-wiki" / thread_id
        return cls(
            thread_id=thread_id,
            docs_dir=docs_dir,
            wiki_dir=wiki_dir,
            raw_dir=wiki_dir / "raw",
            wiki_content=wiki_dir / "wiki",
        )


# ── Wiki Page Metadata (YAML frontmatter) ──────────────────────────────────────

# Valid page categories matching the structured subdirectory layout.
WIKI_PAGE_CATEGORIES = frozenset(
    {
        "entity",
        "concept",
        "source",
        "comparison",
        "synthesis",
        "query",
        "uncategorized",
    }
)

# Directory name → category mapping.
CATEGORY_DIRECTORIES: dict[str, str] = {
    "entities": "entity",
    "concepts": "concept",
    "sources": "source",
    "comparisons": "comparison",
    "synthesis": "synthesis",
    "query": "query",
}


@dataclass
class WikiPageMetadata:
    """YAML frontmatter fields for a wiki page."""

    title: str
    category: str = "uncategorized"
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    updated: str = ""  # ISO-8601 date string

    def to_frontmatter(self) -> str:
        """Serialize metadata as a YAML frontmatter block."""
        import yaml

        data: dict[str, object] = {
            "title": self.title,
            "category": self.category,
        }
        if self.summary:
            data["summary"] = self.summary
        if self.tags:
            data["tags"] = self.tags
        if self.sources:
            data["sources"] = self.sources
        if self.updated:
            data["updated"] = self.updated
        else:
            data["updated"] = datetime.now(UTC).strftime("%Y-%m-%d")

        yaml_str = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False
        ).strip()
        return f"---\n{yaml_str}\n---\n"


def parse_frontmatter(content: str) -> tuple[WikiPageMetadata, str]:
    """Parse YAML frontmatter from markdown content.

    Returns a ``(metadata, body)`` tuple.  If no valid frontmatter is found,
    metadata fields are populated with sensible defaults (title from first
    heading, category ``"uncategorized"``).
    """
    import yaml

    metadata = WikiPageMetadata(title="", category="uncategorized")
    body = content

    if not content.startswith("---"):
        # No frontmatter — derive title from first heading.
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                metadata.title = stripped.lstrip("# ").strip()
                break
        return metadata, body

    # Find closing `---`.
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return metadata, body

    yaml_str = content[3:end_idx].strip()
    body = content[end_idx + 4 :].lstrip("\n")

    try:
        frontmatter = yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        logger.debug("Failed to parse YAML frontmatter; falling back to defaults.")
        return metadata, body

    if not isinstance(frontmatter, dict):
        return metadata, body

    metadata.title = str(frontmatter.get("title", metadata.title))
    category = str(frontmatter.get("category", "uncategorized")).lower()
    metadata.category = (
        category if category in WIKI_PAGE_CATEGORIES else "uncategorized"
    )
    metadata.summary = str(frontmatter.get("summary", ""))
    tags = frontmatter.get("tags", [])
    if isinstance(tags, list):
        metadata.tags = [str(t) for t in tags]
    sources = frontmatter.get("sources", [])
    if isinstance(sources, list):
        metadata.sources = [str(s) for s in sources]
    metadata.updated = str(frontmatter.get("updated", ""))

    return metadata, body


# ── Contradiction Tracking ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Contradiction:
    """A tracked contradiction between two or more source claims."""

    wiki_page: str  # Relative path of the page documenting the contradiction
    claim_a: str
    source_a: str
    claim_b: str
    source_b: str
    resolved: bool = False
    resolution_note: str = ""


@dataclass
class ReviewItem:
    """A single curation signal flagged for human review after ingest."""

    item_type: str  # "missing_page", "duplicate", "suggestion", "research_question"
    title: str
    description: str
    suggested_action: str = ""
    search_query: str = ""  # Pre-generated search query for investigation


@dataclass
class ReviewReport:
    """Post-ingest review report with human-in-the-loop curation signals.

    Produced by a read-only LLM pass after the apply phase.  Flags items
    the LLM could not resolve automatically: missing canonical pages,
    potential duplicates, research directions, and knowledge gaps.
    """

    missing_pages: list[ReviewItem] = field(default_factory=list)
    duplicate_suggestions: list[ReviewItem] = field(default_factory=list)
    research_questions: list[ReviewItem] = field(default_factory=list)
    gaps: list[ReviewItem] = field(default_factory=list)

    @property
    def total_items(self) -> int:
        """Total number of curation items across all categories."""
        return (
            len(self.missing_pages)
            + len(self.duplicate_suggestions)
            + len(self.research_questions)
            + len(self.gaps)
        )

    @property
    def is_empty(self) -> bool:
        """True if the review report contains no curation items."""
        return self.total_items == 0


@dataclass
class SemanticLintFinding:
    """A finding from the read-only semantic contradiction detection pass.

    These are produced by a dedicated LLM scan that systematically compares
    wiki pages for factual conflicts, stale claims, and inconsistencies.
    The findings feed into the mutating lint pass for repair.
    """

    page_a: str  # Relative wiki path of first page
    page_b: str  # Relative wiki path of second page (or "self" for internal)
    claim_a: str
    claim_b: str
    severity: str = "medium"  # "high", "medium", "low"
    finding_type: str = "contradiction"  # contradiction, stale_claim, inconsistency
    resolution_hint: str = ""


@dataclass(frozen=True)
class WikiQueryResult:
    """Result from a wiki query operation."""

    answer: str
    filed_path: str | None = None
    sources_cited: list[SourceCitation] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)


# ── Graph Analysis (Phase 3) ──────────────────────────────────────────────────


@dataclass
class CommunityInfo:
    """A knowledge cluster discovered via Louvain community detection."""

    id: int
    pages: list[str]  # Relative wiki paths of pages in this community
    cohesion: float  # Internal edge density (0.0–1.0), higher = tighter
    size: int = 0

    def __post_init__(self):
        """Set size from pages list if not explicitly provided."""
        if self.size == 0:
            self.size = len(self.pages)

    @property
    def is_sparse(self) -> bool:
        """Communities with cohesion < 0.15 are flagged as sparse/gaps."""
        return self.cohesion < 0.15


@dataclass
class RelevanceEdge:
    """A weighted relationship between two wiki pages."""

    source_page: str
    target_page: str
    direct_links: float = 0.0  # Weight 3.0x
    source_overlap: float = 0.0  # Weight 4.0x
    common_neighbors: float = 0.0  # Weight 1.5x (Adamic-Adar)
    type_affinity: float = 0.0  # Weight 1.0x
    total_score: float = 0.0

    # Signal weights from LLM Wiki's 4-signal relevance model.
    WEIGHTS: dict[str, float] = field(
        default_factory=lambda: {
            "direct_links": 3.0,
            "source_overlap": 4.0,
            "common_neighbors": 1.5,
            "type_affinity": 1.0,
        },
        init=False,
        repr=False,
    )

    def __post_init__(self):
        """Compute total_score from individual signal weights if not provided."""
        if self.total_score == 0.0:
            w = self.WEIGHTS
            self.total_score = (
                self.direct_links * w["direct_links"]
                + self.source_overlap * w["source_overlap"]
                + self.common_neighbors * w["common_neighbors"]
                + self.type_affinity * w["type_affinity"]
            )


@dataclass
class GraphInsight:
    """A structured discovery signal from the knowledge graph."""

    insight_type: str  # surprising_connection, gap, bridge, peripheral_to_hub
    pages: list[str]
    description: str
    suggested_action: str = ""
    score: float = 0.0  # Relevance or severity score
