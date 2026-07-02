"""Async progress tracking and cancellation for thread wiki ingest operations.

Maintains a global registry of active ingest tasks keyed by thread_id.
Supports:
- Real-time progress updates (polled or SSE-streamed).
- Cancellation: calling ``cancel_ingest(thread_id)`` sets a cancellation flag
  and cancels the underlying asyncio.Task. The ingest coroutine checks the flag
  between phases and raises ``asyncio.CancelledError`` promptly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import NamedTuple

from .models import IngestPhase, IngestProgress


class _IngestEntry(NamedTuple):
    """Internal bookkeeping for one active ingest."""

    progress: IngestProgress
    task: asyncio.Task
    cancel_event: asyncio.Event


# Global registry: thread_id → active ingest entry.
_active_ingests: dict[str, _IngestEntry] = {}
_registry_lock = asyncio.Lock()


async def register_ingest(
    thread_id: str,
    task: asyncio.Task,
    *,
    wiki_dir: Path | None = None,
) -> IngestProgress:
    """Register a new ingest task for a thread, replacing any stale entry.

    If a persisted state file exists from a previous crash, the progress is
    pre-populated so the caller can decide whether to resume or restart.
    """
    async with _registry_lock:
        existing = _active_ingests.pop(thread_id, None)
        if existing and not existing.task.done():
            # Cancel the previous ingest before starting a new one.
            existing.cancel_event.set()
            existing.task.cancel()

        # Check for abandoned state from a previous crash.
        abandoned: IngestProgress | None = None
        if wiki_dir is not None:
            abandoned = await load_progress(wiki_dir)

        if abandoned is not None and abandoned.phase not in (
            IngestPhase.READY,
            IngestPhase.ERROR,
            IngestPhase.CANCELLED,
            IngestPhase.IDLE,
        ):
            max_retry = get_max_retry()
            if abandoned.retry_count >= max_retry:
                logger.warning(
                    "Ingest for thread %s has reached max retries (%d/%d); "
                    "removing stale state and starting fresh.",
                    thread_id,
                    abandoned.retry_count,
                    max_retry,
                )
                await remove_progress_snapshot(wiki_dir)
                abandoned = None
            else:
                logger.warning(
                    "Found abandoned ingest state for thread %s at phase %s "
                    "(retry %d/%d). Resuming with preserved progress.",
                    thread_id,
                    abandoned.phase.value,
                    abandoned.retry_count,
                    max_retry,
                )
                # Inherit the retry count from the abandoned attempt.
                progress = abandoned
                progress.retry_count += 1
            # Re-save with incremented retry count.
            if abandoned is not None and wiki_dir is not None:
                await save_progress(abandoned, wiki_dir)
        else:
            progress = IngestProgress(thread_id=thread_id)

        cancel_event = asyncio.Event()
        _active_ingests[thread_id] = _IngestEntry(
            progress=progress,
            task=task,
            cancel_event=cancel_event,
        )
        return progress


async def get_progress(thread_id: str) -> IngestProgress | None:
    """Return the current progress tracker for a thread, or None."""
    async with _registry_lock:
        entry = _active_ingests.get(thread_id)
        if entry is not None:
            return entry.progress
    return None


async def cancel_ingest(
    thread_id: str, *, reason: str = "Cancelled by client."
) -> bool:
    """Cancel an active ingest for the given thread.

    Returns True if a running ingest was found and cancelled, False otherwise.
    """
    async with _registry_lock:
        entry = _active_ingests.get(thread_id)
        if entry is None:
            return False
        if entry.task.done():
            # Already finished; clean up stale entry.
            _active_ingests.pop(thread_id, None)
            return False

        # Signal the coroutine to stop at the next checkpoint.
        entry.cancel_event.set()
        entry.progress.mark_cancelled(reason)
        entry.task.cancel()
        return True


async def cleanup_terminal(thread_id: str) -> None:
    """Remove a terminal (finished, cancelled, or errored) ingest entry from the registry.

    Args:
        thread_id: The thread whose ingest entry should be cleaned up.
    """
    async with _registry_lock:
        entry = _active_ingests.get(thread_id)
        if entry is not None and (entry.task.done() or entry.progress.is_terminal()):
            _active_ingests.pop(thread_id, None)


def is_cancelled_sync(cancel_event: asyncio.Event) -> bool:
    """Check cancellation flag without awaiting (for use inside sync callbacks)."""
    return cancel_event.is_set()


async def check_cancellation(
    cancel_event: asyncio.Event, *, phase_name: str = ""
) -> None:
    """Raise ``asyncio.CancelledError`` if cancellation has been requested.

    Call this between ingest phases so the coroutine exits promptly.
    """
    if cancel_event.is_set():
        raise asyncio.CancelledError(
            f"Ingest cancelled{' during ' + phase_name if phase_name else ''}."
        )


# ── Persistence helpers ───────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

# Maximum retries before giving up on an ingest.
_MAX_RETRY_COUNT = 3

# Filename for the persisted ingest state.
_STATE_FILENAME = ".ingest_state.json"


def _state_path(wiki_dir: Path) -> Path:
    """Return the path to the persisted ingest state file."""
    return wiki_dir / _STATE_FILENAME


async def save_progress(progress: IngestProgress, wiki_dir: Path) -> None:
    """Persist the ingest progress to disk for crash recovery.

    Serializes the current phase, progress percentage, detail message,
    source counts, retry count, and timestamps to a JSON file in the
    wiki workspace directory.  Disk I/O runs in a thread to avoid
    blocking the ASGI event loop.
    """
    state = {
        "thread_id": progress.thread_id,
        "phase": progress.phase.value,
        "progress": progress.progress,
        "detail": progress.detail,
        "source_count": progress.source_count,
        "sources_processed": progress.sources_processed,
        "error": progress.error,
        "retry_count": progress.retry_count,
        "started_at": progress.started_at,
        "completed_at": progress.completed_at,
    }

    def _write() -> None:
        sp = _state_path(wiki_dir)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(state, indent=2), encoding="utf-8")

    try:
        await asyncio.to_thread(_write)
    except Exception:
        logger.exception(
            "Failed to persist ingest state for thread %s", progress.thread_id
        )


async def load_progress(wiki_dir: Path) -> IngestProgress | None:
    """Load a previously persisted ingest progress from disk.

    Returns None if no state file exists or it cannot be parsed.
    Disk I/O runs in a thread to avoid blocking the ASGI event loop.
    """

    def _read() -> str | None:
        sp = _state_path(wiki_dir)
        if not sp.exists():
            return None
        return sp.read_text(encoding="utf-8")

    try:
        raw = await asyncio.to_thread(_read)
    except Exception:
        logger.warning("Failed to read ingest state from %s", _state_path(wiki_dir))
        return None

    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "Corrupted ingest state file at %s; ignoring.", _state_path(wiki_dir)
        )
        return None

    try:
        phase = IngestPhase(data.get("phase", "idle"))
    except ValueError:
        phase = IngestPhase.ERROR

    return IngestProgress(
        thread_id=data.get("thread_id", "unknown"),
        phase=phase,
        progress=data.get("progress", 0),
        detail=data.get("detail", ""),
        source_count=data.get("source_count", 0),
        sources_processed=data.get("sources_processed", 0),
        error=data.get("error"),
        retry_count=data.get("retry_count", 0),
        started_at=data.get("started_at", ""),
        completed_at=data.get("completed_at"),
    )


async def remove_progress_snapshot(wiki_dir: Path) -> None:
    """Delete the persisted ingest state file after successful completion."""
    sp = _state_path(wiki_dir)

    def _remove() -> None:
        if sp.exists():
            sp.unlink()

    try:
        await asyncio.to_thread(_remove)
    except OSError:
        logger.debug("Failed to remove ingest state file at %s", sp)


def get_max_retry() -> int:
    """Return the maximum number of ingest retries before giving up."""
    import os

    try:
        return int(os.getenv("WIKI_INGEST_MAX_RETRY", str(_MAX_RETRY_COUNT)))
    except ValueError:
        return _MAX_RETRY_COUNT
