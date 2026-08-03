# AST-Aware Code Ingestion

Use this deep dive when operating, debugging, or extending Thread Wiki ingestion
for source repositories and code embedded in ordinary documents. It explains
runtime detection, safety, artifacts, citations, limits, and recovery for
maintainers who need more detail than the architecture overview or API guide.

## Choose the ingestion path

AST-aware ingestion enhances the existing Thread Wiki. It does not create a
separate wiki type or query endpoint.

- Ordinary documents retain the existing extraction and text-chunking path.
- A whole file recognized as source code is parsed locally with Tree-sitter.
- Surrounding document content retains existing behavior. Explicit fenced code
  blocks with supported language tags receive a separate semantic overlay and
  remain attached to their original document and line range.
- Uploaded source is parsed only. It is never imported, compiled, or executed.
- Wiki answers cite original `/raw/...` source files and validated 1-based line
  ranges, never derived semantic artifacts.

## Follow the data flow

```text
Upload or public Git URL
        |
        v
docs/threads/<thread-id>/
        |
        +-- ordinary document --> existing extraction and text chunking
        |                       + explicit tagged fences --> embedded AST overlay
        |
        +-- recognized code --> Tree-sitter CST
                                |
                                v
                         normalized semantic model
                                |
                                +-- repository symbol/import index
                                +-- semantic Markdown chunks
                                +-- schema-versioned manifest
        |
        v
existing review -> apply -> post-review -> index flow
        |
        v
wiki query with original source citations
```

Tree-sitter returns a concrete syntax tree with exact byte and point ranges.
`thread_wiki.code_ingestion` normalizes selected nodes into an AST-like,
language-independent model suitable for repository analysis.

## Identify the source type

File extension takes precedence. A shebang is inspected only when a file has no
extension.

| Language | Extensions or shebang |
|---|---|
| Python | `.py`, `.pyw`, extensionless `python` shebang |
| JavaScript / JSX | `.js`, `.mjs`, `.cjs`, `.jsx`, extensionless Node/Deno/Bun shebang |
| TypeScript / TSX | `.ts`, `.mts`, `.cts`, `.tsx` |
| Java | `.java` |
| Go | `.go` |
| Rust | `.rs` |
| C | `.c`, `.h` candidate |
| C++ | `.cc`, `.cpp`, `.cxx`, `.hh`, `.hpp`, `.hxx`, `.h` candidate |
| C# | `.cs` |
| Ruby | `.rb`, extensionless Ruby shebang |
| PHP | `.php`, extensionless PHP shebang |
| Embedded block | Explicit Markdown fence tagged with a language above |

Untagged fences and unsupported language tags remain ordinary document text.
For extracted PDF, Word, PowerPoint, or other document content, embedded
analysis occurs only when the extractor preserves an explicit tagged fence;
the service does not guess from prose or indentation.

For `.h`, both C and C++ grammars parse the source. The tree with fewer error
and missing nodes wins; an exact tie selects C.

## Inspect the normalized schema

`CodeFileAnalysis` records source path, hash, byte size, language, detection
method, parser and grammar versions, parse status, units, imports, and warnings.

`CodeUnit` records:

- stable unit ID;
- kind (`module`, `class`, `interface`, `enum`, `struct`, `trait`, `function`,
  `method`, `constructor`, `constant`, or related container kind);
- name and qualified name;
- stable parent ID;
- signature and documentation;
- original source code;
- exact 1-based start and end lines.

`CodeImport` records module text, imported names when available, source line,
original declaration, and an optional resolved source. Resolution is emitted
only for exact, unambiguous internal matches. Call graphs, speculative type
resolution, and heuristic cross-language linking are intentionally excluded.

Embedded analyses use `origin_kind: embedded`, retain block number and original
document line range, and have independent public counts. Their symbols and
imports never enter repository-level resolution.

## Inspect generated artifacts

```text
docs/threads-wiki/<thread-id>/
├── .code_ingest_manifest.json
├── raw/
│   ├── <original repository paths>
│   └── _code/
│       ├── repository-index.md
│       └── <ordered semantic chunks>.md
└── wiki/
    └── <normal generated wiki pages>
```

`raw/_code/` is regenerated as one run-scoped artifact set. It cannot retain
semantic chunks from edited or deleted code. Derived files are excluded from
uploaded-source counts and raw context-size accounting.

The manifest uses `schema_version: 1` and retains full file warnings, source
hashes, parser metadata, hierarchy, ranges, imports, artifacts, symbols, and
resolved internal imports. Public API warnings are deterministically ordered
and capped at 50.

## Enforce prompt and citation rules

For code ingestion, the LLM must:

1. read `/raw/_code/repository-index.md` first;
2. process semantic chunks in deterministic source order;
3. use derived chunks only as navigation and semantic context;
4. cite original code as `(/raw/pkg/app.py, lines 10-24)`;
5. never cite `/raw/_code/...`.

If a generated answer nevertheless cites a derived artifact, the response
layer maps it back to the artifact's original source and validates its line
range against current source length. Invalid ranges are omitted.

## Configure limits and switches

| Variable | Default | Purpose |
|---|---:|---|
| `WIKI_CODE_AST_ENABLED` | `true` | AST rollback switch |
| `WIKI_EMBEDDED_CODE_AST_ENABLED` | `true` | Explicit fenced-block semantic overlay |
| `WIKI_CODE_PARSE_MAX_BYTES` | `2097152` | Per-file parse limit |
| `WIKI_MAX_CHUNK_CHARS` | `40000` | Semantic unit and document chunk target |
| `WIKI_GIT_ALLOWED_HOSTS` | `github.com,gitlab.com,bitbucket.org` | Public Git host allowlist |
| `WIKI_GIT_IMPORT_TIMEOUT_SECONDS` | `120` | Clone timeout |
| `WIKI_GIT_IMPORT_MAX_FILES` | `5000` | Repository file limit |
| `WIKI_GIT_IMPORT_MAX_BYTES` | `104857600` | Repository byte limit |

All Tree-sitter core and grammar wheels are pinned in `pyproject.toml` and
`uv.lock`; ingestion performs no parser download at runtime.

## Import a public Git repository

`POST /threads/{thread_id}/wiki/import/git` accepts an anonymous public HTTPS
GitHub, GitLab, or Bitbucket URL plus an optional branch or tag. Import runs as
the `importing` progress phase, then starts the same mixed-document ingest flow.
Current route path resolution requires `docs/threads/<thread-id>/` to exist
before clone starts; a Git-only import for a new thread returns `404`.

```json
{
  "url": "https://github.com/example/project.git",
  "ref": "main",
  "topic": "Example project",
  "note": "Focus on architecture and extension points"
}
```

The clone is shallow, single-branch, non-interactive, and invokes Git without a
shell. Credentials, custom ports, URL query data, local/file protocols,
submodules, LFS downloads, hooks, symlinks, VCS metadata, dependency trees, and
build-output trees are excluded. Repository limits are checked before an
existing import is replaced. Private repository credentials are not supported.

## Recover with safe fallback

Usable units from recoverable trees are retained with status `partial`.
Existing text chunking is used when:

- AST processing is disabled;
- source exceeds the parse limit;
- encoding is not UTF-8 or UTF-8 BOM;
- parser or grammar loading fails;
- no usable semantic unit is produced.

Warnings use stable codes such as `ast_disabled`, `source_too_large`,
`unsupported_encoding`, `parser_failure`, `no_usable_units`, and
`syntax_error`. Ingest progress snapshots include the public code-analysis
summary, so status remains consistent after process restart.

## Troubleshoot ingestion

- `fallback_files` increased: inspect warning codes in `/wiki/status`, then the
  full `.code_ingest_manifest.json`.
- `parser_failure`: run `uv sync`; verify pinned grammar wheels install on the
  deployment platform.
- `source_too_large`: raise `WIKI_CODE_PARSE_MAX_BYTES` only after reviewing
  memory limits, or accept safe text fallback.
- Git clone failed: confirm URL and ref are public HTTPS values on an allowed
  host and fit configured time/size limits.
- Need immediate rollback: set `WIKI_CODE_AST_ENABLED=false`; recognized code
  continues through existing text ingestion.
- Need document-only behavior: set `WIKI_EMBEDDED_CODE_AST_ENABLED=false`;
  fenced blocks remain available through ordinary document ingestion.

## Know the limits

- No raw AST JSON is exposed.
- No source execution, compilation, package installation, or dependency fetch.
- No speculative call graph or type inference.
- No private Git authentication, submodule checkout, or Git LFS content.
- Only explicit supported language-tagged fences are parsed; heuristic code
  detection in prose, untagged blocks, or document typography is out of scope.

## Related documentation

- [Architecture overview](overview.md)
- [Enhanced wiki diagram](diagrams/enhanced-llm-wiki-architecture.png)
- [Wiki diagram design specification](wiki-diagram-design.md)
- [Thread Wiki API](../api/wiki.md)
