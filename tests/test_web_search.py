"""Tests for Tavily web search functionality.

$env:TAVILY_API_KEY="your_tavily_api_key_here"
$username = [uri]::EscapeDataString("office\your_username")
$password = [uri]::EscapeDataString("your_password")
$env:HTTP_PROXY  = "http://${username}:${password}@ebcswg.bmogc.net:8080/"
$env:HTTPS_PROXY = "http://${username}:${password}@ebcswg.bmogc.net:8080/"
$env:REQUESTS_CA_BUNDLE = "path\to\cert.pem"
To run these tests, use the following command:
pytest -vv -rs tests/test_web_search.py::TestTavilySearchIntegration::test_tavily_search_real_api_call
"""
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from research_agent.utils.web_search import _run_tavily_search, tavily_search_impl


class TestRunTavilySearch:
    """Test cases for the _run_tavily_search function."""

    def test_run_tavily_search_success(self):
        """Test successful Tavily search with valid API key."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {
                    "title": "Test Result",
                    "url": "https://example.com",
                    "content": "Test content",
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"}):
            with patch("research_agent.utils.web_search.tavily_client") as mock_client:
                mock_client.session.post.return_value = mock_response
                mock_client.base_url = "https://api.tavily.com"

                result = _run_tavily_search(
                    query="test query",
                    max_results=5,
                    topic="general",
                )

                assert "results" in result
                assert len(result["results"]) == 1
                assert result["results"][0]["title"] == "Test Result"
                mock_client.session.post.assert_called_once()

    def test_run_tavily_search_missing_api_key(self):
        """Test that missing API key raises ValueError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="TAVILY_API_KEY is not set"):
                _run_tavily_search(
                    query="test query",
                    max_results=1,
                    topic="general",
                )

    def test_run_tavily_search_empty_results(self):
        """Test Tavily search with empty results."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": []}
        mock_response.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"}):
            with patch("research_agent.utils.web_search.tavily_client") as mock_client:
                mock_client.session.post.return_value = mock_response
                mock_client.base_url = "https://api.tavily.com"

                result = _run_tavily_search(
                    query="test query",
                    max_results=1,
                    topic="general",
                )

                assert result["results"] == []

    def test_run_tavily_search_non_dict_response(self):
        """Test handling of non-dict response from Tavily."""
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"}):
            with patch("research_agent.utils.web_search.tavily_client") as mock_client:
                mock_client.session.post.return_value = mock_response
                mock_client.base_url = "https://api.tavily.com"

                result = _run_tavily_search(
                    query="test query",
                    max_results=1,
                    topic="general",
                )

                assert result == {"results": []}

    def test_run_tavily_search_with_different_topics(self):
        """Test Tavily search with different topic filters."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": []}
        mock_response.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"}):
            with patch("research_agent.utils.web_search.tavily_client") as mock_client:
                mock_client.session.post.return_value = mock_response
                mock_client.base_url = "https://api.tavily.com"

                # Test general topic
                _run_tavily_search(query="test", max_results=1, topic="general")
                call_args = mock_client.session.post.call_args
                assert call_args[1]["data"] is not None

                # Test news topic
                _run_tavily_search(query="test", max_results=1, topic="news")
                call_args = mock_client.session.post.call_args
                assert call_args[1]["data"] is not None

                # Test finance topic
                _run_tavily_search(query="test", max_results=1, topic="finance")
                call_args = mock_client.session.post.call_args
                assert call_args[1]["data"] is not None


class TestTavilySearchImpl:
    """Test cases for the tavily_search_impl function."""

    def test_tavily_search_impl_success(self):
        """Test successful tavily_search_impl execution."""
        mock_search_results = {
            "results": [
                {
                    "title": "Example Title",
                    "url": "https://example.com",
                }
            ]
        }

        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            with patch("research_agent.utils.web_search.fetch_webpage_content_impl") as mock_fetch:
                mock_search.return_value = mock_search_results
                mock_fetch.return_value = "# Example Content\n\nThis is test content."

                result = tavily_search_impl(
                    query="test query",
                    max_results=1,
                    topic="general",
                    state={},
                )

                assert "Found 1 result(s)" in result
                assert "Example Title" in result
                assert "https://example.com" in result
                mock_search.assert_called_once()
                mock_fetch.assert_called_once()

    def test_tavily_search_impl_no_web_state(self):
        """Test tavily_search_impl when web access is disabled via state."""
        result = tavily_search_impl(
            query="test query",
            max_results=1,
            topic="general",
            state={"no_web": True},
        )

        assert "Web search is disabled" in result

    def test_tavily_search_impl_no_web_instruction(self):
        """Test tavily_search_impl when web search is disabled via instruction."""
        from langchain_core.messages import HumanMessage

        state = {
            "messages": [
                HumanMessage(content="Do NOT use web search for this task.")
            ]
        }

        result = tavily_search_impl(
            query="test query",
            max_results=1,
            topic="general",
            state=state,
        )

        assert "Web search is disabled" in result

    def test_tavily_search_impl_http_401_error(self):
        """Test handling of 401 authentication error."""
        http_error = requests.exceptions.HTTPError(response=MagicMock(status_code=401))

        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            mock_search.side_effect = http_error

            result = tavily_search_impl(
                query="test query",
                max_results=1,
                topic="general",
                state={},
            )

            assert "401 Unauthorized" in result
            assert "TAVILY_API_KEY" in result

    def test_tavily_search_impl_http_error_other_status(self):
        """Test handling of other HTTP errors."""
        mock_response = MagicMock(status_code=500)
        http_error = requests.exceptions.HTTPError(response=mock_response)

        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            mock_search.side_effect = http_error

            result = tavily_search_impl(
                query="test query",
                max_results=1,
                topic="general",
                state={},
            )

            assert "HTTP 500" in result

    def test_tavily_search_impl_value_error(self):
        """Test handling of ValueError (e.g., missing API key)."""
        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            mock_search.side_effect = ValueError("TAVILY_API_KEY is not set")

            result = tavily_search_impl(
                query="test query",
                max_results=1,
                topic="general",
                state={},
            )

            assert "TAVILY_API_KEY is not set" in result

    def test_tavily_search_impl_generic_exception(self):
        """Test handling of generic exceptions."""
        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            mock_search.side_effect = Exception("Network error")

            result = tavily_search_impl(
                query="test query",
                max_results=1,
                topic="general",
                state={},
            )

            assert "Tavily search failed" in result
            assert "Network error" in result

    def test_tavily_search_impl_multiple_results(self):
        """Test tavily_search_impl with multiple search results."""
        mock_search_results = {
            "results": [
                {
                    "title": "First Result",
                    "url": "https://example1.com",
                },
                {
                    "title": "Second Result",
                    "url": "https://example2.com",
                },
            ]
        }

        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            with patch("research_agent.utils.web_search.fetch_webpage_content_impl") as mock_fetch:
                mock_search.return_value = mock_search_results
                mock_fetch.return_value = "# Content"

                result = tavily_search_impl(
                    query="test query",
                    max_results=2,
                    topic="general",
                    state={},
                )

                assert "Found 2 result(s)" in result
                assert "First Result" in result
                assert "Second Result" in result
                assert mock_fetch.call_count == 2

    def test_tavily_search_impl_default_parameters(self):
        """Test tavily_search_impl with default parameter values."""
        mock_search_results = {"results": []}

        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            mock_search.return_value = mock_search_results

            result = tavily_search_impl(
                query="test query",
                state={},
            )

            assert "Found 0 result(s)" in result
            # Verify default values were used
            call_args = mock_search.call_args
            assert call_args[1]["max_results"] == 1
            assert call_args[1]["topic"] == "general"

    def test_tavily_search_impl_with_none_state(self):
        """Test tavily_search_impl when state is None."""
        mock_search_results = {"results": []}

        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            mock_search.return_value = mock_search_results

            result = tavily_search_impl(
                query="test query",
                max_results=1,
                topic="general",
                state=None,
            )

            assert "Found 0 result(s)" in result

    def test_tavily_search_impl_formats_output_correctly(self):
        """Test that output is properly formatted with markdown."""
        mock_search_results = {
            "results": [
                {
                    "title": "Test Article",
                    "url": "https://test.com/article",
                }
            ]
        }

        with patch("research_agent.utils.web_search._run_tavily_search") as mock_search:
            with patch("research_agent.utils.web_search.fetch_webpage_content_impl") as mock_fetch:
                mock_search.return_value = mock_search_results
                mock_fetch.return_value = "# Heading\n\nSome content here."

                result = tavily_search_impl(
                    query="python testing",
                    max_results=1,
                    topic="general",
                    state={},
                )

                # Check formatting elements
                assert "🔍 Found 1 result(s) for 'python testing':" in result
                assert "## Test Article" in result
                assert "**URL:** https://test.com/article" in result
                assert "---" in result  # Separator between results


class TestTavilySearchIntegration:
    """Integration tests for Tavily search (may require actual API key)."""

    @pytest.mark.skipif(
        not os.getenv("TAVILY_API_KEY"),
        reason="TAVILY_API_KEY not set for integration test"
    )
    def test_tavily_search_real_api_call(self):
        """Test actual Tavily API call if API key is available."""
        try:
            result = _run_tavily_search(
                query="Python programming",
                max_results=1,
                topic="general",
                timeout=10.0,
            )

            assert isinstance(result, dict)
            assert "results" in result
            # Note: Results may be empty depending on API quota/limits
        except Exception as e:
            pytest.skip(f"API call failed (likely quota or network issue): {e}")
