# Deep Research Agent

A multi-agent research orchestration system that breaks down complex research queries into concurrent sub-agent tasks, synthesizes findings, and outputs structured research reports. Supports multiple output skills (golden dataset, interview prep, study slides, etc.) and works with local models (Ollama) or cloud APIs (Anthropic Claude, OpenAI, Google Gemini).

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
uv run python research_agent_cli.py "What is quantum computing?"

# With document context and specific skill
uv run python research_agent_cli.py "Topic" --doc-folder ./docs --skill golden-dataset

# Track evaluation baseline for regression testing
uv run python research_agent_cli.py "Topic" --skill golden-dataset --eval-golden-dataset --eval-mode baseline
```

### Interactive Development
```bash
langgraph dev                              # Run workflow visualizer at localhost:8123
uv run pytest tests/ -v                    # Run full test suite
```

---

## Architecture Overview

| Component | Purpose | Key File |
|-----------|---------|----------|
| **Orchestration** | Manages research workflow, delegates to sub-agents | [agent.py](agent.py) |
| **CLI Interface** | Standalone research execution with evaluation tracking | [research_agent_cli.py](research_agent_cli.py) |
| **Web API** | FastAPI server for document uploads; OAuth/SSO support | [webapp.py](webapp.py) |
| **Tools** | Web search (Tavily), file I/O, thinking/reflection | [research_agent/tools.py](research_agent/tools.py) |
| **Prompts** | System instructions for researcher agent | [research_agent/prompts.py](research_agent/prompts.py) |
| **Skills** | Pluggable output formatters (golden-dataset, interview-prep, etc.) | [research_agent/skills/](research_agent/skills/) |
| **Model Config** | Multi-provider model abstraction | [model_factory.py](model_factory.py) |
| **Tests** | 20+ test files (unit, integration, E2E) | [tests/](tests/) |

---

## Enhancing the Agent

### Modifying Research Behavior
1. **System Prompts**: Edit [research_agent/prompts.py](research_agent/prompts.py)
   - `RESEARCH_WORKFLOW_INSTRUCTIONS` — high-level workflow guidance
   - `RESEARCHER_INSTRUCTIONS` — tool usage, delegation, hard limits
   - `SUBAGENT_DELEGATION_INSTRUCTIONS` — parallel research strategy

2. **Tool Behavior**: Modify [research_agent/tools.py](research_agent/tools.py)
   - `tavily_search()` — web search behavior
   - `think_tool()` — reflection/strategic pausing
   - `fetch_webpage_content()` — page retrieval logic

3. **Verification Loop**: The post-generation verification system ([research_agent/utils/verification.py](research_agent/utils/verification.py)) enables iterative report refinement:
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
1. Create directory: `research_agent/skills/{skill-name}/`
2. Add YAML definition: `skill.yaml` (see [research_agent/skills/golden_dataset/skill.yaml](research_agent/skills/golden_dataset/skill.yaml) for template)
3. Implement processor: `processor.py` with `process_research_output()` function
4. Register in agent: [research_agent/__init__.py](research_agent/__init__.py) exports it
5. Test via: `uv run python research_agent_cli.py "Topic" --skill {skill-name}`

### Integrating New Tools
1. Add tool function to [research_agent/tools.py](research_agent/tools.py)
2. Export from [research_agent/__init__.py](research_agent/__init__.py)
3. Document in [research_agent/prompts.py](research_agent/prompts.py) `RESEARCHER_INSTRUCTIONS`
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
Track prompt improvements with automated regression testing:
```bash
# Baseline run (first time)
uv run python research_agent_cli.py "AI Safety" --skill golden-dataset \
  --eval-golden-dataset --eval-mode baseline

# Regression check (after changes)
uv run python research_agent_cli.py "AI Safety" --skill golden-dataset \
  --eval-golden-dataset --eval-mode baseline

# View evaluation history
cat output/eval_history/server_runs.jsonl | tail -5
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
uv run python research_agent_cli.py "Your query" -v

# Use LangSmith tracing
export LANGCHAIN_API_KEY=<key>
export LANGCHAIN_TRACING_V2=true
uv run python research_agent_cli.py "Your query"
# Then view at https://smith.langchain.com
```

### Check Model Availability
```bash
uv run python model_factory.py
# Lists: Ollama models, API key status, available providers
```

### Fix SSL Certificate Errors
```bash
# For corporate environments
uv run python research_agent_cli.py "Topic" --verify_ssl False

# Or with custom CA bundle
uv run python research_agent_cli.py "Topic" --ssl-ca-files /path/to/ca-bundle.pem
```

### Run the FastAPI Upload Server
```bash
uv run python webapp.py
# Server at http://localhost:8000
# Upload docs: POST /api/upload
# Trigger research: POST /api/research with {topic, doc_folder, skill}
```

### Profile Agent Performance
```bash
# Time individual components
uv run python -m cProfile -s cumulative research_agent_cli.py "Quick Topic" | head -20

# Check memory usage
uv run python -m memory_profiler research_agent_cli.py "Topic"
```

---

## Deployment

### Docker (Local Testing)
```bash
docker build -t deep-research:latest .
docker run --env TAVILY_API_KEY=<key> deep-research:latest \
  "Research topic" --skill golden-dataset
```

### Azure Container Apps (Production)
See [AZURE_DEPLOY.md](document/AZURE_DEPLOY.md) for complete walkthrough:
```bash
source ./env.sh
bash build.sh      # Build, test, push to ACR
bash deploy.sh     # Deploy to Azure Container Apps
```

---

## File Organization & Naming

**Python Modules**
```
research_agent/
├── __init__.py                    # Public API (tools, skills exports)
├── prompts.py                     # System prompts & instructions
├── tools.py                       # Tool implementations (search, thinking, file I/O)
├── skills/                        # Output formatters
│   ├── golden_dataset/
│   │   ├── skill.yaml
│   │   └── processor.py
│   └── interview_prep/
│       ├── skill.yaml
│       └── processor.py
└── utils/                         # Utilities
    ├── cli.py                     # CLI helpers
    ├── citation_validator.py      # URL reachability + claim grounding
    ├── content_extractors.py      # PDF/DOCX/PPTX/XLSX text extraction
    ├── eval_tracking.py           # Metrics collection, baseline comparison
    ├── json_utils.py              # Robust JSON parsing with repair
    ├── knowledge_filesystem.py    # File I/O with safety limits
    ├── learning.py                # Trend analysis from eval history
    ├── skill_registry.py          # Dynamic skill discovery
    ├── text_search.py             # Hybrid BM25+FAISS search index
    ├── verification.py            # Post-generation adversarial verification
    └── web_search.py              # Tavily search + webpage fetching
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
| `Model not available` | Check `uv run python model_factory.py`; ensure Ollama running or API key valid |
| `Rate limit exceeded` | Increase `MODEL_TPM` / `MODEL_RPM` or wait before retrying |
| `File path errors in tools` | Use `normalize_path_for_filesystem_tools()` helper (in [research_agent/utils/](research_agent/utils/)) |
| `Golden dataset not recorded` | Ensure `--eval-golden-dataset --eval-mode baseline` flags; check `output/eval_history/` |
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
2. **Read [TEST_PROMPTS_VALIDATION_GUIDE.md](document/TEST_PROMPTS_VALIDATION_GUIDE.md)** for prompt validation guidelines
3. **Read [PROMPT_ENHANCEMENT_GUIDE.md](document/PROMPT_ENHANCEMENT_GUIDE.md)** for prompt enhancement guidelines
4. **Read [UPGRADE_GUIDE.md](document/UPGRADE_GUIDE.md)** for upload API documentation
5. **Check [agent.py](agent.py)** to understand orchestration logic
6. **Review [research_agent/prompts.py](research_agent/prompts.py)** for current instructions
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

**Use `context_search` instead of reading files directly** when exploring
the codebase, answering questions about code, or understanding how things
work. `context_search` returns the most relevant code chunks with
confidence scores instead of whole files.

When to use `context_search`:
- Answering questions about the codebase ("how does X work?", "where is Y?")
- Exploring structure or architecture
- Finding related code, functions, or patterns

Other tools:
- `expand_chunk` for full source of a compressed result
- `related_context` for what calls/imports a function
- `session_recall` to recall past decisions

### Cross-session memory

Call `session_recall("topic phrase")` before answering non-trivial questions.
Call `record_decision(decision="...", reason="...")` after making choices.
Call `record_code_area(file_path="...", description="...")` after meaningful work.

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
