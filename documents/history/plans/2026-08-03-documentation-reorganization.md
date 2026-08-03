# Documentation Reorganization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the oversized root README and mixed documentation layout with a concise landing page and a verified, task-oriented handbook under `documents/`.

**Architecture:** Root `README.md` becomes a roughly 200-line entrypoint. Detailed content moves into focused handbook pages organized by reader task, with `documents/README.md` as canonical navigation. A standard-library pytest module enforces line limits, local links, index coverage, H1 structure, legacy-path removal, and duplicate-file removal.

**Tech Stack:** Markdown, Git-aware file moves, Python 3.12 standard library, pytest, existing architecture and OpenAPI checks.

---

## Working-tree constraint

Do not create a clean worktree from `HEAD`: current working tree contains user-authored documentation moves and wording changes that are part of this task but are not all committed. Before each commit, use `git commit --only <paths...>` so unrelated staged changes remain untouched. Never reset, restore, or overwrite unrelated files.

## Final file map

**Create:**

- `documents/README.md` - canonical handbook index.
- `documents/getting-started/installation.md` - prerequisites and installation.
- `documents/getting-started/local-development.md` - local servers and developer setup.
- `documents/getting-started/usage.md` - CLI, LangGraph, and upload API usage modes.
- `documents/guides/configuration.md` - environment and model configuration.
- `documents/guides/authentication.md` - API keys, OAuth, passkeys, and production hardening.
- `documents/guides/reliability.md` - proactive rate shaping, retries, and troubleshooting.
- `documents/guides/evaluation.md` - regression tracking, verification loop, and metrics.
- `documents/architecture/overview.md` - high-level component and data-flow map.
- `documents/deployment/azure/README.md` - Azure deployment entrypoint.
- `documents/deployment/azure/storage.md` - persistence and synchronization.
- `documents/deployment/azure/operations.md` - monitoring, scaling, networking, CI/CD, and cost.
- `documents/deployment/azure/security.md` - identity, Key Vault, networking, and TLS.
- `documents/deployment/azure/troubleshooting.md` - diagnostics and common failures.
- `documents/development/testing.md` - test hierarchy and commands.
- `documents/development/extending-the-agent.md` - prompts, tools, skills, and models.
- `tests/test_documentation.py` - documentation contract tests.

**Move/rename:**

- `documents/AWS_DEPLOY.md` -> `documents/deployment/aws.md`
- `documents/VERCEL_DEPLOY.md` -> `documents/deployment/vercel.md`
- `documents/UPLOAD_API_GUIDE.md` -> `documents/api/upload.md`
- `documents/WIKI_API_GUIDE.md` -> `documents/api/wiki.md`
- `documents/POSTMAN_README.md` -> `documents/api/postman/README.md`
- `documents/postman_collection.json` -> `documents/api/postman/collection.json`
- `documents/postman_environment.json` -> `documents/api/postman/environment.json`
- `documents/CODE_INGESTION_AST.md` -> `documents/architecture/code-ingestion.md`
- `documents/LLM_WIKI_ARCHITECTURE_DIAGRAM_DESIGN.md` -> `documents/architecture/wiki-diagram-design.md`
- `documents/diagrams/*` -> `documents/architecture/diagrams/`
- `documents/TEST_PROMPTS_VALIDATION_GUIDE.md` -> `documents/development/prompt-validation.md`
- `documents/superpowers/plans/*` -> `documents/history/plans/`
- `documents/superpowers/specs/*` -> `documents/history/specs/`

**Modify:**

- `README.md` - concise project landing page.
- Moved reader-facing Markdown files - normalized titles, introductions, navigation, terminology, and links.
- `tests/test_architecture_boundaries.py` - new Clean Architecture document path.
- `AGENTS.md`, `CLAUDE.md`, and maintained repository text files - new documentation links.

**Delete after preservation check:**

- `documents/AZURE_DEPLOY.md` - replaced by focused Azure pages.
- `documents/WIKI_API_GUIDE 2.md` - stale duplicate with no unique newer content.
- Empty `documents/superpowers/` directories after moves.

### Task 1: Add documentation contract tests

**Files:**

- Create: `tests/test_documentation.py`
- Reference: `documents/superpowers/specs/2026-08-03-documentation-reorganization-design.md`

- [ ] **Step 1: Write path and Markdown helpers**

Implement standard-library helpers with these exact responsibilities:

```python
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
```

- [ ] **Step 2: Write contract tests**

Add tests named:

`````python
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
`````

- [ ] **Step 3: Run tests to verify current failures**

Run: `uv run pytest tests/test_documentation.py -q`

Expected before documentation changes: four failures for README length, unresolved legacy-path links, missing index, and legacy-path references. Heading parsing, GitHub slug regression, and single-H1 checks pass; duplicate-name failure appears only when the stale wiki copy still exists.

- [ ] **Step 4: Commit only the contract test**

```bash
git add tests/test_documentation.py
git commit --only tests/test_documentation.py -m "test: define documentation structure contract"
```

### Task 2: Build handbook structure and move canonical files

**Files:** All paths listed under **Move/rename** plus `documents/README.md`.

- [ ] **Step 1: Create destination directories**

Create the approved `getting-started`, `guides`, `api/postman`, `architecture/diagrams`, `deployment/azure`, `development`, `history/plans`, and `history/specs` directories. Use `git mv` for tracked content so history is preserved.

- [ ] **Step 2: Move canonical guides and assets**

Execute every move in **Move/rename**. If an existing staged rename already produced the source file under `documents/`, move from that current path; do not restore old `document/` or `docs/` sources.

- [ ] **Step 3: Move historical records**

Move all current plan/spec Markdown files from `documents/superpowers/` to `documents/history/`. Include the approved design spec. Do not rewrite historical prose yet.

- [ ] **Step 4: Remove stale duplicate when present**

If `documents/WIKI_API_GUIDE 2.md` exists, re-run `diff -u documents/api/wiki.md "documents/WIKI_API_GUIDE 2.md"`. Confirm the duplicate lacks newer AST/Git-import content, then delete only that duplicate. If it is already absent, record that and continue.

- [ ] **Step 5: Create handbook index**

Create `documents/README.md` with sections in this order: Getting started, Guides, API reference, Architecture, Deployment, Development, Historical records. Link every reader-facing Markdown guide individually with a one-sentence description. Link `history/plans/` and `history/specs/` once per collection.

- [ ] **Step 6: Run targeted structure checks**

Run: `uv run pytest tests/test_documentation.py::test_documents_have_no_duplicate_copy_names tests/test_documentation.py::test_document_index_lists_every_reader_guide -q`

Expected: duplicate-name test passes; index test may list only not-yet-created approved pages. Add those pages during later tasks, not placeholders.

- [ ] **Step 7: Commit moves and index only**

Commit only affected `documents/` paths with message `docs: organize handbook structure`.

### Task 3: Create getting-started pages

**Files:**

- Create: `documents/getting-started/installation.md`
- Create: `documents/getting-started/local-development.md`
- Create: `documents/getting-started/usage.md`

- [ ] **Step 1: Extract installation material**

Use current README lines 18-141 as source. Put prerequisites, `uv sync`, model-provider selection, Ollama setup, environment loading, and initial verification in `installation.md`. Move the full environment-variable catalog to the later configuration guide; installation should show only variables required for first run.

- [ ] **Step 2: Extract local development material**

Use README lines 271-358 and current repository commands. Cover `langgraph dev`, external virtual environments, upload server startup, and Windows/corporate-network notes in `local-development.md`. Remove unrelated Git history-rewrite examples.

- [ ] **Step 3: Extract usage modes**

Use README lines 142-364. Document CLI flags/examples, LangGraph server entrypoint, and Document Upload API mode. Link detailed API guides instead of repeating endpoint catalogs.

- [ ] **Step 4: Preserve README as active extraction source**

Do not rewrite root `README.md` yet. Tasks 4 and 7 still consume its current line ranges, including the user-authored wording change. Root rewrite happens only in Task 8 after every destination guide exists.

- [ ] **Step 5: Commit getting-started guides**

Commit only `documents/getting-started/` with message `docs: create project onboarding guides`.

### Task 4: Extract operational guides from README

**Files:**

- Create: `documents/guides/configuration.md`
- Create: `documents/guides/authentication.md`
- Create: `documents/guides/reliability.md`
- Create: `documents/guides/evaluation.md`

- [ ] **Step 1: Build configuration guide**

Consolidate README lines 67-141, 691-717, 1178-1203, 1716-1735, 1762-1777, and 2141-2151. Group variables by model provider, research workflow, file I/O, verification, rate limits, evaluation, authentication, and wiki code ingestion. Cross-check names and defaults against `.env.example`, `model_factory.py`, `research_agent_cli.py`, `webapp/config.py`, and relevant modules.

- [ ] **Step 2: Build authentication guide**

Consolidate README lines 375-401 and 735-1483. Use sections: authentication modes, API keys, OAuth providers, passkeys, request identity, production hardening, and troubleshooting. Keep diagrams only when they add distinct value; remove repeated flows and duplicate provider setup explanations.

- [ ] **Step 3: Build reliability guide**

Consolidate README lines 1706-1856. Merge duplicate rate-limit sections into proactive shaping, reactive retries, tuning profiles, failure messages, and troubleshooting. Cross-check variable names and defaults against current code and `.env.example`.

- [ ] **Step 4: Build evaluation guide**

Consolidate README lines 1857-2228. Cover evaluation concepts, baseline/candidate workflow, metrics, manifests, regression thresholds, verification loop, operational tracking, experiments, privacy, and focused commands. Keep backlog items only if still actionable; label them explicitly as future work.

- [ ] **Step 5: Normalize guide structure**

Each guide gets one H1, a two-sentence purpose, prerequisites where applicable, task-oriented H2 sections, and Related documentation links.

- [ ] **Step 6: Run H1 check for guides**

Run: `uv run pytest tests/test_documentation.py::test_reader_guides_have_one_h1 -q`

Expected: any remaining failures point to moved legacy guides; fix them in their owning tasks.

- [ ] **Step 7: Commit operational guides**

Commit only `documents/guides/` with message `docs: extract configuration and operations guides`.

### Task 5: Organize API and architecture documentation

**Files:**

- Modify: `documents/api/upload.md`
- Modify: `documents/api/wiki.md`
- Modify: `documents/api/postman/README.md`
- Modify: `documents/architecture/code-ingestion.md`
- Modify: `documents/architecture/clean-architecture.md`
- Modify: `documents/architecture/wiki-diagram-design.md`
- Create: `documents/architecture/overview.md`

- [ ] **Step 1: Normalize API guides**

Remove repeated README material from upload and wiki guides. Standardize sections: purpose, authentication, quick example, endpoints, workflows, troubleshooting, related documentation. Preserve AST code-source and public Git import details from the newer wiki guide.

- [ ] **Step 2: Normalize Postman guide**

Update asset names to `collection.json` and `environment.json`, correct relative links, and keep import/setup/testing instructions without duplicating the upload API reference.

- [ ] **Step 3: Create architecture overview**

Describe orchestration, tools, Thread Wiki, custom FastAPI app, model factory, persistence, and output skills. Link the clean architecture and code-ingestion deep dives plus the enhanced wiki diagram. Do not duplicate detailed API or deployment steps.

- [ ] **Step 4: Normalize architecture deep dives**

Add purpose/audience introductions, related-document links, and corrected diagram paths. Preserve current code-ingestion safety claims: uploaded code is parsed, never executed; embedded snippets do not enter repository resolution; derived artifacts are not citation targets.

- [ ] **Step 5: Run API/architecture link checks**

Run: `uv run pytest tests/test_documentation.py::test_local_markdown_links_resolve -q`

Expected: failures outside these areas may remain until deployment/history links are updated; no failures should originate from `documents/api/` or `documents/architecture/`.

- [ ] **Step 6: Commit API and architecture documentation**

Commit only `documents/api/` and `documents/architecture/` with message `docs: organize API and architecture guides`.

### Task 6: Split and normalize deployment documentation

**Files:**

- Modify: `documents/deployment/aws.md`
- Modify: `documents/deployment/vercel.md`
- Read then delete: `documents/AZURE_DEPLOY.md`
- Create: `documents/deployment/azure/README.md`
- Create: `documents/deployment/azure/storage.md`
- Create: `documents/deployment/azure/operations.md`
- Create: `documents/deployment/azure/security.md`
- Create: `documents/deployment/azure/troubleshooting.md`

- [ ] **Step 1: Inventory Azure sections before splitting**

Record every H2/H3 from the 2,876-line source and assign it to exactly one target page. Treat the Azure README as canonical for prerequisites and deployment steps. Do not delete source until all headings are accounted for.

- [ ] **Step 2: Write Azure README**

Move architecture, prerequisites, quick start, deployment sequence, health verification, and navigation into `azure/README.md`. Link to storage, operations, security, and troubleshooting instead of repeating their content.

- [ ] **Step 3: Write Azure storage page**

Move Azure Files architecture, volume mounts, directory creation, synchronization, storage monitoring, migration, rollback, and persistence verification. Reconcile conflicting Cosmos/SQLite recommendations with current deployment scripts and approved architecture records.

- [ ] **Step 4: Write Azure operations page**

Move container networking, configuration management, monitoring, scaling, load testing, CI/CD, cost optimization, version operations, and useful CLI references. Deduplicate repeated diagnostic commands.

- [ ] **Step 5: Write Azure security page**

Move Key Vault, managed identity, secret references/rotation, network restrictions, authentication, and TLS. Link troubleshooting failures to the troubleshooting page.

- [ ] **Step 6: Write Azure troubleshooting page**

Group issues by image/startup, identity/secrets, networking/ports, storage, resources/rate limits, and observability. Put the debugging checklist first and keep each symptom paired with diagnostic and repair commands.

- [ ] **Step 7: Normalize AWS and Vercel guides**

Apply sentence-case headings, purpose/audience introductions, consistent prerequisites, and related-document links. Preserve unique platform instructions; remove duplicated generic configuration already canonical in `guides/configuration.md`.

- [ ] **Step 8: Delete Azure source after inventory comparison**

Compare source heading inventory with target inventories and manually inspect any unassigned section. Delete `documents/AZURE_DEPLOY.md` only after every unique topic is retained or intentionally removed as stale duplication.

- [ ] **Step 9: Run deployment link and H1 checks**

Run: `uv run pytest tests/test_documentation.py::test_local_markdown_links_resolve tests/test_documentation.py::test_reader_guides_have_one_h1 -q`

Expected: PASS for all deployment pages.

- [ ] **Step 10: Commit deployment documentation**

Commit only `documents/deployment/` and deleted Azure source with message `docs: split deployment guides by task`.

### Task 7: Create development guides and normalize history

**Files:**

- Create: `documents/development/testing.md`
- Create: `documents/development/extending-the-agent.md`
- Modify: `documents/development/prompt-validation.md`
- Modify: `documents/history/**/*.md` only for path/link corrections

- [ ] **Step 1: Write testing guide**

Use AGENTS testing hierarchy and current commands. Cover fast unit tests, integration tests, E2E tests, focused prompt/verification/learning tests, coverage, lint, type checking, and golden-dataset regression. Avoid repeating evaluation concepts; link `guides/evaluation.md`.

- [ ] **Step 2: Write extension guide**

Consolidate README lines 1484-1705. Explain orchestration prompts, custom tools, model factory, output skills, delegation, context management, and the required file/test touchpoints for each extension type.

- [ ] **Step 3: Normalize prompt-validation guide**

Update test counts and class names against current `tests/test_prompts_validation.py`. Use one H1, current commands, and links to testing and extension guides.

- [ ] **Step 4: Repair historical links only**

Update moved repository documentation paths inside `documents/history/`. Do not modernize decisions, commands, or prose that intentionally describe historical states.

- [ ] **Step 5: Run development/history checks**

Run: `uv run pytest tests/test_documentation.py::test_reader_guides_have_one_h1 tests/test_documentation.py::test_local_markdown_links_resolve -q`

Expected: PASS.

- [ ] **Step 6: Commit development/history documentation**

Commit only `documents/development/` and link-only history changes with message `docs: organize development and history guides`.

### Task 8: Update repository references and complete navigation

**Files:**

- Modify: `README.md`
- Modify: `documents/README.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `tests/test_architecture_boundaries.py`
- Modify: every Git-tracked maintained text file still referencing an approved old documentation path

- [ ] **Step 1: Replace root README after all extraction is complete**

Use the still-current README as the final source checklist, then replace it with sections in this order: title and one-paragraph purpose, Features, Quick start, Usage modes, Architecture, Development, Documentation, Deployment, Security. Target 180-220 lines and never exceed 250. Preserve the current wording change around documents upload unless using the canonical product name `Document Upload API`.

- [ ] **Step 2: Run concise README test**

Run: `uv run pytest tests/test_documentation.py::test_root_readme_is_concise -q`

Expected: PASS.

- [ ] **Step 3: Find legacy references**

Use `git grep -nE 'document/|docs/superpowers/|AWS_DEPLOY|AZURE_DEPLOY|VERCEL_DEPLOY|UPLOAD_API_GUIDE|WIKI_API_GUIDE|TEST_PROMPTS_VALIDATION_GUIDE|CODE_INGESTION_AST' -- ':!documents/history/**' ':!docs/**' ':!sync/**' ':!sync-aws/**' ':!.worktrees/**' ':!graphify-out/**'`.

Expected: list of maintained references requiring updates; historical references should already point to `documents/history/`.

- [ ] **Step 4: Update maintained links**

Replace each old path with its final canonical path. Update `tests/test_architecture_boundaries.py` from `docs/architecture/clean-architecture.md` to `documents/architecture/clean-architecture.md`.

- [ ] **Step 5: Complete handbook index**

Confirm every reader-facing guide has an individual link and one-sentence description. Keep primary order aligned with the approved folder map.

- [ ] **Step 6: Run documentation contract suite**

Run: `uv run pytest tests/test_documentation.py -q`

Expected: all tests pass.

- [ ] **Step 7: Run legacy-path search**

Run the Step 1 command again.

Expected: no maintained references to old documentation paths.

- [ ] **Step 8: Commit landing page and link migration**

Commit only root README, reference updates, index, and architecture test with message `docs: finalize handbook navigation and links`.

### Task 9: Final verification and content-preservation audit

**Files:** All documentation paths changed by Tasks 1-8.

- [ ] **Step 1: Run complete documentation tests**

Run: `uv run pytest tests/test_documentation.py -q`

Expected: all tests pass.

- [ ] **Step 2: Run affected repository checks**

```bash
uv run pytest tests/test_architecture_boundaries.py tests/test_packaging.py tests/test_prompts_validation.py -q
uv run python scripts/check_architecture.py
uv run python scripts/snapshot_openapi.py --check
```

Expected: all tests/checks pass. If a pre-existing dirty-worktree failure is unrelated, capture exact command and output instead of editing unrelated code.

- [ ] **Step 3: Check Markdown structure and stale files**

Run: `wc -l README.md` and confirm at most 250 lines. Inventory `documents/` and confirm no top-level reader guide remains outside the approved structure, no `* 2.md` file exists, and no empty `documents/superpowers/` directory remains.

- [ ] **Step 4: Audit content preservation**

Compare original README H2/H3 inventory from `git show b2afb27^:README.md` and original Azure inventory from the pre-migration index against final guide headings. For every removed section, identify its canonical destination or record why it was redundant/stale. Restore any unique content accidentally omitted.

- [ ] **Step 5: Inspect final diff**

Run: `git diff --check` and `git status --short`. Confirm unrelated user changes remain present and no generated/runtime documentation data was added.

- [ ] **Step 6: Record final documentation decision**

Record the final file map, canonical index, README line count, removed duplicates, and verification results in CCE memory for future sessions.

- [ ] **Step 7: Final commit if audit changed files**

If the audit required fixes, commit only those documentation/test paths with message `docs: finalize handbook migration`.
