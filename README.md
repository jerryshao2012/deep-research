# Deep Research

Deep Research is a multi-agent research orchestration system built on LangGraph and `deepagents`. It decomposes complex questions into concurrent research tasks, combines web and document context, verifies the result, and renders structured reports through local or cloud models.

## Features

- Plans complex research and delegates bounded work to concurrent sub-agents.
- Searches the web through Tavily and preserves source links in synthesized reports.
- Reads local documents for grounded research without requiring web search.
- Builds per-thread knowledge bases through Thread Wiki document and code ingestion.
- Supports command-line, LangGraph, and Document Upload API workflows.
- Selects local Ollama or cloud models through one configuration layer.
- Produces specialized outputs through pluggable skills such as golden datasets, interview kits, code, and study slides.
- Shapes model traffic, retries transient rate limits, and bounds agent recursion.
- Tracks evaluation metrics and can verify generated reports before completion.
- Supports API keys, OAuth, passkeys, and durable production session stores.
- Includes deployment workflows for Azure Container Apps and AWS App Runner.

## Quick start

Prerequisites: Python 3.12 or 3.13, [`uv`](https://docs.astral.sh/uv/), a supported model provider, and a Tavily API key for web-enabled research.

Install dependencies from the repository root:

```bash
uv sync
```

Configure one model provider. For local Ollama:

```bash
export OLLAMA_API_BASE=http://localhost:11434
export MODEL_NAME=glm-4.7-flash:latest
export TAVILY_API_KEY=your_tavily_api_key_here
```

For Anthropic instead:

```bash
export ANTHROPIC_API_KEY=your_anthropic_api_key_here
export MODEL_NAME=claude-sonnet-4-5-20250929
export TAVILY_API_KEY=your_tavily_api_key_here
```

Keep real credentials in an ignored `.env` file or a secret manager. Run a first query:

```bash
uv run python -m research_agent.cli "What is quantum computing?"
```

For a local-model run without Tavily:

```bash
uv run python -m research_agent.cli \
  "Summarize the purpose of deep research" --no-web
```

Generated reports are written beneath `output/`. See [installation](documents/getting-started/installation.md) for provider setup and [configuration](documents/guides/configuration.md) for the complete environment reference.

## Usage modes

Run a document-backed CLI task:

```bash
uv run python -m research_agent.cli \
  "Summarize the supplied material" \
  --doc-folder /path/to/documents
```

Apply an installed output skill:

```bash
uv run python -m research_agent.cli \
  "Generate grounded question-answer pairs" \
  --doc-folder /path/to/documents \
  --skill golden-dataset
```

List current skill IDs:

```bash
uv run python -m research_agent.cli --skill list
```

For browser-based skill selection and execution, see [Use skills in the UI](documents/guides/skills.md).

Start the configured `research` graph and LangGraph Studio:

```bash
uv run langgraph dev
```

For a standalone documents upload service, start Uvicorn on loopback:

```bash
uv run python -m uvicorn webapp:app \
  --host 127.0.0.1 --port 8000
```

Set `UPLOAD_API_KEY` before relying on a stable API credential. Use `--host 0.0.0.0` only for intentional container or trusted-network exposure.

Choose the interface that matches the workflow:

| Mode | Best for | Detailed guide |
| --- | --- | --- |
| CLI | Automated research, local files, and output skills | [Usage](documents/getting-started/usage.md) |
| LangGraph | Interactive development and compatible clients | [Local development](documents/getting-started/local-development.md) |
| Document Upload API | Document staging and file-management integrations | [API reference](documents/api/upload.md) |
| Thread Wiki API | Per-thread document or repository knowledge bases | [Wiki reference](documents/api/wiki.md) |

## Architecture

`research_agent/agent.py` defines the research graph and state middleware. The application graph plans work, assigns filesystem and wiki tools, delegates bounded web research to a web-only sub-agent, synthesizes a report, and owns evaluation and revision.

| Area | Ownership |
| --- | --- |
| `research_agent/` | Application package: graph composition, runtime tool assignment, evaluation/verification flow, CLI, models, authentication, persistence, and reliability |
| `research_agent/research_subagent/` | Source package for research prompts, tool definitions, and supporting utilities; delegated runtime receives only Tavily search, page fetch, and reflection |
| `thread_wiki/` | Thread-scoped ingestion, knowledge generation, progress, and queries |
| `webapp/` | Document Upload API, wiki routes, OAuth, sessions, and CORS |
| `.deepagents/skills/` | Built-in output-skill definitions and supporting assets |

Start with the [architecture overview](documents/architecture/overview.md). Boundary rules live in [Clean Architecture](documents/architecture/clean-architecture.md), and AST-aware repository processing is covered by [code ingestion](documents/architecture/code-ingestion.md).

## Development

Install the locked environment before running checks:

```bash
uv sync
```

Run the full test suite:

```bash
uv run pytest tests/ -q
```

Run focused documentation and prompt contracts:

```bash
uv run pytest tests/test_documentation.py -q
uv run pytest tests/test_prompts_validation.py -q
```

Install the optional development tools before linting or type-checking:

```bash
uv sync --extra dev
```

Check lint and types when changing Python:

```bash
uv run ruff check .
uv run mypy research_agent/
```

See [testing](documents/development/testing.md) for test layers and verification commands. See [extending the agent](documents/development/extending-the-agent.md) before changing prompts, tools, model providers, delegation, or output skills; prompt-specific checks are in [prompt validation](documents/development/prompt-validation.md).

## Documentation

The [Deep Research handbook](documents/README.md) is the canonical navigation for user, developer, and operator documentation.

- [Getting started](documents/getting-started/installation.md) covers installation and first-run model setup.
- [Usage](documents/getting-started/usage.md) covers CLI, LangGraph, and API workflows.
- [Configuration](documents/guides/configuration.md) lists supported settings and runtime defaults.
- [Authentication](documents/guides/authentication.md) covers API keys, OAuth, passkeys, and production hardening.
- [Reliability](documents/guides/reliability.md) covers rate shaping, retries, and failure recovery.
- [Evaluation](documents/guides/evaluation.md) covers baselines, operational metrics, experiments, and report verification.
- [API reference](documents/api/upload.md) documents the Document Upload API's staging and file-management endpoints.
- [Architecture](documents/architecture/overview.md) maps components, ownership, and data flow.

Historical plans and specifications are retained under `documents/history/`; use them for design context, not current operating instructions.

## Deployment

This repository owns the Python backend. Review deployment-specific prerequisites, secret handling, storage, and health checks before running mutable cloud scripts.

- [Azure Container Apps](documents/deployment/azure/README.md) covers backend deployment, with separate pages for [storage](documents/deployment/azure/storage.md), [operations](documents/deployment/azure/operations.md), [security](documents/deployment/azure/security.md), and [troubleshooting](documents/deployment/azure/troubleshooting.md).
- [AWS App Runner](documents/deployment/aws.md) covers the backend container, ECR, persistence, and operational workflow.
- [Vercel](documents/deployment/vercel.md) covers a separately maintained companion frontend. Vercel does not deploy this repository's LangGraph backend; follow the selected frontend repository for its build and client configuration.

Docker can be used for local image validation, but cloud scripts contain repository-specific defaults. Inspect subscription, region, resource names, registry targets, and secret sources before deployment.

## Security

- Never commit API keys, OAuth secrets, cloud credentials, tokens, or populated secret files.
- Keep TLS verification enabled and use an organization-provided CA bundle when traffic is intercepted.
- Bind local services to loopback unless network exposure is deliberate and protected.
- Use stable secret-manager values for production API keys and session signing.
- Configure exact frontend origins and OAuth redirect URLs; do not use wildcard production policies.
- Store durable authentication and application state in the deployment's supported persistence layer.
- Review [authentication](documents/guides/authentication.md) and the relevant deployment security guide before exposing either service.

Report suspected vulnerabilities privately to the repository maintainers rather than publishing credentials or exploit details in an issue.
