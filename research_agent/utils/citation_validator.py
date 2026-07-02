"""Source citation validator for checking and grounding cited URLs.

Parses generated citations, checks URL reachability, and verifies that references
are actually grounded in the fetched source texts by comparing keywords and sentences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from research_agent.utils.web_search import get_cached_webpage
from thread_wiki.models import SourceCitation
from utils import get_ssl_verify_config


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating a single web citation.

    Attributes:
        url: The URL that was validated.
        reachable: Whether the URL returned a successful HTTP response.
        grounded: Whether the claim is supported by the page content.
        reason: Human-readable explanation of the validation outcome.
    """

    url: str
    reachable: bool
    grounded: bool
    reason: str


_STOP_WORDS = {
    'we', 'our', 'us', 'the', 'a', 'an', 'and', 'or', 'but', 'is', 'are', 'was', 'were', 'be', 'been',
    'have', 'has', 'had', 'do', 'does', 'did', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
    'about', 'as', 'into', 'through', 'during', 'under', 'over', 'between', 'out', 'off', 'both', 'each',
    'few', 'more', 'most', 'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too',
    'very', 's', 't', 'can', 'will', 'just', 'should', 'now', 'i', 'you', 'he', 'she', 'it', 'they', 'them',
    'my', 'your', 'his', 'her', 'its', 'their', 'this', 'that', 'these', 'those'
}


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


async def _check_url_reachable(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Lightweight async URL reachability check."""
    verify_ssl = get_ssl_verify_config()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(verify=verify_ssl, headers=headers, timeout=timeout) as client:
            try:
                resp = await client.head(url)
                if resp.status_code < 400:
                    return True, "Reachable"
            except Exception:
                pass

            resp = await client.get(url)
            if resp.status_code < 400:
                return True, "Reachable"
            else:
                return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


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

    for cit in citations:
        if cit.kind != "web":
            continue

        url = cit.url or cit.raw_path
        if not url:
            continue

        reachable, reach_reason = await _check_url_reachable(url)

        content = None
        if fetched_contents and url in fetched_contents:
            content = fetched_contents[url]
        else:
            content = get_cached_webpage(url)

        if not content:
            if not reachable:
                results.append(ValidationResult(
                    url=url,
                    reachable=False,
                    grounded=False,
                    reason=f"URL unreachable: {reach_reason}"
                ))
            else:
                results.append(ValidationResult(
                    url=url,
                    reachable=True,
                    grounded=False,
                    reason="Grounding skipped (source page content not fetched during run)"
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
                url=url,
                reachable=reachable,
                grounded=False,
                reason="Grounded check failed: claim context could not be extracted"
            ))
            continue

        is_grounded = _is_claim_grounded(claim, content)
        if is_grounded:
            results.append(ValidationResult(
                url=url,
                reachable=True,
                grounded=True,
                reason="Reachable and grounded (claim matches source content)"
            ))
        else:
            results.append(ValidationResult(
                url=url,
                reachable=True,
                grounded=False,
                reason=f"Claim not found in content (Claim: '{claim[:100]}...')"
            ))

    return results
