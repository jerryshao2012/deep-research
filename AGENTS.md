# Deep Research Agent

A multi-agent deep research orchestration system built on LangGraph and the `deepagents` library. It breaks complex research queries into concurrent sub-agent tasks, synthesizes findings, and outputs structured reports through pluggable output skills (golden dataset, interview prep, study slides, etc.). It supports local models (Ollama), cloud APIs (Anthropic Claude, OpenAI, Google Gemini), and a FastAPI server implementing the LangGraph Agent Protocol for asynchronous subagent execution.

---

## Quick Start

### Local Development
```bash
cd deep_research
uv sync                                    # Install dependencies
export TAVILY_API_KEY=<your_key>          # Required for web search
export MODEL_NAME=glm-4.7-flash:latest    # or: claude-3-5-sonnet, gpt-4, etc.
```

### Run Research
```bash
# Basic research query
uv run python -m research_agent.cli "What is quantum computing?"

# With documents context and specific skill
uv run python -m research_agent.cli "Topic" --doc-folder ./docs --skill golden-dataset

# Generate a golden dataset for later regression scoring
uv run python -m research_agent.cli "Topic" --skill golden-dataset
```

### Interactive Development
```bash
langgraph dev                              # LangGraph Platform development server
uv run python -m webapp                    # Document upload API on port 8000
uv run pytest tests/ -v                    # Run full test suite
uv run ruff check .                        # Lint
uv run mypy research_agent/                # Type check
```

Deployment guides: [Azure](documents/deployment/azure/README.md), [AWS](documents/deployment/aws.md), and [Vercel](documents/deployment/vercel.md).

---

## Architecture Overview

| Component | Purpose | Key File |
|-----------|---------|----------|
| **Orchestration** | Core graph, research state, middleware, and sub-agent delegation | [research_agent/agent.py](research_agent/agent.py) |
| **CLI Interface** | Standalone research execution, skills, evaluation tracking, and SSL options | [research_agent/cli.py](research_agent/cli.py) |
| **Web API** | FastAPI app factory for document uploads, OAuth/SSO, wiki routes, and LangGraph custom routes | [webapp/__init__.py](webapp/__init__.py) |
| **Tools** | Source definitions for web, reflection, file, and wiki tools; runtime assignment is owned by application graph | [research_agent/research_subagent/tools.py](research_agent/research_subagent/tools.py) |
| **Prompts** | System instructions for orchestration and delegated research | [research_agent/research_subagent/prompts.py](research_agent/research_subagent/prompts.py) |
| **Skills** | Pluggable output formatters and capabilities | [.deepagents/skills/](.deepagents/skills/) |
| **Model Config** | Multi-provider model abstraction | [research_agent/model_factory.py](research_agent/model_factory.py) |
| **Tests** | 20+ test files (unit, integration, E2E) | [tests/](tests/) |

### Entry Points

| File | Role |
|------|------|
| [research_agent/agent.py](research_agent/agent.py) | Core application graph. Defines `ResearchState`, `ResearchStateMiddleware`, and the `agent` graph. Middleware injects document folder, skill, wiki context, and cited responses before each turn. Referenced by `langgraph.json`. |
| [research_agent/cli.py](research_agent/cli.py) | Packaged standalone CLI for research without the server. Supports `--doc-folder`, `--skill`, evaluation tracking, and SSL customization. Run with `python -m research_agent.cli`. |
| [webapp/__init__.py](webapp/__init__.py) | FastAPI app factory for the Document Upload API. Configures CORS, OAuth sessions, wiki routes, and document endpoints. |
| [research_agent/server.py](research_agent/server.py) | **Deprecated.** Packaged custom LangGraph Platform server, replaced by `langgraph dev`; retained for compatibility. |
| [research_agent/run.py](research_agent/run.py) | **Deprecated.** Packaged thin server launcher, replaced by `langgraph dev`; retained for compatibility. |

### Core Packages

- **`research_agent/`** - Application package:
  - `agent.py` - LangGraph composition, research state, middleware, delegation, and skill integration
  - `cli.py` and `cli_utils.py` - packaged CLI and application-level CLI/SSL helpers
  - `research_agent/model_factory.py`, `research_agent/auth.py`, `research_agent/db.py`, `research_agent/retry_utils.py`, and `research_agent/s3_storage.py` - model, identity, persistence, reliability, and storage adapters
  - `research_subagent/` - researcher source package; source location is not runtime ownership:
    - `prompts.py` - `RESEARCH_WORKFLOW_INSTRUCTIONS`, `RESEARCHER_INSTRUCTIONS`, and `SUBAGENT_DELEGATION_INSTRUCTIONS`
    - `tools.py` - source definitions for `tavily_search`, `fetch_webpage_content`, `think_tool`, file I/O (`ls`, `glob`, `read_file`, `write_file`), and wiki access
    - `utils/` - supporting web search, citation, filesystem, retrieval, skill, evaluation, and verification implementations consumed by application orchestration
    - delegated `research-agent` runtime receives only `tavily_search`, `fetch_webpage_content`, and `think_tool`; application graph owns file/wiki tools plus evaluation and verification flow
  - outer `__init__.py` remains lightweight; `research_subagent/__init__.py` owns the delegated researcher's public prompt and tool exports
- **`.deepagents/skills/`** - Built-in skills. Each skill has a `SKILL.md` with YAML frontmatter and instruction body, and may bundle scripts or assets.
- **`thread_wiki/`** - Thread-level document RAG without a vector database:
  - `service.py` - Wiki initialization, ingest, query, and lint operations
  - `models.py` - Pydantic models for wiki paths, progress, and query results
  - `progress.py` - Ingest phase progress from preparation through completion
  - `routes.py` - FastAPI routes for wiki operations
- **`webapp/`** - FastAPI application:
  - `config.py` - API key, version, CORS, document root, and OAuth settings
  - `routes.py` - Document upload/list/delete and research trigger routes
  - `oauth_handler.py` - OAuth/SSO session management
  - `wiki_hooks.py` - Upload lifecycle hooks that trigger wiki ingest
  - `auth_helpers.py` - Shared authentication utilities

### Infrastructure

- [research_agent/model_factory.py](research_agent/model_factory.py) creates provider-specific LangChain chat and embedding models from environment variables. It supports Azure OpenAI with managed identity, Anthropic, Google Gemini, and Ollama.
- [research_agent/db.py](research_agent/db.py) abstracts SQLite for development, PostgreSQL for production, and Cosmos DB for Azure production. It stores thread and run state.
- [research_agent/auth.py](research_agent/auth.py) authenticates Agent Protocol requests with `LANGCHAIN_API_KEY` or OAuth session tokens. `langgraph.json` registers it as the auth module.
- [research_agent/retry_utils.py](research_agent/retry_utils.py) tracks TPM/RPM quotas and applies exponential backoff to rate limits.
- [research_agent/s3_storage.py](research_agent/s3_storage.py) provides S3-compatible document persistence.

### Data Flow

1. Query enters through the `research_agent.cli` module or LangGraph Platform API served by `langgraph dev` from `langgraph.json`.
2. `ResearchStateMiddleware` in the application package injects document folder, selected skill, wiki context, and prior cited responses.
3. The application graph, built with `create_deep_agent`, delegates bounded web research to a `SubAgent` configured from `research_agent.research_subagent`; that sub-agent receives only Tavily search, page fetch, and reflection tools, while application graph retains file and wiki tools.
4. If a skill is selected, `render_skill_output` passes synthesized result to skill pipeline.
5. Output is written to `output/<thread_id>/`.

### Skills Runtime

Skills are auto-discovered from `.deepagents/skills/<skill-name>/SKILL.md` and supported custom roots such as `docs/.deepagents/skills/` by `research_agent/research_subagent/utils/skill_registry.py`. Each `SKILL.md` contains YAML frontmatter (including `name` and `description`) plus instruction body; selected instructions are injected into researcher system prompt. Skills may bundle processing scripts or other assets. Existing skills include golden-dataset, interview, frontend-slides, study-slides, autoresearch-universal, code-generator, humanizer, and find-skills.

### Testing Notes

- `tests/conftest.py` provides shared fixtures such as `mock_tavily_search` and `temp_docs_dir`.
- Mock external APIs (Tavily and model providers), not internal tools.
- `test_prompts_validation.py` validates prompt quality and structure.
- `test_research_agent_cli_e2e.py` exercises complete workflow and is slowest, most realistic suite.
- `test_research_agent_cli_helpers.py` covers CLI helpers without API calls.

### Key LangGraph Deviations

- `webapp/__init__.py` uses `importlib.util` to load submodules by file path because LangGraph `load_custom_app` loads module without parent package context.
- `research_agent/agent.py` sends wiki queries through thread-pool executors inside running event loops and uses `asyncio.run()` otherwise.
- `ResearchStateMiddleware` seeds filesystem state with research request and wiki context before agent decision step.

---

## Enhancing the Agent

### Modifying Research Behavior
1. **System Prompts**: Edit [research_agent/research_subagent/prompts.py](research_agent/research_subagent/prompts.py)
   - `RESEARCH_WORKFLOW_INSTRUCTIONS` — high-level workflow guidance
   - `RESEARCHER_INSTRUCTIONS` — tool usage, delegation, hard limits
   - `SUBAGENT_DELEGATION_INSTRUCTIONS` — parallel research strategy

2. **Tool Behavior**: Modify [research_agent/research_subagent/tools.py](research_agent/research_subagent/tools.py)
   - `tavily_search()` — web search behavior
   - `think_tool()` — reflection/strategic pausing
   - `fetch_webpage_content()` — page retrieval logic

3. **Verification Loop**: Application middleware in `research_agent/agent.py` owns post-generation evaluation and revision, using implementations from [research_agent/research_subagent/utils/verification.py](research_agent/research_subagent/utils/verification.py):
   - **Citation grounding** — reuses `citation_validator.py` to check URL reachability and claim accuracy
   - **LLM-as-judge sufficiency** — evaluates whether the report fully answers the question
   - **Adversarial gap analysis** — devil's-advocate review to find missing perspectives
   - Reports that fail verification are fed back to the model for revision (up to `MAX_VERIFICATION_ROUNDS` iterations)
   - Controlled via `ENABLE_VERIFICATION` and `MAX_VERIFICATION_ROUNDS` env vars

4. **Validation**: Tests verify your changes don't break core functionality
   ```bash
   uv run pytest tests/test_prompts_validation.py -v  # Validate prompts quality
   uv run pytest tests/test_research_agent_cli_e2e.py  # Test full workflow
   uv run pytest tests/test_verification.py -v          # Test verification loop
   uv run pytest tests/test_learning.py -v              # Test pattern learning
   ```

### Adding New Skills
1. Create directory: `.deepagents/skills/{skill-name}/` (built-in) or `docs/.deepagents/skills/{skill-name}/` (custom/uploaded).
2. Add `SKILL.md` with YAML frontmatter (`name`, `description`, and optional metadata) followed by instructions.
3. Add processing scripts or assets only when skill needs them.
4. Let [research_agent/research_subagent/utils/skill_registry.py](research_agent/research_subagent/utils/skill_registry.py) discover the skill dynamically.
5. Test via: `uv run python -m research_agent.cli "Topic" --skill {skill-name}`

### Integrating New Tools
1. Add tool function to [research_agent/research_subagent/tools.py](research_agent/research_subagent/tools.py)
2. Export delegated-research public APIs from [research_agent/research_subagent/__init__.py](research_agent/research_subagent/__init__.py); keep outer [research_agent/__init__.py](research_agent/__init__.py) lightweight
3. Document in [research_agent/research_subagent/prompts.py](research_agent/research_subagent/prompts.py) `RESEARCHER_INSTRUCTIONS`
4. Add unit tests to [tests/](tests/) (follow [tests/conftest.py](tests/conftest.py) patterns)

---

## Testing Strategy

### Test Hierarchy
```
Unit Tests (fastest)
  ↓ [test_utils.py, test_model_factory.py]
  ↓ Test individual functions, utilities
  ↓
Integration Tests (medium)
  ↓ [test_research_agent_contract.py, test_web_search.py]
  ↓ Test tool interactions, skill processing
  ↓
E2E Tests (slowest, most realistic)
  ↓ [test_research_agent_cli_e2e.py]
  ↓ Full workflow with real/mocked API calls
```

### Running Tests
```bash
# All tests
uv run pytest tests/ -v

# Specific test file
uv run pytest tests/test_research_agent_cli_e2e.py -v

# Specific test with output
uv run pytest tests/test_prompts_validation.py::TestDelegationStrategy -v -s

# Coverage report
uv run pytest tests/ --cov=research_agent --cov-report=html
```

### Test Conventions (See Root [copilot-instructions.md](../.github/copilot-instructions.md))
- **Write tests first** (TDD): Failing test → fix → verify
- **Use pytest fixtures** ([tests/conftest.py](tests/conftest.py)): `mock_tavily_search`, `temp_docs_dir`, etc.
- **No mocking internals**: Test actual tool behavior when possible; mock external APIs only
- **For bugs**: Write failing test first, then fix (Prove-It pattern)

### Golden Dataset Regression
Generate a golden dataset with the packaged CLI, then use the scoring script's supported baseline/candidate workflow:
```bash
# Generate the dataset
uv run python -m research_agent.cli "AI Safety" --skill golden-dataset

# Record a baseline
uv run python .deepagents/skills/golden-dataset/scripts/score_dataset.py \
  /path/to/golden-dataset.csv --output-dir ./output/golden-eval \
  --eval-mode baseline

# Compare a candidate
uv run python .deepagents/skills/golden-dataset/scripts/score_dataset.py \
  /path/to/golden-dataset.csv --output-dir ./output/golden-eval \
  --eval-mode candidate
```

---

## Environment & Configuration

### Required Environment Variables
```bash
# Model Provider (pick ONE)
export TAVILY_API_KEY=...                      # Web search (always required)
export OLLAMA_API_BASE=http://localhost:11434  # Local models
# OR
export ANTHROPIC_API_KEY=sk-ant-...            # Claude
# OR
export OPENAI_API_KEY=sk-...                   # GPT
# OR
export GOOGLE_API_KEY=...                      # Gemini
```

### Optional Configuration
```bash
# Rate Limiting
export MODEL_TPM=120000                        # Tokens per minute quota
export MODEL_RPM=500                           # Requests per minute quota
export GRAPH_RECURSION_LIMIT=200               # Multi-agent recursion depth
export MAX_CONCURRENT_RESEARCH_UNITS=3         # Max parallel sub-agents
export MAX_RESEARCHER_ITERATIONS=3             # Max iterations per researcher

# Persistence and server
export DB_TYPE=sqlite                           # sqlite, postgres, or cosmosdb
export UPLOAD_PORT=8000                         # Document upload API port

# Verification Loop (iterative report refinement)
export ENABLE_VERIFICATION=true                # Enable post-generation verification (default: true)
export MAX_VERIFICATION_ROUNDS=2               # Max revision iterations per report (default: 2)

# Experiment Tracking (zero-code A/B testing)
export EXPERIMENT_ID=prompt-v2                 # Optional experiment identifier
export EXPERIMENT_VARIANT=treatment            # Optional variant label (control/treatment)

# Tracing & Monitoring
export LANGCHAIN_API_KEY=...                   # LangSmith (optional)
export ENABLE_EVAL_TRACKING=true               # Evaluation tracking (default: true)
export EVAL_LOG_QUESTIONS=false                # Log user questions to eval history (default: false)

# File I/O Limits
export MAX_FILES_TO_READ=20                    # Max files in doc folder
export MAX_FILE_READ_DEPTH=3                   # Directory nesting depth
```

### Development Environment
```bash
source ./env.sh                                # Load all development vars
source ./secrets.sh                            # Load sensitive keys (not in git)
```

---

## Common Development Tasks

### Debug a Research Query
```bash
# Run with verbose output
uv run python -m research_agent.cli "Your query" -v

# Use LangSmith tracing
export LANGCHAIN_API_KEY=<key>
export LANGCHAIN_TRACING_V2=true
uv run python -m research_agent.cli "Your query"
# Then view at https://smith.langchain.com
```

### Check Model Configuration and Connectivity
```bash
uv run python -c "import asyncio, json; from webapp.model_diagnostics import run_model_diagnostics; print(json.dumps(asyncio.run(run_model_diagnostics()), indent=2))"
```

This reports detected provider and masked configuration, attempts model construction, and sends a minimal connectivity prompt. It returns structured error details instead of claiming success when configuration or connectivity fails.

### Fix SSL Certificate Errors
```bash
# For corporate environments
uv run python -m research_agent.cli "Topic" --verify_ssl False

# Or with custom CA bundle
uv run python -m research_agent.cli "Topic" --ssl-ca-files /path/to/ca-bundle.pem
```

### Run the FastAPI Upload Server
```bash
uv run python -m webapp
# Server at http://localhost:8000
# Upload docs: POST /api/upload
# Trigger research: POST /api/research with {topic, doc_folder, skill}
```

### Profile Agent Performance
```bash
# Time individual components
uv run python -m cProfile -s cumulative -m research_agent.cli "Quick Topic" | head -20
```

---

## Deployment

### Docker (Local Testing)
```bash
docker build -t deep-research:latest .
docker run --env TAVILY_API_KEY=<key> deep-research:latest \
  "Research topic" --skill golden-dataset
```

### Cloud Deployment Options
- **Azure Container Apps**: See [Azure deployment](documents/deployment/azure/README.md) for the complete walkthrough:
```bash
source ./env.sh
bash build.sh      # Build, test, push to ACR
bash deploy.sh     # Deploy to Azure Container Apps
```

- **AWS App Runner & ECR**: See [AWS deployment](documents/deployment/aws.md) for the complete walkthrough:
```bash
source ./env-aws.sh
bash build-aws.sh   # Build and push to ECR
bash deploy-aws.sh  # Deploy to AWS App Runner
```

- **Vercel UI & Serverless**: See [Vercel deployment](documents/deployment/vercel.md) for companion frontend deployment and backend connection guidance.

---

## File Organization & Naming

**Python Modules**
```
research_agent/
├── __init__.py                    # Lightweight application-package marker
├── agent.py                       # LangGraph composition and middleware
├── cli.py                         # Packaged CLI entrypoint
├── model_factory.py               # Chat and embedding model selection
├── auth.py, db.py                 # Auth and persistence composition
├── retry_utils.py, s3_storage.py  # Reliability and storage adapters
└── research_subagent/             # Researcher source implementation
    ├── __init__.py                # Researcher prompt/tool public API
    ├── prompts.py                 # System prompts and instructions
    ├── tools.py                   # Search, thinking, and file tools
    ├── clarification/             # Requirement clarification behavior
    ├── resume/                    # Incomplete-task resume behavior
    └── utils/                     # Support code; application owns runtime lifecycle

webapp/                            # Independent custom FastAPI package
thread_wiki/                       # Independent per-thread knowledge package

.deepagents/skills/                # Built-in output formatters and capabilities
├── golden-dataset/
│   ├── SKILL.md
│   └── scripts/                   # Optional processing and scoring helpers
└── interview/
    └── SKILL.md
```

**Tests**
```
tests/
├── conftest.py                    # Pytest fixtures (mock tools, temp dirs)
├── test_*.py                      # Test files (unit, integration)
├── test_prompts_validation.py     # Validates prompt quality
├── test_verification.py           # Verification loop unit + integration tests
├── test_learning.py               # Pattern learning and baseline tests
├── test_eval_tracking.py          # Metrics collection and comparison tests
└── test_research_agent_cli_e2e.py # End-to-end workflow tests
```

**Configuration Files**
```
pyproject.toml                      # Python version, dependencies, build config
.env.example                        # Template for environment variables
secrets.sh.example                  # Template for sensitive keys
env.sh                              # Development environment setup
```

---

## Code Quality & Review

### Before Committing
Follow root [copilot-instructions.md](../.github/copilot-instructions.md):
1. **Tests**: `uv run pytest tests/ -v` ✓ (all pass)
2. **Lint**: `uv run ruff check .` ✓ (if ruff available)
3. **Type checking**: `uv run mypy research_agent/` ✓ (if mypy available)
4. **No secrets**: Check for API keys, tokens in code ✓

### Code Review Axes
- **Correctness**: Does the agent produce valid research output?
- **Readability**: Are prompts, tool descriptions, and skill definitions clear?
- **Architecture**: Are responsibilities properly divided (agent vs tools vs skills)?
- **Security**: No leaked API keys; input validation on file paths?
- **Performance**: Queries complete in reasonable time; no unnecessary API calls?

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: deepagents` | Run `uv sync` to install dependencies; activate `.venv/` |
| `TAVILY_API_KEY not set` | Export before running: `export TAVILY_API_KEY=...` |
| `Model not available` | Run [model configuration and connectivity diagnostics](#check-model-configuration-and-connectivity); inspect provider detection, model construction, and test-request errors |
| `Rate limit exceeded` | Increase `MODEL_TPM` / `MODEL_RPM` or wait before retrying |
| `File path errors in tools` | Use `normalize_path_for_filesystem_tools()` in [research_agent/research_subagent/utils/knowledge_filesystem.py](research_agent/research_subagent/utils/knowledge_filesystem.py) |
| `Golden dataset baseline not recorded` | Run the golden-dataset scoring script with `--eval-mode baseline`; check its configured output directory |
| `Verification loop not triggering` | Check `ENABLE_VERIFICATION=true` and `/final_report.md` exists in state files |
| `Verification adds too much latency` | Reduce `MAX_VERIFICATION_ROUNDS` to 1; set `ENABLE_VERIFICATION=false` to disable |
| `Docker build fails on Windows` | Use WSL2; upgrade `uv` to ≥0.5.0 in Dockerfile |

---

## Key Conventions

### Prompt Enhancement
When improving `RESEARCHER_INSTRUCTIONS` or `RESEARCH_WORKFLOW_INSTRUCTIONS`:
- Document **"When to use"** for tools (e.g., when to call `think_tool()`)
- List **"Reflection should address"** for strategic pauses
- Provide concrete examples (not just abstract descriptions)
- Update [tests/test_prompts_validation.py](tests/test_prompts_validation.py) to validate new guidance

### Skill Development
New skills should:
- Have clear YAML definition (purpose, inputs, outputs)
- Include docstring explaining use case
- Return structured data (dict or Pydantic model)
- Include unit test in [tests/](tests/)

### Tool Additions
New tools should:
- Include docstring with **"When to use"** section
- Validate and normalize file paths (for safety)
- Include error handling and logging
- Be tested in isolation before integration

---

## Next Steps for AI Agents

When enhancing this agent:
1. **Read [README.md](README.md)** for architecture and quickstart
2. **Read [prompt validation](documents/development/prompt-validation.md)** for prompt validation guidelines
3. **Read [extending the agent](documents/development/extending-the-agent.md#change-orchestration-prompts)** for prompt enhancement guidelines
4. **Read [Document Upload API](documents/api/upload.md)** for upload API documentation
5. **Check [research_agent/agent.py](research_agent/agent.py)** to understand application orchestration
6. **Review [research_agent/research_subagent/prompts.py](research_agent/research_subagent/prompts.py)** for current orchestrator and delegated-research instructions
7. **Write tests first** (see [tests/conftest.py](tests/conftest.py) for fixtures)
8. **Run validation**: `uv run pytest tests/test_prompts_validation.py -v`
9. **Test end-to-end**: `uv run pytest tests/test_research_agent_cli_e2e.py -v`

---

See parent [copilot-instructions.md](../.github/copilot-instructions.md) for project-wide coding standards (TDD, code review, testing, no secrets in VCS).

<!-- threadroot:begin codex-context-optimizer -->
## Threadroot

Use Threadroot as the Codex context optimizer for this repo.

- Before broad exploration, run `threadroot prep "<task>" --memory tiny --json` or use MCP `context_budget`.
- Read the returned `firstReads` before opening unrelated files.
- Keep prompts small; prefer targeted files, compact failure summaries, and diff-focused follow-ups.
- Store local optimizer evidence only under `.codex/threadroot/`; do not create or rely on `.threadroot/`.
- After Codex changes code, run the narrowest relevant verification and inspect `threadroot score latest` when a run was recorded.

Verification commands:
- Use the narrowest existing test or check that proves the change.
<!-- threadroot:end codex-context-optimizer -->

## Context Engine (CCE)

This project uses Code Context Engine for intelligent code retrieval and
cross-session memory.

### Searching the codebase

**You MUST use `context_search` instead of reading files directly** when
exploring the codebase, answering questions about code, or understanding how
things work. This is a hard requirement, not a suggestion. `context_search`
returns the most relevant code chunks with confidence scores instead of whole
files and tracks token savings automatically.

When to use `context_search`:
- Answering questions about the codebase ("how does X work?", "where is Y?")
- Exploring structure or architecture
- Finding related code, functions, or patterns
- Any time you would otherwise read a file just to understand it

When to use `Read` instead:
- You need to edit a specific file (read before editing)
- You need the exact, complete content of a known file path

Other search tools:
- `expand_chunk` - get full source for a compressed result
- `related_context` - find what calls or imports a function

### Cross-session memory - use it actively

This project has persistent memory across sessions. **Use it both ways: recall
before answering, record after deciding.** Memory not recorded is lost; memory
not recalled does nothing.

**Before answering a non-trivial question, call `session_recall`.** Especially
when:
- Question concerns architecture, design, or naming choices
- User asks "what / why / how did we ..."
- You are about to recommend an approach team may already have chosen or rejected

Pass a topic phrase, not a single word - for example,
`session_recall("auth flow")`, not `session_recall("auth")`. Recall uses vector
similarity, so paraphrases match. If relevant entries exist, lead with them
instead of re-deriving answer.

**After making a non-obvious decision, call `record_decision`.** Especially:
- Choosing one library, pattern, or approach over another
- Resolving ambiguity in specification or requirements
- Establishing convention project should follow
- Anything you would not want to re-litigate next session

Format: `record_decision(decision="...", reason="...")`. Keep both fields short
and specific; they surface verbatim in future sessions.

**After meaningful work in a file, call `record_code_area`.** Especially when:
- You added or substantially modified a function or class
- You traced non-obvious flow and want future sessions to find it quickly

Format: `record_code_area(file_path="...", description="...")`.

Skip recording for trivial reads, formatting changes, or one-off lookups. Goal
is durable signal, not event log.

### Drilling deeper from a recall hit

`session_recall` results include source session ID, for example
`[turn sid:abc123|n:5]`. To drill in:

- `session_timeline(session_id="abc123")` - walk per-turn summaries in order;
  use when user asks what reasoning led to a decision.
- `session_event(event_id=N)` - fetch one tool event's raw input and output;
  use when turn summary references result you need to inspect.

Both are read-only and cheap. Prefer them over re-running calls or asking user
to re-paste context.

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
