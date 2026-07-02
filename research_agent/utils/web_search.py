"""Web search utilities utilizing Tavily search APIs.

Executes search queries, processes search responses, fetches full page contents
via HTTP, caches results, and converts HTML pages into clean markdown formats.
"""

from __future__ import annotations

import os
import threading
from json import dumps as json_dumps
from typing import Annotated, Literal

import httpx
import requests
from langchain_core.tools import InjectedToolArg
from langgraph.prebuilt import InjectedState
from markdownify import markdownify
from tavily import TavilyClient

from utils import get_ssl_verify_config

verify_ssl = get_ssl_verify_config()
tavily_session = requests.Session()
tavily_session.verify = verify_ssl
tavily_client = TavilyClient(session=tavily_session)

_web_page_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def get_cached_webpage(url: str) -> str | None:
    """Retrieve cached webpage content by URL."""
    with _cache_lock:
        return _web_page_cache.get(url)


def _run_tavily_search(
    query: str, max_results: int, topic: str, timeout: float = 60.0
) -> dict:
    """Execute a raw search request against the Tavily API.

    Args:
        query: The search query string.
        max_results: Maximum number of results to request.
        topic: Topic filter (``"general"``, ``"news"``, or ``"finance"``).
        timeout: HTTP request timeout in seconds. Defaults to 60.0.

    Returns:
        The parsed JSON response dict, guaranteed to contain a ``"results"``
        key (defaulting to an empty list).

    Raises:
        ValueError: If ``TAVILY_API_KEY`` is not set.
        requests.HTTPError: If the API returns a non-2xx status code.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise ValueError("TAVILY_API_KEY is not set")

    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "topic": topic,
        "include_answer": False,
        "include_raw_content": False,
    }
    response = tavily_client.session.post(
        f"{tavily_client.base_url}/search",
        data=json_dumps(payload),
        timeout=min(timeout, 120),
        verify=verify_ssl,
        headers={"Content-Type": "application/json"},
    )
    response.raise_for_status()
    response_dict = response.json()
    if not isinstance(response_dict, dict):
        return {"results": []}
    response_dict.setdefault("results", [])
    return response_dict


def fetch_webpage_content_impl(
    url: str,
    timeout: float = 10.0,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Fetch and convert webpage content to markdown.

    Use this tool to retrieve the full content of a specific webpage URL and convert it to readable markdown format.
    This is useful when you have a specific URL and need to extract its content for analysis or summarization.

    Args:
        url: The URL of the webpage to fetch.
        timeout: Request timeout in seconds (default: 10.0).
        state: LangGraph state containing no_web flag (injected automatically).

    Returns:
        The webpage content converted to markdown format, or an error message if the fetch fails.
    """
    # Check if web access is disabled
    if state and state.get("no_web", False):
        return "Note: Web access is disabled for this research task. Cannot fetch webpage content. Please use local documents or internal knowledge only."

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
    }

    try:
        response = httpx.get(
            url=url,
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
        )
        response.raise_for_status()
        content = markdownify(response.text)
        with _cache_lock:
            if len(_web_page_cache) >= 200:
                try:
                    first_key = next(iter(_web_page_cache))
                    _web_page_cache.pop(first_key)
                except StopIteration:
                    pass
            _web_page_cache[url] = content
        return content
    except Exception as exc:
        return f"Error fetching content from {url}: {exc}"


def tavily_search_impl(
    query: str,
    max_results: Annotated[int, InjectedToolArg] = 1,
    topic: Annotated[
        Literal["general", "news", "finance"], InjectedToolArg
    ] = "general",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Search the web for information on a given query.

    Uses Tavily to discover relevant URLs, then fetches and returns full webpage content as markdown.

    Args:
        query: Search query to execute
        max_results: Maximum number of results to return (default: 1)
        topic: Topic filter - 'general', 'news', or 'finance' (default: 'general')
        state: LangGraph state

    Returns:
        Formatted search results with full webpage content
    """
    # Check if web search is disabled via state flag
    if state and state.get("no_web", False):
        return "Note: Web search is disabled for this research task. Please use local documents or internal knowledge only."

    # Legacy check: also respect the instruction text pattern
    if state:
        messages = state.get("messages", [])
        for msg in messages:
            content = (
                getattr(msg, "content", "")
                if not isinstance(msg, dict)
                else msg.get("content", "")
            )
            if isinstance(content, str) and "Do NOT use web search" in content:
                return "Note: Web search is disabled for this research task. Please use local documents or internal knowledge only."

    try:
        search_results = _run_tavily_search(
            query=query,
            max_results=max_results,
            topic=topic,
        )
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 401:
            return (
                "Tavily authentication failed (401 Unauthorized)."
                "Set a valid TAVILY_API_KEY environment variable and retry"
            )
        return f"Tavily request failed with HTTP {status_code}: {exc}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Tavily search failed: {exc}"

    result_texts = []
    for result in search_results.get("results", []):
        url = result["url"]
        title = result["title"]
        content = fetch_webpage_content_impl(url, state=state)
        result_text = f"## {title}\n**URL:** {url}\n\n{content}\n\n---\n"
        result_texts.append(result_text)

    return f"🔍 Found {len(result_texts)} result(s) for '{query}':\n\n{chr(10).join(result_texts)}"
