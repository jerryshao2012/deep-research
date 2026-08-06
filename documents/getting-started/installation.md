# Install and configure Deep Research

Set up the Python environment and one model provider, then run a first research task. This page keeps first-run settings minimal; see the [configuration guide](../guides/configuration.md) for the complete environment reference.

## Check prerequisites

You need:

- Git and a repository checkout;
- Python 3.12 or 3.13;
- the `uv` package manager;
- access to either a local Ollama service or a supported cloud model;
- a Tavily API key for web-enabled research.

Install `uv` on macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

In a restricted corporate environment, install it through Python instead:

```bash
pip install uv
```

## Install project dependencies

From the repository checkout:

```bash
cd /path/to/deep_research
uv sync
```

On Windows, if `uv` is not on `PATH`, run:

```powershell
python -m uv sync
```

If an existing environment has inconsistent packages, rebuild its installed packages:

```bash
uv sync --reinstall
```

## Choose a model provider

Configure one provider for the first run. `MODEL_NAME` must name a model available through that provider.

### Use Ollama locally

Start Ollama in one terminal if it is not already running:

```bash
ollama serve
```

In another terminal, download the same model named by `MODEL_NAME`:

```bash
ollama pull glm-4.7-flash:latest
```

Configure the agent to use that local service and model:

```bash
export OLLAMA_API_BASE=http://localhost:11434
export MODEL_NAME=glm-4.7-flash:latest
export TAVILY_API_KEY=your_tavily_api_key_here
```

Omit `TAVILY_API_KEY` only when you plan to run with `--no-web`.

### Use Anthropic

```bash
export ANTHROPIC_API_KEY=your_anthropic_api_key_here
export MODEL_NAME=claude-sonnet-4-5-20250929
export TAVILY_API_KEY=your_tavily_api_key_here
```

### Use Google Gemini

```bash
export GOOGLE_API_KEY=your_google_api_key_here
export MODEL_NAME=gemini-2.5-pro
export TAVILY_API_KEY=your_tavily_api_key_here
```

The configuration guide also covers other supported providers, optional tracing, reliability controls, and output limits.

## Load settings from an environment file

Shell exports apply only to the current shell. To reuse settings, create `.env` in the repository root and add only the variables for your selected provider plus `TAVILY_API_KEY` when web search is enabled:

```dotenv
OLLAMA_API_BASE=http://localhost:11434
MODEL_NAME=glm-4.7-flash:latest
TAVILY_API_KEY=your_tavily_api_key_here
```

The CLI, agent, and Document Upload server load the root `.env` file automatically. Keep real keys out of version control.

## Verify the installation

Run a small web-enabled query:

```bash
uv run python -m research_agent.cli "What is quantum computing?"
```

Or verify a local-model setup without Tavily:

```bash
uv run python -m research_agent.cli "Summarize the purpose of deep research" --no-web
```

A successful run prints its thread ID and writes the generated report beneath `output/`. Continue with the [usage guide](usage.md) for document-backed tasks, output skills, and server modes.

## Related documentation

- [Usage](usage.md)
- [Local development](local-development.md)
- [Handbook index](../README.md)
