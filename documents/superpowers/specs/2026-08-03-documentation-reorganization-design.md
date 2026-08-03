# Documentation reorganization design

## Purpose

Turn the 2,228-line root README and mixed documentation directory into a concise
project landing page plus a task-oriented handbook. Preserve useful technical
content, remove duplication, and make every reader-facing guide discoverable
through one documentation index.

## Current state

- Root `README.md` mixes quick start, configuration, API reference,
  authentication, architecture, reliability, evaluation, and deployment details.
- Reader-facing guides, diagrams, Postman assets, and historical design records
  share the top level of `documents/`.
- `documents/AZURE_DEPLOY.md` is 2,876 lines and covers setup, storage,
  operations, security, troubleshooting, cost, and reference material.
- `documents/WIKI_API_GUIDE 2.md` is an untracked, stale duplicate of the newer
  wiki API guide.
- Existing working-tree changes already move project documentation from
  `document/` and `docs/` into `documents/`; those user changes must be
  preserved and completed rather than reverted.
- Repository has no dedicated Markdown link or documentation-structure check.

## Goals

1. Limit root `README.md` to at most 250 lines, targeting about 200 lines.
2. Put all project documentation and related assets under `documents/`.
3. Organize reader-facing content by task instead of file history.
4. Split oversized and mixed-topic documents into focused guides.
5. Preserve unique instructions while removing repeated explanations.
6. Normalize naming, headings, links, terminology, code fences, and navigation.
7. Add focused automated checks for documentation structure and local links.

## Non-goals

- Do not change product behavior, APIs, deployment scripts, or configuration.
- Do not rewrite historical plans and specs beyond path and link corrections.
- Do not validate external cloud-provider behavior against live accounts.
- Do not add a documentation site generator or hosted documentation service.
- Do not preserve old repository paths with redirect stubs; all in-repository
  consumers will move to the new paths.

## Information architecture

```text
README.md
documents/
├── README.md
├── getting-started/
│   ├── installation.md
│   ├── local-development.md
│   └── usage.md
├── guides/
│   ├── configuration.md
│   ├── authentication.md
│   ├── reliability.md
│   └── evaluation.md
├── api/
│   ├── upload.md
│   ├── wiki.md
│   └── postman/
│       ├── README.md
│       ├── collection.json
│       └── environment.json
├── architecture/
│   ├── overview.md
│   ├── clean-architecture.md
│   ├── code-ingestion.md
│   └── diagrams/
├── deployment/
│   ├── aws.md
│   ├── vercel.md
│   └── azure/
│       ├── README.md
│       ├── storage.md
│       ├── operations.md
│       ├── security.md
│       └── troubleshooting.md
├── development/
│   ├── testing.md
│   ├── prompt-validation.md
│   └── extending-the-agent.md
└── history/
    ├── plans/
    └── specs/
```

`documents/README.md` is the canonical handbook index. It groups documents by
reader task and gives each link a one-sentence description. Historical records
remain available in a separate section but do not appear in primary onboarding
or operational paths.

## Root README contract

Root `README.md` retains only:

1. Project purpose and major capabilities.
2. Five-minute local quick start.
3. Compact CLI, LangGraph, and upload API usage examples.
4. High-level architecture summary and architecture-document link.
5. Essential test and development commands.
6. Documentation, deployment, and security links.

Detailed environment-variable tables, endpoint catalogs, OAuth flows, rate-limit
tuning, evaluation schemas, cloud commands, and extension instructions move to
the handbook. Upload API endpoint details move to `documents/api/upload.md`;
Thread Wiki endpoint details move to `documents/api/wiki.md`; LangGraph and CLI
invocation details move to `documents/getting-started/usage.md`.

## Content migration

| Existing content | Destination |
| --- | --- |
| README installation and prerequisites | `documents/getting-started/installation.md` |
| README local server setup | `documents/getting-started/local-development.md` |
| README CLI and server usage | `documents/getting-started/usage.md` |
| README environment configuration | `documents/guides/configuration.md` |
| README OAuth, passkeys, and API security | `documents/guides/authentication.md` |
| README reliability and rate limits | `documents/guides/reliability.md` |
| README evaluation and regression tracking | `documents/guides/evaluation.md` |
| README components, tools, and skills | `documents/development/extending-the-agent.md` |
| README testing instructions | `documents/development/testing.md` |
| Existing upload API guide | `documents/api/upload.md` |
| Newer wiki API guide | `documents/api/wiki.md` |
| Postman guide and JSON assets | `documents/api/postman/` |
| Code-ingestion guide | `documents/architecture/code-ingestion.md` |
| Clean Architecture guide | `documents/architecture/clean-architecture.md` |
| Wiki architecture design and diagram assets | `documents/architecture/` and `documents/architecture/diagrams/` |
| AWS deployment guide | `documents/deployment/aws.md` |
| Vercel deployment guide | `documents/deployment/vercel.md` |
| Azure deployment guide | Focused files under `documents/deployment/azure/` |
| Prompt-validation guide | `documents/development/prompt-validation.md` |
| Existing implementation plans/specs | `documents/history/plans/` and `documents/history/specs/` |

README sections and existing guides may overlap. During migration, keep the most
current and complete version of a fact, move it to one canonical document, and
replace other copies with a related-document link.

## Azure guide split

- `deployment/azure/README.md`: architecture, prerequisites, quick start, and
  deployment sequence.
- `deployment/azure/storage.md`: Azure Files, persistence, synchronization,
  migration, rollback, and storage verification.
- `deployment/azure/operations.md`: configuration, monitoring, scaling,
  networking, CI/CD, cost, and operational command reference.
- `deployment/azure/security.md`: identities, Key Vault, network controls,
  authentication, secret rotation, and TLS.
- `deployment/azure/troubleshooting.md`: common failures, diagnostic commands,
  debugging checklist, and version mismatch handling.

Each page starts with scope and prerequisites, avoids repeating resource-creation
steps, and links back to the Azure index and related pages.

## Editorial rules

- Use lowercase kebab-case filenames and directories, except conventional
  `README.md` index files at repository, handbook, Azure, and Postman roots.
- Use one H1 per Markdown file and sentence-case headings.
- Begin each guide with purpose and intended audience.
- Add a table of contents only when a guide remains long enough to need one.
- Tag fenced code blocks with the appropriate language.
- Use repository-relative Markdown links and descriptive link text.
- Prefer short paragraphs, ordered procedures, compact tables, and explicit
  prerequisites.
- Use consistent names: Deep Research, Document Upload API, Thread Wiki,
  LangGraph server, and research agent.
- Keep emoji decoration in the root landing page only; handbook headings remain
  plain and scannable.
- End substantial guides with a short related-document section.

## Accuracy and preservation rules

- Compare commands, filenames, environment variables, and entrypoints with the
  current repository before retaining them.
- Treat current scripts and configuration as authoritative for project-specific
  behavior.
- Do not invent missing commands or silently change operational semantics.
- Preserve the current README wording change from "document upload" to
  "documents upload" where applicable unless normalized terminology requires
  the product name "Document Upload API".
- Before deleting a source section or duplicate file, confirm its unique content
  exists in a canonical destination.
- Keep user-authored staged moves and unrelated working-tree changes intact.

## Validation

Add focused documentation tests that verify:

1. Root `README.md` contains no more than 250 lines.
2. Every repository-local Markdown link in root `README.md` and
   `documents/**/*.md` resolves to an existing file, directory, or heading
   anchor. Ignore external schemes such as `http`, `https`, and `mailto`.
   Resolve relative paths from the containing Markdown file. Validate anchors
   with GitHub-style slugs: lowercase heading text, remove punctuation, replace
   whitespace with hyphens, and append `-n` for repeated slugs.
3. Every reader-facing Markdown guide under `documents/` appears in
   `documents/README.md`. Here, reader-facing means every Markdown file except
   `documents/README.md` and files below `documents/history/`; history
   collections receive one index link per collection instead of one per file.
4. Every reader-facing guide has exactly one H1.
5. No maintained source or documentation file refers to legacy `document/` or
   `docs/superpowers/` paths. Scan Git-tracked project text files with Markdown,
   Python, TOML, JSON, YAML, shell, Dockerfile, and plain configuration names;
   exclude `.git/`, `.venv/`, `.worktrees/`, `.codex/`, `graphify-out/`,
   `vendor/`, `sync/`, `sync-aws/`, and runtime data below `docs/`.
6. Duplicate-style filenames such as `* 2.md` do not exist under `documents/`.

Also run existing architecture, packaging, prompt-validation, and OpenAPI
snapshot checks affected by documentation-path changes. Inspect the final diff
and compare source/destination section inventories to catch lost unique content.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Unique content lost during deduplication | Map every source section before deletion and compare heading/content inventories afterward. |
| Repository links break after moves | Update references repository-wide and run local-link and legacy-path checks. |
| Historical records become noisy in primary navigation | Keep them under `documents/history/` with one index link per collection. |
| Azure split repeats setup steps | Make Azure README canonical for deployment and link focused pages back to it. |
| Existing dirty-worktree changes are overwritten | Inspect diffs before each overlapping edit and restrict changes to documentation-related paths and required tests. |
| Documentation claims remain stale | Validate project-specific facts against current code, scripts, sample environment files, and configuration. |

## Completion criteria

- Root README is a coherent landing page no longer than 250 lines.
- `documents/README.md` provides complete task-oriented navigation.
- All reader-facing project documents and assets use the approved structure.
- Stale duplicate wiki guide is removed after unique-content comparison.
- Azure documentation is split into the five approved focused pages.
- Local links, heading rules, index coverage, and legacy-path checks pass.
- A reader can move from installation to usage, operations, API reference, and
  architecture without searching the repository.
