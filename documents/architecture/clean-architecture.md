# Clean architecture boundaries

Use this guide when placing a new use case, adapter, or cross-feature dependency in the custom FastAPI application. It distinguishes boundaries already enforced from migration targets that are defined but not yet fully inverted.

## Follow dependency direction

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

Architecture policy keeps domain and application layers independent from FastAPI, LangGraph, databases, cloud SDKs, and outward adapters. Current automated enforcement is narrower: configured framework/cloud imports are rejected only in domain modules. Application framework/cloud neutrality remains a policy and migration target, not a mechanically enforced rule. A feature should consume another feature through that feature's public package entrypoint.

## Place work with its owner

| Feature | Owns |
| --- | --- |
| `auth` | OAuth/passkeys, sessions, identity, authorization |
| `chat` | Research runs, stream policy, interrupts, resume behavior |
| `threads` | Thread metadata, state, retention, run records |
| `wiki` | Ingest, query, lint, graph, citations, progress |
| `documents` | Upload, extraction, source lifecycle |
| `skills` | Discovery, validation, installation, removal |

Deployable composition remains split: `webapp` composes custom FastAPI, `../../agent.py` composes LangGraph, and `../../model_factory.py` selects model and checkpoint adapters. Deprecated `../../server.py` is not the production server entrypoint, but it is not consumer-free: active wiki authentication dynamically imports `server.get_current_user`, and compatibility tests still exercise the module. Removing that dependency is an intended migration follow-up.

## Read the migration boundary map

| Boundary | Current state |
| --- | --- |
| `AuthStore` | Active port with SQLite, PostgreSQL, and Cosmos persistence adapters. |
| `ThreadRepository` | Active bounded `InMemoryThreadRepository` for custom chat state. |
| `RunExecutor` | Reserved target contract; active LangGraph run composition is not inverted through it. |
| `Clock` | Active injectable `SystemClock` adapter. |
| `WikiRepository` | Defined wiki page persistence boundary; active wiki routes still invoke concrete `thread_wiki.service` functions in several flows. |
| `SourceStore` | Defined uploaded/extracted source boundary; concrete document and wiki services remain active during migration. |
| `SearchIndex` | Defined evidence indexing/retrieval boundary; not every current call path is port-driven. |
| `ModelRunner` | Defined wiki model invocation boundary; concrete service composition remains in use. |
| `ProgressStore` | Defined long-running ingest progress boundary; current route/service calls are only partially inverted. |

This map describes implemented ports and intended seams, not a claim that all runtime routes already use dependency inversion. FastAPI route functions should remain edge controllers: validate wire input, invoke an application boundary where available, and preserve existing response bodies and status codes.

## Preserve compatibility

HTTP paths, request and response bodies, cookies, SSE event shapes, authentication headers, and persisted formats remain stable during extraction. Typed application errors map back to current wire responses at interface adapters. `../../contracts/custom-api.openapi.json` snapshots active route shapes, but runtime route code and tests remain authoritative for authentication and error responses omitted from that snapshot.

Current compatibility gaps include direct concrete wiki service calls and the wiki authentication dependency on dynamically imported `server.get_current_user`. Treat those as explicit migration work; do not describe the target boundary as current runtime behavior.

## Enforce boundaries

Run:

```bash
uv run python scripts/check_architecture.py
uv run python scripts/snapshot_openapi.py --check
uv run pytest tests/test_architecture_boundaries.py -q
uv run --extra dev mypy --follow-imports=skip --ignore-missing-imports webapp/features/auth/application webapp/features/threads/application webapp/features/wiki/application
```

Architecture checker rejects configured framework imports from domain modules, domain imports of outward layers, application imports of same-feature interface or infrastructure layers, cross-feature internal imports, and local cycles within migrated feature slices. It does not currently reject framework or cloud imports merely because they occur in an application module. Every new use case needs tests with fake ports and focused adapter contract tests.

## Related documentation

- [Architecture overview](overview.md)
- [AST-aware code ingestion](code-ingestion.md)
- [Thread Wiki API](../api/wiki.md)
- [Authentication](../guides/authentication.md)
