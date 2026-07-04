# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A multi-agent deep research orchestration system built on LangGraph and the `deepagents` library. It breaks complex research queries into concurrent sub-agent tasks, synthesizes findings, and outputs structured reports via pluggable output skills (golden dataset, interview prep, study slides, etc.). Supports local models (Ollama) and cloud APIs (Anthropic Claude, OpenAI, Google Gemini). Also includes a FastAPI server implementing the LangGraph Agent Protocol for async subagent execution.

## Commands

```bash
# Install dependencies
uv sync

# Run a research query (CLI)
uv run python research_agent_cli.py "Your research topic"
uv run python research_agent_cli.py "Topic" --doc-folder ./docs --skill golden-dataset

# Development server (LangGraph Platform on port 2024)
langgraph dev

# Document upload server (FastAPI on port 8000)
uv run python -m webapp

# LangGraph visualizer
langgraph dev    # opens at localhost:8123

# Tests
uv run pytest tests/ -v                             # all tests
uv run pytest tests/test_research_agent_cli.py -v   # single file
uv run pytest tests/test_prompts_validation.py::TestDelegationStrategy -v -s  # single test
uv run pytest tests/ --cov=research_agent --cov-report=html  # coverage

# Lint & type check
uv run ruff check .
uv run mypy research_agent/

# Docker build + deploy to Azure
bash build.sh
bash deploy.sh
```

## Architecture

### Entry Points

| File | Role |
|------|------|
| `agent.py` | **Core agent graph.** Defines `ResearchState`, `ResearchStateMiddleware`, and the `agent` graph. The middleware injects state (doc folder, skill, wiki context, cited responses) before each turn. This is the entry point referenced by `langgraph.json`. |
| `research_agent_cli.py` | **Standalone CLI** for running research without the server. Supports `--doc-folder`, `--skill`, evaluation tracking, and SSL customization. |
| `webapp/__init__.py` | **FastAPI app factory** for the Document Upload API (port 8000). Configures CORS, OAuth sessions, wiki routes, and document endpoints. |
| `server.py` | ⚠️ **DEPRECATED.** Custom LangGraph Platform server — replaced by `langgraph dev`. Kept for reference only. |
| `run.py` | ⚠️ **DEPRECATED.** Thin launcher for `server:app` — replaced by `langgraph dev`. Kept for reference only. |

### Core Packages

- **`research_agent/`** — The agent's brain:
  - `prompts.py` — System instructions: `RESEARCH_WORKFLOW_INSTRUCTIONS`, `RESEARCHER_INSTRUCTIONS`, `SUBAGENT_DELEGATION_INSTRUCTIONS`
  - `tools.py` — LangChain tools: `tavily_search`, `fetch_webpage_content`, `think_tool`, file I/O (`ls`, `glob`, `read_file`, `write_file`), skill output rendering
  - `skills/` — Pluggable output formatters. Each skill is a directory with a `SKILL.md` (YAML frontmatter: name, description, keywords, instructions) and a `pipeline.py` for processing. Skills are dynamically discovered by `skill_registry.py`.
  - `utils/` — CLI helpers, web search impl, citation validation, knowledge filesystem, JSON utilities, retrieval (FAISS indexing), eval tracking

- **`thread_wiki/`** — Thread-level document RAG without a vector database:
  - `service.py` — Core wiki operations (init, ingest, query, lint) using an LLM to synthesize raw documents into a `/wiki/` knowledge base
  - `models.py` — Pydantic models for wiki paths, progress tracking, query results
  - `progress.py` — Tracks ingest phase progress (preparing → converting → wiki → completion)
  - `routes.py` — FastAPI router exposing wiki endpoints under the webapp

- **`webapp/`** — FastAPI application:
  - `config.py` — API key, version, CORS origins, docs root, OAuth toggle
  - `routes.py` — Document upload, list, delete; research trigger
  - `oauth_handler.py` — OAuth/SSO session management
  - `wiki_hooks.py` — Lifecycle hooks that auto-trigger wiki ingest on document upload
  - `auth_helpers.py` — Shared auth utilities

### Infrastructure

- **`model_factory.py`** — Multi-provider model abstraction. Creates LangChain chat models from env vars (`MODEL_NAME`, provider API keys). Supports Azure OpenAI (with managed identity), Anthropic, Google Gemini, Ollama. Also creates embedding models for retrieval.
- **`db.py`** — Database abstraction over SQLite (dev), PostgreSQL (prod), and CosmosDB (Azure prod). Stores threads and runs state.
- **`auth.py`** — Authentication for the Agent Protocol. Validates API keys (`LANGCHAIN_API_KEY`) and OAuth session tokens. Configured in `langgraph.json` as the auth module.
- **`retry_utils.py`** — Rate limit handling with TPM/RPM tracking and exponential backoff.
- **`s3_storage.py`** — S3-compatible blob storage for document persistence.

### Configuration

All config is environment-variable-driven. Key vars:

| Variable | Purpose |
|----------|---------|
| `TAVILY_API_KEY` | **Required.** Web search API key |
| `MODEL_NAME` | Model identifier (e.g., `claude-3-5-sonnet`, `gpt-4`, `glm-4.7-flash:latest`) |
| `OLLAMA_API_BASE` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | Provider selection |
| `MAX_CONCURRENT_RESEARCH_UNITS` | Max parallel sub-agents (default: 3) |
| `MAX_RESEARCHER_ITERATIONS` | Max iterations per researcher (default: 3) |
| `GRAPH_RECURSION_LIMIT` | LangGraph recursion depth (default: 200) |
| `MODEL_TPM` / `MODEL_RPM` | Rate limiting (tokens/requests per minute) |
| `DB_TYPE` | `sqlite` (dev), `postgres` (prod), or `cosmosdb` (Azure) |
| `ENABLE_EVAL_TRACKING` | Log metrics to `output/eval_history/` |
| `UPLOAD_PORT` | Document upload server port (default: 8000) |

Local dev: `source ./env.sh` loads all vars; `source ./secrets.sh` loads sensitive keys (gitignored).

### Data Flow

1. User query arrives via CLI (`research_agent_cli.py`) or the LangGraph Platform API (`langgraph dev` running the agent graph from `langgraph.json`)
2. `ResearchStateMiddleware` injects state: doc folder path, skill choice, wiki context (if docs were uploaded and ingested), existing cited responses from prior turns
3. The agent graph executes: researcher agent → sub-agent delegation (`create_deep_agent` + `SubAgent`) → tool calls (web search, file I/O, thinking) → synthesis
4. If a skill is selected, `render_skill_output` passes the synthesized result to the skill's pipeline
5. Output is written to `output/<thread_id>/`

### Skills

Skills are auto-discovered from `research_agent/skills/<skill-name>/SKILL.md` by `skill_registry.py`. Each `SKILL.md` has YAML frontmatter (`name`, `description`, `keywords`, `instructions`). The instructions are injected into the researcher's system prompt when the skill is selected. Processing logic lives in `pipeline.py`. Existing skills: golden-dataset, interview-prep, frontend-slides, study-slides, autoresearch-universal, code-generator, humanizer, find-skills.

### Testing

- `tests/conftest.py` provides shared pytest fixtures (`mock_tavily_search`, `temp_docs_dir`, etc.)
- Test patterns: mock external APIs (Tavily, model providers), not internal tools
- `test_prompts_validation.py` validates prompt quality/structure
- `test_research_agent_cli_e2e.py` is the slowest but most realistic — full workflow tests
- `test_research_agent_cli_helpers.py` covers CLI helpers without API calls

### Key Deviations from Standard LangGraph

- `webapp/__init__.py` uses `importlib.util` to load submodules by file path (not relative imports) because LangGraph's `load_custom_app` loads the module without a parent package context.
- `agent.py` wraps wiki queries in thread-pool executors when called from within a running event loop (LangGraph dev `ainvoke`), using `asyncio.run()` directly otherwise.
- The `ResearchStateMiddleware` is an `AgentMiddleware` that seeds the filesystem state with the research request and wiki context before the agent's decision step.

For more detailed guidance on enhancing prompts, adding skills/tools, testing strategy, and troubleshooting, see **AGENTS.md**.

<!-- cce-block-version: 4 -->
## Context Engine (CCE)

This project uses Code Context Engine for intelligent code retrieval and
cross-session memory.

### Searching the codebase

**You MUST use `context_search` instead of reading files directly** when
exploring the codebase, answering questions about code, or understanding how
things work. This is a hard requirement, not a suggestion. `context_search`
returns the most relevant code chunks with confidence scores instead of whole
files, and tracks token savings automatically.

When to use `context_search`:
- Answering questions about the codebase ("how does X work?", "where is Y?")
- Exploring structure or architecture
- Finding related code, functions, or patterns
- Any time you would otherwise read a file just to understand it

When to use `Read` instead:
- You need to edit a specific file (read before editing)
- You need the exact, complete content of a known file path

Other search tools:
- `expand_chunk` — get full source for a compressed result
- `related_context` — find what calls/imports a function

### Cross-session memory — use it actively

This project has persistent memory across Claude Code sessions. **You must
use it both ways: recall before answering, record after deciding.** Memory
that is not recorded is lost; memory that is not recalled does nothing.

**Before answering a non-trivial question, call `session_recall`.**
Especially when:
- The question touches architecture, design, or naming choices
- The user asks "what / why / how did we ..."
- You are about to recommend an approach the team may have already chosen
  or already rejected

Pass a topic phrase, not a single word — e.g. `session_recall("auth flow")`,
not `session_recall("auth")`. Recall is vector-similarity-based, so paraphrases
match. If recall returns relevant entries, lead with them ("Per a prior
decision: ...") instead of re-deriving the answer.

**After making a non-obvious decision, call `record_decision`.** Especially:
- Choosing one library / pattern / approach over another
- Resolving an ambiguity in the spec or requirements
- Establishing a convention the project should follow going forward
- Anything you would not want to re-litigate next session

Format: `record_decision(decision="...", reason="...")`. Keep both fields
short and specific — they are surfaced verbatim at the start of future
sessions.

**After meaningful work in a file, call `record_code_area`.** Especially when:
- You added or substantially modified a function/class
- You traced through a non-obvious flow and want future-you to find it fast

Format: `record_code_area(file_path="...", description="...")`.

Skip recording for trivial reads, formatting changes, or one-off lookups —
the goal is durable signal, not an event log.

### Drilling deeper from a recall hit

`session_recall` results are tagged with the source session id, e.g.
`[turn sid:abc123|n:5]`. To drill in:

- `session_timeline(session_id="abc123")` — walk the per-turn summaries of
  that session in order. Use this when the user asks "what was the
  reasoning?" or "how did we get there?".
- `session_event(event_id=N)` — fetch a specific tool event's raw input
  and output (capped at 4 KB at read time). Use this when a turn summary
  references a tool result you actually need to inspect.

Both are read-only and cheap. Prefer them over re-running tool calls or
asking the user to re-paste context.

### Output style

Respond in compressed style. Drop articles (a, an, the) in prose. Use
sentence fragments over full sentences. Use short synonyms (fix not resolve,
check not investigate). Pattern: [thing] [action] [reason]. [next step].
No filler, hedging, pleasantries, trailing summaries, or restating what
the user said. One sentence if one sentence is enough.

When suggesting code changes, show only the changed lines with 3 lines of
context. Never rewrite entire files. Multiple changes in one file: show each
change separately. Never echo back unchanged code the user already has.

Code blocks, file paths, commands, error messages: always written in full.
Security warnings and destructive action confirmations: use full clarity.
<!-- /cce-block -->
