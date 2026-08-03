"""Contract tests for wiki, source, search, model, and progress ports."""

from __future__ import annotations


def test_wiki_feature_exposes_all_application_ports() -> None:
    from webapp.features.wiki import (
        ModelRunner,
        ProgressStore,
        SearchIndex,
        SourceStore,
        WikiRepository,
    )

    assert {"get_page", "save_page", "list_pages"}.issubset(
        WikiRepository.__dict__
    )
    assert {"list_sources", "read_source", "write_source"}.issubset(
        SourceStore.__dict__
    )
    assert {"index", "search"}.issubset(SearchIndex.__dict__)
    assert "generate" in ModelRunner.__dict__
    assert {"get", "save"}.issubset(ProgressStore.__dict__)
