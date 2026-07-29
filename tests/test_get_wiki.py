"""Opt-in live integration test for wiki content retrieval."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_RUN_LIVE_WIKI_TESTS = os.getenv("RUN_LIVE_WIKI_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


@pytest.mark.skipif(
    not _RUN_LIVE_WIKI_TESTS,
    reason="Set RUN_LIVE_WIKI_TESTS=true to run the live model-backed wiki query.",
)
def test_live_wiki_query() -> None:
    """Query a real prepared wiki only when explicitly enabled."""
    from thread_wiki.models import ThreadWikiPaths
    from thread_wiki.service import run_query

    thread_id = os.getenv(
        "WIKI_TEST_THREAD_ID",
        "019eec4d-ddf5-7353-bcab-94c41ce68205",
    )
    question = os.getenv(
        "WIKI_TEST_QUESTION",
        (
            "What was BMO's overall financial performance in fiscal 2025, "
            "and how did key metrics change compared to fiscal 2024?"
        ),
    )
    base_dir = Path(os.getenv("WIKI_TEST_BASE_DIR", str(Path.cwd()))).resolve()
    paths = ThreadWikiPaths.resolve(thread_id, base_dir)
    index_path = paths.wiki_content / "index.md"

    assert index_path.is_file(), (
        f"Live wiki fixture is not ready: {index_path}. "
        "Set WIKI_TEST_BASE_DIR and WIKI_TEST_THREAD_ID to a prepared wiki."
    )

    result = asyncio.run(
        run_query(
            paths,
            f"Thread {thread_id[:8]}",
            question,
            file_results=False,
        )
    )

    assert result.answer.strip()
