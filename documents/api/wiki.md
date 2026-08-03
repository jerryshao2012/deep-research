# Thread Wiki API

Use Thread Wiki to build and query a filesystem-backed, per-thread knowledge base without an embedding model or vector database. Uploaded documents remain source of truth under `docs/threads/<thread-id>/`; staged text, generated pages, indexes, and derived navigation artifacts live under `docs/threads-wiki/<thread-id>/`.

Examples target the custom FastAPI application at `http://localhost:8000`. Start it with:

```bash
uv run python -m uvicorn webapp:app --host 127.0.0.1 --port 8000
```

`langgraph dev` is a separate LangGraph API surface, typically at its printed URL (default port `2024`). Bind Uvicorn to `0.0.0.0` only in a container or on a trusted network.

## Authentication

All Thread Wiki routes are protected by delegated LangGraph authentication. Present a valid configured API key or OAuth session in `x-api-key`, or as `Authorization: Bearer <credential>`. When both headers are present, the `x-api-key` credential takes precedence.

Static-key selection is `LANGCHAIN_API_KEY`, then `UPLOAD_API_KEY`. Wiki routes have no generated-key fallback: if neither configured key nor a valid OAuth session is available, authentication fails. See [Authentication](../guides/authentication.md) for session behavior and production configuration.

Authentication gates these routes, but handlers do not currently enforce per-thread ownership. Deployments with multiple users must add authorization at a trusted gateway or application boundary.

Runtime route code and tests are wire truth. The versioned OpenAPI snapshot is useful for route shapes, but currently omits complete authentication/security declarations and some runtime error responses.

## Quick example

```bash
export DEEP_RESEARCH_API_KEY='replace-me'
export THREAD_ID='abc-123'

curl -X POST "http://localhost:8000/threads/${THREAD_ID}/wiki/ingest" \
  -H "x-api-key: ${DEEP_RESEARCH_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"note":"Initial source review"}'

curl -H "x-api-key: ${DEEP_RESEARCH_API_KEY}" \
  "http://localhost:8000/threads/${THREAD_ID}/wiki/status"

curl -X POST "http://localhost:8000/threads/${THREAD_ID}/wiki/query" \
  -H "x-api-key: ${DEEP_RESEARCH_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"question":"What are the main findings?","file_results":true}'
```

Ingest and query require source documents and a ready wiki respectively; see workflow and error details below.

## Endpoints

| Method and path | Purpose | Important behavior |
| --- | --- | --- |
| `POST /threads/{id}/wiki/ingest` | Start or replace ingest. | Optional `topic` and `note`; requires source documents. |
| `POST /threads/{id}/wiki/import/git` | Import a public repository, then ingest it. | Accepts `url` plus optional `ref`, `topic`, and `note`; existing thread folder required. |
| `GET /threads/{id}/wiki/status` | Poll ingest state. | Returns phase, progress, source counts, readiness, errors, timestamps, and optional review/code-analysis summaries. |
| `GET /threads/{id}/wiki/progress` | Stream progress as SSE. | Emits `progress`, terminal `end`, and keepalive `heartbeat` events. |
| `POST /threads/{id}/wiki/ingest/cancel` | Cancel active ingest. | Stops at a cancellation checkpoint and reports whether a task was cancelled. |
| `POST /threads/{id}/wiki/query` | Ask a grounded question. | Requires `question`; optional `file_results` defaults to `true`; wiki must be ready. |
| `POST /threads/{id}/wiki/lint` | Reconcile wiki structure and references. | Optional `topic` and `note`; wiki must be initialized. |
| `GET /threads/{id}/wiki/insights` | Read generated insight summaries. | Wiki must be initialized. |
| `GET /threads/{id}/wiki/graph` | Read wiki nodes and edges. | Wiki must be initialized. |
| `DELETE /threads/{id}/wiki` | Delete thread wiki and uploaded sources. | Removes both wiki workspace and `docs/threads/{id}` documents. |
| `GET /threads/{id}/wiki/tree` | Browse wiki files as a tree. | Returns current workspace hierarchy. |
| `GET /threads/{id}/wiki/file?path=...` | Read one wiki file. | `path` must resolve safely inside the thread wiki workspace. |

Start ingest with an optional hint:

```json
{
  "topic": "Quarterly policy review",
  "note": "Compare exceptions across source files"
}
```

Status phases include `idle`, `initializing`, `importing`, `staging_sources`, `analyzing`, `applying`, `reviewing`, `refreshing_index`, `merging`, and `ready`; terminal failure states are `error` and `cancelled`. Stream progress with:

```bash
curl -N -H "x-api-key: ${DEEP_RESEARCH_API_KEY}" \
  "http://localhost:8000/threads/${THREAD_ID}/wiki/progress"
```

Query responses contain an answer, optional filed wiki path, and structured source citations. `file_results: true` lets durable answers be filed under the wiki query area.

## Workflows

Upload documents to `docs/threads/<id>` through the [Document upload API](upload.md). A successful thread-folder upload launches background ingest. Deleting a thread source cancels conflicting work, removes source-derived references, and launches lint reconciliation.

Recognized source inputs include PDF, DOCX, PPTX, XLSX, Markdown, text, JSON, YAML, CSV, supported whole source-code files, and supported explicitly language-tagged code fences. Whole-code detection uses extension first and inspects a shebang only for extensionless Python, Node, Ruby, or PHP scripts. Supported source extensions are `.py`, `.pyw`, `.js`, `.mjs`, `.cjs`, `.jsx`, `.ts`, `.mts`, `.cts`, `.tsx`, `.java`, `.go`, `.rs`, `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hh`, `.hpp`, `.hxx`, `.cs`, `.rb`, and `.php`; ambiguous `.h` files are parsed as C and C++ and the lower-error result wins, with C on an exact tie.

Uploaded code is parsed locally and never imported, compiled, or executed. Explicit tagged snippets remain attached to their containing document and do not enter repository symbol/import resolution. Untagged or uncertain snippets stay ordinary document content.

Public Git import accepts only anonymous public HTTPS GitHub, GitLab, or Bitbucket URLs. Clone is shallow and non-interactive; private credentials, submodules, Git LFS, dependency fetches, and source execution are unsupported. Current path resolution happens before clone, so `docs/threads/<id>` must already exist; a Git-only import for a new thread returns `404`.

Grounded answers cite original raw document pages or original code line ranges. Derived `/raw/_code/` indexes and semantic chunks are navigation aids, never evidence or citation targets.

Research-agent use is explicit: the agent must call `llm_wiki_query`. Wiki context is not automatically injected into each research request.

Typical sequence:

1. Upload at least one source to `threads/<id>` or create that thread folder before public Git import.
2. Let auto-ingest run, or call `POST .../ingest`.
3. Poll `status` or stream `progress` until `wiki_ready` is true.
4. Query directly, or let research orchestration call `llm_wiki_query`.
5. Use `lint`, `insights`, `graph`, `tree`, and `file` for maintenance and inspection.
6. Delete the wiki only when uploaded thread sources should also be removed.

## Troubleshooting

- `401`: send a valid configured key or OAuth session using an accepted header. Unlike upload routes, wiki authentication has no process-local generated-key fallback.
- `404`: source-dependent routes cannot find `docs/threads/<id>`, the requested wiki file is missing, or Git import was attempted before the thread folder existed.
- `409` on query: wait for `wiki_ready: true`; query requires ready state.
- `409` on lint, insights, or graph: run ingest first; these routes require an initialized wiki.
- Ingest appears stalled: inspect `status`, keep the SSE connection open for `progress` and `heartbeat`, then cancel and restart if needed.
- Code fell back to text: inspect `code_analysis.warnings` and the manifest. Oversized input, unsupported encoding, disabled parsing, parser failure, or no usable units intentionally use safe text fallback.
- Git import fails: verify anonymous public HTTPS URL, allowed host, optional ref, existing thread folder, and configured time/file/byte limits.
- Citation is absent: invalid or stale derived line ranges are omitted; treat only original raw sources and validated line ranges as evidence.

## Related documentation

- [Document upload API](upload.md)
- [Authentication](../guides/authentication.md)
- [AST-aware code ingestion](../architecture/code-ingestion.md)
- [Architecture overview](../architecture/overview.md)
- [Wiki diagram design specification](../architecture/wiki-diagram-design.md)
