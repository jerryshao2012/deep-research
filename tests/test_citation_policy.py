from __future__ import annotations

import tracemalloc
from dataclasses import FrozenInstanceError
from time import perf_counter

import pytest

from research_agent.research_subagent.utils.citation_policy import (
    CitationAudit,
    CitationDefect,
    audit_web_citations,
)


def codes(report: str) -> list[str]:
    return [defect.code for defect in audit_web_citations(report).defects]


def test_accepts_numbered_sources_and_normalizes_unique_urls() -> None:
    report = """Claim one [1]; claim two [2].

## Sources
[1] [Primary](https://one.publisher.org/a),
2. https://two.publisher.org/path).
[3]: https://one.publisher.org/a
"""

    assert audit_web_citations(report) == CitationAudit(
        urls=("https://one.publisher.org/a", "https://two.publisher.org/path"),
        defects=(),
    )


@pytest.mark.parametrize("heading", ["Sources", "References", "Bibliography", "Works Cited"])
def test_recognizes_each_source_heading_and_stops_at_peer_heading(heading: str) -> None:
    report = f"""A claim [1].

## {heading}
[1] https://good.publisher.org/reference

## Notes
[2] https://example.com/not-a-source
"""

    audit = audit_web_citations(report)

    assert audit.urls == ("https://good.publisher.org/reference",)


def test_accepts_inline_numbered_link() -> None:
    audit = audit_web_citations("Claim [1](https://source.publisher.org/path).")

    assert audit.urls == ("https://source.publisher.org/path",)
    assert audit.defects == ()


def test_reports_missing_url_without_concrete_http_source() -> None:
    assert codes("## Sources\n[1] A peer-reviewed book") == ["missing_url"]


@pytest.mark.parametrize(
    "bad_source",
    [
        "Conceptual Source",
        "placeholder",
        "example source",
        "source needed",
        "citation needed",
        "TBD",
        "[Reference](https://example.com/a)",
        "https://docs.example.org/a",
        "https://example.net/a",
        "https://sub.example.com/a",
        "https://localhost/a",
        "https://host.example/a",
        "https://host.invalid/a",
        "https://host.test/a",
        "https://host.localhost/a",
    ],
)
def test_rejects_placeholder_sources_and_reserved_hosts(bad_source: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {bad_source}")

    assert "placeholder_source" in [defect.code for defect in audit.defects]


@pytest.mark.parametrize("value", ["ftp://source.example.org/a", "https:///missing-authority"])
def test_rejects_non_web_or_authorityless_urls(value: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {value}")

    assert "malformed_reference" in [defect.code for defect in audit.defects]
    assert "missing_url" in [defect.code for defect in audit.defects]


def test_placeholder_link_does_not_count_as_concrete_source() -> None:
    audit = audit_web_citations("[Example Source](https://valid.publisher.org/a)")

    assert [defect.code for defect in audit.defects] == ["missing_url", "placeholder_source"]


@pytest.mark.parametrize(
    "url",
    [
        "https://EXAMPLE.com./a",
        "https://sub.example.org./a",
        "https://host.test./a",
    ],
)
def test_rejects_terminal_dot_reserved_hosts(url: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {url}")

    assert "placeholder_source" in [defect.code for defect in audit.defects]
    assert audit.urls == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://alice@public.publisher.org/report",
        "https://alice:secret@public.publisher.org/report",
    ],
)
def test_rejects_credential_bearing_web_urls(url: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {url}")

    assert "malformed_reference" in [defect.code for defect in audit.defects]
    assert audit.urls == ()


@pytest.mark.parametrize(
    "destination",
    [
        "mailto:author@publisher.org",
        "file:///tmp/source",
        "ftp://publisher.org/a",
        "javascript:void",
        "javascript:alert(1)",
    ],
)
def test_rejects_non_web_citation_links_even_with_valid_source(destination: str) -> None:
    report = f"""Claim [1](https://valid.publisher.org/a) and [2]({destination}).

## Sources
[1] https://valid.publisher.org/a
"""

    assert "malformed_reference" in [defect.code for defect in audit_web_citations(report).defects]


@pytest.mark.parametrize("marker", ["[1, nope]", "[1,,2]", "[1;]", "[1--2]"])
def test_marks_mixed_or_malformed_numeric_groups(marker: str) -> None:
    report = f"""Claims {marker}.

## Sources
[1] https://one.publisher.org/a
[2] https://two.publisher.org/a
"""

    assert "malformed_reference" in [defect.code for defect in audit_web_citations(report).defects]


def test_keeps_prose_bare_urls_when_sources_heading_exists() -> None:
    report = """See https://prose.publisher.org/a.

## Sources
[1] https://source.publisher.org/a
"""

    assert audit_web_citations(report).urls == (
        "https://prose.publisher.org/a",
        "https://source.publisher.org/a",
    )


def test_masks_variable_length_inline_code_spans() -> None:
    report = """Actual [1]. ``[88]`` and ```[77]```.

## Sources
[1] https://source.publisher.org/a
"""

    assert audit_web_citations(report).defects == ()


def test_normalizes_deduplicates_and_caps_defects_without_hiding_codes() -> None:
    report = " ".join(f"[{number}]" for number in range(1, 50))
    audit = audit_web_citations(report)

    assert audit.defects == tuple(sorted(set(audit.defects)))
    assert len(audit.defects) <= 16
    assert {defect.code for defect in audit.defects} == {"missing_url", "unresolved_reference"}
    assert CitationDefect("missing_url", "web") < CitationDefect("unresolved_reference", "source:1")


@pytest.mark.parametrize("marker", [f"[1, {'nope ' * 80}]", "[1," + "2," * 80 + "3]"])
def test_rejects_oversized_numeric_groups_without_fail_open(marker: str) -> None:
    report = f"""Claim {marker}.

## Sources
[1] https://source.publisher.org/a
"""

    audit = audit_web_citations(report)

    assert "malformed_reference" in [defect.code for defect in audit.defects]
    assert audit.urls == ("https://source.publisher.org/a",)


@pytest.mark.parametrize("label", ["0", "1000", "12345678901234567890"])
@pytest.mark.parametrize("destination", ["https://valid.publisher.org/a", "mailto:author@publisher.org"])
def test_rejects_out_of_range_numeric_link_labels(label: str, destination: str) -> None:
    audit = audit_web_citations(f"Claim [{label}]({destination}).")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


def test_rejects_very_long_numeric_link_label_without_parsing_it_as_an_int() -> None:
    audit = audit_web_citations(f"Claim [{'9' * 5_000}](https://valid.publisher.org/a).")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


def test_preserves_balanced_parentheses_in_bare_and_markdown_urls() -> None:
    report = """See https://publisher.org/Foo_(bar)).

## Sources
[1] [Source](https://publisher.org/Foo_(bar)).
"""

    assert audit_web_citations(report).urls == ("https://publisher.org/Foo_(bar)",)


def test_fenced_code_requires_a_valid_closing_fence() -> None:
    report = """```
[99]
```not-a-close
[88]
```
Actual [1].

## Sources
[1] https://source.publisher.org/a
"""

    assert audit_web_citations(report).defects == ()


def test_four_space_fence_cannot_close_an_open_fence() -> None:
    report = """```
[99]
    ```
[88]
```
Actual [1].

## Sources
[1] https://source.publisher.org/a
"""

    assert audit_web_citations(report).defects == ()


def test_link_scanner_handles_nested_parentheses_and_bounds_adversarial_brackets() -> None:
    nested = audit_web_citations("[1](https://publisher.org/a((b)))")
    adversarial = audit_web_citations("[" * 10_000 + "](https://valid.publisher.org/a)")

    assert nested.urls == ("https://publisher.org/a((b))",)
    assert [defect.code for defect in adversarial.defects].count("malformed_reference") == 1


@pytest.mark.parametrize("title", ['"A source title"', "'A source title'", "(A source title)"])
def test_link_destination_excludes_optional_markdown_title(title: str) -> None:
    report = f"""Claim [1](https://publisher.org/article {title}).

## Sources
[1] https://publisher.org/article
"""

    assert audit_web_citations(report).urls == ("https://publisher.org/article",)
    assert audit_web_citations(report).defects == ()


@pytest.mark.parametrize("suffix", ['"unterminated', "unexpected-title"])
def test_rejects_malformed_link_title_or_destination(suffix: str) -> None:
    audit = audit_web_citations(f"Claim [1](https://publisher.org/article {suffix})")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


@pytest.mark.parametrize("destination", ["s3://bucket/source", "file:///tmp/source", "mailto:author@publisher.org", "javascript:void", "data:text/plain,source", "ftp://publisher.org/source"])
def test_rejects_any_explicit_non_http_uri_scheme(destination: str) -> None:
    report = f"""Claim [1](https://valid.publisher.org/a) and [2]({destination}).

## Sources
[1] https://valid.publisher.org/a
[2] {destination}
"""

    assert "malformed_reference" in [defect.code for defect in audit_web_citations(report).defects]


@pytest.mark.parametrize("token", ["s3://bucket/source", "file:///tmp/source", "mailto:author@publisher.org", "javascript:void", "data:text/plain,source", "ftp://publisher.org/source", "custom:**"])
def test_rejects_explicit_non_http_bare_source_token(token: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {token}\n[2] https://valid.publisher.org/a")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


def test_rejects_explicit_non_http_uri_in_non_numeric_markdown_link() -> None:
    audit = audit_web_citations("See [storage](s3://bucket/source) and https://valid.publisher.org/a")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


def test_ignores_prose_word_colon_without_uri_content() -> None:
    audit = audit_web_citations("A prose word: continues. https://valid.publisher.org/a")

    assert audit.defects == ()


def test_ignores_bold_list_label_colon_as_markdown_not_uri() -> None:
    report = """Claim supported by source [1].

*   **Attributes:** These are properties associated with nodes and edges [1].
*   **Message Passing:** Information is aggregated from neighboring nodes [1].

### Sources
1. Publisher: Graph overview (https://valid.publisher.org/a)
"""

    audit = audit_web_citations(report)

    assert audit.urls == ("https://valid.publisher.org/a",)
    assert audit.defects == ()


def test_prior_closed_markdown_does_not_hide_explicit_non_http_uri() -> None:
    audit = audit_web_citations("**Note** custom:** https://valid.publisher.org/a")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


def test_escaped_markdown_opener_does_not_hide_explicit_non_http_uri() -> None:
    audit = audit_web_citations(r"\**fake custom:** https://valid.publisher.org/a")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


def test_long_escaped_markdown_opener_fails_closed_at_lookback_boundary() -> None:
    audit = audit_web_citations("\\" * 509 + "**f custom:** https://valid.publisher.org/a")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


@pytest.mark.parametrize(
    "prefix, suffix",
    [("* ", "*"), ("~ ", "~"), ("** ", "**"), ("__ ", "__")],
)
def test_invalid_markdown_openers_do_not_hide_explicit_non_http_uri(
    prefix: str,
    suffix: str,
) -> None:
    audit = audit_web_citations(f"{prefix}custom:{suffix} https://valid.publisher.org/a")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


def test_markdown_label_detection_scales_with_dense_uri_candidates() -> None:
    started = perf_counter()
    audit_web_citations("** " + "a:** " * 2_000 + "https://valid.publisher.org/a")
    small = perf_counter() - started
    started = perf_counter()
    audit_web_citations("** " + "a:** " * 16_000 + "https://valid.publisher.org/a")
    large = perf_counter() - started

    assert large < small * 24 + 0.2


def _unmatched_backtick_runs(size: int) -> str:
    chunks: list[str] = []
    length = 1
    used = 0
    while used + length + 1 <= size:
        chunks.append("`" * length + "x")
        used += length + 1
        length += 1
    return "".join(chunks)


def test_inline_code_masking_scales_for_unmatched_delimiter_runs() -> None:
    started = perf_counter()
    audit_web_citations(_unmatched_backtick_runs(20_000))
    fast = perf_counter() - started
    started = perf_counter()
    audit_web_citations(_unmatched_backtick_runs(160_000))
    slow = perf_counter() - started

    assert slow < fast * 12 + 0.1


def test_inline_code_pairs_sequentially_without_crossing_delimiters() -> None:
    report = """Actual [1]. `alpha ``beta` [77] ``

## Sources
[1] https://source.publisher.org/a
"""

    assert "unresolved_reference" in [defect.code for defect in audit_web_citations(report).defects]


def test_bare_windows_and_bibliographic_prefixes_are_not_uri_tokens() -> None:
    report = """## Sources
[1] C:\\docs\\book.pdf
[2] ISBN:978-1-2345-6789-0
[3] DOI:10.1234/example
"""

    assert audit_web_citations(report).defects == (CitationDefect("missing_url", "web"),)


@pytest.mark.parametrize("token", ["custom:value", "ssh:user@host"])
def test_rejects_any_other_rfc_bare_uri_scheme(token: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {token}\n[2] https://valid.publisher.org/a")

    assert "malformed_reference" in [defect.code for defect in audit.defects]


@pytest.mark.parametrize(
    "url",
    [
        "https://ｅｘａｍｐｌｅ．ｃｏｍ/a",
        "https://ｌｏｃａｌｈｏｓｔ/a",
        "https://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://169.254.1.1/a",
        "https://224.0.0.1/a",
        "https://0.0.0.0/a",
        "https://[::1]/a",
        "https://[fc00::1]/a",
        "https://2130706433/a",
        "https://0x7f000001/a",
        "https://0177.0.0.1/a",
    ],
)
def test_rejects_canonicalized_placeholder_and_nonpublic_hosts(url: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {url}")

    assert "placeholder_source" in [defect.code for defect in audit.defects]
    assert audit.urls == ()


@pytest.mark.parametrize("url", ["https://8.8.8.8/a", "https://[2606:4700:4700::1111]/dns-query"])
def test_accepts_public_literal_ip_hosts(url: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {url}")

    assert audit.defects == ()
    assert audit.urls


def _many_source_links(count: int) -> str:
    return "## Sources\n" + "\n".join(
        f"[1] https://source{index}.publisher.org/a" for index in range(count)
    )


def test_source_interval_membership_scales_near_linearly() -> None:
    started = perf_counter()
    audit_web_citations(_many_source_links(1_000))
    small = perf_counter() - started
    started = perf_counter()
    audit_web_citations(_many_source_links(16_000))
    large = perf_counter() - started

    assert large < small * 32 + 0.2


def test_repeated_numeric_ranges_keep_defects_bounded_during_discovery() -> None:
    report = " ".join("[1-999]" for _ in range(128)) + "\n\n## Sources\n[1] https://valid.publisher.org/a"
    tracemalloc.start()
    audit = audit_web_citations(report)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(audit.defects) <= 16
    assert peak < 8_000_000


def _alternating_headings(count: int) -> str:
    return "\n".join(
        f"## {'Sources' if index % 2 == 0 else 'Other'}\n[1] https://source{index}.publisher.org/a"
        for index in range(count)
    )


def test_source_heading_ranges_scale_without_suffix_slicing() -> None:
    started = perf_counter()
    audit_web_citations(_alternating_headings(1_000))
    small = perf_counter() - started
    started = perf_counter()
    audit_web_citations(_alternating_headings(16_000))
    large = perf_counter() - started

    assert large < small * 32 + 0.2


@pytest.mark.parametrize("url", ["https://127.1/a", "https://127.0.1/a", "https://0x7f.0.0.1/a", "https://127.0x1/a"])
def test_rejects_legacy_numeric_ipv4_aliases(url: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] {url}")

    assert "placeholder_source" in [defect.code for defect in audit.defects]


@pytest.mark.parametrize("host", ["a1.de", "cafe1.de", "1face.de"])
def test_accepts_dns_labels_that_only_superficially_look_numeric(host: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] https://{host}/a")

    assert audit.defects == ()
    assert audit.urls == (f"https://{host}/a",)


@pytest.mark.parametrize("host", ["08.com", "09.de", "0xfoo.com", "0xample.com", "huge.com"])
def test_accepts_dns_hosts_with_non_numeric_legacy_ipv4_components(host: str) -> None:
    audit = audit_web_citations(f"## Sources\n[1] https://{host}/a")

    assert audit.defects == ()
    assert audit.urls == (f"https://{host}/a",)


def test_rejects_single_out_of_range_numeric_host() -> None:
    audit = audit_web_citations("## Sources\n[1] https://4294967296/a")

    assert "placeholder_source" in [defect.code for defect in audit.defects]


def test_canonicalizes_public_legacy_numeric_ipv4() -> None:
    audit = audit_web_citations("## Sources\n[1] https://0x08080808/a")

    assert audit.defects == ()
    assert audit.urls == ("https://8.8.8.8/a",)


def test_masks_bracketed_ipv6_bare_uri_before_numeric_marker_scan() -> None:
    audit = audit_web_citations("https://[2606:4700:4700::1111]/dns-query")

    assert audit.defects == ()
    assert audit.urls == ("https://[2606:4700:4700::1111]/dns-query",)


def test_rejects_bare_loopback_ipv6_without_numeric_marker_defect() -> None:
    audit = audit_web_citations("https://[::1]/a")

    assert [defect.code for defect in audit.defects] == ["missing_url", "placeholder_source"]


def test_normalizes_only_hostname_and_preserves_fullwidth_path_query_and_fragment() -> None:
    audit = audit_web_citations("https://valid.publisher.org/Ｆｏｏ?x=ｙ#Ｆ")

    assert audit.urls == ("https://valid.publisher.org/Ｆｏｏ?x=ｙ#Ｆ",)


def test_numeric_scanner_is_bounded_for_unmatched_brackets() -> None:
    started = perf_counter()
    audit_web_citations("[" * 1_024)
    small = perf_counter() - started
    started = perf_counter()
    audit_web_citations("[" * 16_384)
    large = perf_counter() - started

    assert large < small * 32 + 0.1


def test_unmatched_numeric_bracket_advances_to_later_marker() -> None:
    audit = audit_web_citations("Broken [1 then [2]")

    assert {defect.code for defect in audit.defects} == {
        "malformed_reference",
        "missing_url",
        "unresolved_reference",
    }


def test_reports_unresolved_single_group_and_descending_markers() -> None:
    report = """Missing [1, 3; 5] and malformed [5-2].

## Sources
[1] https://source.publisher.org/one
"""

    assert codes(report) == [
        "malformed_reference",
        "unresolved_reference",
        "unresolved_reference",
    ]


def test_expands_bounded_ascending_numeric_ranges() -> None:
    report = """Claims [2-4].

## References
[2] https://source.publisher.org/two
[3] https://source.publisher.org/three
[4] https://source.publisher.org/four
"""

    assert audit_web_citations(report).defects == ()


def test_ignores_markers_in_code_source_entries_escaped_text_and_link_labels() -> None:
    report = r"""Actual claim [1].
`[99]` \[88] [look [77]](https://label.publisher.org/a)
```markdown
[66]
```

## Sources
[1] https://source.publisher.org/one
[55] https://source.publisher.org/entry
"""

    assert audit_web_citations(report).defects == ()


def test_audit_and_defects_are_frozen() -> None:
    audit = audit_web_citations("https://source.publisher.org/a")

    with pytest.raises(FrozenInstanceError):
        audit.urls = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        CitationDefect("missing_url", "safe").detail = "changed"  # type: ignore[misc]
