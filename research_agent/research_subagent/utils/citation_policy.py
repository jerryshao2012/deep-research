"""Deterministic structural checks for web citations in Markdown reports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal
from urllib.parse import urlsplit, urlunsplit

CitationDefectCode = Literal[
    "missing_url",
    "placeholder_source",
    "unresolved_reference",
    "malformed_reference",
]

_MAX_DEFECTS = 16
_MAX_DETAIL_NUMBER = 999
_SOURCE_HEADINGS = {"sources", "references", "bibliography", "works cited"}
_PLACEHOLDER_RE = re.compile(
    r"\b(?:conceptual\s+source|placeholder|example\s+source|source\s+needed|"
    r"citation\s+needed|tbd)\b",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$")
_ENTRY_RE = re.compile(r"^[ \t]*(?:\[(?P<bracket>\d{1,4})\][ \t]*:?|(?P<dot>\d{1,4})\.)[ \t]+(?P<body>.*)$")
_DEFINITION_RE = re.compile(r"^[ \t]*\[\d{1,4}\][ \t]*:")
_LINK_RE = re.compile(
    r"(?<!\\)\[(?P<label>(?:[^\[\]\\\n]|\\.|\[[^\[\]\\\n]*\])*)\]"
    r"\([ \t]*(?P<url>[^()\s]+)(?:[ \t]+[^)]*)?\)",
)
_ANY_SCHEME_URL_RE = re.compile(r"(?<![\w+.-])(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s<>()\[\]{}\"']*)")
_NUMERIC_MARKER_RE = re.compile(r"(?<!\\)\[(?P<body>[^\]\n]{1,80})\]")


@dataclass(frozen=True, slots=True)
class CitationDefect:
    """One bounded, report-safe structural citation failure."""

    code: CitationDefectCode
    detail: str


@dataclass(frozen=True, slots=True)
class CitationAudit:
    """Normalized web URLs and deterministic citation failures."""

    urls: tuple[str, ...]
    defects: tuple[CitationDefect, ...]


def audit_web_citations(report: str) -> CitationAudit:
    """Audit report citation structure without I/O, networking, or model calls."""
    visible = _mask_fenced_and_inline_code(report)
    source_ranges = _source_ranges(visible)
    entry_spans = _numbered_source_entries(visible, source_ranges)
    links = tuple(_LINK_RE.finditer(visible))
    defects: list[CitationDefect] = []
    valid_urls: set[str] = set()

    def add(code: CitationDefectCode, detail: str) -> None:
        if len(defects) < _MAX_DEFECTS:
            defects.append(CitationDefect(code, detail))

    invalid_entry_spans: set[tuple[int, int]] = set()
    source_urls: dict[int, str] = {}
    for span, number, body in entry_spans:
        if _PLACEHOLDER_RE.search(body):
            add("placeholder_source", _source_detail(number))
            invalid_entry_spans.add(span)

    invalid_link_spans: set[tuple[int, int]] = set()
    for link in links:
        if _PLACEHOLDER_RE.search(link.group("label")):
            add("placeholder_source", "link")
            invalid_link_spans.add(link.span())

    candidates: list[tuple[str, tuple[int, int] | None, int | None]] = []
    for link in links:
        candidates.append((link.group("url"), link.span(), None))
    for match in _ANY_SCHEME_URL_RE.finditer(visible):
        if not source_ranges or _is_source_candidate(match.span(), source_ranges, entry_spans):
            candidates.append((match.group("url"), match.span(), None))

    seen_candidates: set[tuple[int, int]] = set()
    for raw_url, span, _ in candidates:
        if span is not None and span in seen_candidates:
            continue
        if span is not None:
            seen_candidates.add(span)
        if span is not None and _span_is_contained(
            span, invalid_link_spans | invalid_entry_spans
        ):
            continue
        normalized = _normalise_web_url(raw_url)
        if normalized is None:
            if _is_source_candidate(span, source_ranges, entry_spans) or raw_url.lower().startswith(("http", "ftp")):
                add("malformed_reference", "url")
            continue
        if _is_reserved_host(normalized):
            add("placeholder_source", "url")
            continue
        valid_urls.add(normalized)

    for span, number, body in entry_spans:
        if span in invalid_entry_spans:
            continue
        for raw_url in _urls_in_text(body):
            normalized = _normalise_web_url(raw_url)
            if normalized is not None and not _is_reserved_host(normalized):
                source_urls.setdefault(number, normalized)
                break

    prose = _mask_spans(visible, [*source_ranges, *(link.span() for link in links), *_entry_spans_to_ranges(entry_spans)])
    for marker in _NUMERIC_MARKER_RE.finditer(prose):
        _audit_numeric_marker(marker.group("body"), source_urls, add)

    if not valid_urls:
        add("missing_url", "web")
    return CitationAudit(tuple(sorted(valid_urls)), tuple(defects))


def _mask_fenced_and_inline_code(text: str) -> str:
    lines = text.splitlines(keepends=True)
    masked: list[str] = []
    fence: str | None = None
    for line in lines:
        match = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if fence is not None:
            masked.append(_blank(line))
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            continue
        if match:
            fence = match.group(1)
            masked.append(_blank(line))
        else:
            masked.append(re.sub(r"`[^`\n]*`", _blank_match, line))
    return "".join(masked)


def _source_ranges(text: str) -> list[tuple[int, int]]:
    lines = text.splitlines(keepends=True)
    positions: list[tuple[int, int, str]] = []
    offset = 0
    for line in lines:
        match = _HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            positions.append((offset, len(match.group("marks")), match.group("title").strip().rstrip(":").lower()))
        offset += len(line)
    ranges: list[tuple[int, int]] = []
    for index, (start, level, title) in enumerate(positions):
        if title not in _SOURCE_HEADINGS:
            continue
        end = len(text)
        for next_start, next_level, _ in positions[index + 1 :]:
            if next_level <= level:
                end = next_start
                break
        line_end = text.find("\n", start)
        ranges.append((len(text) if line_end < 0 else line_end + 1, end))
    return ranges


def _numbered_source_entries(
    text: str, source_ranges: list[tuple[int, int]]
) -> list[tuple[tuple[int, int], int, str]]:
    entries: list[tuple[tuple[int, int], int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        line_end = offset + len(line)
        match = _ENTRY_RE.match(line.rstrip("\r\n"))
        in_sources = any(start <= offset < end for start, end in source_ranges)
        definition = bool(_DEFINITION_RE.match(line))
        if match and (in_sources or definition):
            number = int(match.group("bracket") or match.group("dot"))
            if 1 <= number <= _MAX_DETAIL_NUMBER:
                span = (offset, line_end)
                entries.append((span, number, match.group("body")))
        offset = line_end
    return entries


def _audit_numeric_marker(
    body: str,
    source_urls: dict[int, str],
    add_defect: Callable[[CitationDefectCode, str], None],
) -> None:
    parts = re.split(r"[;,]", body)
    if not parts or any(not part.strip() for part in parts):
        add_defect("malformed_reference", "marker")
        return
    for part in parts:
        value = part.strip()
        single = re.fullmatch(r"\d{1,4}", value)
        range_match = re.fullmatch(r"(\d{1,4})\s*-\s*(\d{1,4})", value)
        if single:
            _check_reference(int(value), source_urls, add_defect)
        elif range_match:
            first, last = (int(group) for group in range_match.groups())
            if not (1 <= first <= last <= _MAX_DETAIL_NUMBER):
                add_defect("malformed_reference", "range")
            else:
                for number in range(first, last + 1):
                    _check_reference(number, source_urls, add_defect)
        elif re.match(r"\d", value):
            add_defect("malformed_reference", "marker")


def _check_reference(
    number: int,
    source_urls: dict[int, str],
    add: Callable[[CitationDefectCode, str], None],
) -> None:
    if not 1 <= number <= _MAX_DETAIL_NUMBER:
        add("malformed_reference", "number")
    elif number not in source_urls:
        add("unresolved_reference", _source_detail(number))


def _urls_in_text(text: str) -> tuple[str, ...]:
    values = [match.group("url") for match in _LINK_RE.finditer(text)]
    values.extend(match.group("url") for match in _ANY_SCHEME_URL_RE.finditer(text))
    return tuple(values)


def _normalise_web_url(raw_url: str) -> str | None:
    value = raw_url.strip().strip("<>").rstrip(".,;:!?")
    while value.endswith(")") and value.count("(") < value.count(")"):
        value = value[:-1]
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "", parsed.query, ""))


def _is_reserved_host(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "localhost" or host.endswith(".localhost") or host.endswith((".example", ".invalid", ".test")) or any(
        host == root or host.endswith(f".{root}") for root in ("example.com", "example.org", "example.net")
    )


def _is_source_candidate(
    span: tuple[int, int] | None,
    source_ranges: list[tuple[int, int]],
    entries: list[tuple[tuple[int, int], int, str]],
) -> bool:
    if span is None:
        return False
    return any(start <= span[0] < end for start, end in source_ranges) or any(
        start <= span[0] < end for (start, end), _, _ in entries
    )


def _span_is_contained(span: tuple[int, int], containers: set[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in containers)


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _entry_spans_to_ranges(entries: list[tuple[tuple[int, int], int, str]]) -> list[tuple[int, int]]:
    return [span for span, _, _ in entries]


def _blank(value: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in value)


def _blank_match(match: re.Match[str]) -> str:
    return _blank(match.group(0))


def _source_detail(number: int) -> str:
    return f"source:{min(max(number, 1), _MAX_DETAIL_NUMBER)}"
