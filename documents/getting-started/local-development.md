# Run Deep Research locally

Start the LangGraph development server, the standalone Document Upload server, or a companion UI from a local checkout. Complete [installation](installation.md) first so dependencies and model settings are available.

## Start the LangGraph development server

From the repository root, run:

```bash
uv run langgraph dev
```

`langgraph.json` loads `.env`, exposes the `research` assistant from `research_agent/agent.py`, and mounts the custom FastAPI application from top-level `webapp`. LangGraph opens Studio in a browser; use the deployment URL printed in the terminal and assistant ID `research` when connecting another client.

See [usage](usage.md#run-the-langgraph-server) for the request workflow.

## Keep the virtual environment outside the repository

`uv run langgraph dev` watches the project tree recursively. On Windows, changes under `.venv/Lib/site-packages` can cause continuous WatchFiles restart loops, especially when the checkout is inside OneDrive.

Use this immediate workaround:

```powershell
.\.venv\Scripts\langgraph.exe dev --no-reload --no-browser
```

For a durable fix, put the `uv` environment outside the repository and reinstall dependencies there:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\uv\venvs\deep_research"
uv sync --reinstall
& "$env:UV_PROJECT_ENVIRONMENT\Scripts\langgraph.exe" dev --no-browser
```

## Start the Document Upload server

Run the standalone FastAPI application:

```bash
uv run python -m uvicorn webapp:app --host 127.0.0.1 --port 8000
```

This command listens on loopback at `127.0.0.1:8000`. Use `--host 0.0.0.0` only for a container or intentional trusted-network exposure. Protected routes always enforce authentication: set `UPLOAD_API_KEY` in `.env` for a stable explicit key; otherwise the server uses `LANGCHAIN_API_KEY`, then generates a process-local key. Change `--port` when needed.

```dotenv
UPLOAD_API_KEY=your_generated_key_here
```

Check startup with:

```bash
curl http://localhost:8000/health
```

Follow the [Document Upload API guide](../api/upload.md) for documents upload requests, authentication, storage, and troubleshooting. The [Thread Wiki API guide](../api/wiki.md) covers wiki workflows built from uploaded content.

## Connect a companion UI

LangGraph Studio is available through `uv run langgraph dev`. You can also connect a Deep Agents UI to the deployment URL printed by the server and assistant ID `research`; follow the [Deep Agents UI connection instructions](https://github.com/langchain-ai/deep-agents-ui?tab=readme-ov-file#connecting-to-a-langgraph-server).

On Windows, add `%AppData%\npm` to `PATH` if a global Yarn install is not found. In a corporate environment, configure npm and Yarn with the organization-provided registry and authenticate before installing UI dependencies:

```bash
npm config set "bin-links" true
npm config set registry https://registry.example.com/npm/
npm login --auth-type=web
npm install -g yarn
yarn config set registry https://registry.example.com/npm/
yarn install
yarn dev
```

Replace the example registry with the URL supplied by your organization and use its required login method. For TLS interception, configure the corporate CA instead of disabling certificate checks:

```bash
npm config set cafile /path/to/ca-bundle.pem
yarn config set cafile /path/to/ca-bundle.pem
```

## Work behind a corporate TLS proxy

For research requests, prefer the corporate PEM CA bundle:

```bash
export SSL_CERT_FILE=/path/to/ca-bundle.pem
uv run python -m research_agent.cli "Research topic"
```

In PowerShell, set `$env:SSL_CERT_FILE = "C:\path\to\ca-bundle.pem"` before running the same CLI command.

For temporary local diagnosis only, SSL verification can be disabled:

```bash
uv run python -m research_agent.cli "Research topic" --verify_ssl False
```

Use `pip install uv` if the standalone `uv` installer is blocked. See [installation](installation.md#check-prerequisites) for that setup path.

## Related documentation

- [Installation](installation.md)
- [Usage](usage.md)
- [Document Upload API](../api/upload.md)
- [Handbook index](../README.md)
