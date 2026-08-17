from __future__ import annotations

from dataclasses import FrozenInstanceError

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

    assert [defect.code for defect in audit.defects] == ["placeholder_source", "missing_url"]


def test_reports_unresolved_single_group_and_descending_markers() -> None:
    report = """Missing [1, 3; 5] and malformed [5-2].

## Sources
[1] https://source.publisher.org/one
"""

    assert codes(report) == [
        "unresolved_reference",
        "unresolved_reference",
        "malformed_reference",
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
