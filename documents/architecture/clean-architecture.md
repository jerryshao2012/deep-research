# Clean Architecture Boundaries

## Dependency direction

Capabilities use package-local feature slices. Dependencies point inward:

```text
interfaces -> application -> domain
infrastructure -> application/domain
composition root -> interfaces/application/infrastructure
```

- `domain`: framework-neutral entities, values, and policies.
- `application`: use cases and outbound port protocols.
- `interfaces`: FastAPI, LangGraph middleware/tool, CLI, and DTO adapters.
- `infrastructure`: database, filesystem, model, search, and cloud adapters.

Domain and application layers must not import FastAPI, LangGraph, databases,
cloud SDKs, or another outward adapter. A feature may consume another feature
only through that feature's public package entrypoint.

## Feature ownership

| Feature | Owns |
| --- | --- |
| `auth` | OAuth/passkeys, sessions, identity, authorization |
| `chat` | research runs, stream policy, interrupts, resume behavior |
| `threads` | thread metadata, state, retention, run records |
| `wiki` | ingest, query, lint, graph, citations, progress |
| `documents` | upload, extraction, source lifecycle |
| `skills` | discovery, validation, installation, removal |

Existing deployables remain unchanged. `webapp` is custom FastAPI composition,
`../../agent.py` is LangGraph composition, and `../../model_factory.py` selects model and
checkpoint adapters. Deprecated `../../server.py` is frozen: production entrypoints
must use official LangGraph Platform plus the custom FastAPI application.

## Compatibility

HTTP paths, request/response bodies, cookies, SSE event shapes, authentication
headers, and persisted formats remain stable during extraction. Typed
application errors are mapped back to current wire responses at interface
adapters. `../../contracts/custom-api.openapi.json` records the active custom API.

## Implemented dependency map

| Application port | Active adapter or composition |
| --- | --- |
| `AuthStore` | SQLite, PostgreSQL, and Cosmos persistence adapters |
| `ThreadRepository` | bounded `InMemoryThreadRepository` for custom chat state |
| `RunExecutor` | contract reserved for active LangGraph composition |
| `Clock` | injectable `SystemClock` adapter |
| `WikiRepository` | wiki page persistence boundary |
| `SourceStore` | uploaded and extracted source boundary |
| `SearchIndex` | evidence indexing and retrieval boundary |
| `ModelRunner` | wiki generation/model invocation boundary |
| `ProgressStore` | long-running ingest progress boundary |

FastAPI route functions remain edge controllers: validate wire input, invoke
ports, and preserve existing response bodies and status codes. Deprecated
`../../server.py` remains compatibility-test-only because `../../tests/test_server.py`
still exercises it; production Docker, entrypoint, and LangGraph configs have
zero consumers.

## Enforcement

Run:

```bash
uv run python scripts/check_architecture.py
uv run python scripts/snapshot_openapi.py --check
uv run pytest tests/test_architecture_boundaries.py -q
uv run --extra dev mypy --follow-imports=skip --ignore-missing-imports webapp/features/auth/application webapp/features/threads/application webapp/features/wiki/application
```

Architecture checker rejects outward imports from inward layers,
cross-feature internal imports, and local cycles within migrated feature
slices. Every new use case requires tests with fake ports and focused adapter
contract tests.
