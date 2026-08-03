"""Backend-neutral ports for wiki application workflows."""

# ruff: noqa: D102

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


class WikiRepository(Protocol):
    """Persistence boundary for generated wiki pages."""

    def get_page(self, thread_id: str, path: str) -> Mapping[str, Any] | None: ...

    def save_page(
            self, thread_id: str, path: str, page: Mapping[str, Any]
    ) -> None: ...

    def list_pages(self, thread_id: str) -> Sequence[Mapping[str, Any]]: ...


class SourceStore(Protocol):
    """Storage boundary for uploaded and extracted sources."""

    def list_sources(self, thread_id: str) -> Sequence[str]: ...

    def read_source(self, thread_id: str, path: str) -> bytes: ...

    def write_source(self, thread_id: str, path: str, content: bytes) -> None: ...


class SearchIndex(Protocol):
    """Indexing and retrieval boundary for wiki evidence."""

    def index(self, thread_id: str, documents: Sequence[Mapping[str, Any]]) -> None: ...

    def search(
            self, thread_id: str, query: str, *, limit: int = 10
    ) -> Sequence[Mapping[str, Any]]: ...


class ModelRunner(Protocol):
    """Model invocation boundary for wiki generation and lint workflows."""

    async def generate(
            self, prompt: str, *, context: Mapping[str, Any] | None = None
    ) -> str: ...


class ProgressStore(Protocol):
    """Persistence boundary for long-running ingest progress."""

    async def get(self, thread_id: str) -> Mapping[str, Any] | None: ...

    async def save(self, thread_id: str, progress: Mapping[str, Any]) -> None: ...
