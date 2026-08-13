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

## Tool behavior and runtime ownership

| Tool or group | Runtime owner | Current behavior and caveats |
| --- | --- | --- |
| `tavily_search` | Delegated researcher | Uses Tavily to discover URLs, then returns fetched page content as Markdown. Result count and `general`/`news`/`finance` topic are injected runtime controls; `--no-web` disables external retrieval. |
| `fetch_webpage_content` | Delegated researcher | Fetches one known URL and converts readable content to Markdown. Use after URL discovery or when caller already knows source; network, timeout, and extraction failures remain evidence gaps. |
| `think_tool` | Orchestrator and delegated researcher | Records strategic reflection in `OUTPUT_FOLDER/research_reflection.log` and attempts to publish `/research_reflection.md` to graph state. Reflection should compare findings, contradictions, gaps, source quality, and search budget; it is not evidence by itself. |
| `ls`, `glob`, `read_file` | Orchestrator | Prefer virtual graph filesystem, then use constrained local fallback. Markdown reads accept a case-insensitive exact-heading selector such as `guide.md#Installation`; file access still follows configured containment and context limits. |
| `read_docs_folder` | Orchestrator | Extracts PDF, text, Markdown, Word, PowerPoint, and Excel input. It prefers ready wiki content, caches extracts, summarizes oversized folders, and supports targeted `specific_files` reads internally. |
| `llm_wiki_query` | Orchestrator | Explicitly queries current thread wiki and persists cited findings as `/cited_response*.md`. Result is research material that must be checked, combined with other evidence, and written into final report. |
| `write_file` | Orchestrator | Writes state/output artifacts and overwrites same path. Final-report handling normalizes known document source paths; callers still own synthesis, citation numbering, and safe destination selection. |

Skill registry helpers and bundled renderer scripts are application utilities, not a promise that every helper is exposed as agent-callable tool. Add or expose one only through registration path described in [Extend the research agent](../development/extending-the-agent.md#add-a-custom-tool).

## Research control and data flow

1. Caller submits research question through CLI or LangGraph surface; custom web clients may first create thread state or upload documents through FastAPI.
2. Orchestration plans work and invokes researcher or subagent nodes. Model construction remains behind model factory rather than inside workflow nodes.
3. Tools acquire web or local evidence. Thread knowledge enters research only through an explicit `llm_wiki_query` call; it is not injected automatically.
4. Evidence and intermediate state remain attached to graph/thread execution. Long-lived sources and wiki artifacts remain under their owning persistence boundaries.
5. Synthesis produces report, then verification may request revision. Selected output skill transforms final research into its requested artifact shape.

### Research mechanics

- **Planning:** orchestrator decomposes complex request into bounded research units and tracks unfinished work; plan representation is runtime detail, not stable public API.
- **Local context:** when document folder is supplied, application-owned file tools read normalized, bounded source set before delegating external research. Delegated researcher cannot read filesystem.
- **Web evidence:** delegated `research-agent` uses Tavily URL discovery, page fetch, and `think_tool`; it returns evidence to orchestrator rather than writing final application files.
- **Reflection:** `think_tool` pauses search loop to summarize findings, test source quality, identify contradictions/gaps, and choose next query or stopping point.
- **Synthesis:** orchestrator consolidates delegated findings, local/wiki evidence, and citations. Sub-agent output is input evidence, not final answer by itself.
- **Skills:** selected skill instructions shape final artifact. Application rendering validates and writes skill output under thread output folder; installed skills and schema requirements vary by skill.
- **Verification:** citation grounding, sufficiency judging, and adversarial gap analysis can request bounded revision after report generation.

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
