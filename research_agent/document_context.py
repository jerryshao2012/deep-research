"""Fail-closed eligibility checks for document-dependent tools."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from thread_wiki.models import ThreadWikiPaths, _resolve_wiki_base_dir
from thread_wiki.source_types import SUPPORTED_WIKI_SOURCE_SUFFIXES

DOCUMENT_TOOL_NAMES = {"llm_wiki_query", "read_docs_folder"}

try:
    MAX_GLOB_DEPTH = max(0, int(os.environ.get("MAX_GLOB_DEPTH", "3")))
except ValueError:
    MAX_GLOB_DEPTH = 0


def tool_name(tool: object) -> str | None:
    """Return a tool name without requiring a concrete tool implementation."""
    if isinstance(tool, Mapping):
        name = tool.get("name")
    else:
        name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def configure_document_tools(
        tools: Iterable[object], documents_available: bool
) -> list[object]:
    """Remove only document tools when physical document context is unavailable."""
    if documents_available:
        return list(tools)
    return [tool for tool in tools if tool_name(tool) not in DOCUMENT_TOOL_NAMES]


def _normalized_folder(value: object) -> Path | None:
    """Normalize one configured folder while rejecting ambiguous filesystem input."""
    if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value == os.curdir
    ):
        return None
    try:
        supplied = Path(value)
    except (TypeError, ValueError, OSError):
        return None
    if ".." in supplied.parts:
        return None
    try:
        folder = supplied.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if folder == folder.parent or not folder.is_dir():
        return None
    return folder


def _has_readable_source(root: Path) -> bool:
    """Return whether *root* contains a supported non-empty physical source."""
    depth_limit = MAX_GLOB_DEPTH
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False

    try:
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            try:
                relative = current_path.resolve().relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                directories[:] = []
                continue
            if len(relative.parts) >= depth_limit:
                directories[:] = []

            for filename in filenames:
                candidate = current_path / filename
                try:
                    candidate_relative = candidate.resolve().relative_to(resolved_root)
                    if len(candidate_relative.parts) > depth_limit:
                        continue
                    if candidate.suffix.lower() not in SUPPORTED_WIKI_SOURCE_SUFFIXES:
                        continue
                    if not candidate.is_file() or candidate.stat().st_size <= 0:
                        continue
                    with candidate.open("rb") as source:
                        source.read(1)
                except (OSError, RuntimeError, ValueError):
                    continue
                return True
    except (OSError, RuntimeError, ValueError):
        return False
    return False


def _wiki_base_for_doc_folder(doc_folder: Path) -> Path:
    """Resolve wiki base, preserving the canonical ``docs/`` project boundary."""
    fallback = doc_folder.parent
    for parent in doc_folder.parents:
        if parent.name == "docs":
            fallback = parent.parent
            break
    return _resolve_wiki_base_dir(fallback)


def has_document_context(state: Mapping[str, Any] | None) -> bool:
    """Determine whether state has a readable physical supported document source.

    Graph-state files are intentionally ignored: only configured upload folders and
    exact Thread Wiki raw directories may establish document-tool eligibility.
    """
    if not isinstance(state, Mapping) or state.get("has_documents") is False:
        return False

    doc_folder = _normalized_folder(state.get("doc_folder"))
    if doc_folder is None:
        return False

    if _has_readable_source(doc_folder):
        return True

    try:
        paths = ThreadWikiPaths.resolve(
            doc_folder.name,
            _wiki_base_for_doc_folder(doc_folder),
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return _has_readable_source(paths.raw_dir)
