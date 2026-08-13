# Document upload API

Use custom FastAPI document routes to upload, inspect, extract, download, and delete research sources. Examples target the local web application at `http://localhost:8000`; `langgraph dev` starts a separate LangGraph surface, typically at the URL it prints (default port `2024`).

Start the custom application locally:

```bash
uv run python -m uvicorn webapp:app --host 127.0.0.1 --port 8000
```

Bind to `0.0.0.0` only inside a container or on a trusted network.

## Authentication

`GET /health` is public. Every other route in this guide is protected.

- A configured static API key is accepted only in `X-API-Key`.
- An authenticated OAuth session may be presented in `X-API-Key` or as `Authorization: Bearer <session-token>`.
- Static-key selection is `UPLOAD_API_KEY`, then `LANGCHAIN_API_KEY`, then a process-local generated key. A generated key changes on restart and is unsuitable for shared or durable environments.

See [Authentication](../guides/authentication.md) for OAuth sessions, production controls, and key rotation.

## Quick example

```bash
export DEEP_RESEARCH_API_KEY='replace-me'

curl http://localhost:8000/health

curl -X POST http://localhost:8000/documents/upload \
  -H "X-API-Key: ${DEEP_RESEARCH_API_KEY}" \
  -F 'folder=policy' \
  -F 'files=@report.pdf'

curl -H "X-API-Key: ${DEEP_RESEARCH_API_KEY}" \
  'http://localhost:8000/documents/list?folder=policy'
```

Upload returns HTTP `201` with saved-file results, a saved-file count, remaining free-space data, and Thread Wiki trigger flags.

Representative response for ordinary folder:

```json
{
  "folder": "policy",
  "count": 2,
  "saved": [
    {"filename": "source-1.pdf", "path": "docs/policy/source-1.pdf", "size": 642000},
    {"filename": "source-2.docx", "path": "docs/policy/source-2.docx", "size": 523000}
  ],
  "total_uploaded_bytes": 1165000,
  "free_space_bytes": 98765432100,
  "free_space_human": "92.00 GB",
  "wiki_ingest_started": false,
  "wiki_ingest_thread_id": null
}
```

For `folder=threads/<thread-id>`, successful response sets `wiki_ingest_started` to `true` and returns extracted thread ID. Ingest starts asynchronously; poll Thread Wiki status instead of treating upload response as wiki-ready signal.

Representative list response:

```json
{
  "folder": "policy",
  "count": 2,
  "items": [
    {"name": "archive", "type": "folder", "size": null},
    {"name": "source-1.pdf", "type": "file", "size": 642000}
  ]
}
```

## Endpoints

| Method and path | Input | Result |
| --- | --- | --- |
| `GET /health` | None | HTTP `200` health status; no authentication required. |
| `GET /storage/info` | None | Storage metrics, model-factory diagnostics, and raw process `environment_variables`. |
| `GET /documents/view/{filename}` | `folder` query, default `policy` | Inline file response for browser viewing. |
| `GET /documents/extract/{filename}` | `folder` query, default `policy` | Extracted text and metadata for `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.txt`, or `.md`. |
| `POST /documents/upload` | Multipart `files` (required, repeatable) and `folder` (default `policy`) | HTTP `201`; per-file save results, counts, free space, and wiki-trigger state. |
| `GET /documents/list` | `folder` query, default `policy` | Sorted `items` containing both files and folders, plus folder and count. |
| `GET /documents/download/{filename}` | `folder` query, default `policy` | Attachment response for the requested file. |
| `DELETE /documents/{filename}` | `folder` query, default `policy` | Deletes one file and reports the deleted path. |
| `DELETE /documents/folder/{folder}` | Folder path parameter | Deletes direct files in the folder; keeps the folder and nested directories. |

> **Security warning:** `/storage/info` currently returns `dict(os.environ)` without redaction. The response may contain API keys, cloud credentials, and other secrets. Do not share, log, export, or screenshot it. This is a current security limitation, not a sanitized diagnostics contract.

Folder input is normalized before resolving it under the configured documents root: backslashes become `/`, surrounding whitespace and leading or trailing slashes are removed, and `.` or repeated separators collapse. For example, `/etc` becomes `etc` and `a/./b` becomes `a/b`. Empty normalized values and paths retaining a `..` component are rejected. Uploaded filenames are reduced to their basename. Extraction rejects unsupported extensions even if they can be stored or downloaded.

Upload multiple files by repeating the multipart field:

```bash
curl -X POST http://localhost:8000/documents/upload \
  -H "X-API-Key: ${DEEP_RESEARCH_API_KEY}" \
  -F 'folder=research/sources' \
  -F 'files=@source-1.pdf' \
  -F 'files=@source-2.docx'
```

View, extract, download, and delete a file:

```bash
curl -H "X-API-Key: ${DEEP_RESEARCH_API_KEY}" \
  'http://localhost:8000/documents/view/report.pdf?folder=policy'

curl -H "X-API-Key: ${DEEP_RESEARCH_API_KEY}" \
  'http://localhost:8000/documents/extract/report.pdf?folder=policy'

curl -H "X-API-Key: ${DEEP_RESEARCH_API_KEY}" \
  'http://localhost:8000/documents/download/report.pdf?folder=policy' \
  --output report.pdf

curl -X DELETE -H "X-API-Key: ${DEEP_RESEARCH_API_KEY}" \
  'http://localhost:8000/documents/report.pdf?folder=policy'
```

## Workflows

For CLI research, upload sources to a project folder and pass that folder to the agent:

```bash
uv run python -m research_agent.cli 'Summarize policy changes' --doc-folder ./docs/policy
```

For Thread Wiki, upload to `threads/<thread-id>`:

```bash
curl -X POST http://localhost:8000/documents/upload \
  -H "X-API-Key: ${DEEP_RESEARCH_API_KEY}" \
  -F 'folder=threads/abc-123' \
  -F 'files=@report.pdf'
```

That folder pattern triggers background wiki ingest after a successful upload. Deleting a source from a thread folder cancels active ingest as needed, cascades source cleanup, and launches wiki lint reconciliation. Use the [Thread Wiki API](wiki.md) to monitor or query the resulting wiki; research-agent retrieval remains an explicit `llm_wiki_query` tool call rather than automatic context injection.

## Troubleshooting

- `401 Invalid or missing API key`: send the selected static key in `X-API-Key`, or a valid OAuth session in an accepted header. Restart-dependent generated keys often explain a previously working local request.
- `400` path error: provide a nonempty folder and remove every `..` component. Leading slashes and `.` segments are normalized rather than rejected, so check the normalized folder returned by the API when a path is surprising.
- `404`: list the same folder and verify case-sensitive filename and folder values.
- Extraction error: use one of the six extraction formats listed above and confirm the file is readable.
- Connection refused: start Uvicorn on port `8000` and keep Postman or curl pointed at the custom application, not the separate `langgraph dev` port.
- Upload or wiki hook failure: check server logs and free space from `GET /storage/info`; the upload response reports saved files and whether wiki ingest was triggered.

## Related documentation

- [Thread Wiki API](wiki.md)
- [Postman collection](postman/README.md)
- [Authentication](../guides/authentication.md)
- [Local development](../getting-started/local-development.md)
- [Architecture overview](../architecture/overview.md)
