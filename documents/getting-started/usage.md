# Use Deep Research

Run research through the command-line interface or the LangGraph server. Use the Document Upload API to stage and manage material that CLI, LangGraph, or Thread Wiki workflows can subsequently consume.

## Run a command-line research task

Pass a subject directly:

```bash
uv run python -m research_agent.cli "Research AI agents"
```

The CLI prints the run's thread ID and saves its report beneath `output/`.

### CLI options

| Option | Purpose |
| --- | --- |
| `subject` | Research subject. Optional when `--subject-file` supplies it. |
| `--subject-file PATH` | Read the subject from a file. |
| `--verify_ssl [VALUE]` | Enable or disable HTTPS certificate verification; defaults to `True`. |
| `--ssl-ca-files PATH` | Parsed but not currently applied by the TLS resolver; use `SSL_CERT_FILE` instead. |
| `--verbose [VALUE]` | Show progress; defaults to `True`. Set `False` for no progress display. |
| `--doc-folder PATH` | Use supported documents in a folder as research material. |
| `--no-web` | Disable Tavily web search. |
| `--skill ID` | Apply a structured output skill; use `--skill list` to inspect current IDs. |
| `--title TITLE` | Set the research title used for output naming. |
| `--thread-id ID` | Reuse a thread ID for state tracking; otherwise one is generated. |
| `--help`, `-h` | Print CLI help. |

### Use local documents and an output skill

Generate study slides from a document folder:

```bash
uv run python -m research_agent.cli "Research AI agents" --doc-folder /path/to/documents --skill study-slides
```

Generate an interview question kit:

```bash
uv run python -m research_agent.cli "Research AI agents" --doc-folder /path/to/documents --skill interview
```

Prepare a longer interview with questions and answers:

```bash
uv run python -m research_agent.cli "Prepare a 60-minute interview with questions and answers" --doc-folder /path/to/interview_material --skill interview-coach-pro
```

Generate a golden dataset grounded in supplied documents:

```bash
uv run python -m research_agent.cli "Generate 20 question-answer pairs from the provided documents" --doc-folder /path/to/policy_documents --skill golden-dataset
```

### Understand document-grounded research

`--doc-folder` keeps local evidence with the application orchestrator. When a ready Thread Wiki exists, the orchestrator can query it for cited findings; otherwise, or when a query is incomplete, application-owned file tools read a bounded set of supported documents. The delegated `research-agent` is intentionally web-only and cannot open local files, so relevant local excerpts are included in its task prompt when outside research is needed.

For a document-grounded task, treat supplied files as source of truth for claims about their contents. With web search enabled, the orchestrator may delegate targeted searches to fill explicit gaps, check current external facts, or add context; those web findings do not silently replace local evidence. Use `--no-web` when every claim must come from supplied material.

The `golden-dataset` skill maps generated questions and ideal answers back to localized document evidence. Use a focused subject such as the desired item count and domain, then inspect generated sources and metrics rather than assuming that a completed run is fully grounded. Large folders are subject to configured file-count, depth, inline-size, and extraction limits; narrow the folder or split the run when previews indicate material was omitted.

Generate code from a subject file:

```bash
uv run python -m research_agent.cli --subject-file /path/to/coding-task.txt --skill code-generator
```

For browser-based skill selection, see [Use skills in the UI](../guides/skills.md).

### Control context and state

Use only local documents, with no web search:

```bash
uv run python -m research_agent.cli "Research AI agents" --doc-folder /path/to/documents --no-web
```

Read the subject from a file and reuse a known thread:

```bash
uv run python -m research_agent.cli --subject-file /path/to/research-subject.txt --doc-folder /path/to/documents --thread-id my-research-thread
```

List the currently installed structured output skills:

```bash
uv run python -m research_agent.cli --skill list
```

For notebook-based exploration, run:

```bash
uv run jupyter notebook research_agent.ipynb
```

## Run the LangGraph server

Start the configured `research` graph and custom FastAPI application:

```bash
uv run langgraph dev
```

Submit a query through LangGraph Studio, or connect a compatible client using the deployment URL printed in the terminal and assistant ID `research`. For Windows restart-loop fixes, external virtual environments, and UI setup, see [local development](local-development.md#start-the-langgraph-development-server).

## Use the Document Upload API

Start the standalone documents upload service:

```bash
uv run python -m uvicorn webapp:app --host 127.0.0.1 --port 8000
```

This command serves the API on loopback at `http://localhost:8000`; use `--host 0.0.0.0` only for a container or intentional trusted-network exposure. Protected routes always enforce authentication: set `UPLOAD_API_KEY` for a stable explicit key; otherwise the server uses `LANGCHAIN_API_KEY`, then generates a process-local key. Follow the [Document Upload API guide](../api/upload.md) for request examples and the endpoint reference.

After staging documents, point `--doc-folder` at their folder for a CLI task, or consume them through a supported LangGraph workflow. Uploads under a thread folder can also feed a per-thread knowledge base; see the [Thread Wiki API guide](../api/wiki.md) for ingestion, query, and repository-import workflows.

## Related documentation

- [Installation](installation.md)
- [Local development](local-development.md)
- [Use skills in the UI](../guides/skills.md)
- [Document Upload API](../api/upload.md)
- [Thread Wiki API](../api/wiki.md)
- [Handbook index](../README.md)
