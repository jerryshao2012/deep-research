from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ROOT / "documents"
HISTORY = DOCUMENTS / "history"
EXTERNAL_SCHEMES = {"http", "https", "mailto"}
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
PLAIN_TEXT_NAMES = {".dockerignore", ".env.example", ".gitignore", "AGENTS.md", "CLAUDE.md"}
LEGACY_PATHS = ("document" + "/", "docs" + "/superpowers/")


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
