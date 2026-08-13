from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ROOT / "documents"
HISTORY = DOCUMENTS / "history"
README_SNAPSHOT = (
    HISTORY
    / "snapshots"
    / "README-e0ae10676b7cf892bbf567adab79e17cc3ab7c8a.md"
)
README_AUDIT = (
    HISTORY
    / "audits"
    / "README-e0ae10676b7cf892bbf567adab79e17cc3ab7c8a-content-audit.md"
)
README_SNAPSHOT_COMMIT = "e0ae10676b7cf892bbf567adab79e17cc3ab7c8a"
README_SNAPSHOT_SHA256 = "b612197938d0d433731cf5401eb350f43d43a2aa76ad6b10e6c7b96c3df07d9e"
AUDIT_DISPOSITIONS = {
    "already-covered",
    "restored",
    "corrected",
    "archived-only",
}
EXTERNAL_SCHEMES = {"http", "https", "mailto"}
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
PLAIN_TEXT_NAMES = {".dockerignore", ".env.example", ".gitignore", "AGENTS.md", "CLAUDE.md"}
LEGACY_PATHS = ("document" + "/", "docs" + "/superpowers/")
MAINTAINED_SOURCE_DOCS = (
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    *(
        path
        for section in (
            "api",
            "architecture",
            "deployment",
            "development",
            "getting-started",
            "guides",
        )
        for path in sorted((DOCUMENTS / section).rglob("*.md"))
    ),
)
LEGACY_SOURCE_DOC_PATTERNS = (
    r"research_agent_cli\.py",
    r"uv run python -m research_agent\.model_factory",
    r"uv run python (?:\./)?model_factory\.py",
    r"`(?:\./)?model_factory\.py`",
    r"research_agent/(?:tools|prompts|utils)(?:/|\.py)",
    r"research_agent\.(?:tools|prompts|utils)(?:\.|\b)",
    r"\[(?:agent|auth|model_factory|server|run)\.py\]"
    r"\((?:agent|auth|model_factory|server|run)\.py\)",
    r"\bmemory_profiler\b",
)


def _markdown_files() -> list[Path]:
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "README.md",
            "documents",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(
        ROOT / relative
        for relative in completed.stdout.splitlines()
        if relative.endswith(".md") and (ROOT / relative).is_file()
    )


def _reader_guides() -> list[Path]:
    return [
        path
        for path in _markdown_files()
        if DOCUMENTS in path.parents
        if path != DOCUMENTS / "README.md" and HISTORY not in path.parents
    ]


def _markdown_headings(markdown: str) -> list[tuple[str, str]]:
    headings: list[tuple[str, str]] = []
    fence_character = ""
    fence_length = 0
    for line in markdown.splitlines():
        fence = FENCE_RE.match(line)
        if fence_character:
            if fence:
                marker, suffix = fence.groups()
                if (
                    marker[0] == fence_character
                    and len(marker) >= fence_length
                    and not suffix.strip()
                ):
                    fence_character = ""
                    fence_length = 0
            continue
        if fence:
            marker, info = fence.groups()
            if marker[0] == "~" or "`" not in info:
                fence_character = marker[0]
                fence_length = len(marker)
                continue
        heading = HEADING_RE.match(line)
        if heading:
            headings.append(heading.groups())
    return headings


def _github_slugs(markdown: str) -> set[str]:
    counts: dict[str, int] = {}
    slugs: set[str] = set()
    for _, heading in _markdown_headings(markdown):
        base = re.sub(r"[^\w\s-]", "", heading.lower())
        base = re.sub(r"\s", "-", base)
        count = counts.get(base, 0)
        candidate = base if count == 0 else f"{base}-{count}"
        while candidate in slugs:
            count += 1
            candidate = f"{base}-{count}"
        counts[base] = count + 1
        slugs.add(candidate)
    return slugs


def _local_link_targets(path: Path) -> list[tuple[Path, str]]:
    targets: list[tuple[Path, str]] = []
    for raw in LINK_RE.findall(path.read_text(encoding="utf-8")):
        target = raw.strip().strip("<>").split(maxsplit=1)[0]
        parsed = urlsplit(target)
        if parsed.scheme in EXTERNAL_SCHEMES:
            continue
        linked_path = path if not parsed.path else (path.parent / unquote(parsed.path)).resolve()
        targets.append((linked_path, unquote(parsed.fragment)))
    return targets


def _read_audit_rows() -> list[tuple[int, int, str, str, str, str]]:
    rows: list[tuple[int, int, str, str, str, str]] = []
    for line in README_AUDIT.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not re.fullmatch(r"`\d+-\d+`", cells[0]):
            continue
        start, end = (int(value) for value in cells[0].strip("`").split("-"))
        rows.append(
            (start, end, cells[1], cells[2].strip("`"), cells[3], cells[4])
        )
    return rows


def test_markdown_headings_ignore_fenced_code_blocks() -> None:
    markdown = """# Actual heading

   ````shell
# Backtick comment
   ````

~~~ python
# Tilde comment
~~~~

## Actual section
"""

    assert _markdown_headings(markdown) == [
        ("#", "Actual heading"),
        ("##", "Actual section"),
    ]
    assert _github_slugs(markdown) == {"actual-heading", "actual-section"}


def test_github_slugs_match_punctuation_and_duplicate_behavior() -> None:
    markdown = """## 🚀 Quickstart
## Multi-Agent Complex Workflows: Evaluation & Regression Tracking
## Repeated heading
## Repeated heading
## Foo
## Foo
## Foo-1
"""

    assert _github_slugs(markdown) == {
        "-quickstart",
        "multi-agent-complex-workflows-evaluation--regression-tracking",
        "repeated-heading",
        "repeated-heading-1",
        "foo",
        "foo-1",
        "foo-1-1",
    }


def test_root_readme_is_concise() -> None:
    assert len((ROOT / "README.md").read_text(encoding="utf-8").splitlines()) <= 250


def test_reader_guides_have_one_h1() -> None:
    for path in _reader_guides():
        h1s = [
            heading
            for level, heading in _markdown_headings(path.read_text(encoding="utf-8"))
            if level == "#"
        ]
        assert len(h1s) == 1, path.relative_to(ROOT)


def test_local_markdown_links_resolve() -> None:
    failures: list[str] = []
    for source in _markdown_files():
        if source == README_SNAPSHOT:
            # Immutable historical evidence intentionally retains its original,
            # now-obsolete relative links. The adjacent audit maps current targets.
            continue
        for target, anchor in _local_link_targets(source):
            if not target.exists():
                failures.append(f"{source.relative_to(ROOT)} -> {target}")
                continue
            if anchor and target.is_file() and target.suffix.lower() == ".md":
                slugs = _github_slugs(target.read_text(encoding="utf-8"))
                if anchor not in slugs:
                    failures.append(f"{source.relative_to(ROOT)} -> {target}#{anchor}")
    assert not failures, "\n".join(failures)


def test_document_index_lists_every_reader_guide() -> None:
    index_targets = {
        target.resolve()
        for target, _ in _local_link_targets(DOCUMENTS / "README.md")
        if target.suffix.lower() == ".md"
    }
    missing = [path.relative_to(ROOT) for path in _reader_guides() if path.resolve() not in index_targets]
    assert not missing, missing


def test_legacy_documentation_paths_are_absent() -> None:
    completed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    allowed_suffixes = {".md", ".py", ".toml", ".json", ".yaml", ".yml", ".sh"}
    excluded_roots = {".git", ".venv", ".worktrees", ".codex", "graphify-out", "vendor", "sync", "sync-aws", "docs"}
    failures: list[str] = []
    for relative in completed.stdout.splitlines():
        path = ROOT / relative
        if not path.is_file() or set(path.relative_to(ROOT).parts) & excluded_roots:
            continue
        if path == ROOT / "tests" / "test_documentation.py" or HISTORY in path.parents:
            continue
        if (
            path.suffix.lower() not in allowed_suffixes
            and path.name not in PLAIN_TEXT_NAMES
            and not path.name.startswith("Dockerfile")
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(legacy in text for legacy in LEGACY_PATHS):
            failures.append(relative)
    assert not failures, failures


def test_documents_have_no_duplicate_copy_names() -> None:
    assert not list(DOCUMENTS.rglob("* 2.md"))


def test_maintained_source_docs_use_packaged_architecture() -> None:
    failures: list[str] = []
    for path in MAINTAINED_SOURCE_DOCS:
        text = path.read_text(encoding="utf-8")
        for pattern in LEGACY_SOURCE_DOC_PATTERNS:
            match = re.search(pattern, text)
            if match:
                failures.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert not failures, "\n".join(failures)


def test_maintained_source_docs_cover_current_deployment_guides() -> None:
    deployment_guides = set((DOCUMENTS / "deployment").rglob("*.md"))
    assert deployment_guides <= set(MAINTAINED_SOURCE_DOCS)
    assert not any(HISTORY in path.parents for path in MAINTAINED_SOURCE_DOCS)


def test_historical_readme_snapshot_is_byte_identical() -> None:
    payload = README_SNAPSHOT.read_bytes()
    historical = subprocess.run(
        ["git", "show", f"{README_SNAPSHOT_COMMIT}:README.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout

    assert payload == historical
    assert hashlib.sha256(payload).hexdigest() == README_SNAPSHOT_SHA256
    assert len(payload.decode("utf-8").splitlines()) == 2228


def test_historical_readme_audit_covers_every_line_once() -> None:
    rows = _read_audit_rows()
    expected_start = 1

    assert rows
    for start, end, heading, disposition, target, rationale in rows:
        assert start == expected_start, (expected_start, start, heading)
        assert end >= start, (start, end, heading)
        assert disposition in AUDIT_DISPOSITIONS, (heading, disposition)
        if disposition == "archived-only":
            assert target == "—", (heading, target)
            assert rationale, heading
        else:
            assert LINK_RE.search(target), (heading, target)
        expected_start = end + 1

    assert expected_start == 2229


def test_historical_readme_audit_destinations_resolve() -> None:
    failures: list[str] = []
    for target, anchor in _local_link_targets(README_AUDIT):
        if target == README_SNAPSHOT:
            continue
        if not target.exists():
            failures.append(str(target))
            continue
        if anchor and target.suffix.lower() == ".md":
            slugs = _github_slugs(target.read_text(encoding="utf-8"))
            if anchor not in slugs:
                failures.append(f"{target}#{anchor}")

    assert not failures, "\n".join(failures)


def test_authentication_guide_keeps_provider_registration_processes() -> None:
    guide = (DOCUMENTS / "guides" / "authentication.md").read_text(encoding="utf-8")
    required_details = (
        "### Register a Google OAuth client",
        "OAuth consent screen",
        "Authorized JavaScript origins",
        "Authorized redirect URIs",
        "http://localhost:2024/auth/callback/google",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "### Register a GitHub OAuth app",
        "GitHub Developer Settings",
        "Register application",
        "Generate a new client secret",
        "http://localhost:2024/auth/callback/github",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "### Configure backend OAuth environment",
        "OAUTH_SECRET_KEY",
        "Never commit",
        "secret store",
        "FRONTEND_URLS",
        "### Validate the OAuth login flow",
    )

    missing = [detail for detail in required_details if detail not in guide]
    assert not missing, missing


def test_reorganized_guides_keep_major_historical_topics_discoverable() -> None:
    reliability = (DOCUMENTS / "guides" / "reliability.md").read_text(encoding="utf-8")
    evaluation = (DOCUMENTS / "guides" / "evaluation.md").read_text(encoding="utf-8")
    index = (DOCUMENTS / "README.md").read_text(encoding="utf-8")

    assert reliability.startswith("# Reliability & Rate Limiting\n")
    assert evaluation.startswith(
        "# Multi-Agent Complex Workflows Evaluation & Regression Tracking\n"
    )
    assert "[Reliability](guides/reliability.md)" in index
    assert "[Evaluation](guides/evaluation.md)" in index


def test_audit_records_block_level_compression_scan() -> None:
    audit = README_AUDIT.read_text(encoding="utf-8")
    usage = (DOCUMENTS / "getting-started" / "usage.md").read_text(encoding="utf-8")
    authentication = (DOCUMENTS / "guides" / "authentication.md").read_text(
        encoding="utf-8"
    )
    architecture = (DOCUMENTS / "architecture" / "overview.md").read_text(
        encoding="utf-8"
    )
    historical_areas = (
        "Contents and project overview",
        "Quickstart and usage",
        "Cloud deployment",
        "Security and authentication",
        "Thread wiki",
        "OAuth registration and login",
        "Components and tools",
        "Resources and extension points",
        "Reliability",
        "Rate-limit handling",
        "Evaluation and regression tracking",
        "Future work and references",
    )

    assert "## Block-level compression scan" in audit
    for area in historical_areas:
        assert f"| {area} |" in audit, area
    assert "### Understand document-grounded research" in usage
    assert "### Follow the end-to-end OAuth flow" in authentication
    assert "## Tool behavior and runtime ownership" in architecture
