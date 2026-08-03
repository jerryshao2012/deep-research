# Test document routes with Postman

Use the checked-in Postman collection for repeatable local document API checks. Endpoint details remain in the [upload guide](../upload.md) and [wiki guide](../wiki.md), not in this collection README.

## Import

Import both files into Postman:

- [collection.json](collection.json)
- [environment.json](environment.json)

Start the custom FastAPI application separately:

```bash
uv run python -m uvicorn webapp:app --host 127.0.0.1 --port 8000
```

The collection default targets this standalone server. `langgraph dev` also mounts the custom application on its separate LangGraph surface; use the URL it prints (default port `2024`) instead of `base_url` above when testing that surface.

## Environment

Select the imported environment and set:

| Variable | Local value | Purpose |
| --- | --- | --- |
| `base_url` | `http://localhost:8000` | Custom FastAPI application. |
| `api_key` | Configured upload API key | Sent by collection requests in `X-API-Key`. |

Keep secrets in Postman environment values rather than collection examples or version control.

## Authentication

Collection uses `X-API-Key: {{api_key}}`. Static upload-key precedence is `UPLOAD_API_KEY`, then `LANGCHAIN_API_KEY`, then a process-local generated key. `/health` is unauthenticated; protected requests require the configured credential. See [authentication guide](../../guides/authentication.md) for OAuth and production behavior.

## Sensitive storage response

`Storage Info` currently returns storage metrics, model-factory diagnostics, and raw `environment_variables` from `dict(os.environ)` without redaction.

> **Security warning:** This payload may contain API keys, cloud credentials, and other secrets. Do not share, log, export, or screenshot the response. Treat this as a current security limitation, not a sanitized diagnostics contract.

Prefer skipping `Storage Info` entirely until the runtime redacts environment values. If controlled local diagnostics are unavoidable, use an isolated trusted session and disable or avoid Postman Console logging; never export, screenshot, or share the response.

## Testing

Run requests in a stateful sequence:

1. Health check confirms `base_url` and server availability.
2. Upload a small disposable file named exactly `example.pdf` to the default `policy` folder; the checked-in download and delete requests use that filename and folder.
3. List that folder and inspect the saved item.
4. Download `example.pdf`.
5. Delete `example.pdf` or the disposable folder contents.

For server startup and environment setup, see [local development](../../getting-started/local-development.md).

## Troubleshooting

- `401`: confirm current environment is selected, `api_key` has no extra whitespace, and server uses the same configured key.
- Connection refused: verify Uvicorn is listening on `127.0.0.1:8000` and `base_url` is exactly `http://localhost:8000`.
- `404`: list target folder, then check case-sensitive filename and folder values.
- `400` path error: provide a nonempty folder and remove every `..` component. Leading slashes normalize away and `.` segments collapse rather than causing an error.
- Upload request fails: use multipart form-data, keep field name `files`, and provide at least one file.
