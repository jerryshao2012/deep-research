# Architecture overview

Deep Research separates research orchestration, evidence acquisition, per-thread knowledge, custom web capabilities, model selection, persistence, and output formatting. This map is for contributors deciding where a change belongs; detailed API and deployment procedures live elsewhere.

## System map

```text
User / caller
     |
     +--> LangGraph research surface --> research_agent application --> delegated researcher
     |                                      |                          |
     |                                      +--> tools/retrieval-+
     |                                      +--> Thread Wiki query
     |                                      +--> model factory --> model provider
     |                                      +--> state/checkpoint persistence
     |                                      +--> output skill --> report/artifact
     |
     +--> custom FastAPI webapp --> auth, chat, documents, wiki, skills
                                         |        |        |
                                         +--------+--------+--> files/databases/state
```

Control flows from an entry surface into an owning application capability. `research_agent` owns LangGraph composition, runtime tool assignment, evaluation and verification flow, CLI, model configuration, authentication, persistence, and reliability. `research_agent.research_subagent` contains researcher prompts, tool definitions, and supporting retrieval/evaluation utilities, but its delegated runtime is intentionally web-only: Tavily search, page fetch, and reflection. Top-level `webapp` and `thread_wiki` remain independent packages. Data flows through ports or existing compatibility boundaries to filesystem, database, search, model, and cloud adapters. The custom FastAPI application and LangGraph runtime are separate entry surfaces even when they share models, thread identifiers, and stored sources.

## Component ownership

| Area | Responsibility | Primary boundary |
| --- | --- | --- |
| Orchestration and research workflow | Decompose questions, delegate research, synthesize evidence, verify reports, and manage graph state. | LangGraph composition in [`research_agent/agent.py`](../../research_agent/agent.py). |
| Researcher source package | Supply researcher instructions plus web, reflection, filesystem, wiki, retrieval, evaluation, and verification implementations. The delegated sub-agent receives only Tavily search, page fetch, and reflection; application graph owns other runtime assignments and lifecycle hooks. | [`research_agent/research_subagent/`](../../research_agent/research_subagent/) subpackage. |
| Thread Wiki | Stage per-thread sources, build linked wiki pages, analyze code, query grounded knowledge, and maintain citations/progress. | `thread_wiki` routes and services, with explicit `llm_wiki_query` use by research orchestration. |
| Custom FastAPI webapp | Compose authentication, chat, document lifecycle, wiki, and skill management for browser and custom clients. | `webapp:app`; see [clean architecture boundaries](clean-architecture.md). |
| Model factory and providers | Resolve configured aliases/providers, construct chat models, and select compatible checkpoint behavior. | [`research_agent/model_factory.py`](../../research_agent/model_factory.py) and provider SDK adapters. |
| Persistence and state | Store uploaded sources, generated wiki workspaces, auth/session data, thread/run state, checkpoints, progress, and evaluation history. | Filesystem plus SQLite, PostgreSQL, Cosmos, or configured platform adapters. |
| Output skills | Discover, validate, and apply pluggable output contracts such as golden datasets and interview preparation. | Skill registry searches `.deepagents/skills/` and the supported `docs/.deepagents/skills/` extension root. This repository currently supplies the former; the latter need not exist. |

## Research control and data flow

1. Caller submits research question through CLI or LangGraph surface; custom web clients may first create thread state or upload documents through FastAPI.
2. Orchestration plans work and invokes researcher or subagent nodes. Model construction remains behind model factory rather than inside workflow nodes.
3. Tools acquire web or local evidence. Thread knowledge enters research only through an explicit `llm_wiki_query` call; it is not injected automatically.
4. Evidence and intermediate state remain attached to graph/thread execution. Long-lived sources and wiki artifacts remain under their owning persistence boundaries.
5. Synthesis produces report, then verification may request revision. Selected output skill transforms final research into its requested artifact shape.

## Document and wiki data flow

Custom FastAPI document routes validate paths and persist uploaded sources below documents root. A `threads/<id>` upload can launch background Thread Wiki ingest. Wiki staging extracts ordinary documents, parses recognized source code without executing it, and builds generated knowledge. Query results cite original pages or source lines; derived semantic artifacts support navigation only.

For detailed ingestion behavior, see [AST-aware code ingestion](code-ingestion.md), [wiki diagram design specification](wiki-diagram-design.md), and the [enhanced wiki architecture image](diagrams/enhanced-llm-wiki-architecture.png).

## Boundary and lifecycle rules

- Entry surfaces own wire protocols; application/domain boundaries own policies and use cases; infrastructure owns external systems.
- Source files, generated wiki content, checkpoints, and auth/session records have different lifecycles and must not be conflated.
- Uploaded code is data: parsed but never imported, compiled, or executed.
- Original sources are evidence. Generated `/raw/_code/` artifacts are not citation targets.
- Current architecture is mid-migration: some declared ports are active, some routes still call concrete services, and `RunExecutor` remains a target boundary.

## Related documentation

- [Clean architecture boundaries](clean-architecture.md)
- [AST-aware code ingestion](code-ingestion.md)
- [Wiki diagram design specification](wiki-diagram-design.md)
- [Document upload API](../api/upload.md)
- [Thread Wiki API](../api/wiki.md)
