"""Tests for input validation and type checking."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from research_agent.utils.citation_validator import (
    _extract_claim_for_citation,
    _extract_claim_for_url,
    _is_claim_grounded,
    validate_web_citations,
)
from thread_wiki.models import SourceCitation


def test_extract_claim_for_citation() -> None:
    text = "The quarterly revenue was $15.4B [1]. Other details."
    claim = _extract_claim_for_citation(text, 1)
    assert claim == "The quarterly revenue was $15.4B."

    # Decimal number handling before citation
    text_dec = "Interest rate remains at 4.2 [2]."
    claim_dec = _extract_claim_for_citation(text_dec, 2)
    assert claim_dec == "Interest rate remains at 4.2."


def test_extract_claim_for_url() -> None:
    text = "Find resources at https://example.com/docs for guidance."
    claim = _extract_claim_for_url(text, "https://example.com/docs")
    assert claim == "Find resources at for guidance."


def test_is_claim_grounded() -> None:
    fetched = "We reported quarterly revenue of 15.4B in our earnings release."

    # Exact match
    assert _is_claim_grounded("quarterly revenue of 15.4B", fetched) is True

    # Keyword proximity match (semantic variance)
    assert _is_claim_grounded("quarterly revenue was 15.4B", fetched) is True

    # Non-match
    assert _is_claim_grounded("quarterly revenue of 20B", fetched) is False


@pytest.mark.anyio
async def test_validate_web_citations() -> None:
    citations = [
        SourceCitation(kind="web", url="https://example.com/profit"),
        SourceCitation(kind="web", url="https://example.com/unreachable"),
    ]
    text = (
        "We earned $10B [1].\n\n"
        "### Sources\n"
        "[1] Earnings Page: https://example.com/profit\n"
        "[2] Stale Page: https://example.com/unreachable\n"
    )

    async def side_effect(url, **kwargs):
        if "unreachable" in url:
            return False, "HTTP 404"
        return True, "Reachable"

    # Patch explicitly using context managers to avoid ordering issues
    with patch("research_agent.utils.citation_validator._check_url_reachable",
               new_callable=AsyncMock) as mock_reachable, \
            patch("research_agent.utils.citation_validator.get_cached_webpage") as mock_get_cached:

        mock_reachable.side_effect = side_effect

        def get_cached_side_effect(url):
            if "profit" in url:
                return "This document shows quarterly profit was $10B."
            return None

        mock_get_cached.side_effect = get_cached_side_effect

        results = await validate_web_citations(citations, text)
        assert len(results) == 2

        profit_res = [r for r in results if "profit" in r.url][0]
        assert profit_res.reachable is True
        assert profit_res.grounded is True

        unreach_res = [r for r in results if "unreachable" in r.url][0]
        assert unreach_res.reachable is False
        assert unreach_res.grounded is False
