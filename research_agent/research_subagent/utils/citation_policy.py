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
_MAX_MARKER_BODY_LENGTH = 256
_MAX_MARKER_TOKENS = 32
_MAX_LINK_LABEL_LENGTH = 512
_MAX_LINK_DESTINATION_LENGTH = 2_048
_MAX_LINK_NESTING = 8
_SOURCE_HEADINGS = {"sources", "references", "bibliography", "works cited"}
_PLACEHOLDER_RE = re.compile(
    r"\b(?:conceptual\s+source|placeholder|example\s+source|source\s+needed|"
    r"citation\s+needed|tbd)\b",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$")
_ENTRY_RE = re.compile(r"^[ \t]*(?:\[(?P<bracket>\d{1,4})\][ \t]*:?|(?P<dot>\d{1,4})\.)[ \t]+(?P<body>.*)$")
_DEFINITION_RE = re.compile(r"^[ \t]*\[\d{1,4}\][ \t]*:")


@dataclass(frozen=True, order=True, slots=True)
class CitationDefect:
    """One bounded, report-safe structural citation failure."""

    code: CitationDefectCode
    detail: str


@dataclass(frozen=True, slots=True)
class CitationAudit:
    """Normalized web URLs and deterministic citation failures."""

    urls: tuple[str, ...]
    defects: tuple[CitationDefect, ...]


@dataclass(frozen=True, slots=True)
class _MarkdownLink:
    label: str
    url: str
    span: tuple[int, int]


def audit_web_citations(report: str) -> CitationAudit:
    """Audit report citation structure without I/O, networking, or model calls."""
    visible = _mask_fenced_and_inline_code(report)
    source_ranges = _source_ranges(visible)
    entry_spans = _numbered_source_entries(visible, source_ranges)
    links, malformed_link_spans = _scan_markdown_links(visible)
    defects: list[CitationDefect] = []
    valid_urls: set[str] = set()

    def add(code: CitationDefectCode, detail: str) -> None:
        defects.append(CitationDefect(code, detail))

    invalid_entry_spans: set[tuple[int, int]] = set()
    source_urls: dict[int, str] = {}
    for span, number, body in entry_spans:
        if _PLACEHOLDER_RE.search(body):
            add("placeholder_source", _source_detail(number))
            invalid_entry_spans.add(span)

    invalid_link_spans: set[tuple[int, int]] = set()
    for _ in malformed_link_spans:
        add("malformed_reference", "link")
    for link in links:
        if _PLACEHOLDER_RE.search(link.label):
            add("placeholder_source", "link")
            invalid_link_spans.add(link.span)
        numeric_label = _numeric_link_label(link.label)
        if numeric_label is not None and not 1 <= numeric_label <= _MAX_DETAIL_NUMBER:
            add("malformed_reference", "number")

    candidates: list[tuple[str, tuple[int, int]]] = []
    for link in links:
        candidates.append((link.url, link.span))
    link_spans = {link.span for link in links}
    for raw_url, span in _scan_uri_tokens(visible):
        if not _span_is_within_any(span, link_spans):
            candidates.append((raw_url, span))

    seen_candidates: set[tuple[int, int]] = set()
    for raw_url, span in candidates:
        if span in seen_candidates:
            continue
        seen_candidates.add(span)
        if _span_is_contained(span, invalid_link_spans | invalid_entry_spans):
            continue
        normalized = _normalise_web_url(raw_url)
        if normalized is None:
            if _is_citation_link(span, links, source_ranges, entry_spans) or _is_source_candidate(
                span, source_ranges, entry_spans
            ) or _is_explicit_uri(raw_url):
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

    prose = _mask_spans(
        visible,
        [*source_ranges, *(link.span for link in links), *malformed_link_spans, *_entry_spans_to_ranges(entry_spans)],
    )
    _scan_numeric_markers(prose, source_urls, add)

    if not valid_urls:
        add("missing_url", "web")
    return CitationAudit(tuple(sorted(valid_urls)), _normalise_defects(defects))


def _mask_fenced_and_inline_code(text: str) -> str:
    lines = text.splitlines(keepends=True)
    masked: list[str] = []
    fence: str | None = None
    for line in lines:
        line_fence = _line_fence(line)
        if fence is not None:
            masked.append(_blank(line))
            if (
                line_fence
                and line_fence[0] == fence[0]
                and line_fence[1] >= len(fence)
                and _is_fence_suffix(line[line_fence[2] :])
            ):
                fence = None
            continue
        if line_fence:
            fence = line_fence[0] * line_fence[1]
            masked.append(_blank(line))
        else:
            masked.append(_mask_inline_code(line))
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
    if not re.match(r"\s*\d", body):
        return
    if len(body) > _MAX_MARKER_BODY_LENGTH:
        add_defect("malformed_reference", "marker")
        return
    parts = re.split(r"[;,]", body)
    if len(parts) > _MAX_MARKER_TOKENS:
        add_defect("malformed_reference", "marker")
        return
    for part in parts:
        value = part.strip()
        if not value:
            add_defect("malformed_reference", "marker")
            continue
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
        else:
            add_defect("malformed_reference", "marker")


def _scan_numeric_markers(
    text: str,
    source_urls: dict[int, str],
    add_defect: Callable[[CitationDefectCode, str], None],
) -> None:
    index = 0
    text_length = len(text)
    while index < text_length:
        if text[index] == "\\":
            index += 2
            continue
        if text[index] != "[":
            index += 1
            continue
        body_start = index + 1
        cursor = body_start
        first_nonspace: str | None = None
        nested_open = False
        while cursor < text_length and text[cursor] not in "]\n":
            if first_nonspace is None and not text[cursor].isspace():
                first_nonspace = text[cursor]
            if text[cursor] == "[":
                nested_open = True
                break
            cursor += 1
        body_length = cursor - body_start
        if nested_open or cursor >= text_length or text[cursor] != "]":
            if first_nonspace is not None and first_nonspace.isdigit() and body_length > _MAX_MARKER_BODY_LENGTH:
                add_defect("malformed_reference", "marker")
            elif first_nonspace is not None and first_nonspace.isdigit() and nested_open:
                add_defect("malformed_reference", "marker")
            index = body_start
            continue
        if first_nonspace is not None and first_nonspace.isdigit():
            if body_length > _MAX_MARKER_BODY_LENGTH:
                add_defect("malformed_reference", "marker")
            else:
                _audit_numeric_marker(text[body_start:cursor], source_urls, add_defect)
        index = cursor + 1


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
    links, _ = _scan_markdown_links(text)
    values = [link.url for link in links]
    link_spans = {link.span for link in links}
    values.extend(raw_url for raw_url, span in _scan_uri_tokens(text) if not _span_is_within_any(span, link_spans))
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
    host = parsed.hostname.lower().rstrip(".")
    if not host:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "", parsed.query, ""))


def _is_reserved_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
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


def _is_citation_link(
    span: tuple[int, int],
    links: tuple[_MarkdownLink, ...],
    source_ranges: list[tuple[int, int]],
    entries: list[tuple[tuple[int, int], int, str]],
) -> bool:
    if _is_source_candidate(span, source_ranges, entries):
        return True
    return any(link.span == span and _numeric_link_label(link.label) is not None for link in links)


def _numeric_link_label(label: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*", label)
    if not match:
        return None
    digits = match.group(1).lstrip("0") or "0"
    return _MAX_DETAIL_NUMBER + 1 if len(digits) > 3 else int(digits)


def _span_is_contained(span: tuple[int, int], containers: set[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in containers)


def _span_is_within_any(span: tuple[int, int], containers: set[tuple[int, int]]) -> bool:
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


def _line_fence(line: str) -> tuple[str, int, int] | None:
    index = 0
    while index < len(line) and line[index] == " " and index < 3:
        index += 1
    if index < len(line) and line[index] == " ":
        return None
    if index >= len(line) or line[index] not in "`~":
        return None
    marker = line[index]
    end = index
    while end < len(line) and line[end] == marker:
        end += 1
    return (marker, end - index, end) if end - index >= 3 else None


def _is_fence_suffix(value: str) -> bool:
    return all(character in " \t\r\n" for character in value)


def _scan_markdown_links(text: str) -> tuple[tuple[_MarkdownLink, ...], tuple[tuple[int, int], ...]]:
    links: list[_MarkdownLink] = []
    malformed: list[tuple[int, int]] = []
    index = 0
    text_length = len(text)
    while index < text_length:
        if text[index] == "\\":
            index += 2
            continue
        if text[index] != "[":
            index += 1
            continue
        start = index
        label_end, overflow = _scan_link_label(text, start)
        if overflow:
            malformed.append((start, max(start + 1, label_end)))
            index = max(start + 1, label_end)
            continue
        if label_end is None or label_end + 1 >= text_length or text[label_end + 1] != "(":
            index = start + 1
            continue
        destination_start = label_end + 2
        while destination_start < text_length and text[destination_start] in " \t":
            destination_start += 1
        destination_end, closing, overflow = _scan_link_destination(text, destination_start)
        if overflow or destination_end is None or closing is None:
            malformed.append((start, max(start + 1, destination_end or destination_start)))
            index = max(start + 1, destination_end or destination_start)
            continue
        label = _unescape_markdown(text[start + 1 : label_end])
        url = _unescape_markdown(text[destination_start:destination_end])
        if not url:
            malformed.append((start, closing + 1))
        else:
            links.append(_MarkdownLink(label, url, (start, closing + 1)))
        index = closing + 1
    return tuple(links), tuple(malformed)


def _scan_link_label(text: str, start: int) -> tuple[int | None, bool]:
    depth = 1
    index = start + 1
    while index < len(text):
        if index - start > _MAX_LINK_LABEL_LENGTH:
            return index, True
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == "\n":
            return None, False
        if character == "[":
            depth += 1
            if depth > _MAX_LINK_NESTING:
                return index + 1, True
        elif character == "]":
            depth -= 1
            if depth == 0:
                return index, False
        index += 1
    return None, False


def _scan_link_destination(text: str, start: int) -> tuple[int | None, int | None, bool]:
    depth = 0
    index = start
    while index < len(text):
        if index - start > _MAX_LINK_DESTINATION_LENGTH:
            return index, None, True
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == "\n":
            return None, None, False
        if character in " \t" and depth == 0:
            title_start = index
            while title_start < len(text) and text[title_start] in " \t":
                title_start += 1
            if title_start < len(text) and text[title_start] == ")":
                return index, title_start, False
            closing, overflow = _scan_link_title(text, title_start)
            return (index, closing, overflow) if closing is not None else (None, None, overflow)
        if character == "(":
            depth += 1
            if depth > _MAX_LINK_NESTING:
                return index + 1, None, True
        elif character == ")":
            if depth == 0:
                return index, index, False
            depth -= 1
        index += 1
    return None, None, False


def _scan_link_title(text: str, start: int) -> tuple[int | None, bool]:
    if start >= len(text):
        return None, False
    delimiter = text[start]
    if delimiter in "\"'":
        index = start + 1
        while index < len(text):
            if index - start > _MAX_LINK_DESTINATION_LENGTH:
                return None, True
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == "\n":
                return None, False
            if text[index] == delimiter:
                index += 1
                while index < len(text) and text[index] in " \t":
                    index += 1
                return (index, False) if index < len(text) and text[index] == ")" else (None, False)
            index += 1
        return None, False
    if delimiter != "(":
        return None, False
    depth = 1
    index = start + 1
    while index < len(text):
        if index - start > _MAX_LINK_DESTINATION_LENGTH:
            return None, True
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "\n":
            return None, False
        if text[index] == "(":
            depth += 1
            if depth > _MAX_LINK_NESTING:
                return None, True
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                index += 1
                while index < len(text) and text[index] in " \t":
                    index += 1
                return (index, False) if index < len(text) and text[index] == ")" else (None, False)
        index += 1
    return None, False


def _unescape_markdown(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 1
        result.append(value[index])
        index += 1
    return "".join(result)


def _scan_uri_tokens(text: str) -> tuple[tuple[str, tuple[int, int]], ...]:
    tokens: list[tuple[str, tuple[int, int]]] = []
    index = 0
    while index < len(text):
        if not text[index].isalpha() or (index and _is_scheme_character(text[index - 1])):
            index += 1
            continue
        start = index
        index += 1
        while index < len(text) and _is_scheme_character(text[index]):
            index += 1
        if index >= len(text) or text[index] != ":":
            continue
        content_start = index + 1
        if content_start >= len(text) or text[content_start].isspace():
            index = content_start
            continue
        end = content_start
        parenthesis_depth = 0
        while end < len(text):
            character = text[end]
            if character.isspace() or character in "<>[]{}\"'":
                break
            if character == "(":
                parenthesis_depth += 1
            elif character == ")":
                if parenthesis_depth == 0:
                    break
                parenthesis_depth -= 1
            end += 1
        if end > content_start and _is_clear_bare_uri(text[start:index], text[content_start:end]):
            tokens.append((text[start:end], (start, end)))
        index = max(end, content_start)
    return tuple(tokens)


def _is_scheme_character(character: str) -> bool:
    return character.isalnum() or character in "+.-"


def _is_clear_bare_uri(scheme: str, content: str) -> bool:
    if not content:
        return False
    normalized_scheme = scheme.lower()
    if normalized_scheme in {"isbn", "doi"}:
        return False
    return not (len(scheme) == 1 and content[0] in "\\/")


def _is_explicit_uri(value: str) -> bool:
    if not value or not value[0].isalpha():
        return False
    index = 1
    while index < len(value) and _is_scheme_character(value[index]):
        index += 1
    return index + 1 < len(value) and value[index] == ":" and not value[index + 1].isspace()


def _mask_inline_code(line: str) -> str:
    changes = [0] * (len(line) + 1)
    runs: list[tuple[int, int, int]] = []
    index = 0
    while index < len(line):
        if line[index] != "`":
            index += 1
            continue
        start = index
        while index < len(line) and line[index] == "`":
            index += 1
        runs.append((start, index, index - start))
    next_same: list[int | None] = [None] * len(runs)
    next_by_length: dict[int, int] = {}
    for run_index in range(len(runs) - 1, -1, -1):
        _, _, delimiter_length = runs[run_index]
        next_same[run_index] = next_by_length.get(delimiter_length)
        next_by_length[delimiter_length] = run_index
    run_index = 0
    while run_index < len(runs):
        closing_index = next_same[run_index]
        if closing_index is None:
            run_index += 1
            continue
        opening_start, _, _ = runs[run_index]
        _, closing_end, _ = runs[closing_index]
        changes[opening_start] += 1
        changes[closing_end] -= 1
        run_index = closing_index + 1
    active = 0
    chars = list(line)
    for index, character in enumerate(chars):
        active += changes[index]
        if active and character != "\n":
            chars[index] = " "
    return "".join(chars)


def _normalise_defects(defects: list[CitationDefect]) -> tuple[CitationDefect, ...]:
    unique = sorted(set(defects))
    representatives = [
        next(defect for defect in unique if defect.code == code)
        for code in sorted({defect.code for defect in unique})
    ]
    selected = set(representatives)
    for defect in unique:
        if len(selected) >= _MAX_DEFECTS:
            break
        selected.add(defect)
    return tuple(sorted(selected))


def _source_detail(number: int) -> str:
    return f"source:{min(max(number, 1), _MAX_DETAIL_NUMBER)}"
