"""Process-local thread state adapter for cross-deployment synchronization."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from webapp.features.threads.application import Clock, ThreadRepository


class SystemClock:
    """Production wall-clock adapter."""

    def now(self) -> float:
        """Return current Unix timestamp."""
        return time.time()


@dataclass
class _ThreadEntry:
    values: dict[str, Any]
    last_access: float
    created: float


class InMemoryThreadRepository(ThreadRepository):
    """Bounded, expiring in-memory thread state repository."""

    def __init__(
        self,
        *,
        max_threads: int = 1_000,
        ttl_seconds: float = 3 * 24 * 3600,
        clock: Clock | None = None,
    ) -> None:
        """Initialize bounded repository with an injectable clock."""
        self._max_threads = max_threads
        self._ttl_seconds = ttl_seconds
        self._clock = clock or SystemClock()
        self._entries: dict[str, _ThreadEntry] = {}
        self._lock = threading.RLock()

    def get_values(self, thread_id: str) -> dict[str, Any]:
        """Return copied values and refresh access time."""
        with self._lock:
            self._cleanup_expired()
            entry = self._touch(thread_id)
            return dict(entry.values)

    def merge_values(self, thread_id: str, values: dict[str, Any]) -> None:
        """Merge values, refresh access time, and enforce repository bounds."""
        with self._lock:
            self._cleanup_expired()
            entry = self._touch(thread_id)
            entry.values.update(values)
            excess = len(self._entries) - self._max_threads
            if excess > 0:
                oldest = sorted(
                    self._entries.items(), key=lambda item: item[1].last_access
                )[:excess]
                for stale_id, _entry in oldest:
                    del self._entries[stale_id]

    def _touch(self, thread_id: str) -> _ThreadEntry:
        now = self._clock.now()
        entry = self._entries.get(thread_id)
        if entry is None:
            entry = _ThreadEntry(values={}, last_access=now, created=now)
            self._entries[thread_id] = entry
        else:
            entry.last_access = now
        return entry

    def _cleanup_expired(self) -> None:
        cutoff = self._clock.now() - self._ttl_seconds
        expired = [
            thread_id
            for thread_id, entry in self._entries.items()
            if entry.last_access < cutoff
        ]
        for thread_id in expired:
            del self._entries[thread_id]
