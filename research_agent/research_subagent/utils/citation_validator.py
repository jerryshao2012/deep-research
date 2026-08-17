"""Source citation validator for checking and grounding cited URLs.

Parses generated citations, checks URL reachability, and verifies that references
are actually grounded in the fetched source texts by comparing keywords and sentences.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import queue
import re
import socket
import ssl
import threading
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urljoin, urlsplit

import httpx

from research_agent.cli_utils import get_ssl_verify_config
from research_agent.research_subagent.utils.web_search import get_cached_webpage
from thread_wiki.models import SourceCitation


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating a single web citation.

    Attributes:
        url: The URL that was validated.
        reachable: Whether the URL returned a successful HTTP response.
        grounded: Whether the claim is supported by the page content.
        reason: Human-readable explanation of the validation outcome.
    """

    citation_index: int
    reachable: bool
    grounded: bool
    category: CitationValidationCategory


class CitationValidationCategory(StrEnum):
    """Fixed, report-safe citation validation outcomes."""

    UNREACHABLE = "unreachable"
    GROUNDING_UNAVAILABLE = "grounding_unavailable"
    CLAIM_CONTEXT_MISSING = "claim_context_missing"
    GROUNDED = "grounded"
    UNGROUNDED = "ungrounded"


@dataclass(frozen=True)
class _HeaderResponse:
    status_code: int
    headers: object


_STOP_WORDS = {
    'we', 'our', 'us', 'the', 'a', 'an', 'and', 'or', 'but', 'is', 'are', 'was', 'were', 'be', 'been',
    'have', 'has', 'had', 'do', 'does', 'did', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
    'about', 'as', 'into', 'through', 'during', 'under', 'over', 'between', 'out', 'off', 'both', 'each',
    'few', 'more', 'most', 'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too',
    'very', 's', 't', 'can', 'will', 'just', 'should', 'now', 'i', 'you', 'he', 'she', 'it', 'they', 'them',
    'my', 'your', 'his', 'her', 'its', 'their', 'this', 'that', 'these', 'those'
}

_MAX_REDIRECTS = 3
_DNS_TIMEOUT_SECONDS = 1.0
_RESOLVER_WORKERS = 4
_RESOLVER_QUEUE_SIZE = 4
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_INVALID_REDIRECT_TARGET = object()
_resolver_lock = threading.Lock()
_resolver_pid: int | None = None
_resolver_queue: queue.Queue | None = None
_resolver_threads: list[threading.Thread] = []


def _extract_claim_for_citation(text: str, cite_index: int) -> str | None:
    """Extract sentence containing [cite_index] reference."""
    pattern = rf"\[{cite_index}\]"
    matches = list(re.finditer(pattern, text))
    if not matches:
        return None

    idx = matches[0].start()

    # Scan backward for sentence start
    start = idx
    while start > 0:
        if text[start - 1] in {'.', '?', '!', '\n'}:
            # Handle decimal numbers like 4.2
            if start > 1 and text[start - 2].isdigit() and text[start - 1] == '.':
                start -= 1
                continue
            break
        start -= 1

    # Scan forward for sentence end
    end = idx
    while end < len(text):
        if text[end] in {'.', '?', '!', '\n'}:
            end += 1  # Include the punctuation mark
            break
        end += 1

    sentence = text[start:end].strip()
    sentence = re.sub(pattern, "", sentence)
    sentence = re.sub(r"\s+([.,?!])", r"\1", sentence)
    return " ".join(sentence.split())


def _extract_claim_for_url(text: str, url: str) -> str | None:
    """Extract sentence containing raw URL reference."""
    escaped_url = re.escape(url)
    matches = list(re.finditer(escaped_url, text))
    if not matches:
        return None

    idx = matches[0].start()

    start = idx
    while start > 0:
        if text[start - 1] in {'.', '?', '!', '\n'}:
            if start > 1 and text[start - 2].isdigit() and text[start - 1] == '.':
                start -= 1
                continue
            break
        start -= 1

    end = idx + len(url)
    while end < len(text):
        if text[end] in {'.', '?', '!', '\n'}:
            end += 1  # Include the punctuation mark
            break
        end += 1

    sentence = text[start:end].strip()
    sentence = sentence.replace(url, "")
    sentence = re.sub(r"\s+([.,?!])", r"\1", sentence)
    return " ".join(sentence.split())


def _is_claim_grounded(claim: str, fetched_text: str) -> bool:
    """Determine if a claim is grounded in fetched webpage text (using keyword proximity)."""

    def clean(s: str) -> str:
        s = s.lower()
        s = re.sub(r"[^\w\s]", " ", s)
        return " ".join(s.split())

    cleaned_claim = clean(claim)
    cleaned_fetched = clean(fetched_text)

    if not cleaned_claim or not cleaned_fetched:
        return False

    if cleaned_claim in cleaned_fetched:
        return True

    claim_words = cleaned_claim.split()

    # Critical check: digits/numbers in the claim must exist in the fetched content
    digit_tokens = [w for w in claim_words if any(c.isdigit() for c in w)]
    for token in digit_tokens:
        clean_token = re.sub(r"\D", "", token)
        if clean_token and clean_token not in cleaned_fetched:
            return False

    # Filter stopwords
    content_words = [w for w in claim_words if w not in _STOP_WORDS and (len(w) > 2 or w.isdigit())]
    if not content_words:
        content_words = claim_words

    # Check matching ratio
    matches = sum(1 for w in content_words if w in cleaned_fetched)
    ratio_threshold = 0.6 if len(content_words) >= 4 else 0.5

    if not content_words or (matches / len(content_words)) < ratio_threshold:
        return False

    # Proximity check
    matching_words = [w for w in content_words if w in cleaned_fetched]
    if not matching_words:
        return False

    fetched_words = cleaned_fetched.split()
    word_indices = {w: [] for w in matching_words}
    for idx, w in enumerate(fetched_words):
        for mw in matching_words:
            if mw in w or w in mw:
                word_indices[mw].append(idx)

    if any(not indices for indices in word_indices.values()):
        return False

    all_positions = []
    for mw, indices in word_indices.items():
        for pos in indices:
            all_positions.append((pos, mw))
    all_positions.sort()

    unique_words_needed = set(matching_words)
    for i in range(len(all_positions)):
        current_set = set()
        start_pos = all_positions[i][0]
        for j in range(i, len(all_positions)):
            pos, mw = all_positions[j]
            if pos - start_pos > 100:
                break
            current_set.add(mw)
            if current_set == unique_words_needed:
                return True

    return False


def _resolve_host_addresses(host: str, port: int | None) -> tuple[str, ...]:
    """Resolve a hostname for safe fetch checks; patch this boundary in tests."""
    service = port or 443
    addresses = {
        info[4][0]
        for info in socket.getaddrinfo(host, service, type=socket.SOCK_STREAM)
    }
    return tuple(sorted(addresses))


def _resolver_worker(work_queue: queue.Queue) -> None:
    while True:
        host, port, loop, future = work_queue.get()
        try:
            result = _resolve_host_addresses(host, port)
        except BaseException as error:
            result = error
        try:
            loop.call_soon_threadsafe(_deliver_resolution, future, result)
        except RuntimeError:
            pass
        finally:
            work_queue.task_done()


def _deliver_resolution(future: asyncio.Future, result: object) -> None:
    if future.done() or future.cancelled():
        return
    if isinstance(result, BaseException):
        future.set_exception(result)
    else:
        future.set_result(result)


def _resolver_runtime() -> queue.Queue:
    global _resolver_pid, _resolver_queue, _resolver_threads
    with _resolver_lock:
        if _resolver_pid != os.getpid() or _resolver_queue is None:
            _resolver_pid = os.getpid()
            _resolver_queue = queue.Queue(maxsize=_RESOLVER_QUEUE_SIZE)
            _resolver_threads = [threading.Thread(target=_resolver_worker, args=(_resolver_queue,), daemon=True) for _ in range(_RESOLVER_WORKERS)]
            for worker in _resolver_threads:
                worker.start()
        return _resolver_queue


def _resolver_runtime_stats() -> dict[str, int]:
    work_queue = _resolver_runtime()
    return {"workers": len(_resolver_threads), "outstanding": work_queue.qsize() + sum(worker.is_alive() for worker in _resolver_threads)}


def _submit_resolution(host: str, port: int) -> asyncio.Future | None:
    future = asyncio.get_running_loop().create_future()
    try:
        _resolver_runtime().put_nowait((host, port, asyncio.get_running_loop(), future))
    except queue.Full:
        return None
    return future


def _static_url_parts(
    url: str,
) -> tuple[str, str | None, str | None, str | None, int | None] | None:
    """Read URL authority properties without exposing parse errors to callers."""
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme.lower(),
            parsed.hostname,
            parsed.username,
            parsed.password,
            parsed.port,
        )
    except (TypeError, UnicodeError, ValueError):
        return None


def _unsafe_static_url(
    url: str,
    parts: tuple[str, str | None, str | None, str | None, int | None] | None = None,
) -> bool:
    parts = _static_url_parts(url) if parts is None else parts
    if parts is None:
        return True
    scheme, host, username, password, _ = parts
    if (
        scheme not in {"http", "https"}
        or not host
        or username is not None
        or password is not None
    ):
        return True
    normalized = host.lower().rstrip(".")
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


async def _safe_fetch_url(
    url: str,
    resolution_cache: dict[tuple[str, int], tuple[bool, str]] | None = None,
) -> tuple[bool, str]:
    """Reject static and DNS-resolved non-global targets before each request.

    DNS is checked immediately before every request, including redirects.  A
    resolver can still rebind between this check and connect because httpx does
    not expose address pinning; callers must treat this as defense in depth.
    """
    parts = _static_url_parts(url)
    if _unsafe_static_url(url, parts):
        return False, "Unsafe URL target"

    assert parts is not None
    scheme, host, _, _, configured_port = parts
    port = configured_port or (443 if scheme == "https" else 80)
    key = (host or "", port)
    if resolution_cache and key in resolution_cache:
        return resolution_cache[key]
    try:
        future = _submit_resolution(host or "", port)
        if future is None:
            raise TimeoutError
        addresses = await asyncio.wait_for(future, timeout=_DNS_TIMEOUT_SECONDS)
    except (OSError, ValueError, TimeoutError):
        result = (False, "DNS resolution failed")
        if resolution_cache is not None:
            resolution_cache[key] = result
        return result
    if not addresses:
        result = (False, "DNS resolution returned no addresses")
        if resolution_cache is not None:
            resolution_cache[key] = result
        return result

    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            result = (False, "DNS resolution returned an invalid address")
            if resolution_cache is not None:
                resolution_cache[key] = result
            return result
        if not address.is_global:
            result = (False, "Unsafe DNS target")
            if resolution_cache is not None:
                resolution_cache[key] = result
            return result
    result = (True, "Safe target")
    if resolution_cache is not None:
        resolution_cache[key] = result
    return result


def _redirect_target(url: str, response: httpx.Response | _HeaderResponse) -> str | object | None:
    if response.status_code not in _REDIRECT_STATUSES:
        return None
    location = response.headers.get("location")
    if not location:
        return ""
    try:
        return urljoin(url, location)
    except (TypeError, UnicodeError, ValueError):
        return _INVALID_REDIRECT_TARGET


def _safe_request_error(error: Exception) -> str:
    """Classify transport failures without exposing endpoint or exception text."""
    if isinstance(error, httpx.TimeoutException):
        return "Request timeout"
    if isinstance(error, httpx.ConnectError) and isinstance(error.__cause__, ssl.SSLError):
        return "TLS error"
    if isinstance(error, httpx.TransportError):
        return "Transport error"
    return "Transport error"


async def _check_url_reachable(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Check reachability without proxies and with DNS screening per hop."""
    verify_ssl = get_ssl_verify_config()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
    }
    try:
        async with asyncio.timeout(timeout):
            resolution_cache: dict[tuple[str, int], tuple[bool, str]] = {}
            safe, reason = await _safe_fetch_url(url, resolution_cache)
            if not safe:
                return False, reason
            async with httpx.AsyncClient(
            verify=verify_ssl,
            headers=headers,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            ) as client:
                current_url = url
                for redirect_count in range(_MAX_REDIRECTS + 1):
                    safe, reason = await _safe_fetch_url(current_url, resolution_cache)
                    if not safe:
                        return False, reason

                    try:
                        async with client.stream("HEAD", current_url) as stream_response:
                            status_code, headers = stream_response.status_code, stream_response.headers
                    except Exception:
                        status_code, headers = 599, {}

                    if status_code != 599:
                        target = _redirect_target(current_url, _HeaderResponse(status_code, headers))
                        if target is not None:
                            if target is _INVALID_REDIRECT_TARGET:
                                return False, "Unsafe URL target"
                            if not target:
                                return False, "Redirect missing location"
                            if redirect_count == _MAX_REDIRECTS:
                                return False, "Too many redirects"
                            current_url = target
                            continue
                        if status_code < 400:
                            return True, "Reachable"

                    async with client.stream("GET", current_url) as stream_response:
                        status_code, headers = stream_response.status_code, stream_response.headers
                    target = _redirect_target(current_url, _HeaderResponse(status_code, headers))
                    if target is not None:
                        if target is _INVALID_REDIRECT_TARGET:
                            return False, "Unsafe URL target"
                        if not target:
                            return False, "Redirect missing location"
                        if redirect_count == _MAX_REDIRECTS:
                            return False, "Too many redirects"
                        current_url = target
                        continue
                    if status_code < 400:
                        return True, "Reachable"
                    return False, f"HTTP {status_code}"
    except TimeoutError:
        return False, "Request timeout"
    except Exception as error:
        return False, _safe_request_error(error)


async def validate_web_citations(
        citations: list[SourceCitation],
        text_content: str,
        fetched_contents: dict[str, str] | None = None
) -> list[ValidationResult]:
    """Validate web citations against reachable endpoints and matching text content."""
    results: list[ValidationResult] = []

    sources_block_urls = {}
    sources_pattern = re.compile(r"^\s*\[(\d{1,3})]\s*(.*?):\s*(https?://\S+)\s*$", re.MULTILINE)
    for match in sources_pattern.finditer(text_content):
        idx = int(match.group(1))
        url = match.group(3).strip()
        sources_block_urls[url] = idx

    for citation_index, cit in enumerate(citations, start=1):
        if cit.kind != "web":
            continue

        url = cit.url or cit.raw_path
        if not url:
            continue

        reachable, _ = await _check_url_reachable(url)
        if not reachable:
            results.append(ValidationResult(citation_index=citation_index, reachable=False, grounded=False, category=CitationValidationCategory.UNREACHABLE))
            continue

        content = None
        if fetched_contents and url in fetched_contents:
            content = fetched_contents[url]
        else:
            content = get_cached_webpage(url)

        if not content:
            results.append(ValidationResult(
                citation_index=citation_index,
                reachable=True,
                grounded=False,
                category=CitationValidationCategory.GROUNDING_UNAVAILABLE,
            ))
            continue

        claim = None
        if url in sources_block_urls:
            idx = sources_block_urls[url]
            claim = _extract_claim_for_citation(text_content, idx)
        else:
            claim = _extract_claim_for_url(text_content, url)

        if not claim:
            results.append(ValidationResult(
                citation_index=citation_index,
                reachable=reachable,
                grounded=False,
                category=CitationValidationCategory.CLAIM_CONTEXT_MISSING,
            ))
            continue

        is_grounded = _is_claim_grounded(claim, content)
        if is_grounded:
            results.append(ValidationResult(
                citation_index=citation_index,
                reachable=reachable,
                grounded=True,
                category=CitationValidationCategory.GROUNDED,
            ))
        else:
            results.append(ValidationResult(
                citation_index=citation_index,
                reachable=reachable,
                grounded=False,
                category=CitationValidationCategory.UNGROUNDED,
            ))

    return results
