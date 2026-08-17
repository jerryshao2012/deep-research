# Configure Deep Research

Use this guide to configure model access, research behavior, storage limits, verification, evaluation, authentication, and Thread Wiki ingestion. Runtime names and defaults below follow current code; where `.env.example` or the former README differs, the runtime behavior is authoritative.

## Prepare the environment

Install dependencies and copy only settings you need into a repository-root `.env` file. The CLI, agent, and web application load that file automatically; keep credentials in local environment variables or a deployment secret store, never in version control.

At minimum, configure one supported chat-model provider. Web-enabled research also requires `TAVILY_API_KEY`; omit it only when every run uses `--no-web`.

## Configure a model provider

`research_agent/model_factory.py` selects the first complete configuration in this order: AWS Bedrock-compatible endpoint, Azure OpenAI with an explicit API version, Azure OpenAI without an API version, Google, Anthropic, then Ollama. `MODEL_NAME` has no implicit runtime default and is required for AWS, Google, Anthropic, and Ollama.

| Provider | Required variables | Notes |
| --- | --- | --- |
| AWS Bedrock-compatible endpoint | `AWS_BEDROCK_ENDPOINT`, `AWS_BEARER_TOKEN_BEDROCK`, `MODEL_NAME` | Uses the OpenAI-compatible chat client. |
| Azure OpenAI, API key | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY` | Add `AZURE_OPENAI_API_VERSION` to use the explicit-version Azure client. |
| Azure OpenAI, managed identity | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`, `AZURE_AUTH_TYPE=managed_identity`, `AZURE_CLIENT_ID`, `AZURE_OPENAI_SCOPE` | Managed identity is supported only by the explicit-version branch. |
| Google Gemini | `GOOGLE_API_KEY`, `MODEL_NAME` | Chat temperature is fixed to `0.0`. |
| Anthropic | `ANTHROPIC_API_KEY`, `MODEL_NAME` | Chat temperature is fixed to `0.0`. |
| Ollama | `OLLAMA_API_BASE`, `MODEL_NAME` | `OLLAMA_BASE_URL` and `OLLAMA_MODEL` are stale sample names and are not read by the model factory. |

### Configure embeddings

Embedding selection is separate from chat-model selection. Providers are tried in the order below; initialization failure falls through to the next configured provider.

| Provider | Selection requirements | Model behavior and authentication |
| --- | --- | --- |
| Azure OpenAI | `AZURE_EMBEDDING_NAME`, `AZURE_EMBEDDING_DEPLOYMENT_NAME`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION` | No embedding name or deployment default. API-key auth is the default and requires `AZURE_OPENAI_API_KEY`; managed identity requires `AZURE_AUTH_TYPE=managed_identity` and `AZURE_OPENAI_SCOPE`, with `AZURE_CLIENT_ID` optional for a user-assigned identity. |
| OpenAI | `OPENAI_API_KEY` | Uses the fixed `text-embedding-3-small` model. This key does not select an OpenAI chat model. |
| Google | `GOOGLE_API_KEY` | Uses the fixed `models/gemini-embedding-001` model. |
| Ollama | `OLLAMA_API_BASE` | Model resolves from `EMBEDDING_MODEL_NAME`, then `MODEL_NAME`, then `nomic-embed-text`. |
| Local fallback | No provider initializes | Uses deterministic `SimpleLocalEmbeddings` with 1,536 dimensions. |

Configure `TAVILY_API_KEY` for web search. A missing key raises an error only when the research workflow actually calls Tavily.

### Configure TLS verification

`VERIFY_SSL` defaults to `true`. For a corporate CA, leave verification enabled and set a bundle path; the resolver checks `SSL_CAINFO`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, then `CURL_CA_BUNDLE`.

The CLI option `--ssl-ca-files` is parsed but not applied by the current TLS resolver. Use one of the environment variables above; disable verification only for controlled diagnosis.

## Tune the research workflow

| Variable | Runtime default | Purpose |
| --- | ---: | --- |
| `MAX_CONCURRENT_RESEARCH_UNITS` | `3` | Maximum concurrent delegated research units. |
| `MAX_RESEARCHER_ITERATIONS` | `3` | Maximum iterations inside each researcher unit. |
| `GRAPH_RECURSION_LIMIT` | `200` | LangGraph execution recursion limit. |
| `MAX_RESUME_ROUNDS` | `3` | Maximum rounds for resuming incomplete user-triggered work. |
| `ENABLE_REQUIREMENT_CLARIFICATION` | `true` | Emergency switch for requirement clarification; capable clients can still choose per-run behavior. |

These controls bound agent work; they do not change model-provider RPM or TPM quotas. See [Reliability](reliability.md) for request shaping and retries.

## Limit file input and output

| Variable | Runtime default | Purpose |
| --- | ---: | --- |
| `REPORTS_OUTPUT_FOLDER` | `./output` | Base folder used for reports. The CLI derives `OUTPUT_FOLDER` from it. |
| `MAX_GLOB_DEPTH` | `3` | Maximum relative depth accepted during folder discovery. |
| `MAX_FILES_TO_READ` | `20` | Maximum files read in one folder operation. |
| `MAX_TOTAL_SIZE_MB` | `50` | Maximum aggregate size before folder sampling applies. |
| `MAX_INLINE_FILE_CHARS` | `40000` | Maximum content returned inline before large-file handling. |
| `LARGE_FILE_PREVIEW_CHARS` | `12000` | Preview size for a large file. |
| `LARGE_FILE_HEADING_LIMIT` | `24` | Maximum headings included in a large-file outline. |
| `SECTION_CHUNK_LIMIT` | `3` | Maximum section chunks selected per file operation. |

`DOC_FOLDER` and `OUTPUT_FOLDER` are also set by the CLI for the current process. Prefer `--doc-folder` and `REPORTS_OUTPUT_FOLDER` instead of setting those transient variables manually.

## Configure verification

| Variable | Runtime default | Purpose |
| --- | ---: | --- |
| `ENABLE_VERIFICATION` | `true` | Run post-generation report verification. |
| `MAX_VERIFICATION_ROUNDS` | `2` | Maximum feedback-and-revision rounds. |

Verification applies after a non-empty `/final_report.md` is written. It adds model and citation-check latency; see [Evaluation](evaluation.md#operate-the-verification-loop) for verdict rules and failure behavior.

## Configure rate limits and retries

| Variable | Runtime default | Purpose |
| --- | ---: | --- |
| `MODEL_TPM` | `120000` | Provider token quota supplied to proactive shaping. |
| `MODEL_RPM` | `500` | Provider request quota supplied to proactive shaping. |
| `MODEL_CALL_TIMEOUT_SECONDS` | `300` | Total wall-clock deadline for each model call; invalid or nonpositive values fall back to `300`. |
| `OLLAMA_FORCE_UNLOAD_ON_CANCEL` | `false` | Request Ollama model unload after cancellation; keep disabled for shared or cloud deployments. |
| `MODEL_MAX_RETRIES` | `5` | Retries after the initial rate-limited call. |
| `MODEL_INITIAL_BACKOFF` | `1.0` | Initial nominal delay in seconds. |
| `MODEL_MAX_BACKOFF` | `60.0` | Maximum nominal delay in seconds. |
| `MODEL_BACKOFF_MULTIPLIER` | `2.0` | Exponential delay multiplier. |
| `MODEL_RETRY_JITTER` | `true` | Randomize each wait to 50-100% of its nominal delay. |

Set TPM and RPM to actual deployment quotas, not desired throughput. Both must be positive for proactive shaping to run; reactive retries are configured independently.

## Configure evaluation tracking

| Variable | Runtime default | Purpose |
| --- | ---: | --- |
| `ENABLE_EVAL_TRACKING` | `true` | Append operational metrics after final report creation. |
| `EVAL_HISTORY_FILE` | `./output/eval_history/server_runs.jsonl` | Operational JSONL destination. |
| `EVAL_LOG_QUESTIONS` | `false` | Store raw user subjects when true; otherwise use `[REDACTED]`. |
| `EXPERIMENT_ID` | unset | Group operational records into an experiment. |
| `EXPERIMENT_VARIANT` | unset | Label a deployment variant such as `control` or `treatment`. |

These variables control server-style operational tracking. Golden-dataset baseline and candidate scoring uses the separate `score_dataset.py --eval-mode` workflow described in [Evaluation](evaluation.md#compare-a-golden-dataset-baseline-and-candidate).

## Configure authentication and the web application

| Variable | Runtime default | Purpose |
| --- | --- | --- |
| `UPLOAD_API_KEY` | unset | Preferred stable key for protected custom webapp routes. |
| `LANGCHAIN_API_KEY` | unset | LangGraph key and custom-webapp fallback. |
| `UPLOAD_HOST` | `0.0.0.0` | Host used by the package's internal launcher; explicit Uvicorn arguments take precedence. |
| `UPLOAD_PORT` | `8000` | Port used by the package's internal launcher. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | unset | Google OAuth client credentials. |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | unset | Default GitHub OAuth client credentials. |
| `FRONTEND_URLS` | unset | Comma-separated frontend redirect allowlist prepended to built-in origins. |
| `OAUTH_SECRET_KEY` | process-local random value | Session-cookie signing secret; a stable 32-4096 byte secret is mandatory when passkeys are enabled and recommended for every deployment. |

Authentication key precedence differs by surface. LangGraph resolves `LANGCHAIN_API_KEY` then `UPLOAD_API_KEY` and fails configuration if neither is set; protected custom webapp routes resolve `UPLOAD_API_KEY`, then `LANGCHAIN_API_KEY`, then generate a process-local key. See [Authentication](authentication.md) before exposing either service.

Auth persistence defaults to `DB_TYPE=sqlite` and an in-memory `SQLITE_DB_PATH` when unset. Use `AUTH_STORE_TYPE` to override the auth backend independently; supported values are SQLite, PostgreSQL, and Cosmos DB. Passkeys require durable configuration.

## Configure Thread Wiki and code ingestion

### Set agent and content limits

| Variable | Runtime default | Purpose |
| --- | ---: | --- |
| `WIKI_AGENT_RECURSION_LIMIT` | `100` | Maximum tool-calling turns per wiki agent invocation. |
| `WIKI_AGENT_TIMEOUT_SECONDS` | `300` | Overall wiki-agent timeout. |
| `WIKI_INGEST_PHASE_TIMEOUT_SECONDS` | `600` | Timeout for an ingest phase. |
| `WIKI_QUERY_TIMEOUT_SECONDS` | `180` | Query timeout. |
| `WIKI_LINT_TIMEOUT_SECONDS` | `300` | Lint timeout. |
| `WIKI_INGEST_MAX_RETRY` | `3` | Retry count for ingestion progress recovery. |
| `WIKI_MAX_CHUNK_CHARS` | `40000` | Maximum source chunk size. |
| `WIKI_CHUNK_OVERLAP_CHARS` | `2000` | Character overlap between adjacent chunks. |
| `WIKI_CONTEXT_MAX_CHARS` | `512000` | Context budget used by the wiki service. |
| `WIKI_BASE_DIR` | project/docs resolution | Explicit base directory override; also influences custom webapp `DOCS_ROOT`. |

Former samples list different timeout values and the names `WIKI_INDEX_REPAIR_TIMEOUT_SECONDS` and `WIKI_INGEST_MAX_WAIT_SECONDS`. Current runtime does not consume those two names, so do not rely on them.

### Set code and repository-import limits

| Variable | Runtime default | Purpose |
| --- | ---: | --- |
| `WIKI_CODE_AST_ENABLED` | `true` | Parse recognized whole source files with bundled Tree-sitter grammars. |
| `WIKI_EMBEDDED_CODE_AST_ENABLED` | `true` | Parse supported language-tagged code fences. |
| `WIKI_CODE_PARSE_MAX_BYTES` | `2097152` | Per-file AST parsing cap; larger sources use text fallback. |
| `WIKI_GIT_ALLOWED_HOSTS` | `github.com,gitlab.com,bitbucket.org` | Public repository host allowlist. |
| `WIKI_GIT_IMPORT_TIMEOUT_SECONDS` | `120` | Clone/import timeout. |
| `WIKI_GIT_IMPORT_MAX_FILES` | `5000` | Maximum validated checkout files. |
| `WIKI_GIT_IMPORT_MAX_BYTES` | `104857600` | Maximum validated checkout bytes. |

The ingestion pipeline parses uploaded code; it does not execute source files. For endpoint workflows and safety boundaries, see the [Thread Wiki API](../api/wiki.md) and [code-ingestion architecture](../architecture/code-ingestion.md).

## Related documentation

- [Installation](../getting-started/installation.md)
- [Usage](../getting-started/usage.md)
- [Authentication](authentication.md)
- [Reliability](reliability.md)
- [Evaluation](evaluation.md)
- [Thread Wiki API](../api/wiki.md)
- [Handbook index](../README.md)
