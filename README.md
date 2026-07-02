# 🚀 Deep Research

## Contents

- [🚀 Quickstart](#-quickstart)
- [Usage Options](#usage-options)
- [🔒 Security & Authentication](#-security--authentication)
- [🔑 OAuth Authentication](#-oauth-authentication)
- [🧩 Deep Research Agent Components](#-deep-research-agent-components)
- [📚 Resources](#-resources)
- [🛡️ Reliability & Rate Limiting](#-reliability--rate-limiting)
- [🛡️ Rate Limit Handling](#-rate-limit-handling)
- [Multi-Agent Complex Workflows Evaluation & Regression Tracking](#multi-agent-complex-workflows-evaluation--regression-tracking)
- [📖 Thread Wiki (Document RAG)](#-thread-wiki-document-rag)

## 🚀 Quickstart

**Prerequisites**: Install [uv](https://docs.astral.sh/uv/) package manager:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

*Alternative installation for restricted corporate environments:*
```bash
pip install uv
```

Ensure you are in the `deep_research` directory:

```bash
cd deep_research
```

Install packages:

```bash
uv sync
```

* If `uv` is not available on your system path, you can try:
```bash
# In Windows if PATH is not setup properly
python -m uv sync
```
Note: use `uv sync --reinstall` to reinstall all packages if you see some errors.

Set your API keys in your environment:

```bash
# Option 1: Using Ollama (LOCAL - FREE)
export OLLAMA_API_BASE=http://localhost:11434
export MODEL_NAME=glm-4.7-flash:latest                    # or qwen3.5:latest, deepseek-r1:14b, etc.
export TAVILY_API_KEY=your_tavily_api_key_here            # ✅ Required for web search

# Option 2: Using Cloud APIs
export ANTHROPIC_API_KEY=your_anthropic_api_key_here      # For Claude model
export GOOGLE_API_KEY=your_google_api_key_here            # For Gemini model ([get one here](https://ai.google.dev/gemini-api/docs))
export TAVILY_API_KEY=your_tavily_api_key_here            # Required for web search ([get one here](https://www.tavily.com/)) with a generous free tier
export LANGCHAIN_TRACING_V2=true                          # Enable LangSmith tracing
export LANGSMITH_ENDPOINT=https://api.smith.langchain.com # LangSmith endpoint
export LANGCHAIN_API_KEY=your_langsmith_api_key_here      # [LangSmith API key](https://smith.langchain.com/settings) (free to sign up)
export LANGCHAIN_PROJECT=deep-research-deepagents         # The project name to log traces to

# Research Agent Configuration
# Maximum number of concurrent research units (sub-agents) that can run simultaneously
export MAX_CONCURRENT_RESEARCH_UNITS=3
# Maximum number of iterations per researcher agent before stopping
export MAX_RESEARCHER_ITERATIONS=3

# Wiki Agent Timeouts
export WIKI_AGENT_TIMEOUT_SECONDS=600
export WIKI_INGEST_PHASE_TIMEOUT_SECONDS=300
export WIKI_INDEX_REPAIR_TIMEOUT_SECONDS=180
export WIKI_QUERY_TIMEOUT_SECONDS=120
export WIKI_LINT_TIMEOUT_SECONDS=60
export WIKI_INGEST_MAX_WAIT_SECONDS=600

# Evaluation Tracking (for langgraph dev server)
# Enable automatic metrics logging during development
export ENABLE_EVAL_TRACKING=true
# Path to JSONL file for storing operational metrics
export EVAL_HISTORY_FILE=./output/eval_history/server_runs.jsonl

# =============================================================================
# RELIABILITY & RATE LIMITING
# =============================================================================

# Proactive Rate Shaping (TPM and RPM limits)
# Set these based on your provider's deployment quotas
# Tokens Per Minute:
# Represents the maximum number of tokens (input + output) you are allowed to send to the model provider within a
# rolling 60-second window.
export MODEL_TPM=120000
# Requests Per Minute:
# Represents the maximum number of individual API calls you can make per minute.
export MODEL_RPM=500

# Graph Recursion Limit
# Controls the maximum depth of recursive graph execution in LangGraph
# Default LangGraph recursion limit is 100, but deep research workflows often require more iterations
# Increase this value for complex multi-agent research tasks that involve many sub-agent delegations
export GRAPH_RECURSION_LIMIT=200

# Filesystem and Output Configuration
# Maximum directory depth for glob pattern matching
export MAX_GLOB_DEPTH=3
# Default output folder for generated reports and documents
export REPORTS_OUTPUT_FOLDER=./output
# Maximum number of files to read in a single operation
export MAX_FILES_TO_READ=20
# Maximum total size in MB for batch file reading operations
export MAX_TOTAL_SIZE_MB=50
```

---

## Usage Options

You can run this example in three ways:

### Option 1: Command Line Script

Run the standalone Python script to execute the research agent:

```bash
uv run python research_agent_cli.py "Research AI Agents"
```

#### Command Line Options

The CLI supports the following options:

```
positional arguments:
  subject               Research subject. If omitted, a subject file may be used instead.

optional arguments:
  --subject-file SUBJECT_FILE
                        Optional file path to read the research subject from
  --verify_ssl [VERIFY_SSL]
                        Verify SSL certificates (default: True). Set to False to skip SSL verification
  --ssl-ca-files SSL_CA_FILES
                        Path to a PEM CA bundle to use for HTTPS verification
  --verbose [VERBOSE]   Show progress (default: True). When False, runs agent without progress display
  --help, -h            Show this help message and exit
  --doc-folder DOC_FOLDER
                        Optional folder containing supported documents to use as research material
  --no-web              Disable web search (Tavily) during research
  --skill {list,<available-skill-ids>}
                        Optional structured output skill. Use '--skill list' to see all available skills.
  --title TITLE         Optional research title for output file naming
  --eval-golden-dataset Enable golden-dataset regression tracking and JSONL report output
  --eval-mode {baseline,candidate}
                        Evaluation mode for --eval-golden-dataset (default: candidate)
  --eval-history-file EVAL_HISTORY_FILE
                        Optional JSONL output file path for evaluation history.
                        Default: ./output/eval_history/golden_dataset_runs.jsonl
```

#### Examples

Basic research task:
```bash
uv run python research_agent_cli.py "Research AI Agents"
```

With document folder and structured output:
```bash
uv run python research_agent_cli.py "Research AI Agents" --doc-folder ./docs --skill study-slides
```

Generate an interview question kit:
```bash
uv run python research_agent_cli.py "Research AI Agents" --doc-folder ./docs --skill interview
```

Prepare a comprehensive interview with questions and answers:
```bash
uv run python research_agent_cli.py "Preparing a 60 minutes interview with list of question and answer" --doc-folder ./docs/interview_prep --skill interview-coach-pro
```

Generate a golden dataset:
```bash
uv run python research_agent_cli.py "Generate 20 question-answer pairs for the documents provided" --doc-folder ./docs/policy/ --skill golden-dataset
```

Create a baseline evaluation entry for a fixed golden-dataset test case:
```bash
uv run python research_agent_cli.py "Generate 5 question-answer pairs for the documents provided" --doc-folder ./docs/policy/ --skill golden-dataset --eval-golden-dataset --eval-mode baseline
```

Run a candidate evaluation and compare against the latest baseline with the same test input:
```bash
uv run python research_agent_cli.py "Generate 5 question-answer pairs for the documents provided" --doc-folder ./docs/policy/ --skill golden-dataset --eval-golden-dataset --eval-mode candidate
```

Write JSONL history to a custom location:
```bash
uv run python research_agent_cli.py "Generate 5 question-answer pairs for the documents provided" --doc-folder ./docs/policy/ --skill golden-dataset --eval-golden-dataset --eval-history-file ./output/eval_history/my_runs.jsonl
```

> Important: comparisons are only performed when the test case is exactly the same (same subject text, skill, doc-folder, model, and flags). Inputs like "Generate 5 pairs..." and "Generate 10 pairs..." are intentionally treated as non-comparable.

<img width="937" alt="Deep Research Agent Architecture" src="./resources/Deep_Research_Agent_with_Golden_Dataset_Generation_Skill.png" />

1.  **Context Injection**: A curated set of "Source-of-Truth" documents (PDFs, Markdown, or technical specs) is provided to the agent's filesystem, populating the 'Local Context (doc folder)'.

2.  **Synthesis-First, Prioritized Context Strategy**: The agent is instructed to prioritize the `[read_doc_folder]` tool for Knowledge Retrieval from the local filesystem as its primary source. The research logic, orchestrated by the harness and monitored by the `think_tool`, follows a clear priority strategy to always exhaust internal knowledge first.

3.  **Golden Dataset Skill (The Auditor)**:
    * **Question Generation**: It analyzes the provided (and potentially augmented) documents to identify key technical facts, contradictions, or complex logic.
    * **Context Pinpointing**: It maps the generated question directly to the specific localized context (e.g., paragraph or page in the local document).
    * **Answer Extraction**: It generates the "Ideal Answer" based strictly on that pinpointed source context.

4.  **Tavily as a Prioritized Fallback (Not Direct Tool Call)**: The system possesses a distinct, parallel `[tavily_search]` tool for external web search. Within the Context Strategy, it is treated as a prioritized fallback, meaning it is only triggered conceptually as a second priority if the provided local documents are explicitly incomplete. It is executed independently by the sub-agents and is generally flagged as "out-of-distribution" for the golden set, which is intended to be grounded in the internal dataset.

Generate code using code-generator skill:
```bash
uv run python research_agent_cli.py --subject-file ./input/coding-create-a-image.txt --skill code-generator
```

Without web search (using only local documents):
```bash
uv run python research_agent_cli.py "Research AI Agents" --doc-folder ./docs --no-web
```

Read subject from a file:
```bash
uv run python research_agent_cli.py --subject-file ./input/interview-subject.txt --doc-folder ./docs
```

Show available structured output skills:
```bash
uv run python research_agent_cli.py --skill list
```

Structured skills are skill-driven. Add a new `research_agent/skills/<skill>/SKILL.md`
with frontmatter, instructions, a JSON Schema block, and a render template to make a new
skill available through `--skill` without changing core CLI or tool wiring.

If you prefer an interactive notebook, you can still run it via:
```bash
uv run jupyter notebook research_agent.ipynb
```

### Option 2: LangGraph Server

Run a local [LangGraph server](https://langchain-ai.github.io/langgraph/tutorials/langgraph-platform/local-server/) with a web interface:

```bash
langgraph dev
```

If you hit continuous restart loops with messages like `WatchFiles detected changes in .venv/Lib/site-packages/...`, use this immediate workaround:

```powershell
.\.venv\Scripts\langgraph.exe dev --no-reload --no-browser
```

Why this happens:
- `langgraph dev` watches the project tree recursively.
- When dependencies are being updated (or files under `.venv` are touched by sync/indexing tools), WatchFiles sees `.venv` changes and repeatedly reloads.

Recommended durable fix on Windows:
- Move the virtual environment outside the project folder (especially if the repo is inside OneDrive).
- Recreate dependencies there, then run `langgraph dev` again.

Example:

```powershell
# Pick a venv location outside the repo
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\uv\venvs\deep_research"
uv sync --reinstall

# Then start normally
& "$env:UV_PROJECT_ENVIRONMENT\Scripts\langgraph.exe" dev --no-browser
```

LangGraph server will open a new browser window with the Studio interface, which you can submit your search query to:

<img width="1915" alt="Screenshot 2026-04-03 at 10 27 11 AM" src="./resources/Screenshot 2026-04-03 at 10 27 11 AM.png" />

You can also connect the LangGraph server to a [UI specifically designed for deepagents](https://github.com/jerryshao2012/bmo-deepagent-ui.git):

```bash
git clone https://github.com/jerryshao2012/bmo-deepagent-ui.git
cd bmo-deep-agents-ui

# Update a remote branch with rewritten history
# git reset --hard <commit-hash>
# git push --force-with-lease 

# Install yarn
npm config set "bin-links" true
npm config set "strict-ssl" false
npm config set registry https://bmostaging.jfrog.io/artifactory/api/npm/bmoai-npm-virtual/
# Choose SAML SSO
npm login --auth-type=web
npm install -g yarn
# Add %AppData%\npm to PATH for Windows

# For corporation network
yarn config set "strict-ssl" false
# Get configuration from npm config list
yarn config set registry https://bmostaging.jfrog.io/artifactory/api/npm/bmoai-npm-virtual/

yarn install
yarn dev
```

Then follow the instructions in the [deep-agents-ui README](https://github.com/langchain-ai/deep-agents-ui?tab=readme-ov-file#connecting-to-a-langgraph-server) to connect the UI to the running LangGraph server. Get the Deployment URL and Assistant ID from the terminal output and langgraph.json file, respectively:

- **Deployment URL**: http://127.0.1:2024
- **Assistant ID**: research

**Open Deep Agents UI** at [http://localhost:3000](http://localhost:3000) and input the Deployment URL and Assistant ID:

- **Deployment URL**: The URL for the LangGraph deployment you are connecting to
- **Assistant ID**: The ID of the assistant or agent you want to use
- [Optional] **LangSmith API Key**: Your LangSmith API key (format: `lsv2_pt_...`). This may be required for accessing deployed LangGraph applications. You can also provide this via the `NEXT_PUBLIC_LANGSMITH_API_KEY` environment variable.

This provides a user-friendly chat interface and visualization of files in state.

<img width="1917" alt="Screenshot 2026-04-03 at 12:44 11 PM" src="resources/Screenshot 2026-04-03 at 12 44 11 PM.png" />

Example:
```text
Generate 20 pairs of questions and answers using the `golden-dataset` skill for the documents provided in this folder '.\docs\policy\'.
```
```text
Give me an overview of AI Evaluation through Harness Engineering
```

### Option 3: Document Upload API

The project includes a FastAPI-based document upload service that allows you to programmatically upload documents to the research agent's docs folder via HTTP API.

---

## 🔒 Security & Authentication

The LangGraph server and the Document Upload API are secured with API key authentication to ensure that only authorized users can access the research capabilities and manage documents.

### LangGraph Server Authentication

When running `langgraph dev`, the server is protected by a custom authentication handler. All API requests must include a valid API key in the headers.

- **Environment Variable**: `LANGCHAIN_API_KEY`
- **Supported Headers**: 
  - `x-api-key: your_key` (recommended)
  - `Authorization: Bearer your_key`

If `LANGCHAIN_API_KEY` is not set, the server will fallback to using `UPLOAD_API_KEY` for authentication.

### Document Upload API Authentication

The custom document upload service in `webapp.py` is also secured.

- **Environment Variable**: `UPLOAD_API_KEY`
- **Supported Header**: `X-API-Key: your_key`

> [!TIP]
> For a seamless experience, you can set both `LANGCHAIN_API_KEY` and `UPLOAD_API_KEY` to the same secure value in your `.env` file.

---

#### Starting the API Server

##### Development Server (LangGraph Platform on port 2024)

Use the official LangGraph Platform server:

```bash
langgraph dev
```

This starts the full LangGraph Platform with all endpoints, SSE streaming,
checkpoint-based state persistence, and the Studio UI at http://127.0.0.1:2024.

##### Standalone Upload API Server (port 8000)

To start only the document upload API:
```bash
# Set environment variables (or add to .env file)
export UPLOAD_API_KEY=your_secure_api_key_here
export UPLOAD_HOST=0.0.0.0  # Listen on all interfaces for public access
export UPLOAD_PORT=8000

# Start the upload server
uv run python webapp.py
```
  - Alternatively, configure separate connection variables: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`.
  - *Note*: Requires the `psycopg` package (run `pip install "psycopg[binary]"` or `uv add "psycopg[binary]"`).

#### API Endpoints


**1. Upload Documents** (`POST /documents/upload`)

Upload one or more files to a specified folder within the `docs` directory.

**Headers:**
- `X-API-Key`: Your API key for authentication

**Form Data:**
- `folder`: Target folder path relative to `docs/` (default: `policy`)
- `files`: One or more files to upload

**Example with curl:**

```bash
curl -X POST http://localhost:8000/documents/upload \
  -H 'X-API-Key: your_secure_api_key_here' \
  -F 'folder=policy' \
  -F 'files=@document1.pdf' \
  -F 'files=@document2.pdf'
```

**Example with Python requests:**

```python
import requests

api_key = "your_secure_api_key_here"
url = "http://localhost:8000/documents/upload"

files = [
    ('files', ('document1.pdf', open('document1.pdf', 'rb'), 'application/pdf')),
    ('files', ('document2.pdf', open('document2.pdf', 'rb'), 'application/pdf')),
]
data = {'folder': 'policy'}
headers = {'X-API-Key': api_key}

response = requests.post(url, files=files, data=data, headers=headers)
print(response.json())
```

**Response:**

```json
{
  "folder": "policy",
  "count": 2,
  "saved": [
    {
      "filename": "document1.pdf",
      "path": "docs/policy/document1.pdf",
      "size": 642000
    },
    {
      "filename": "document2.pdf",
      "path": "docs/policy/document2.pdf",
      "size": 523000
    }
  ],
  "total_uploaded_bytes": 1165000,
  "free_space_bytes": 98765432100,
  "free_space_human": "92.00 GB"
}
```

**3. List Files** (`GET /documents/list`)

List all files in a folder with their names and sizes.

```bash
curl -H 'X-API-Key: your_secure_api_key_here' \
  'http://localhost:8000/documents/list?folder=policy'
```

**Response:**

```json
{
  "folder": "policy",
  "count": 2,
  "files": [
    {
      "name": "document1.pdf",
      "size": 642000
    },
    {
      "name": "document2.pdf",
      "size": 523000
    }
  ]
}
```

**4. Download File** (`GET /documents/download/{filename}`)

Download a specific file from a folder.

```bash
curl -H 'X-API-Key: your_secure_api_key_here' \
  'http://localhost:8000/documents/download/document1.pdf?folder=policy' \
  -o downloaded_document.pdf
```

**5. Health Check** (`GET /health`)

Check if the API is running and get basic storage info (no authentication required).

```bash
curl http://localhost:8000/health
```

**Response:**

```json
{
  "status": "healthy",
  "docs_root": "/path/to/deep_research/docs",
  "free_space_bytes": 98765432100,
  "free_space_human": "92.00 GB"
}
```

**6. Storage Information** (`GET /storage/info`)

Get detailed storage usage information (requires API key).

```bash
curl -H 'X-API-Key: your_secure_api_key_here' http://localhost:8000/storage/info
```

**Response:**

```json
{
  "total_space_bytes": 500000000000,
  "used_space_bytes": 401234567900,
  "free_space_bytes": 98765432100,
  "total_space_human": "465.66 GB",
  "used_space_human": "373.66 GB",
  "free_space_human": "92.00 GB",
  "usage_percentage": 80.25
}
```

#### Security

- **API Key Authentication**: All upload and storage endpoints require a valid API key in the `X-API-Key` header
- **Path Validation**: Folder paths are validated to prevent directory traversal attacks
- **Filename Sanitization**: Uploaded filenames are sanitized to prevent security issues

---

## 📖 Thread Wiki (Document RAG)

The Thread Wiki feature provides **per-thread RAG without a vector database**. When documents are uploaded to a thread folder (`docs/threads/<thread-id>/`), they are automatically ingested into a wiki knowledge base at `docs/threads-wiki/<thread-id>/`. The deep research agent then automatically queries this wiki for grounded context when answering questions.

### Architecture

```
Upload documents         Auto-ingest            Wiki workspace          Research agent
docs/threads/<id>/  →   LLM review+apply   →   docs/threads-wiki/<id>/  →  Wiki context injected
  .pdf, .docx,              (background)         wiki/, raw/, log.md         into agent runs
  .pptx, .xlsx,
  .md, .txt, .csv
```

### Supported File Types

| Format | Extensions | Extraction |
|--------|-----------|------------|
| PDF | `.pdf` | PyMuPDF4LLM → Markdown (fallback: pypdf) |
| Word | `.docx` | python-docx (paragraphs + tables) |
| PowerPoint | `.pptx` | python-pptx (slides + speaker notes) |
| Excel | `.xlsx` | openpyxl (sheets → text) |
| Text | `.md`, `.txt`, `.json`, `.yaml`, `.yml`, `.csv` | Direct read |

### Automatic Behaviors

- **Auto-ingest on upload**: Uploading to `threads/<thread-id>/` triggers background wiki ingest automatically.
- **Auto-cancel + lint on delete**: Deleting documents cancels any active ingest and runs lint reconciliation to clean up stale wiki references.
- **Wiki context in research agent**: When a thread has a ready wiki, the research agent automatically queries it for context relevant to the user's question. The wiki knowledge is injected as grounded context alongside web search and other tools.

### Wiki API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/threads/{id}/wiki/ingest` | POST | Trigger wiki ingest for uploaded documents |
| `/threads/{id}/wiki/status` | GET | Poll ingest progress (phase, percentage) |
| `/threads/{id}/wiki/progress` | GET | SSE stream for real-time ingest progress |
| `/threads/{id}/wiki/ingest/cancel` | POST | Cancel an in-progress ingest |
| `/threads/{id}/wiki/query` | POST | Query wiki knowledge base directly |
| `/threads/{id}/wiki/lint` | POST | Run lint reconciliation (auto-triggered on delete) |

### Ingest Lifecycle

```
idle → initializing (5%) → staging_sources (15%) → analyzing (40%) → applying (70%) → refreshing_index (90%) → ready (100%)
                                                                                                                  ↓
                                                                                                        error / cancelled
```

### Quick Examples

**Trigger ingest manually:**
```bash
curl -X POST http://localhost:2024/threads/abc-123/wiki/ingest \
  -H 'x-api-key: your_api_key'
```

**Poll ingest progress:**
```bash
curl -H 'x-api-key: your_api_key' \
  'http://localhost:2024/threads/abc-123/wiki/status'
# {"phase": "analyzing", "progress": 40, "is_active": true, "wiki_ready": false}
```

**Stream progress via SSE:**
```bash
curl -N -H 'x-api-key: your_api_key' \
  'http://localhost:2024/threads/abc-123/wiki/progress'
```

**Query the wiki:**
```bash
curl -X POST http://localhost:2024/threads/abc-123/wiki/query \
  -H 'x-api-key: your_api_key' \
  -H 'Content-Type: application/json' \
  -d '{"question": "What are the key findings across all documents?"}'
```

**Cancel ingest:**
```bash
curl -X POST http://localhost:2024/threads/abc-123/wiki/ingest/cancel \
  -H 'x-api-key: your_api_key'
```

> **Full API reference**: See [WIKI_API_GUIDE.md](./document/WIKI_API_GUIDE.md) for detailed request/response schemas, frontend integration examples, and troubleshooting.

#### Configuration

Add these variables to your `.env` file:

```properties
# API Key for document upload endpoint authentication
UPLOAD_API_KEY=your_secure_api_key_here

# Host and port for the document upload API server
UPLOAD_HOST=0.0.0.0
UPLOAD_PORT=8000
```

To generate a secure API key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

#### Public Access

To expose the API publicly:

1. Set `UPLOAD_HOST=0.0.0.0` to listen on all network interfaces
2. Configure your firewall/router to forward port 8000 (or your chosen port)
3. Use a reverse proxy (nginx, Apache) for production deployments with SSL/TLS
4. Consider using a strong API key and rate limiting for public deployments

**⚠️ Security Warning**: When exposing publicly, ensure you:
- Use a strong, randomly generated API key
- Enable HTTPS/SSL in production
- Implement rate limiting
- Monitor access logs

---

## 🔑 OAuth Authentication

This section provides a complete guide to Google and GitHub OAuth authentication for the deep research agent. OAuth authentication enables rich user identity metadata and session management while maintaining full backward compatibility with existing API key authentication.

### 📋 Overview & Key Features

Successfully integrated Google and GitHub OAuth authentication for the deep research agent, allowing:
- **Dual Authentication**: Supports both API keys (legacy/service-to-service) and OAuth session tokens (user-facing applications) simultaneously.
- **Rich User Metadata**: Returns profile picture, full name, email, and provider-specific details (e.g., bio, followers count, locale).
- **Session Management**: Session tokens with automatic expiration (24-hour TTL) and background cleanup.
- **Provider Agnostic**: Standardized internal metadata structures that make it easy to add more OAuth providers (e.g., Microsoft, Facebook).
- **Backward Compatible**: Existing API key endpoints and workflows are unaffected.

---

### 📐 System Architecture

The following diagrams illustrate the architecture, data flows, and lifecycles of the OAuth integration.

#### 1. End-to-End OAuth Flow

```mermaid
graph TB
    User[User/Browser] -->|1. Login Request| WebApp[FastAPI Web App]
    WebApp -->|2. Redirect| OAuthProvider[OAuth Provider]
    OAuthProvider -->|3. Auth Code| WebApp
    WebApp -->|4. Exchange Token| OAuthProvider
    OAuthProvider -->|5. User Info| WebApp
    WebApp -->|6. Create Session| SessionStore[Session Store]
    WebApp -->|7. Return Token & Redirect| User
    User -->|8. API Request + Token| WebApp
    WebApp -->|9. Validate Token| SessionStore
    SessionStore -->|10. User Data| WebApp
    WebApp -->|11. Response with Metadata| User
    
    style OAuthProvider fill:#e1f5ff
    style SessionStore fill:#fff4e1
    style WebApp fill:#f0f0f0
```

#### 2. Authentication Decision Flow (Backend Middleware)

```mermaid
graph TD
    Start[Request Received] --> Extract[Extract Credential from Header]
    Extract --> CheckOAuth{Is OAuth<br/>Session Token?}
    CheckOAuth -->|Yes| ValidateSession[Validate Session]
    CheckOAuth -->|No| CheckAPIKey{Is Valid<br/>API Key?}
    
    ValidateSession --> SessionValid{Session<br/>Valid?}
    SessionValid -->|Yes| ReturnOAuth[Return OAuth User Metadata]
    SessionValid -->|No| Reject[Reject 401]
    
    CheckAPIKey --> APIKeyValid{API Key<br/>Valid?}
    APIKeyValid -->|Yes| ReturnAdmin[Return Admin Identity]
    APIKeyValid -->|No| Reject
    
    ReturnOAuth --> End[Authentication Complete]
    ReturnAdmin --> End
    Reject --> End
    
    style ReturnOAuth fill:#d4edda
    style ReturnAdmin fill:#d4edda
    style Reject fill:#f8d7da
```

#### 3. Detailed Component Interaction

```mermaid
sequenceDiagram
    participant C as Client (Browser)
    participant W as WebApp (FastAPI)
    participant O as OAuth Handler
    participant P as OAuth Provider<br/>(Google/GitHub)
    participant S as Session Store
    participant A as Auth Module
    
    Note over C,P: OAuth Login Flow
    C->>W: GET /auth/login/google
    W->>O: get_oauth_login_url()
    O-->>W: Authorization URL
    W-->>C: Redirect to Google
    
    C->>P: User authenticates
    P-->>C: Redirect with auth code
    
    C->>W: GET /auth/callback/google?code=...
    W->>O: handle_google_callback()
    O->>P: Exchange code for token
    P-->>O: Access token + ID token
    O->>P: Get user info
    P-->>O: User profile data
    O->>S: create_session(user_data)
    S-->>O: session_token
    O-->>W: user_data + session_token
    W-->>C: Redirect to FRONTEND_URL/login/success?token=session_token
    
    Note over C,A: API Request Flow
    C->>W: API request with session_token (via X-API-Key/Auth header)
    W->>A: authenticate(headers)
    A->>S: validate_session(token)
    S-->>A: user_data or None
    
    alt Valid OAuth Session
        A-->>W: Full user metadata
        W-->>C: Response with user context
    else Invalid Session, Try API Key
        A->>A: Check API key
        alt Valid API Key
            A-->>W: {"identity": "admin"}
            W-->>C: Response
        else Invalid
            A-->>W: 401 Unauthorized
            W-->>C: Error response
        end
    end
```

#### 4. Data Transformation Flow

```mermaid
graph LR
    A[OAuth Provider] -->|Raw User Data| B[OAuth Handler]
    B -->|Transform| C[Standardized Format]
    C -->|Store| D[Session Store]
    C -->|Return| E[Auth Module]
    E -->|Enrich| F[Response Metadata]
    
    subgraph Google Data
        G1[sub - Unique ID]
        G2[email]
        G3[name]
        G4[picture]
        G5[email_verified]
        G6[locale]
        G7[given_name]
        G8[family_name]
    end
    
    subgraph GitHub Data
        H1[id - Unique ID]
        H2[login - username]
        H3[name]
        H4[avatar_url]
        H5[bio]
        H6[location]
        H7[company]
        H8[blog]
        H9[followers]
        H10[following]
        H11[public_repos]
        H12[created_at]
    end
    
    subgraph Standardized Output
        I1[identity - provider:id]
        I2[email]
        I3[name]
        I4[provider]
        I5[avatar_url]
        I6[metadata - additional fields]
    end
    
    G1 & G2 & G3 & G4 & G5 & G6 & G7 & G8 --> B
    H1 & H2 & H3 & H4 & H5 & H6 & H7 & H8 & H9 & H10 & H11 & H12 --> B
    B --> I1 & I2 & I3 & I4 & I5 & I6
    
    style A fill:#e1f5ff
    style D fill:#fff4e1
    style F fill:#d4edda
```

#### 5. Session Token Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Created: User logs in via OAuth
    Created --> Active: Session token generated
    Active --> Active: Token validated on requests
    Active --> Expired: 24 hours elapsed
    Expired --> [*]: Session cleaned up
    Active --> Revoked: Manual logout (clear cookie)
    Revoked --> [*]: Session removed
    
    note right of Created
        Session contains:
        - User data from OAuth provider
        - Provider name (google/github)
        - Creation timestamp
        - Expiration timestamp
    end note
    
    note right of Active
        Each API request:
        1. Extract token from header
        2. Lookup in session store
        3. Check expiration
        4. Return user metadata
    end note
    
    note right of Expired
        Cleanup process:
        - Periodic scan for expired sessions
        - Remove from session store
        - Free memory/resources
    end note
```

#### 6. Security & Deployment Architecture

```mermaid
graph TB
    subgraph Client Side
        Browser[Browser/App]
        TokenStorage[Token Storage<br/>Secure Cookie]
    end
    
    subgraph Server Side
        API[FastAPI Server]
        Auth[Auth Module]
        OAuth[OAuth Handler]
        Sessions[Session Store]
    end
    
    subgraph External
        Google[Google OAuth]
        GitHub[GitHub OAuth]
    end
    
    Browser -->|HTTPS| API
    API --> Auth
    Auth --> OAuth
    Auth --> Sessions
    
    OAuth -->|OAuth 2.0| Google
    OAuth -->|OAuth 2.0| GitHub
    
    Browser -.->|Stores session token| TokenStorage
    
    style Google fill:#4285f4,color:#fff
    style GitHub fill:#333,color:#fff
    style Sessions fill:#fff4e1
    style Auth fill:#d4edda
```

---

### ⚙️ Credentials & Provider Setup

To configure OAuth client credentials for local development and production, follow these setup steps:

#### Quick Reference: Google OAuth Configuration

| Configuration Item | Local Development | Production (Azure) |
|-------------------|-------------------|-------------------|
| **Frontend URL** | `http://localhost:3000` | `https://deepagent-ui.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io` |
| **Backend Callback** | `http://localhost:2024/auth/callback/google` | `https://deep-research-agent-0312.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io/auth/callback/google` |
| **Authorized JS Origins** | `http://localhost:3000` | Your frontend HTTPS URL |
| **Authorized Redirect URIs** | `http://localhost:2024/auth/callback/google` | Your backend HTTPS URL + `/auth/callback/google` |

> [!NOTE]
> - The **LangGraph dev server** runs on port **2024** by default (not 8000)
> - The **frontend UI** typically runs on port **3000**
> - Both **Authorized JavaScript origins** AND **Authorized redirect URIs** must be configured in Google Cloud Console

#### Quick Reference: GitHub OAuth Configuration

| Configuration Item | Local Development | Production (Azure) |
|-------------------|-------------------|-------------------|
| **Application Name** | `BMO Deep Agent Local` | `BMO Deep Agent` |
| **Homepage URL** | `http://localhost:3000` | `https://deepagent-ui.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io` |
| **Authorization Callback URL** | `http://localhost:2024/auth/callback/github` | `https://deep-research-agent-0312.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io/auth/callback/github` |
| **Device Flow** | Optional | Optional |

> [!NOTE]
> - GitHub OAuth apps are **free** and can be created under your personal GitHub account or organization
> - Unlike Google OAuth, GitHub uses **Homepage URL** (not separate "Authorized JavaScript origins")
> - You must create **separate OAuth apps** for local and production environments

#### 1. Google OAuth Setup
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project or select an existing one.
3. Navigate to **APIs & Services > OAuth consent screen**.
4. Select **External** (or **Internal** if Workspace organization) and click **Create**.
5. Fill out the required fields (**App name**, **User support email**, **Developer contact information**) and click **Save and Continue**.
6. Navigate to **APIs & Services > Credentials**.
7. Click **+ Create Credentials** at the top and select **OAuth client ID**.
8. Set the **Application type** to **Web application**.
9. Add a name (e.g., "BMO Deep Agent").
10. **Configure Authorized JavaScript origins** (required for browser-based OAuth flows):
    - For Local Development: `http://localhost:3000`
    - For Production: Add your frontend URL for Azure (e.g., `https://deepagent-ui.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io`)
    - For Production: Add your frontend URL for AWS (e.g., `https://d600y3wyk0xvf.cloudfront.net`)
11. **Configure Authorized redirect URIs** (must match your backend callback endpoints exactly):
    - For Local Development: `http://localhost:2024/auth/callback/google`
    - For Production: Add your backend URL for Azure (e.g., `https://deep-research-agent-0312.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io/auth/callback/google`)
    - For Production: Add your backend URL for AWS (e.g., `https://bh3z333bky.us-east-1.awsapprunner.com/auth/callback/google`)
12. Click **Create** and copy the generated **Client ID** and **Client Secret**.

> [!IMPORTANT]
> - The redirect URI must match **exactly** (including protocol, host, and port) between Google Cloud Console and your application configuration.
> - Google distinguishes between `http://localhost:2024` and `http://127.0.0.1:2024` - use `localhost` consistently.
> - In production, always use HTTPS URLs for both origins and redirect URIs.

#### 2. GitHub OAuth Setup

> [!IMPORTANT]
> You need to create **two separate GitHub OAuth applications**: one for local development and one for production.

##### Local Development App
1. Go to your [GitHub Developer Settings](https://github.com/settings/developers).
2. Click on **OAuth Apps** in the left sidebar, then click **New OAuth App**.
3. Fill in the application details:
   - **Application name**: `BMO Deep Agent Local`
   - **Homepage URL**: `http://localhost:3000` (points to the frontend UI)
   - **Authorization callback URL**: `http://localhost:2024/auth/callback/github` (points to the backend API callback)
   - **Enable Device Flow**: Optional - check this if you want to support device-based authentication flows
4. Click **Register application**.
5. Copy the **Client ID** from the application dashboard page.
6. Click **Generate a new client secret** and copy the generated secret value immediately.

##### Production App
1. Repeat the same process to create a production OAuth app.
2. Fill in the application details:
   - **Application name**: `BMO Deep Agent`
     - **Homepage URL**: `https://deepagent-ui.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io` (your frontend URL)
     - **Authorization callback URL**: `https://deep-research-agent-0312.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io/auth/callback/github` (your backend URL)
     - **Enable Device Flow**: Optional - check this if needed for your deployment

   - **Application name**: `BMO Deep Agent AWS`
     - **Homepage URL**: `https://d600y3wyk0xvf.cloudfront.net` (your frontend URL)
     - **Authorization callback URL**: `https://bh3z333bky.us-east-1.awsapprunner.com/auth/callback/github` (your backend URL)
     - **Enable Device Flow**: Optional - check this if needed for your deployment
   - **Application name**: `BMO Deep Agent Vercel`
     - **Homepage URL**: `https://bmo-deepagent-ui.vercel.app` (your frontend URL)
     - **Authorization callback URL**: `https://deep-research-agent-0312.salmonrock-b46ff20d.canadacentral.azurecontainerapps.io/auth/callback/github` (your backend URL)
     - **Enable Device Flow**: Optional - check this if needed for your deployment
3. Click **Register application**.
4. Copy the **Client ID** and **Client Secret** for production use.

> [!IMPORTANT]
> - GitHub uses **Homepage URL** (not "Authorized JavaScript origins" like Google) to identify your application
> - The **Authorization callback URL** must match exactly (including protocol, host, and port) between GitHub and your application
> - GitHub distinguishes between `http://localhost:2024` and `http://127.0.0.1:2024` - use `localhost` consistently
> - In production, always use HTTPS URLs for both homepage and callback URLs
> - GitHub OAuth scope `user:email` is required to access user email addresses (configured automatically in the code)

#### 3. Update the Backend Environment Config
Create or open the `.env` file in the backend `deep_research` directory and add the copied client IDs and secrets:

```env
# Google OAuth Credentials
GOOGLE_CLIENT_ID="your_google_client_id_here"
GOOGLE_CLIENT_SECRET="your_google_client_secret_here"

# GitHub OAuth Credentials
GITHUB_CLIENT_IDS="your_github_client_id_here"
GITHUB_CLIENT_SECRETS="your_github_client_secret_here"

# OAuth Session Secret Key (for token verification and signing)
OAUTH_SECRET_KEY="generate-a-random-secret-string-here"

# Target Frontend URLs to redirect to after successful callback
FRONTEND_URLS="http://localhost:3000"
```
> [!TIP]
> You can generate a strong random secret key by running:
> `python -c "import secrets; print(secrets.token_urlsafe(32))"`

---

### 🚀 Usage & Quickstart

#### 1. Start the FastAPI Server
Ensure all packages are synced, and start the application server.

**Option A: LangGraph Dev Server (Recommended)**
```bash
cd deep_research
uv sync
langgraph dev
langgraph dev --no-reload --no-browser
```

**Option B: Upload API Only**
```bash
cd deep_research
uv sync
python webapp.py
```

#### 2. Test the Authentication Flow

##### Method A: OAuth Authentication (Recommended)
1. Navigate in your browser to start the flow:
   - **Google Login**: `http://localhost:2024/auth/login/google`
   - **GitHub Login**: `http://localhost:2024/auth/login/github`
2. Complete authorization on the provider's login screen.
3. Upon success, the backend redirects you to:
   `http://localhost:3000/login/success?token=SESSION_TOKEN`
4. Copy the `session_token` parameter from the URL.

> [!NOTE]
> The LangGraph dev server runs on port **2024**, so all authentication endpoints are accessible at `http://localhost:2024/auth/*`

##### Method B: Legacy API Key Authentication
API Key authentication remains fully active. Requests containing your admin key will work identically:
```bash
curl http://localhost:8000/documents/list \
  -H "X-API-Key: your-admin-api-key"
```

#### 3. Access Protected API Endpoints
Make authenticated requests using the obtained OAuth `session_token` via the `X-API-Key` or `Authorization: Bearer` headers:

```bash
# Verify using X-API-Key header
curl http://localhost:8000/documents/list \
  -H "X-API-Key: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

# Verify using Authorization header
curl http://localhost:8000/documents/list \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
```

---

### 💻 Code & Agent Integration

#### Auth Middleware & Context Resolution
The `auth.py` authentication module parses headers and handles validation automatically:

```python
# auth.py
@auth.authenticate
async def authenticate(headers: dict) -> Auth.types.MinimalUserDict:
    # 1. Try resolving OAuth session token
    user_data = user_manager.validate_session(credential)
    if user_data:
        return {
            "identity": user_data["identity"],
            "email": user_data.get("email"),
            "name": user_data.get("name"),
            "provider": user_data.get("provider"),
            "avatar_url": user_data.get("picture") or user_data.get("avatar_url"),
            "metadata": user_data.get("metadata", {})
        }
    
    # 2. Fall back to admin API key
    if credential == expected_api_key:
        return {"identity": "admin"}
```

#### Profile Payload Structures
Endpoints extracting authenticated user state will receive structured dictionaries:

##### Google OAuth Profile
```json
{
  "identity": "google:1182736459",
  "email": "user@gmail.com",
  "name": "John Doe",
  "provider": "google",
  "avatar_url": "https://lh3.googleusercontent.com/...",
  "metadata": {
    "email_verified": true,
    "locale": "en",
    "given_name": "John",
    "family_name": "Doe"
  }
}
```

##### GitHub OAuth Profile
```json
{
  "identity": "github:987654321",
  "email": "user@github.com",
  "name": "John Doe",
  "provider": "github",
  "avatar_url": "https://avatars.githubusercontent.com/u/987654321",
  "metadata": {
    "username": "johndoe",
    "bio": "Software Developer",
    "location": "San Francisco, CA",
    "company": "Tech Corp",
    "blog": "https://johndoe.dev",
    "followers": 150,
    "following": 75,
    "public_repos": 30,
    "created_at": "2019-05-15T10:30:00Z"
  }
}
```

> [!NOTE]
> GitHub OAuth provides **rich profile metadata** including username, bio, followers, repositories, and more. The `username` field in metadata is the GitHub handle (login), while `name` is the display name.

##### API Key User (Legacy Admin)
```json
{
  "identity": "admin"
}
```

#### Accessing Authenticated User Info in Code
To consume the identity metadata inside your FastAPI route handlers:

```python
# Access user details via request state
@app.get("/documents/list")
async def list_documents(request: Request):
    user_identity = request.state.user_identity
    print(f"Request made by user ID: {user_identity['identity']}")
    print(f"Provider: {user_identity.get('provider')}")
```

To invoke threads via the LangGraph SDK with authentication headers:

```python
from langgraph_sdk import get_client

client = get_client(url="http://localhost:8000")

async def run_agent_workflow(session_token: str):
    # Pass session_token in the headers
    thread = await client.threads.create(
        headers={"x-api-key": session_token}
    )
    # The authenticated user metadata is now bound to this workflow trace
```

---

### 🛡️ Production & Security Hardening

#### 1. Distributed Session Store (Redis)
In-memory session storage (`InMemorySessionStore`) resets whenever the backend restarts and cannot scale across multiple instances. For production, switch to Redis:

```python
import redis
import json
import secrets
from datetime import datetime, timedelta

class RedisSessionStore:
    def __init__(self, redis_url="redis://localhost:6379"):
        self.redis = redis.from_url(redis_url)
        
    def create_session(self, user_data: dict, provider: str) -> str:
        session_token = secrets.token_urlsafe(32)
        session_data = {
            "user_data": user_data,
            "provider": provider,
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(hours=24)).isoformat()
        }
        # Set redis key with 24-hour expiration (86400 seconds)
        self.redis.setex(
            f"session:{session_token}",
            86400,
            json.dumps(session_data)
        )
        return session_token

    def validate_session(self, session_token: str) -> dict | None:
        data = self.redis.get(f"session:{session_token}")
        if not data:
            return None
        session_data = json.loads(data)
        expires_at = datetime.fromisoformat(session_data["expires_at"])
        if datetime.utcnow() > expires_at:
            self.redis.delete(f"session:{session_token}")
            return None
        return session_data["user_data"]
```

#### 2. Performance Characteristics

| Operation | Latency | Throughput | Notes |
|-----------|---------|------------|-------|
| OAuth Redirection / Callback | ~500ms | 100 req/s | Governed by Google/GitHub round-trips |
| Session Token Generation | ~1ms | 1000 req/s | Cryptographic token creation |
| Session Verification (In-Memory) | ~0.5ms | 2000 req/s | Dict lookup |
| Session Verification (Redis) | ~1.5ms | 1500 req/s | Cache network request |
| Legacy API Key Validation | ~0.1ms | 10000 req/s | Constant-time string match |

#### 3. Security Checklist
- [ ] **HTTPS Redirection**: Enable strict TLS termination. Redirection endpoints transmit sensitive state tokens and authentication cookies.
- [ ] **State Parameters / CSRF**: Generate and validate `state` tokens on the initiator endpoint (`/auth/login/*`) to prevent Cross-Site Request Forgery.
- [ ] **Azure Key Vault integration**: Safely reference secrets in Azure/AWS App Services using managed identities.
- [ ] **Rate Limiting**: Apply request limits to authentication routes to defend against token brute-force attempts.
- [ ] **Session Expiry Cleanup**: Configure clean-up cron routines for expired session tokens.

---

### 🛠️ Troubleshooting & Diagnostics

#### 1. "OAuth authentication is not enabled"
This means the client secrets/IDs are not parsed correctly or dependency packages are missing.
- Check that your `.env` contains all four client configuration properties with quotes.
- Run `uv sync` to ensure Authlib and itsdangerous are installed in your workspace.

#### 2. "Invalid Redirect URI" error from Google/GitHub
The redirection URI on the provider dashboard must match *exactly* to the protocol, host, and port of the FastAPI app callback endpoint:
- Correct local callback URI: `http://localhost:2024/auth/callback/google`
- Note: Google Cloud Console distinguishes `http://localhost:2024` from `http://127.0.0.1:2024`.
- **Important**: The LangGraph dev server typically runs on port 2024, not port 8000. Verify your server port matches the redirect URI configuration.
- For production deployments, ensure the full HTTPS URL is configured (e.g., `https://your-domain.com/auth/callback/google`).

#### 2.1. "Origin Mismatch" error from Google
Google OAuth requires **Authorized JavaScript origins** to be configured in addition to redirect URIs:
- Add your frontend URL to Authorized JavaScript origins (e.g., `http://localhost:3000` for local development)
- This is separate from Authorized redirect URIs - both must be configured
- Origins don't include the path (just protocol + host + port)

#### 2.2. "Invalid redirect_uri" error from GitHub
GitHub OAuth common issues:
- The **Authorization callback URL** must match exactly between GitHub and your app configuration
- GitHub uses **Homepage URL** (not "Authorized JavaScript origins") - only one URL needs to be configured
- Verify the callback URL includes the correct port (`2024` for LangGraph dev server)
- Common mistake: Using `http://localhost:8000` instead of `http://localhost:2024`
- Email addresses may be null if user has no public email - the code falls back to first available email

> [!TIP]
> GitHub OAuth is simpler than Google OAuth - you only need to configure the **Homepage URL** and **Authorization callback URL**, with no separate "origins" configuration.

#### 2.3. GitHub vs Google OAuth Key Differences

| Feature | Google OAuth | GitHub OAuth |
|---------|-------------|-------------|
| **Consent Screen** | Required (External/Internal) | Not required |
| **Homepage URL** | Not used | Required |
| **Authorized Origins** | Required (JS origins) | Not used |
| **Redirect URIs** | Required | Required (Authorization callback URL) |
| **Scopes** | `openid email profile` | `user:email` |
| **User ID Field** | `sub` | `id` |
| **Username Field** | Not available | `login` (in metadata) |
| **Email Verification** | `email_verified` field | Must check email visibility |

> [!NOTE]
> GitHub OAuth users may not have a public email address. The application will use the first available email from the user's email list, which may be private.

#### 3. Run Validation Tests
The project includes a validation script `test_oauth_setup.py` that verifies imports, session creation mechanics, and environment settings. Run:
```bash
python test_oauth_setup.py
```

## 🧩 Deep Research Agent Components

What is used in the deep research agent?

<img width="377" alt="Deep Research Graph" src="./resources/deep_research_graph.png" />

### 1. Planning (`write_todos`)
- **Workflow Orchestration**: The research workflow starts with creating a todo list using the `write_todos` tool to break down the user's research request into focused tasks.
- **Progress Tracking**: This list is used for task breakdown and ensures systematic research coverage.

### 2. Filesystem & Context Gathering
- **Document Reading**: A specialized `read_doc_folder` tool is used to extract text from various document formats, including:
  - PDF (`.pdf`)
  - Word (`.docx`)
  - PowerPoint (`.pptx`)
  - Excel (`.xlsx`)
  - Text and Markdown (`.txt`, `.md`)
- **Caching**: Extracted text from documents is cached under the active output folder (for example `output/` or `output/<doc-folder-name>/`) to avoid redundant processing.
- **Storage**: Findings and final reports are saved to files like `/research_request.md` and `/final_report.md` (using `write_file`).

### 3. Web Research Tools
- **Tavily Search**: The primary tool for web research is `tavily_search`, which performs web searches to gather information.
- **Webpage Fetching**: `fetch_webpage_content` is used to retrieve and process content from specific URLs found during searches.

### 4. Sub-Agents & Delegation
- **Delegation Strategy**: The orchestrator uses the `task()` tool (provided by the `deepagents` framework) to delegate specific research tasks to specialized sub-agents.
- **Parallel Execution**: Sub-agents can run in parallel for comparison tasks or multi-faceted research (configured in `agent.py`).
- **Context Isolation**: Each sub-agent operates within its own context, and findings are later synthesized by the orchestrator.

### 5. Smart Defaults & Prompting
- **Structured Prompts**: Extensive prompt templates in `research_agent/prompts.py` (e.g., `RESEARCH_WORKFLOW_INSTRUCTIONS`, `RESEARCHER_INSTRUCTIONS`) define detailed behaviors for:
  - Research planning and limits
  - Citation formatting (`[1]`, `[2]`...)
  - Report writing patterns (Comparisons, Lists, Summaries)
  - Tool usage rules (e.g., must use `think_tool` after each search)

### 6. Structured Output Skills
- **Structured Skills**: The agent can generate structured data using skills like `golden-dataset`.
- **Validation and Finalization**: Tools like `render_skill_output` and `finalize_golden_dataset_output` are used to validate schemas, export CSVs, and run or re-run quality metrics.

### 7. Context Management
- **Reflection**: The `think_tool` is used for "inner monologue" and strategic planning, helping the agent reflect on findings before deciding the next step.
- **Synthesis**: The orchestrator is responsible for consolidating findings and citations from all sub-agents into a final, coherent report.
- **Auto-summarization**: The underlying `deepagents` framework likely handles conversation pruning or summarization when context limits are reached.

## 📚 Resources

- **[Deep Research Course](https://academy.langchain.com/courses/deep-research-with-langgraph)** - Full course on deep research with LangGraph

### Custom Model

By default, `deepagents` uses `"claude-sonnet-4-5-20250929"`. You can customize this by passing any [LangChain model object](https://python.langchain.com/docs/integrations/chat/). See the Deep Agents package [README](https://github.com/langchain-ai/deepagents?tab=readme-ov-file#model) for more details.

```python
import os
from langchain.chat_models import init_chat_model
from deepagents import create_deep_agent

# Using Ollama (Local)
model = init_chat_model(model=f"ollama:{os.getenv("MODEL_NAME")}", base_url=os.getenv("OLLAMA_API_BASE"))

# Using Claude
model = init_chat_model(model=os.getenv("MODEL_NAME"), temperature = 0.0)

# Using Gemini
from langchain_google_genai import ChatGoogleGenerativeAI

model = ChatGoogleGenerativeAI(model=os.getenv("MODEL_NAME"))

# Using AzureOpenAI
from langchain_openai import AzureChatOpenAI
from pydantic import SecretStr

model = AzureChatOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
    api_key=SecretStr(os.getenv("AZURE_OPENAI_API_KEY", ""))
)

agent = create_deep_agent(
    model=model,
)
```

### Custom Instructions

The deep research agent uses custom instructions defined in `research_agent/prompts.py` that complement (rather than duplicate) the default middleware instructions. You can modify these in any way you want.

| Instruction Set | Purpose |
|----------------|---------|
| `RESEARCH_WORKFLOW_INSTRUCTIONS` | Defines the 5-step research workflow: save request → plan with TODOs → delegate to sub-agents → synthesize → respond. Includes research-specific planning guidelines like batching similar tasks and scaling rules for different query types. |
| `SUBAGENT_DELEGATION_INSTRUCTIONS` | Provides concrete delegation strategies with examples: simple queries use 1 sub-agent, comparisons use 1 per element, multi-faceted research uses 1 per aspect. Sets limits on parallel execution (max 3 concurrent) and iteration rounds (max 3). |
| `RESEARCHER_INSTRUCTIONS` | Guides individual research sub-agents to conduct focused web searches. Includes hard limits (2-3 searches for simple queries, max 5 for complex), emphasizes using `think_tool` after each search for strategic reflection, and defines stopping criteria. |

### Custom Tools

The deep research agent adds the following custom tools beyond the built-in deepagent tools. You can also use your own tools, including via MCP servers. See the Deep Agents package [README](https://github.com/langchain-ai/deepagents?tab=readme-ov-file#mcp) for more details.

#### Web Search & Content Retrieval

<table>
  <tr>
    <th width="250">Tool Name</th>
    <th>Description</th>
  </tr>
  <tr>
    <td><code>tavily_search</code></td>
    <td>Advanced web search tool that uses Tavily purely as a URL discovery engine. Performs searches using Tavily API to find relevant URLs, fetches full webpage content via HTTP with proper User-Agent headers (avoiding 403 errors), converts HTML to markdown, and returns the complete content without summarization to preserve all information for the agent's analysis. Supports configurable result counts and topic filtering (general/news/finance). Respects the <code>no_web</code> state flag to disable web access when needed. Works with both Claude and Gemini models.</td>
  </tr>
  <tr>
    <td><code>fetch_webpage_content</code></td>
    <td>Fetches and converts a specific webpage URL to markdown format. Useful when you have a direct URL and need to extract its content for analysis. Uses proper User-Agent headers and respects SSL verification settings. Also checks the <code>no_web</code> state flag before fetching.</td>
  </tr>
</table>

#### Strategic Thinking & Reflection

<table>
  <tr>
    <th width="250">Tool Name</th>
    <th>Description</th>
  </tr>
  <tr>
    <td><code>think_tool</code></td>
    <td>Strategic reflection mechanism that helps the agent pause and assess progress between searches, analyze findings, identify gaps, and plan next steps. Records reflections to timestamped log files in the output folder for audit trails. Essential for maintaining coherent research strategy across multiple iterations.</td>
  </tr>
</table>

#### Filesystem & Document Processing

<table>
  <tr>
    <th width="250">Tool Name</th>
    <th>Description</th>
  </tr>
  <tr>
    <td><code>read_file</code></td>
    <td>Reads the content of a file at the given path. Implements a two-tier fallback strategy: first checks LangGraph state virtual filesystem (DeepAgents backend), then falls back to the local filesystem if not available. Normalizes paths for cross-platform compatibility.<br><br><strong>Section Selection:</strong> For Markdown files, append <code>#</code> followed by the heading text to read specific sections (e.g., <code>report.md#Introduction</code> or <code>docs/guide.md## Installation Steps</code>). The section selector is case-insensitive and matches the exact heading text including <code>#</code> symbols.</td>
  </tr>
  <tr>
    <td><code>ls</code></td>
    <td>Lists the contents of a directory with fallback support. Tries virtual filesystem in state first, then local filesystem. Returns filenames with "/" suffix for subdirectories. Normalizes paths for consistent behavior across Windows and Unix systems.</td>
  </tr>
  <tr>
    <td><code>glob</code></td>
    <td>Finds files matching a glob pattern with recursive support (e.g., <code>**/*.md</code>). Implements dual-path resolution: virtual filesystem first, then local filesystem. Handles complex patterns and normalizes paths for cross-platform compatibility.</td>
  </tr>
  <tr>
    <td><code>read_doc_folder</code></td>
    <td>Extracts text content from supported document files in a specified folder. Supports <code>.pdf</code>, <code>.txt</code>, <code>.md</code>, <code>.docx</code>, <code>.pptx</code>, and <code>.xlsx</code> formats. Automatically resolves folder paths from agent state or environment variables. Caches extracted content under the active output folder to avoid redundant processing. For large folders, returns a summary instead of full content; use the <code>specific_files</code> parameter to target individual documents.</td>
  </tr>
</table>

#### Skill Management System

<table>
  <tr>
    <th width="250">Tool Name</th>
    <th>Description</th>
  </tr>
  <tr>
    <td><code>list_available_skills</code></td>
    <td>Lists all available skills registered in the dynamic skill registry with their descriptions. Scans the <code>research_agent/skills/</code> directory and extracts metadata from SKILL.md frontmatter. Helps the agent discover what specialized capabilities are available for structured output generation (e.g., frontend-slides, golden-dataset, interview-prep).</td>
  </tr>
  <tr>
    <td><code>read_skill_supporting_file</code></td>
    <td>Reads supporting files from a skill directory (e.g., CSS templates, style presets, HTML architecture guides, animation patterns). Use this when a skill's instructions reference external resources needed for implementation. Provides error messages with available file listings if the requested file doesn't exist.</td>
  </tr>
</table>

#### Structured Output Rendering

<table>
  <tr>
    <th width="250">Tool Name</th>
    <th>Description</th>
  </tr>
  <tr>
    <td><code>render_skill_output</code></td>
    <td>Generic skill renderer that loads a skill definition from <code>research_agent/skills/*/SKILL.md</code>, validates the provided JSON payload against that skill's schema, applies default values for optional fields, coerces data types, and renders the final Markdown output using template specifications. <strong>Use ONLY for structured skills with JSON schemas</strong>—do NOT use for unstructured markdown documents. Returns validation errors if the payload doesn't match the schema.</td>
  </tr>
</table>

#### Golden Dataset Generation & Evaluation

<table>
  <tr>
    <th width="250">Tool Name</th>
    <th>Description</th>
  </tr>
  <tr>
    <td><code>finalize_golden_dataset_output</code></td>
    <td>Golden-dataset only: validates the same JSON payload as <code>render_skill_output</code>, exports a CSV under the output folder via <code>skills/golden_dataset/pipeline.py</code>, then runs quality metrics so export and evaluation always happen in order. Generates human-readable quality reports (<code>final_report.md</code>) alongside raw metrics (<code>golden_dataset_metrics.md</code>). Persists files to LangGraph state for downstream access. Calculates chat elapsed time for performance tracking.</td>
  </tr>
</table>

#### Frontend Slides Presentation Generation

<table>
  <tr>
    <th width="250">Tool Name</th>
    <th>Description</th>
  </tr>
  <tr>
    <td><code>frontend-slides</code></td>
    <td>Generates self-contained HTML slide decks from markdown-style slide content. Accepts presentation content with headings (<code># [Slide 1] Title:</code>), headlines, subtitles, body text, bullet lists, and callout blocks. Supports 12 visual presets (Bold Signal, Electric Studio, Creative Voltage, Dark Botanical, Notebook Tabs, Pastel Geometry, Split Pastel, Vintage Editorial, Neon Cyber, Terminal Green, Swiss Modern, Paper & Ink) and 6 animation styles (dramatic, techy, playful, professional, calm, editorial). Includes optional inline editing mode. Saves generated HTML to both <code>./output</code> and <code>./reports</code> folders. Persists files to LangGraph state.</td>
  </tr>
  <tr>
    <td><code>frontend-slides-export-pdf</code></td>
    <td>Exports an HTML presentation to PDF format using Playwright. Calls <code>scripts/export-pdf.sh</code> which captures screenshots of each slide and compiles them into a single PDF document. Note: animations are not preserved in PDF output. Supports compact mode (1280x720 instead of 1920x1080) for smaller file sizes. Requires Playwright installation.</td>
  </tr>
  <tr>
    <td><code>frontend-slides-deploy</code></td>
    <td>Deploys an HTML presentation to a live Vercel URL using the Vercel CLI. Calls <code>scripts/deploy.sh</code> which requires Vercel CLI installation and authentication. Provides shareable public links for presentations.</td>
  </tr>
  <tr>
    <td><code>frontend-slides-extract-pptx</code></td>
    <td>Extracts content and images from PowerPoint (.pptx) files. Runs <code>scripts/extract-pptx.py</code> which returns JSON structures containing slides, text, and embedded images. Facilitates conversion of existing presentations to HTML format. Outputs extracted data to a specified directory for further processing.</td>
  </tr>
</table>

## 🛡️ Reliability & Rate Limiting

When building high-throughput agents, treating LLM providers as finite-capacity systems is critical. This project implements a dual-layer approach to ensure reliability:

### 1. Proactive Rate Shaping
Instead of waiting for `429 Too Many Requests` errors, the harness proactively controls the flow of tokens and requests. This is handled by the `AsyncRateLimiter` in `deep_research/retry_utils.py`.

- **TPM (Tokens Per Minute) Control**: Tracks a rolling 60-second window of estimated tokens to stay under deployment quotas.
- **RPM (Requests Per Minute) Pacing**: Ensures requests are evenly spaced to avoid triggering micro-burst limits (often 1–10 seconds).
- **Safe Margins**: Operates at ~80% of hard limits to absorb jitter and shared usage.

To enable, set these environment variables:
```properties
# Proactive Rate Shaping (TPM and RPM limits)
# Set these based on your provider's deployment quotas
# Tokens Per Minute:
# Represents the maximum number of tokens (input + output) you are allowed to send to the model provider within a
# rolling 60-second window.
MODEL_TPM=120000
# Requests Per Minute:
# Represents the maximum number of individual API calls you can make per minute.
MODEL_RPM=500
```

### 2. Reactive Retries
For unpredictable server-side issues or shared capacity drops, a reactive layer handles retries with **Exponential Backoff and Jitter**.

- **Jitter**: Prevents "thundering herd" problems by randomizing retry delays.
- **Header Respect**: Logic can be extended to respect `Retry-After` headers from providers.
- **Configurable**: Adjust `MODEL_MAX_RETRIES` and `MODEL_INITIAL_BACKOFF` as needed.

### Strategic Recommendations
1. **Estimate accurately**: Use `tiktoken` (integrated in `AsyncRateLimiter`) for precise token counting.
2. **Layer your defenses**: Always use proactive shaping *with* reactive retries.
3. **Deployment-specific limits**: Configure unique limits for different models or regions to maximize throughput.

## 🛡️ Rate Limit Handling

### Overview

Model API calls have rate limits that can cause report generation to fail. The deep research agent includes an **automatic retry mechanism with exponential backoff** that gracefully handles rate limit errors from any model provider (OpenAI, Anthropic, Google, Azure, Ollama).

### How It Works

1. **Automatic Detection**: Rate limit errors are automatically detected (429 errors, "too many requests", quota exceeded, etc.)
2. **Exponential Backoff**: When a rate limit is hit, the system waits before retrying:
   - First retry: ~1 second wait
   - Second retry: ~2 seconds wait  
   - Third retry: ~4 seconds wait
   - Fourth retry: ~8 seconds wait
   - Fifth retry: ~16 seconds wait
   - (capped at maximum backoff of 60 seconds)
3. **Jitter**: Random variation (±50%) is added to prevent "thundering herd" when multiple clients retry simultaneously
4. **Maximum Retries**: By default, retries up to 5 times before giving up
5. **Smart Filtering**: Content filter errors (Azure) are NOT retried as they won't succeed on retry

### Configuration

All retry behavior is configurable via environment variables in your `.env` file:

```properties
# Maximum number of retry attempts when rate limit errors occur
MODEL_MAX_RETRIES=5
# Initial backoff time in seconds before first retry
MODEL_INITIAL_BACKOFF=1.0
# Maximum backoff time in seconds (cap for exponential backoff)
MODEL_MAX_BACKOFF=60.0
# Multiplier for exponential backoff (backoff = initial * multiplier^attempt)
MODEL_BACKOFF_MULTIPLIER=2.0
# Add jitter to prevent thundering herd problem (true/false)
MODEL_RETRY_JITTER=true
```

### Tuning Recommendations

#### For Strict Rate Limits (e.g., free tier APIs)
```properties
MODEL_MAX_RETRIES=10
MODEL_INITIAL_BACKOFF=2.0
MODEL_MAX_BACKOFF=120.0
MODEL_BACKOFF_MULTIPLIER=2.0
```

#### For Lenient Rate Limits (e.g., paid tiers)
```properties
MODEL_MAX_RETRIES=3
MODEL_INITIAL_BACKOFF=0.5
MODEL_MAX_BACKOFF=30.0
MODEL_BACKOFF_MULTIPLIER=1.5
```

#### For Local Models (Ollama)
```properties
MODEL_MAX_RETRIES=2
MODEL_INITIAL_BACKOFF=0.5
MODEL_MAX_BACKOFF=10.0
MODEL_BACKOFF_MULTIPLIER=1.5
```

### What Gets Retried

The retry wrapper is automatically applied to all model invocations (`model.invoke()` and `model.ainvoke()`) across all agents in this project.

### Error Messages You'll See

When rate limits are hit, you'll see warning messages like:
```
WARNING:retry_utils:Rate limit hit in invoke (attempt 1/6). Retrying in 1.23s... Error: Rate limit exceeded: 429 Too Many Requests
```

If all retries are exhausted:
```
ERROR:retry_utils:Rate limit error persisted after 5 retries in invoke. Last error: Rate limit exceeded
```

### Troubleshooting

#### Still Getting Failures?

1. **Increase max retries**: Set `MODEL_MAX_RETRIES=10` or higher
2. **Increase initial backoff**: Set `MODEL_INITIAL_BACKOFF=5.0` to start with longer waits
3. **Check your API quota**: You may need to upgrade your plan
4. **Review logs**: Check which specific error is occurring

#### Retries Taking Too Long?

1. **Reduce max retries**: Set `MODEL_MAX_RETRIES=2`
2. **Reduce backoff multiplier**: Set `MODEL_BACKOFF_MULTIPLIER=1.5`
3. **Disable jitter**: Set `MODEL_RETRY_JITTER=false`

#### Want to Disable Retries?

Set `MODEL_MAX_RETRIES=0` in your `.env` file.

### Verification

You can verify the retry mechanism is working correctly by running:

```bash
python tests/test_retry_utils.py
```

This will run a series of verification tests to ensure rate limit detection, backoff calculation, and retry logic are functioning properly.

To run with pytest instead:
```bash
pytest tests/test_retry_utils.py -v
```

---

## Multi-Agent Complex Workflows Evaluation & Regression Tracking

📄 Paper Reference: A Trace-Based Assurance Framework for Agentic AI Orchestration

**Paper**: [A Trace-Based Assurance Framework for Agentic AI Orchestration: Contracts, Testing, and Governance](https://arxiv.org/abs/2603.18096)  
**Authors**: Ciprian Paduraru, Petru-Liviu Bouruc, Alin Stefanescu  
**Submitted**: March 18, 2026  
**Subjects**: Multiagent Systems (cs.MA), Artificial Intelligence (cs.AI)

This paper presents a comprehensive assurance framework for Agentic AI systems where Large Language Models (LLMs) orchestrate multiple agents and interact with external services, retrieval components, and shared memory. The framework addresses failures beyond incorrect outputs, including:

- **Long-horizon interaction failures**: Non-termination, role drift across extended workflows
- **Stochastic decision propagation**: Unsupported claims spreading through agent interactions
- **External side effects**: API calls, database writes, and message sends causing unintended consequences
- **Security vulnerabilities**: Attacks via untrusted context or external channels

**Key Components**

1. **Message-Action Traces (MAT)**: Executions are instrumented as traces with explicit step and trace contracts that provide:
   - Machine-checkable verdicts on execution correctness
   - Localization of the first violating step for debugging
   - Deterministic replay capabilities for testing and reproduction

2. **Stress Testing Framework**: Formulated as budgeted counterexample search over bounded perturbations to identify failure modes systematically.

3. **Structured Fault Injection**: Tests system resilience by injecting faults at service, retrieval, and memory boundaries to assess containment under realistic operational faults and degraded conditions.

4. **Runtime Governance**: Enforces per-agent capability limits and action mediation (allow, rewrite, block) at the language-to-action boundary as a runtime component.

5. **Trace-Based Metrics**: Defines standardized metrics for comparative evaluations across stochastic seeds, models, and orchestration configurations:
   - Task success rates
   - Termination reliability
   - Contract compliance
   - Factuality indicators
   - Containment rate
   - Governance outcome distributions

### 📊 Golden Dataset Evaluation & Regression Tracking

#### Overview

The golden-dataset skill now includes comprehensive evaluation tracking to monitor quality, efficiency, and regressions across model updates in **multi-agent complex workflows**. Each run is logged as a JSONL record with rich metrics, enabling comparison between baseline and candidate implementations. This system tracks orchestrator agents, sub-agents, tool execution patterns, parameter validation, and self-correction behaviors across the entire multi-agent workflow.

#### Key Metrics Tracked

| Category | Metric | Description |
|----------|--------|-------------|
| **Test Pass Rate (Completeness)** | `completeness.pass` | Boolean flag: for the `golden-dataset` skill, `/golden_dataset_metrics.md` and `/final_report.md` are generated in the LangGraph state `files` sandbox. Percentage of code produced by the agent that passes automated tests if the `code-generator` skill is used. |
| | `completeness.has_golden_dataset_metrics_md` | Whether the quality metrics report was generated. |
| | `completeness.has_final_report_md` | Whether the final report was generated. |
| **Success Rate of Tool Execution** | `tool_execution.total_tool_calls` | Total number of tool invocations across the entire run. |
| | `tool_execution.successful_tool_calls` | Count of tool calls that returned valid responses. |
| | `tool_execution.failed_tool_calls` | Count of tool calls that failed or returned error content. |
| | `tool_execution.success_rate` | Reliability of the agent in using tools (e.g., search, file editing) correctly. If the number of tool calls significantly increases, it should be considered a failure. Ratio of successful to total tool calls. (1.0 if no calls made) |
| | `tool_execution.retry_count` | Number of times a tool was retried after initial failure. |
| | `tool_execution.unique_tools_with_errors` | Count of distinct tools that encountered errors. |
| | `tool_execution.tools_corrected_count` | Count of tools where the agent successfully self-corrected after failure. |
| **Parameter Validation Quality** | `parameter_validation.average_quality_score` | Average quality score (0-1) of tool call parameters across all invocations. Higher is better. |
| | `parameter_validation.valid_parameter_rate` | Ratio of tool calls with valid/required parameters present. Measures parameter completeness. |
| | `parameter_validation.total_calls_analyzed` | Total number of tool calls analyzed for parameter quality. |
| | `parameter_validation.calls_with_missing_params` | Count of tool calls missing required parameters. Lower is better. |
| **Error Rate/Failure Rate** | `failure.intervention_required` | Boolean. True if completeness failed, stream fallback was used, or tool failure rate > 0. Frequency of failures requiring human intervention. |
| | `failure.failure_rate` | Ratio: 1.0 if intervention needed, 0.0 otherwise. |
| **Self-Correction Behavior** | `self_correction.correction_events` | Total number of times the agent detected and corrected a tool failure. |
| | `self_correction.self_correction_rate` | Ratio of tools with errors that were successfully corrected (0-1). Higher indicates better recovery capability. |
| | `self_correction.tools_attempted_correction` | List of tool names where the agent attempted self-correction. |
| | `self_correction.correction_types` | Types of corrections observed: `retry_same_tool` (retried with different params) or `alternative_tool` (switched to different tool). |
| **Token Efficiency/Cost Per Task** | `token_efficiency.available` | Boolean. Whether token usage metadata was captured. |
| | `token_efficiency.prompt_tokens` | Total input tokens across all messages. |
| | `token_efficiency.completion_tokens` | Total output tokens across all messages. |
| | `token_efficiency.total_tokens` | Sum of prompt and completion tokens. Monitoring the cost effectiveness of the orchestration. |
| | `token_efficiency.tokens_per_successful_task` | Aggregate token count if completeness passed, else null. |
| **Latency** | `latency.runtime_seconds` | End-to-end execution time in seconds. Time taken to complete a complex task. |

**Parameter Analysis Details**:
- **File operations** (`read_file`, `write_file`): Checks for `path` or `file_path` arguments
- **Search operations** (`tavily_search`): Checks for `query` or `search_query` arguments
- **Thinking operations** (`think_tool`): Checks for `thought` or `reasoning` arguments
- **Generic tools**: Validates at least one non-empty argument is provided
- **Quality scoring**: 0.5 base for required params + up to 0.5 bonus for multiple well-formed arguments

**Self-Correction Detection Logic**:
- **Retry detection**: Agent calls same tool again after a failure (indicates parameter adjustment)
- **Alternative tool**: Agent switches to a different tool after failure (indicates strategy change)
- **Success tracking**: Monitors if retry/alternative led to successful execution
- **Recovery rate**: Measures how often the agent recovers from its own mistakes

#### Run Record Structure

Each JSONL entry contains:
```json
{
  "timestamp_utc": "2026-04-23T10:15:30.123456+00:00",
  "run_type": "baseline",
  "manifest": {
    "subject": "Generate 10 question-answer pairs...",
    "skill": "golden-dataset",
    "doc_folder": "./docs/policy",
    "no_web": false,
    "model_name": "claude-sonnet-4-5-20250929",
    "verify_ssl": "True"
  },
  "manifest_hash": "a1b2c3d4e5f6g7h8...",
  "model_name": "claude-sonnet-4-5-20250929",
  "git_sha": "abc1234",
  "runtime_seconds": 45.2,
  "stream_fallback_used": false,
  "output_file": "./output/bmo_policy_qa_pairs-2026-04-23_10_15_30.md",
  "metrics": { /* as detailed above */ }
}
```

#### Manifest & Comparison Logic

**Manifest Hash**: Canonical SHA256 hash of the test case (subject, skill, doc_folder, model, etc.). Used for **same-input comparisons only**.

**Critical Design**:
- `"Generate 5 pairs..."` vs `"Generate 10 pairs..."` are **non-comparable** (different manifest hashes).
- Only runs with identical manifests are compared.
- Prevents false regressions when comparing different test cases.

#### Usage: Create Baseline & Evaluate Candidate

##### Step 1: Record a Baseline
```bash
uv run python research_agent_cli.py \
  "Generate 5 question-answer pairs for the documents provided" \
  --doc-folder ./docs/policy/ \
  --skill golden-dataset \
  --eval-golden-dataset \
  --eval-mode baseline
```

Output: JSONL entry appended to `./output/eval_history/golden_dataset_runs.jsonl`

##### Step 2: Run a Candidate (Same Input)
```bash
uv run python research_agent_cli.py \
  "Generate 5 question-answer pairs for the documents provided" \
  --doc-folder ./docs/policy/ \
  --skill golden-dataset \
  --eval-golden-dataset \
  --eval-mode candidate
```

**Comparison Output**:
- Fetches latest baseline with matching manifest hash.
- Computes per-metric verdicts: `better`, `same`, `worse`, or `unavailable`.
- Logs verdict summary to stdout.
- Overall verdict combines all metrics:
  - `better` if any metric improved and none regressed.
  - `worse` if any metric degraded (e.g., completeness dropped, failure rate increased, tool calls > baseline * 1.30, parameter quality decreased).
  - `same` if no significant change.

**Compared Metrics**:
- **Completeness**: Pass/fail status for required artifacts
- **Tool Execution**: Total tool call count growth (>30% = worse)
- **Failure Rate**: Intervention requirement changes
- **Token Efficiency**: Token usage changes (>20% increase = worse)
- **Latency**: Runtime changes (>15% increase = worse)
- **Parameter Validation**: Average quality score changes (>10% difference = better/worse)
- **Self-Correction**: Correction rate improvements/regressions

##### Step 3: Custom History File
```bash
uv run python research_agent_cli.py \
  "Generate 5 question-answer pairs for the documents provided" \
  --doc-folder ./docs/policy/ \
  --skill golden-dataset \
  --eval-golden-dataset \
  --eval-history-file ./output/my_eval_runs.jsonl
```

#### Regression Thresholds (Built-In)

| Metric | Threshold | Condition |
|--------|-----------|-----------|
| **Tool Growth** | 30% | Candidate tool_calls > baseline * 1.30 → **worse** |
| **Token Efficiency** | 20% | Candidate total_tokens > baseline * 1.20 → **worse** |
| **Latency** | 15% | Candidate runtime_seconds > baseline * 1.15 → **worse** |
| **Parameter Quality** | 10% | Candidate avg_quality_score < baseline * 0.90 → **worse**; > baseline * 1.10 → **better** |
| **Self-Correction Rate** | N/A | Any improvement → **better**; any decrease → **worse** |

#### Programmatic Access

```python
from research_agent.utils.eval_tracking import (
    build_manifest,
    collect_run_metrics,
    make_run_record,
    append_jsonl,
    load_jsonl,
    latest_baseline,
    compare_records,
)

# Build manifest
manifest = build_manifest(
    subject="Generate 5 pairs",
    skill="golden-dataset",
    doc_folder="./docs/policy",
    no_web=False,
    model_name="claude-sonnet-4-5-20250929",
    verify_ssl=True,
)

# Collect metrics from a run result
metrics = collect_run_metrics(
    result={"messages": [...], "files": {...}},
    runtime_seconds=45.2,
    stream_fallback_used=False,
)

# Create run record
record = make_run_record(
    manifest=manifest,
    run_type="candidate",
    metrics=metrics,
    runtime_seconds=45.2,
    model_name="claude-sonnet-4-5-20250929",
    stream_fallback_used=False,
    output_file="./output/run.md",
    git_sha="abc1234",
)

# Append to history
history_path = Path("./output/eval_history/runs.jsonl")
append_jsonl(history_path, record)

# Load and compare
records = load_jsonl(history_path)
baseline = latest_baseline(records, manifest_hash=record["manifest_hash"])
comparison = compare_records(baseline=baseline, candidate=record)
print(f"Overall verdict: {comparison['overall_verdict']}")
print(f"Per-metric: {comparison['per_metric']}")
```

#### Testing

Run the evaluation tracking tests:
```bash
pytest tests/test_eval_tracking.py -v
```

Tests verify:
- Manifest hash stability and change detection.
- Completeness gating (both artifacts required).
- Tool-call success/failure parsing.
- Baseline selection (latest matching manifest).
- Non-comparable manifest mismatches.
- JSONL append and reload integrity.

### Server Operational Metrics Tracking

The evaluation tracking system also works with LangGraph Studio's development server for **operational metrics collection**. Unlike CLI regression testing (which compares same-input baselines), dev mode tracks facts across different inputs:

```bash
# Enable eval tracking for langgraph dev
export ENABLE_EVAL_TRACKING=true
export EVAL_HISTORY_FILE=./output/eval_history/server_runs.jsonl

# Start the dev server
langgraph dev
```

**What gets tracked** (facts only, no comparison):
- **Tool execution**: Total calls, success rate, retries, parameter quality
- **Self-correction**: Number of corrections, correction types
- **Token efficiency**: Prompt/completion tokens, total cost proxy
- **Latency**: Runtime in seconds
- **Context**: Model name, skill used, doc folder, subject (for reference)

**How it works**:
1. The `ResearchStateMiddleware.after_model()` hook detects when final artifacts are created
2. Metrics are automatically collected from the agent state
3. Simple JSONL records appended to history file (one per run)
4. Console summary printed after each completion

**Example output**:
```
✅ Metrics logged: 45.2s | 12 tools (92% success) | 8450 tokens | param quality: 0.85 | 2 corrections
```

**JSONL record structure**:
```json
{
  "timestamp_utc": "2026-04-26T10:15:30.123456+00:00",
  "model_name": "claude-sonnet-4-5-20250929",
  "context": {
    "subject": "Generate question-answer pairs...",
    "skill": "golden-dataset",
    "doc_folder": "./docs/policy",
    "no_web": false
  },
  "runtime_seconds": 45.2,
  "metrics": {
    "tool_execution": { ... },
    "parameter_validation": { ... },
    "self_correction": { ... },
    "token_efficiency": { ... },
    "latency": { ... }
  }
}
```

**Use cases**:
- Monitor agent performance trends over time
- Identify problematic patterns (low success rates, high retries)
- Track token usage and costs across different tasks
- Debug parameter quality issues
- Analyze self-correction behavior

**Note**: This is separate from CLI regression testing (`golden_dataset_runs.jsonl`). Dev mode tracks diverse inputs; CLI mode compares identical inputs.

### Next Steps & Backlog

The following advanced metrics are planned for future implementation to further enhance the evaluation system:

#### 1. Orchestration & Planning Metrics
- **Plan Quality Score**: Measures the logic, completeness, and feasibility of the initial `write_todos` output.
- **Plan Adherence Rate**: How often the agents actually follow the generated plan versus deviating into irrelevant sub-tasks.
- **Step Efficiency**: The ratio of the "optimal path" (minimum steps required) to the "actual path" taken.
- **Task Decomposition Accuracy**: Specifically for `task()` tools—did the orchestrator correctly isolate context and parameters for the sub-agent?

#### 2. Interaction & Coordination Metrics
- **Handoff Success Rate**: Measures the percentage of context preserved when data moves from an Orchestrator to a Sub-agent or between specialized skills.
- **Action Redundancy**: Frequency of duplicate tool calls (e.g., two agents calling `read_doc_folder` on the same file).
- **Inter-Agent Consistency**: Do different agents analyzing the same source material arrive at non-contradictory "Source of Truth" cached data?
- **Infinite Loop Detection**: Identifying recursive reasoning loops (e.g., two agents passing the same task back and forth).

#### 3. "Skill" Execution Metrics (Golden Dataset Specific)
- **Grounding Rate**: The percentage of claims in the final dataset that can be directly traced back to a source in the `read_doc_folder`.
- **Schema Compliance**: Percentage of outputs that pass the `render_target_output` validation without formatting errors.
- **Negative Constraint Adherence**: How well the agent avoids using `tavily_search` (or other web tools) when the instruction is to remain "local-only."
- **Finalization Latency**: Total time from `write_todos` to `finalize_golden_dataset_output`.

#### 4. Technical Efficiency & Cost
- **Cost per Success**: Total token/compute spend divided by the number of validated, unique golden data points generated.
- **Token Efficiency (Compression)**: The ratio of raw input tokens (PDFs/Spreadsheets) to the final structured output tokens.
- **Time-to-First-Action (TTFA)**: How fast the system moves from the user prompt to the first logical `think_tool` or `write_todos` step.
