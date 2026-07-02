"""Event-driven background hooks for automatic thread wiki management.

Launches fire-and-forget asynchronous tasks to auto-ingest newly uploaded documents
or run database lint reconciliation sweeps after document delete events.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Auto-ingest after upload ──────────────────────────────────────────────────


async def trigger_wiki_auto_ingest(thread_id: str) -> None:
    """Register progress and launch a background wiki-ingest task."""
    try:
        from thread_wiki import progress as wiki_progress
        from thread_wiki.models import ThreadWikiPaths, _resolve_wiki_base_dir

        # Path.resolve() calls os.path.realpath → os.getcwd (blocking I/O).
        # Wrap the entire resolution in a thread to avoid blocking the event loop.
        def _resolve_paths() -> ThreadWikiPaths:
            base_dir = _resolve_wiki_base_dir(Path(__file__).resolve().parent.parent)
            return ThreadWikiPaths.resolve(thread_id, base_dir)

        paths = await asyncio.to_thread(_resolve_paths)
        topic = f"Thread {thread_id[:8]}"

        # Register progress with a placeholder task first.
        placeholder = asyncio.create_task(asyncio.sleep(0))
        prog = await wiki_progress.register_ingest(thread_id, placeholder)
        cancel_event = wiki_progress._active_ingests[thread_id].cancel_event

        # Replace placeholder with the real background task.
        task = asyncio.create_task(
            _wiki_ingest_background(paths, topic, prog, cancel_event),
            name=f"wiki-auto-ingest-{thread_id}",
        )
        wiki_progress._active_ingests[thread_id] = wiki_progress._IngestEntry(
            progress=prog,
            task=task,
            cancel_event=cancel_event,
        )
        prog.advance(prog.phase, "Auto-ingest queued after upload.")
        logger.info("Auto-ingest triggered for thread %s", thread_id)
    except Exception:
        logger.exception("Failed to trigger wiki auto-ingest for thread %s", thread_id)


async def _wiki_ingest_background(
    paths, topic: str, progress_obj, cancel_event
) -> None:
    """Run wiki ingest in the background; swallows all exceptions."""
    from thread_wiki import progress as wiki_progress
    from thread_wiki.service import run_ingest

    try:
        await run_ingest(paths, topic, progress_obj, cancel_event)
    except asyncio.CancelledError:
        logger.info("Auto-ingest cancelled for thread %s", paths.thread_id)
    except Exception:
        logger.exception("Auto-ingest failed for thread %s", paths.thread_id)
    finally:
        await wiki_progress.cleanup_terminal(paths.thread_id)


# ── Delete hooks (cancel ingest + lint) ───────────────────────────────────────


async def trigger_wiki_delete_hooks(
    thread_id: str,
    deleted_filename: str | None = None,
) -> None:
    """Cancel any active ingest, cascade-delete source references, and launch lint."""
    try:
        from thread_wiki import progress as wiki_progress
        from thread_wiki.models import ThreadWikiPaths
        from thread_wiki.service import _cascade_delete_source_references

        # Step 1 — cancel running ingest to prevent stale writes.
        cancelled = await wiki_progress.cancel_ingest(
            thread_id,
            reason=f"Document deleted: {deleted_filename or 'multiple'}",
        )
        if cancelled:
            logger.info(
                "Cancelled active ingest for thread %s due to deletion", thread_id
            )

        # Step 2 — cascade-delete references to the deleted source.
        from thread_wiki.models import _resolve_wiki_base_dir

        def _resolve_paths() -> ThreadWikiPaths:
            base_dir = _resolve_wiki_base_dir(Path(__file__).resolve().parent.parent)
            return ThreadWikiPaths.resolve(thread_id, base_dir)

        paths = await asyncio.to_thread(_resolve_paths)
        if paths.wiki_dir.exists() and deleted_filename:
            try:
                report = _cascade_delete_source_references(
                    paths.wiki_dir, deleted_filename
                )
                logger.info(
                    "Cascade deletion for %r: updated %d frontmatter entries, "
                    "%d pages have body references, source summary: %s",
                    deleted_filename,
                    len(report["pages_updated"]),
                    len(report["pages_with_body_refs"]),
                    report["source_summary_page"] or "none",
                )
            except Exception:
                logger.exception(
                    "Cascade deletion failed for %r; will rely on lint pass.",
                    deleted_filename,
                )

        # Step 3 — launch lint reconciliation if the wiki directory exists.
        if paths.wiki_dir.exists():
            topic = f"Thread {thread_id[:8]}"
            note = (
                f"Source file '{deleted_filename}' was deleted. "
                "Cascade deletion ran first; reconcile remaining references."
                if deleted_filename
                else "Multiple source files were deleted. Reconcile wiki pages."
            )
            asyncio.create_task(
                _wiki_lint_background(paths, topic, note),
                name=f"wiki-lint-{thread_id}",
            )
            logger.info("Lint triggered for thread %s after deletion", thread_id)
    except Exception:
        logger.exception("Failed to trigger wiki delete hooks for thread %s", thread_id)


async def _wiki_lint_background(paths, topic: str, note: str) -> None:
    """Run wiki lint in the background; swallows all exceptions."""
    from thread_wiki.service import run_lint

    try:
        await run_lint(paths, topic, note=note)
    except Exception:
        logger.exception("Lint failed for thread %s", paths.thread_id)
