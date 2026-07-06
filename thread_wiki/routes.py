"""FastAPI routes for thread-level wiki management.

Provides endpoints for:
- Triggering wiki ingest (background with progress tracking)
- Querying ingest progress (polling + SSE streaming)
- Cancelling an in-progress ingest
- Querying the wiki knowledge base
- Running lint reconciliation after document deletions
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from langgraph_sdk.auth.types import MinimalUserDict
from pydantic import BaseModel, Field

from research_agent.utils.knowledge_filesystem import clear_thread_cache

from . import progress as progress_tracker
from .models import ThreadWikiPaths, WikiQueryResult
from .service import run_ingest, run_lint, run_query

logger = logging.getLogger(__name__)

# ── Router setup ──────────────────────────────────────────────────────────────

router = APIRouter(tags=["Wiki"])

# Base directory for resolving thread paths.
# This is set once at import time relative to this file's parent (deep_research/).
_BASE_DIR = Path(__file__).resolve().parent.parent


def _sse_frame(event: str, data: Any, event_id: int | None = None) -> str:
    """Build one SSE frame."""
    payload = json.dumps(data, default=str)
    id_part = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_part}event: {event}\ndata: {payload}\n\n"


# ── Pydantic models ───────────────────────────────────────────────────────────


class WikiIngestRequest(BaseModel):
    """Request body for triggering wiki ingest."""

    note: str | None = None
    topic: str | None = None


class WikiQueryRequestModel(BaseModel):
    """Request body for querying the wiki."""

    question: str
    file_results: bool = True


class WikiLintRequest(BaseModel):
    """Request body for running wiki lint."""

    note: str | None = None
    topic: str | None = None


class WikiIngestResponse(BaseModel):
    """Response from triggering wiki ingest."""

    thread_id: str
    status: str
    message: str


class ReviewItemOut(BaseModel):
    """A single curation signal from the post-ingest review."""

    item_type: str
    title: str
    description: str
    suggested_action: str = ""
    search_query: str = ""


class ReviewReportOut(BaseModel):
    """Post-ingest review report with human-in-the-loop curation signals."""

    missing_pages: list[ReviewItemOut] = []
    duplicate_suggestions: list[ReviewItemOut] = []
    research_questions: list[ReviewItemOut] = []
    gaps: list[ReviewItemOut] = []
    total_items: int = 0
    is_empty: bool = True


class WikiStatusResponse(BaseModel):
    """Response for wiki ingest status."""

    thread_id: str
    phase: str
    progress: int
    detail: str
    source_count: int
    sources_processed: int
    error: str | None
    started_at: str | None
    completed_at: str | None
    is_active: bool
    wiki_ready: bool
    review_report: ReviewReportOut | None = None


class SourceCitationOut(BaseModel):
    """A single structured source citation parsed from an answer."""

    kind: str = "raw"
    raw_path: str | None = None
    page: int | None = None
    locator: str | None = None
    url: str | None = None


class WikiQueryResponse(BaseModel):
    """Response from a wiki query."""

    answer: str
    filed_path: str | None = None
    sources_cited: list[SourceCitationOut] = Field(default_factory=list)


class WikiLintResponse(BaseModel):
    """Response from a wiki lint operation."""

    result: str
    topic: str


class GraphInsightOut(BaseModel):
    """A single knowledge graph insight."""

    insight_type: str
    pages: list[str]
    description: str
    suggested_action: str = ""
    score: float = 0.0


class GraphInsightsResponse(BaseModel):
    """Response from the graph insights endpoint."""

    thread_id: str
    total_pages: int
    total_links: int
    communities: list[dict] = []
    insights: list[GraphInsightOut] = []
    hubs: list[dict] = []
    sinks: list[str] = []
    orphans: list[str] = []


# ── Auth dependency (self-contained to avoid circular import) ──


def _to_review_item_out(item) -> ReviewItemOut:
    """Convert a ReviewItem domain model to a ReviewItemOut response model."""
    return ReviewItemOut(
        item_type=item.item_type,
        title=item.title,
        description=item.description,
        suggested_action=item.suggested_action,
        search_query=item.search_query,
    )


async def _wiki_get_current_user(request: Request) -> MinimalUserDict:
    """Authenticate wiki routes using the same auth pattern as auth.py.

    Delegates to the auth module at request time (not import time)
    to avoid circular imports.
    """
    import server as _server

    return await _server.get_current_user(request)


# ── Helper ────────────────────────────────────────────────────────────────────


def _resolve_paths(thread_id: str) -> ThreadWikiPaths:
    """Resolve wiki paths for a thread, validating the docs directory exists."""
    paths = ThreadWikiPaths.resolve(thread_id, _BASE_DIR)
    if not paths.docs_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No documents found for thread '{thread_id}'. Upload documents first.",
        )
    return paths


def _topic_from_thread(thread_id: str, override: str | None = None) -> str:
    """Derive a topic label from the thread ID or use the override."""
    if override:
        return override
    # Use a short form of the thread_id as the topic label.
    short = thread_id[:8]
    return f"Thread {short}"


def _wiki_is_ready(paths: ThreadWikiPaths) -> bool:
    """Check if the wiki has been initialized and has content."""
    index_path = paths.wiki_content / "index.md"
    if not index_path.exists():
        return False
    content = index_path.read_text(encoding="utf-8")
    return "_No pages yet._" not in content


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post(
    "/threads/{thread_id}/wiki/ingest",
    response_model=WikiIngestResponse,
)
async def trigger_wiki_ingest(
    thread_id: str,
    body: WikiIngestRequest = WikiIngestRequest(),
    current_user=Depends(_wiki_get_current_user),
) -> WikiIngestResponse:
    """Trigger wiki ingest for a thread's uploaded documents.

    If an ingest is already running, it will be cancelled and replaced.
    The ingest runs as a background task; poll `/wiki/status` or stream
    `/wiki/progress` for real-time updates.
    """
    paths = _resolve_paths(thread_id)
    topic = _topic_from_thread(thread_id, body.topic)

    # Cancel any existing ingest for this thread.
    await progress_tracker.cancel_ingest(
        thread_id, reason="Replaced by new ingest request."
    )

    # Pre-create a placeholder task to register progress first, then replace
    # it with the real task. This avoids a race where the background function
    # looks up the registry before registration completes.
    placeholder_task: asyncio.Task = asyncio.create_task(asyncio.sleep(0))
    prog = await progress_tracker.register_ingest(
        thread_id, placeholder_task, wiki_dir=paths.wiki_dir
    )

    # Now create the real background task with the registered progress/cancel.
    cancel_event = progress_tracker._active_ingests[thread_id].cancel_event
    task = asyncio.create_task(
        _run_ingest_background(paths, topic, body.note, prog, cancel_event),
        name=f"wiki-ingest-{thread_id}",
    )

    # Update the registry entry with the real task.
    progress_tracker._active_ingests[thread_id] = progress_tracker._IngestEntry(
        progress=prog,
        task=task,
        cancel_event=cancel_event,
    )
    prog.advance(prog.phase, "Ingest queued.")

    return WikiIngestResponse(
        thread_id=thread_id,
        status="started",
        message="Wiki ingest started. Poll /wiki/status or stream /wiki/progress for updates.",
    )


async def _run_ingest_background(
    paths: ThreadWikiPaths,
    topic: str,
    note: str | None,
    progress_obj,
    cancel_event: asyncio.Event,
) -> None:
    """Background ingest worker with directly injected progress and cancel objects."""
    try:
        await run_ingest(paths, topic, progress_obj, cancel_event, note=note)
    except asyncio.CancelledError:
        logger.info("Ingest cancelled for thread %s", paths.thread_id)
    except Exception:
        logger.exception("Ingest failed for thread %s", paths.thread_id)
    finally:
        await progress_tracker.cleanup_terminal(paths.thread_id)


@router.get(
    "/threads/{thread_id}/wiki/status",
    response_model=WikiStatusResponse,
)
async def get_wiki_status(
    thread_id: str,
    current_user=Depends(_wiki_get_current_user),
) -> WikiStatusResponse:
    """Get current wiki ingest status and progress for a thread."""
    paths = ThreadWikiPaths.resolve(thread_id, _BASE_DIR)
    prog = await progress_tracker.get_progress(thread_id)

    if prog is None:
        # No active ingest — check if wiki is already built.
        ready = _wiki_is_ready(paths)
        return WikiStatusResponse(
            thread_id=thread_id,
            phase="ready" if ready else "idle",
            progress=100 if ready else 0,
            detail="Wiki is ready." if ready else "No ingest has been run yet.",
            source_count=0,
            sources_processed=0,
            error=None,
            started_at=None,
            completed_at=None,
            is_active=False,
            wiki_ready=ready,
        )

    review_out = None
    if prog.review_report is not None:
        review_out = ReviewReportOut(
            missing_pages=[
                _to_review_item_out(ri) for ri in prog.review_report.missing_pages
            ],
            duplicate_suggestions=[
                _to_review_item_out(ri)
                for ri in prog.review_report.duplicate_suggestions
            ],
            research_questions=[
                _to_review_item_out(ri) for ri in prog.review_report.research_questions
            ],
            gaps=[_to_review_item_out(ri) for ri in prog.review_report.gaps],
            total_items=prog.review_report.total_items,
            is_empty=prog.review_report.is_empty,
        )
    return WikiStatusResponse(
        thread_id=thread_id,
        phase=prog.phase.value,
        progress=prog.progress,
        detail=prog.detail,
        source_count=prog.source_count,
        sources_processed=prog.sources_processed,
        error=prog.error,
        started_at=prog.started_at,
        completed_at=prog.completed_at,
        is_active=prog.is_active(),
        wiki_ready=_wiki_is_ready(paths),
        review_report=review_out,
    )


@router.get("/threads/{thread_id}/wiki/progress")
async def stream_wiki_progress(
    thread_id: str,
    current_user=Depends(_wiki_get_current_user),
):
    """SSE stream for real-time ingest progress updates.

    The frontend can connect to this endpoint and receive progress events
    as the ingest proceeds. The stream ends when the ingest reaches a
    terminal state (ready, error, or cancelled).
    """

    async def event_stream():
        import time as _time

        seq = 0
        last_phase = None
        last_progress = None
        last_emit_time = _time.monotonic()
        _HEARTBEAT_INTERVAL = 15.0  # seconds between heartbeat pings

        while True:
            prog = await progress_tracker.get_progress(thread_id)

            if prog is None:
                # No active ingest.
                paths = ThreadWikiPaths.resolve(thread_id, _BASE_DIR)
                ready = _wiki_is_ready(paths)
                yield _sse_frame(
                    "end",
                    {
                        "thread_id": thread_id,
                        "phase": "ready" if ready else "idle",
                        "progress": 100 if ready else 0,
                        "wiki_ready": ready,
                    },
                    event_id=seq,
                )
                return

            emitted = False

            # Emit on phase change.
            if prog.phase != last_phase:
                yield _sse_frame("progress", prog.to_dict(), event_id=seq)
                seq += 1
                last_phase = prog.phase
                last_emit_time = _time.monotonic()
                emitted = True

            # Emit on progress percentage change.
            if prog.progress != last_progress:
                yield _sse_frame("progress", prog.to_dict(), event_id=seq)
                seq += 1
                last_progress = prog.progress
                last_emit_time = _time.monotonic()
                emitted = True

            # Terminal state → emit end and close stream.
            if prog.is_terminal():
                paths = ThreadWikiPaths.resolve(thread_id, _BASE_DIR)
                yield _sse_frame(
                    "end",
                    {
                        **prog.to_dict(),
                        "wiki_ready": _wiki_is_ready(paths),
                    },
                    event_id=seq,
                )
                return

            # Send heartbeat if no event was emitted for a while.
            if not emitted and (
                _time.monotonic() - last_emit_time >= _HEARTBEAT_INTERVAL
            ):
                yield _sse_frame(
                    "heartbeat",
                    {
                        "thread_id": thread_id,
                        "phase": prog.phase.value,
                        "progress": prog.progress,
                    },
                    event_id=seq,
                )
                seq += 1
                last_emit_time = _time.monotonic()

            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post(
    "/threads/{thread_id}/wiki/ingest/cancel",
)
async def cancel_wiki_ingest(
    thread_id: str,
    current_user=Depends(_wiki_get_current_user),
) -> dict[str, Any]:
    """Cancel an in-progress wiki ingest for a thread.

    Returns immediately. The background task will stop at the next
    phase checkpoint.
    """
    cancelled = await progress_tracker.cancel_ingest(
        thread_id, reason="Cancelled by client request."
    )
    return {
        "thread_id": thread_id,
        "cancelled": cancelled,
        "message": "Ingest cancelled." if cancelled else "No active ingest to cancel.",
    }


@router.post(
    "/threads/{thread_id}/wiki/query",
    response_model=WikiQueryResponse,
)
async def llm_wiki_query(
    thread_id: str,
    body: WikiQueryRequestModel,
    current_user=Depends(_wiki_get_current_user),
) -> WikiQueryResponse:
    """Query the thread's wiki knowledge base.

    Returns a grounded answer with citations from the ingested documents.
    If the answer has durable value, it will be filed into the wiki for
    future reference.
    """
    paths = _resolve_paths(thread_id)

    # Check that wiki is ready before allowing queries.
    if not _wiki_is_ready(paths):
        raise HTTPException(
            status_code=409,
            detail=(
                "Wiki is not ready yet. Run ingest first or wait for the "
                "current ingest to complete."
            ),
        )

    topic = _topic_from_thread(thread_id)
    result: WikiQueryResult = await run_query(
        paths, topic, body.question, file_results=body.file_results
    )

    return WikiQueryResponse(
        answer=result.answer,
        filed_path=result.filed_path,
        sources_cited=[
            SourceCitationOut(
                kind=c.kind,
                raw_path=c.raw_path,
                page=c.page,
                locator=c.locator,
                url=c.url,
            )
            for c in result.sources_cited
        ],
    )


@router.post(
    "/threads/{thread_id}/wiki/lint",
    response_model=WikiLintResponse,
)
async def lint_wiki(
    thread_id: str,
    body: WikiLintRequest = WikiLintRequest(),
    current_user=Depends(_wiki_get_current_user),
) -> WikiLintResponse:
    """Run lint reconciliation on the thread's wiki.

    Use this after document deletions to reconcile stale references,
    remove orphan pages, and refresh cross-links.
    """
    paths = _resolve_paths(thread_id)

    if not paths.wiki_dir.exists():
        raise HTTPException(
            status_code=409,
            detail="Wiki has not been initialized. Run ingest first.",
        )

    topic = _topic_from_thread(thread_id, body.topic)
    result = await run_lint(paths, topic, note=body.note)

    return WikiLintResponse(result=result, topic=topic)


@router.get(
    "/threads/{thread_id}/wiki/insights",
    response_model=GraphInsightsResponse,
)
async def get_wiki_insights(
    thread_id: str,
    current_user=Depends(_wiki_get_current_user),
) -> GraphInsightsResponse:
    """Get knowledge graph insights for a thread's wiki.

    Runs community detection, relevance scoring, and graph analysis to
    surface surprising connections, knowledge gaps, bridge nodes, and
    missing cross-references.
    """
    from thread_wiki.service import _analyze_graph

    paths = _resolve_paths(thread_id)

    if not paths.wiki_dir.exists():
        raise HTTPException(
            status_code=409,
            detail="Wiki has not been initialized. Run ingest first.",
        )

    graph_report = await asyncio.to_thread(_analyze_graph, paths.wiki_dir)

    # Convert CommunityInfo objects to dicts for the response.
    communities_out: list[dict] = []
    for c in graph_report.get("communities", []):
        communities_out.append(
            {
                "id": c.id,
                "pages": c.pages[:10],  # Cap at 10 pages per community
                "cohesion": c.cohesion,
                "size": c.size,
                "is_sparse": c.is_sparse,
            }
        )

    # Convert GraphInsight objects.
    insights_out: list[GraphInsightOut] = []
    for ins in graph_report.get("graph_insights", []):
        insights_out.append(
            GraphInsightOut(
                insight_type=ins.insight_type,
                pages=ins.pages,
                description=ins.description,
                suggested_action=ins.suggested_action,
                score=ins.score,
            )
        )

    return GraphInsightsResponse(
        thread_id=thread_id,
        total_pages=graph_report["total_pages"],
        total_links=graph_report["total_links"],
        communities=communities_out,
        insights=insights_out,
        hubs=graph_report.get("hubs", []),
        sinks=graph_report.get("sinks", []),
        orphans=graph_report.get("orphans", []),
    )


class GraphNodeOut(BaseModel):
    """A node in the wiki knowledge graph."""

    id: str
    title: str
    category: str = "uncategorized"
    tags: list[str] = []
    community_id: int | None = None


class GraphEdgeOut(BaseModel):
    """An edge (link) in the wiki knowledge graph."""

    source: str
    target: str
    weight: float = 1.0


class CommunityOut(BaseModel):
    """A knowledge community."""

    id: int
    cohesion: float
    size: int


class WikiGraphResponse(BaseModel):
    """Response from the wiki graph endpoint."""

    thread_id: str
    nodes: list[GraphNodeOut] = []
    edges: list[GraphEdgeOut] = []
    communities: list[CommunityOut] = []
    total_pages: int = 0
    total_links: int = 0


@router.get(
    "/threads/{thread_id}/wiki/graph",
    response_model=WikiGraphResponse,
)
async def get_wiki_graph(
    thread_id: str,
    current_user=Depends(_wiki_get_current_user),
) -> WikiGraphResponse:
    """Get the wiki knowledge graph as nodes and edges for visualization.

    Returns page nodes with frontmatter metadata (title, category, tags),
    wikilink edges between pages, and Louvain community assignments.
    """
    from thread_wiki.service import _build_graph_payload

    paths = _resolve_paths(thread_id)

    if not paths.wiki_dir.exists():
        raise HTTPException(
            status_code=409,
            detail="Wiki has not been initialized. Run ingest first.",
        )

    payload = await asyncio.to_thread(_build_graph_payload, paths.wiki_dir)

    return WikiGraphResponse(
        thread_id=thread_id,
        nodes=[GraphNodeOut(**n) for n in payload["nodes"]],
        edges=[GraphEdgeOut(**e) for e in payload["edges"]],
        communities=[CommunityOut(**c) for c in payload["communities"]],
        total_pages=len(payload["nodes"]),
        total_links=len(payload["edges"]),
    )


@router.delete(
    "/threads/{thread_id}/wiki",
)
async def delete_thread_wiki(
    thread_id: str,
    current_user=Depends(_wiki_get_current_user),
) -> dict[str, Any]:
    """Delete a thread's LLM wiki workspace and uploaded documents."""
    # 1. Cancel any active ingest for this thread.
    await progress_tracker.cancel_ingest(
        thread_id, reason="Thread wiki is being deleted."
    )

    # 2. Resolve paths for the thread.
    paths = ThreadWikiPaths.resolve(thread_id, _BASE_DIR)

    # 3. Clean up uploaded documents directory if it exists.
    if paths.docs_dir.exists() and paths.docs_dir.is_dir():
        await asyncio.to_thread(
            shutil.rmtree, str(paths.docs_dir), ignore_errors=True, onerror=None
        )
        logger.info("Deleted documents folder for thread %s", thread_id)

    # 4. Clean up wiki directory if it exists.
    if paths.wiki_dir.exists() and paths.wiki_dir.is_dir():
        await asyncio.to_thread(
            shutil.rmtree, str(paths.wiki_dir), ignore_errors=True, onerror=None
        )
        logger.info("Deleted wiki folder for thread %s", thread_id)

    clear_thread_cache(thread_id)

    return {
        "thread_id": thread_id,
        "message": "Thread wiki and documents deleted successfully.",
    }


def _build_directory_tree(root_dir: Path, current_dir: Path) -> dict[str, Any]:
    """Recursively build a directory tree dict."""
    try:
        relative_path = str(current_dir.relative_to(root_dir))
    except ValueError:
        relative_path = ""
    if relative_path == ".":
        relative_path = ""

    children = []
    if current_dir.exists() and current_dir.is_dir():
        items = sorted(
            list(current_dir.iterdir()), key=lambda p: (not p.is_dir(), p.name.lower())
        )
        for item in items:
            if item.name.startswith("."):
                continue
            item_rel = str(item.relative_to(root_dir))
            if item.is_dir():
                children.append(_build_directory_tree(root_dir, item))
            else:
                children.append(
                    {
                        "name": item.name,
                        "path": item_rel,
                        "type": "file",
                        "size": item.stat().st_size if item.exists() else 0,
                    }
                )

    return {
        "name": current_dir.name if relative_path != "" else "threads-wiki",
        "path": relative_path,
        "type": "directory",
        "children": children,
    }


def _count_files(node: dict[str, Any]) -> int:
    if node.get("type") == "file":
        return 1
    count = 0
    for child in node.get("children", []):
        count += _count_files(child)
    return count


@router.get(
    "/threads/{thread_id}/wiki/tree",
)
async def get_thread_wiki_tree(
    thread_id: str,
    current_user=Depends(_wiki_get_current_user),
) -> dict[str, Any]:
    """Get the full directory tree structure for a thread's wiki workspace."""
    paths = ThreadWikiPaths.resolve(thread_id, _BASE_DIR)
    if not paths.wiki_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Wiki directory does not exist for thread '{thread_id}'. Run ingest first.",
        )

    tree = await asyncio.to_thread(
        _build_directory_tree, paths.wiki_dir, paths.wiki_dir
    )
    file_count = _count_files(tree)
    return {
        "thread_id": thread_id,
        "tree": tree,
        "file_count": file_count,
    }


@router.get(
    "/threads/{thread_id}/wiki/file",
)
async def get_thread_wiki_file(
    thread_id: str,
    path: str = Query(
        ..., description="Relative file path inside threads-wiki/<thread_id>"
    ),
    current_user=Depends(_wiki_get_current_user),
) -> dict[str, Any]:
    """Read and return the text content of a specific file in the thread's wiki workspace."""
    paths = ThreadWikiPaths.resolve(thread_id, _BASE_DIR)
    if not paths.wiki_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Wiki directory does not exist for thread '{thread_id}'.",
        )

    # Validate safe relative path
    rel_path = PurePosixPath(path.replace("\\", "/").strip().strip("/"))
    if any(part in {"", ".", ".."} for part in rel_path.parts):
        raise HTTPException(
            status_code=400,
            detail="Invalid relative file path.",
        )

    target_file = paths.wiki_dir.joinpath(*rel_path.parts)
    try:
        target_file.relative_to(paths.wiki_dir)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Path outside wiki directory.",
        )

    if not (await asyncio.to_thread(target_file.exists)) or not (
        await asyncio.to_thread(target_file.is_file)
    ):
        raise HTTPException(
            status_code=404,
            detail=f"File '{path}' not found in thread wiki workspace.",
        )

    size = await asyncio.to_thread(lambda: target_file.stat().st_size)

    if target_file.suffix.lower() in {".pkl"} or (
        len(rel_path.parts) > 0 and rel_path.parts[0] == "index"
    ):
        return {
            "thread_id": thread_id,
            "path": str(rel_path),
            "name": target_file.name,
            "size": size,
            "content": "Content view is unavailable",
        }

    def _read_file():
        return target_file.read_text(encoding="utf-8", errors="replace")

    content = await asyncio.to_thread(_read_file)

    return {
        "thread_id": thread_id,
        "path": str(rel_path),
        "name": target_file.name,
        "size": size,
        "content": content,
    }
