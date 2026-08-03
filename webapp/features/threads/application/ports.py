"""Ports for thread state and run execution workflows."""

# ruff: noqa: D102

from __future__ import annotations

from typing import Any, Protocol


class Clock(Protocol):
    """Time source used by application and persistence policies."""

    def now(self) -> float: ...


class ThreadRepository(Protocol):
    """Persistence boundary for chat thread state."""

    def get_values(self, thread_id: str) -> dict[str, Any]: ...

    def merge_values(self, thread_id: str, values: dict[str, Any]) -> None: ...


class RunExecutor(Protocol):
    """Execution boundary for a run associated with a thread."""

    async def execute(self, run_id: str, thread_id: str) -> None: ...
