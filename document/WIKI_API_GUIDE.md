# Thread Wiki API Guide

The Thread Wiki feature provides **per-thread RAG (Retrieval-Augmented Generation) without a vector database**. When documents are uploaded to a thread's folder, they are automatically ingested into a wiki knowledge base that the deep research agent uses as grounded context.

## How It Works

```
Upload documents → Auto-ingest → Wiki knowledge base → Query / Research agent
   ./docs/threads/<id>/     ./docs/threads-wiki/<id>/
```

1. **Upload**: Documents uploaded to `./docs/threads/<thread-id>/`
2. **Ingest**: Automatically triggered — sources are staged, reviewed by LLM, and applied to wiki pages
3. **Query**: The wiki can be queried directly via API, and the research agent automatically injects wiki context when answering questions
4. **Delete**: Removing documents cancels any active ingest and runs lint reconciliation

### Supported File Types

| Format | Extension | Extraction Method |
|--------|-----------|-------------------|
| PDF | `.pdf` | PyMuPDF4LLM → Markdown (fallback: pypdf) |
| Word | `.docx` | python-docx (paragraphs + tables) |
| PowerPoint | `.pptx` | python-pptx (slides + speaker notes) |
| Excel | `.xlsx` | openpyxl (sheets → pipe-delimited text) |
| Markdown | `.md` | Direct read |
| Text | `.txt` | Direct read |
| JSON | `.json` | Direct read |
| YAML | `.yaml`, `.yml` | Direct read |
| CSV | `.csv` | Direct read |

---

## Wiki API Endpoints

All wiki endpoints require authentication via `x-api-key` or `Authorization: Bearer` header.

### 1. Trigger Wiki Ingest

`POST /threads/{thread_id}/wiki/ingest`

Trigger (or re-trigger) wiki ingest for a thread's uploaded documents. If an ingest is already running, it will be cancelled and replaced.

**Request body (optional):**
```json
{
  "note": "Additional context for the ingest",
  "topic": "Custom topic label (default: Thread <id>)"
}
```

**Response:**
```json
{
  "thread_id": "abc-123",
  "status": "started",
  "message": "Wiki ingest started. Poll /wiki/status or stream /wiki/progress for updates."
}
```

**Example:**
```bash
curl -X POST http://localhost:2024/threads/abc-123/wiki/ingest \
  -H 'x-api-key: your_api_key' \
  -H 'Content-Type: application/json' \
  -d '{"note": "Initial document batch"}'
```

### 2. Get Wiki Ingest Status

`GET /threads/{thread_id}/wiki/status`

Poll the current ingest progress. Use this for periodic polling.

**Response:**
```json
{
  "thread_id": "abc-123",
  "phase": "analyzing",
  "progress": 40,
  "detail": "Analyzing 3 sources...",
  "source_count": 3,
  "sources_processed": 3,
  "error": null,
  "started_at": "2026-06-21T10:30:00+00:00",
  "completed_at": null,
  "is_active": true,
  "wiki_ready": false
}
```

**Ingest phases:**
| Phase | Progress | Description |
|-------|----------|-------------|
| `idle` | 0% | No ingest has been run |
| `initializing` | 5% | Creating wiki scaffold |
| `staging_sources` | 15% | Collecting and staging source files |
| `analyzing` | 40% | LLM review/analysis pass (read-only) |
| `applying` | 70% | LLM apply pass (writing wiki pages) |
| `refreshing_index` | 90% | Rebuilding wiki index |
| `ready` | 100% | Ingest completed successfully |
| `error` | -1 | Ingest failed |
| `cancelled` | -1 | Ingest was cancelled |

**Example:**
```bash
curl -H 'x-api-key: your_api_key' \
  'http://localhost:2024/threads/abc-123/wiki/status'
```

### 3. Stream Ingest Progress (SSE)

`GET /threads/{thread_id}/wiki/progress`

Real-time Server-Sent Events stream for ingest progress. Connect once and receive events as the ingest proceeds.

**Event types:**
- `progress` — emitted on phase change or progress update
- `end` — emitted when ingest reaches a terminal state (stream closes)

**Example:**
```bash
curl -N -H 'x-api-key: your_api_key' \
  'http://localhost:2024/threads/abc-123/wiki/progress'
```

**JavaScript (EventSource):**
```javascript
const source = new EventSource('/threads/abc-123/wiki/progress', {
  headers: { 'x-api-key': 'your_api_key' }
});

source.addEventListener('progress', (event) => {
  const data = JSON.parse(event.data);
  console.log(`Phase: ${data.phase}, Progress: ${data.progress}%`);
  updateProgressBar(data.progress);
});

source.addEventListener('end', (event) => {
  const data = JSON.parse(event.data);
  console.log(`Ingest complete. Wiki ready: ${data.wiki_ready}`);
  source.close();
});
```

### 4. Cancel Ingest

`POST /threads/{thread_id}/wiki/ingest/cancel`

Cancel a running ingest. The background task stops at the next phase checkpoint.

**Response:**
```json
{
  "thread_id": "abc-123",
  "cancelled": true,
  "message": "Ingest cancelled."
}
```

**Example:**
```bash
curl -X POST http://localhost:2024/threads/abc-123/wiki/ingest/cancel \
  -H 'x-api-key: your_api_key'
```

### 5. Query Wiki

`POST /threads/{thread_id}/wiki/query`

Query the thread's wiki knowledge base. Returns a grounded answer with citations from the ingested documents.

> **Note**: The wiki must be in `ready` state (ingest completed) before querying.

**Request body:**
```json
{
  "question": "What are the key findings about topic X?",
  "file_results": true
}
```

- `file_results` (default `true`): If the answer has durable value, file it as a wiki page at `/wiki/query/<slug>.md` for future reference.

**Response:**
```json
{
  "answer": "Based on the ingested documents, the key findings are...\n\nSources: (Source: /raw/document.pdf.md, p. 42)",
  "filed_path": "/wiki/query/key-findings-about-topic-x.md",
  "sources_cited": [
    {
      "kind": "raw",
      "raw_path": "/raw/document.pdf.md",
      "page": 42,
      "locator": null,
      "url": null
    }
  ]
}
```

Each `SourceCitation` object contains:
- `kind`: one of `"raw"` (uploaded document), `"web"` (URL), or `"section"` (file#heading)
- `raw_path`: the document path (for `raw`/`section` kinds)
- `page`: page number when derivable (for PDFs)
- `locator`: free-form locator (slide number, sheet+row, heading text, or web source title)
- `url`: URL (for `web` kind)

**Example:**
```bash
curl -X POST http://localhost:2024/threads/abc-123/wiki/query \
  -H 'x-api-key: your_api_key' \
  -H 'Content-Type: application/json' \
  -d '{"question": "Summarize the main themes across all uploaded documents"}'
```

### 6. Run Lint Reconciliation

`POST /threads/{thread_id}/wiki/lint`

Run lint reconciliation on the wiki. Use this after document deletions to reconcile stale references, fix orphan pages, and repair cross-links.

> **Note**: Lint is automatically triggered when documents are deleted from a thread folder. Use this endpoint for manual reconciliation.

**Request body (optional):**
```json
{
  "note": "Focus on removing references to deleted_file.pdf",
  "topic": "Custom topic label"
}
```

**Response:**
```json
{
  "result": "## Reconciled Changes\n- Removed 2 stale references...",
  "topic": "Thread abc-123"
}
```

**Example:**
```bash
curl -X POST http://localhost:2024/threads/abc-123/wiki/lint \
  -H 'x-api-key: your_api_key' \
  -H 'Content-Type: application/json' \
  -d '{"note": "Manual reconciliation after bulk delete"}'
```

---

## Automatic Behaviors

### Auto-Ingest on Upload

When documents are uploaded to `./docs/threads/<thread-id>/`, wiki ingest is **automatically triggered** in the background. No explicit API call is needed.

```bash
# This automatically triggers wiki ingest for thread abc-123
curl -X POST http://localhost:2024/documents/upload \
  -H 'X-API-Key: your_api_key' \
  -F 'folder=threads/abc-123' \
  -F 'files=@report.pdf' \
  -F 'files=@notes.docx'
```

### Auto-Cancel + Lint on Delete

When documents are deleted from a thread folder:
1. Any **active ingest is cancelled** immediately
2. A **lint reconciliation** is triggered to clean up stale wiki references

```bash
# This automatically cancels ingest + triggers lint for thread abc-123
curl -X DELETE 'http://localhost:2024/documents/report.pdf?folder=threads/abc-123' \
  -H 'X-API-Key: your_api_key'
```

### Wiki Context in Research Agent

When the research agent processes a message on a thread with a ready wiki, it **automatically queries the wiki** for relevant context and injects it into the agent's message stream. This means:

- Users don't need to explicitly query the wiki
- The research agent enriches its answers with ingested document knowledge
- Web search and other tools still work alongside wiki context

---

## Typical Workflow

### Frontend Integration

```
1. User creates a thread
   POST /threads → { thread_id: "abc-123" }

2. User uploads documents to thread folder
   POST /documents/upload (folder=threads/abc-123)
   → Auto-ingest starts

3. Frontend connects to SSE stream for progress
   GET /threads/abc-123/wiki/progress
   → Shows progress bar

4. Ingest completes (wiki_ready: true)

5. User sends a research question
   POST /threads/abc-123/runs
   → Agent automatically uses wiki context

6. (Optional) User queries wiki directly for quick answers
   POST /threads/abc-123/wiki/query

7. User deletes a document
   DELETE /documents/old-report.pdf?folder=threads/abc-123
   → Auto-cancel ingest + auto-lint
```

### Wiki Directory Structure

```
deep_research/
├── docs/
│   ├── threads/
│   │   └── <thread-id>/          # Uploaded source documents
│   │       ├── report.pdf
│   │       ├── notes.docx
│   │       └── data.xlsx
│   └── threads-wiki/
│       └── <thread-id>/          # Wiki workspace
│           ├── AGENTS.md         # Agent behavior config
│           ├── log.md            # Append-only change log
│           ├── raw/              # Staged sources (extracted text)
│           │   ├── report.pdf.md
│           │   ├── notes.docx.md
│           │   └── data.xlsx.md
│           └── wiki/             # Generated wiki pages
│               ├── index.md      # Content catalog
│               ├── entities/
│               ├── concepts/
│               └── queries/
```

---

## API Endpoints Summary

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/threads/{id}/wiki/ingest` | POST | Trigger wiki ingest |
| `/threads/{id}/wiki/status` | GET | Poll ingest progress |
| `/threads/{id}/wiki/progress` | GET | SSE stream for real-time progress |
| `/threads/{id}/wiki/ingest/cancel` | POST | Cancel active ingest |
| `/threads/{id}/wiki/query` | POST | Query wiki knowledge base |
| `/threads/{id}/wiki/lint` | POST | Run lint reconciliation |

---

## Troubleshooting

### Wiki returns 409 "Wiki is not ready yet"
The wiki hasn't completed ingest. Check status with `GET /threads/{id}/wiki/status` and wait for `phase: ready`.

### Ingest stuck in a phase
Check server logs. You can cancel and re-trigger ingest:
```bash
curl -X POST http://localhost:2024/threads/abc-123/wiki/ingest/cancel -H 'x-api-key: ...'
curl -X POST http://localhost:2024/threads/abc-123/wiki/ingest -H 'x-api-key: ...'
```

### Document type not supported
Only `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.md`, `.txt`, `.json`, `.yaml`, `.yml`, `.csv` are supported. Check server logs for "Unsupported source type" warnings.

### Content extraction fails for a specific file
Check that the required dependencies are installed:
```bash
uv sync  # Installs pymupdf4llm, python-docx, python-pptx, openpyxl
```
