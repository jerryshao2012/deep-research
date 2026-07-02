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
