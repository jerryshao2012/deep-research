# Ollama Model-Call Timeout and Strict Web Citations Design

## Goal

Prevent local Ollama requests from keeping research runs indefinitely active after the client stops or a model call stalls, and prevent web-enabled reports with placeholder or unresolved citations from passing verification.

The immediate runtime failure was an Ollama generation that continued consuming the model after the LangGraph run was stopped. The immediate quality failure was a completed report whose source list contained placeholder entries such as `Conceptual Source` and unresolved numbered references, yet verification still marked it as accepted.

## Scope

This change applies to every model invocation owned by the root research graph and its delegated research agents, plus final-report verification.

It must:

- bound each model call with `MODEL_CALL_TIMEOUT_SECONDS`, defaulting to 300 seconds;
- cancel the active model request when the timeout expires or the LangGraph run is cancelled;
- keep Ollama model unload disabled by default;
- optionally unload the active Ollama model when `OLLAMA_FORCE_UNLOAD_ON_CANCEL=true`;
- isolate unload errors from the original timeout or cancellation result;
- expose a clear timeout/cancellation failure instead of leaving the run indefinitely active;
- require concrete web citations whenever web research is enabled;
- reject placeholder sources and unresolved numbered references deterministically before the LLM judge runs; and
- preserve no-web and document-only research, which is not required to cite public URLs.

It does not change task planning, subagent concurrency, live nested-tool streaming, report ownership, completion retry limits, or frontend historical restoration.

## Considered Approaches

### 1. Middleware call guard plus deterministic verifier

Wrap the actual sync and async model handlers in middleware, and add a deterministic citation preflight to report verification.

This is selected because it covers root and delegated calls at the graph execution boundary, reacts to both timeout and cancellation, preserves provider independence, and makes citation acceptance reproducible.

### 2. ChatOllama client timeout plus prompt guidance

Configure only `ChatOllama` request timeouts and strengthen prompts asking the model to include valid URLs.

This does not cover every model provider or every call path, does not guarantee cancellation propagation, and cannot prevent the LLM judge from accepting placeholder citations.

### 3. External Ollama proxy or process supervisor

Put a proxy or scheduler between LangGraph and Ollama to enforce deadlines and evict models.

This adds deployment complexity and still leaves citation quality inside the application. It is unnecessary for the current local-development failure.

## Model-Call Guard

### Configuration

Add two environment variables:

- `MODEL_CALL_TIMEOUT_SECONDS`: positive finite duration in seconds; default `300`; `0` or a negative value disables the deadline; malformed or non-finite values fall back to `300`.
- `OLLAMA_FORCE_UNLOAD_ON_CANCEL`: boolean, default `false`; only explicit true values enable unloading.

Configuration is resolved once per guard instance. The timeout value remains provider-neutral. Force-unload logic is reachable only when the configured provider is Ollama and the opt-in flag is true.

### Sync and async execution

Introduce a small model-call guard used by both root and delegated agent middleware stacks. It wraps the handler itself, not model construction, so it covers every request made through the compiled graph.

For asynchronous calls, use an async deadline around the awaited handler. On deadline expiry, cancel the handler task, await its cancellation cleanup, then raise a dedicated `ModelCallTimeoutError`. On external task cancellation, preserve `CancelledError` after cleanup so LangGraph retains cancellation semantics.

For synchronous calls, execute the handler through a bounded worker future. Timeout stops waiting and attempts to cancel the future. Python cannot forcibly terminate a thread already inside a native HTTP call, so the optional Ollama unload is the bounded provider-side escape hatch. The caller still receives `ModelCallTimeoutError` immediately; a late worker result is ignored.

The dedicated timeout error subclasses `RuntimeError` and contains only safe operational text: provider, configured duration, and whether unload was requested. It does not include prompts, document content, credentials, or model responses.

### Ollama cancellation and optional unload

Cancellation always targets the active model request first. Default behavior performs no model unload.

When `OLLAMA_FORCE_UNLOAD_ON_CANCEL=true`, provider detection confirms Ollama, and a timeout or cancellation occurs, schedule one best-effort unload request for the active model. Use the configured Ollama base URL and model name, with a short independent timeout. The unload request uses Ollama's local generate endpoint with `keep_alive: 0` and no research prompt content.

Unload behavior must:

- never run for Anthropic, OpenAI, Gemini, Azure, or another provider;
- never run for Ollama unless the flag is explicitly enabled;
- be bounded so an unavailable Ollama server cannot delay cancellation indefinitely;
- run at most once for a guarded call; and
- log a warning on failure without replacing the original timeout or cancellation exception.

Concurrent Ollama calls may share a model. Force unload is therefore intentionally an operator opt-in: it can terminate model residency needed by sibling calls, while the safe default only cancels the current request.

### Placement and ordering

Register the guard at the outer execution boundary of both the root research model and delegated research subagent model calls. Existing request-rewriting middleware still builds the final `ModelRequest`; the guard surrounds the resulting handler invocation. This preserves system-message ordering required by strict Ollama templates and does not persist control messages.

Timeout does not trigger the completion guard's automatic continuation. It is an execution failure, not a valid terminal model response. Existing state and checkpoints remain available for an explicit user retry or resume.

## Deterministic Web-Citation Gate

### When strict checks apply

Strict checks apply only when the current research request permits web research. Verification must receive explicit request context rather than infer web usage from report prose.

No-web and document-only runs are exempt from the URL requirement. They continue through existing report sufficiency and document-grounding checks.

### Required citation properties

Before invoking the LLM judge, parse the final report and require:

1. at least one concrete `http://` or `https://` source URL;
2. no placeholder source labels or targets, including case-insensitive forms such as `Conceptual Source`, `placeholder`, `example source`, `source needed`, `citation needed`, `TBD`, or equivalent empty/non-URL source entries; and
3. no unresolved numbered references such as `[1]`, `[2]`, or ranges whose referenced source number has no corresponding concrete URL-bearing source entry.

Markdown links, bare URLs, and URL-bearing numbered source-list entries are accepted. Duplicate URLs are normalized for counting but are not themselves failures. Non-web document references may coexist with web citations.

The gate checks citation structure and resolvability. Existing citation validation remains responsible for URL reachability and claim support when fetched content is available.

### Verdict and revision feedback

Any deterministic defect returns `needs_revision` immediately without calling the LLM judge and marks the verdict as citation-blocking. Feedback lists bounded, actionable defect categories and reference numbers, for example:

- `No concrete HTTP(S) source URL found.`
- `Placeholder source entry found: Conceptual Source.`
- `Unresolved numbered citations: [2], [5].`

Feedback is injected through the existing ephemeral verification-feedback path. It must not persist a mid-history `SystemMessage`, and the normal verification-round limit still bounds revision attempts. A corrected report is rechecked from scratch.

Citation-blocking defects are non-waivable. If they remain on the final verification round, raise a dedicated safe `ReportCitationError` after checkpointing the bounded defect categories. Existing accepted-at-verification-limit behavior remains available only for non-structural LLM-judge findings. This guarantees a web-enabled report with missing, placeholder, or unresolved citations cannot be finalized as verified.

### Request context propagation

Carry a boolean strict-web-citation requirement from research state into both sync and async verification calls. The value derives from the authoritative request setting used to disable web research. Do not infer it from whether Tavily happened to return results: a web-enabled run that failed to fetch sources must still revise its report or explicitly fail verification.

## Failure and Cancellation Semantics

- Model timeout: run fails with `ModelCallTimeoutError`; no completion continuation is consumed.
- External cancellation: cancellation propagates unchanged after bounded cleanup.
- Default Ollama cancellation: cancel request only; model remains loaded according to Ollama policy.
- Opt-in Ollama cancellation: cancel request, then best-effort bounded unload.
- Unload failure: warning only; original timeout/cancellation outcome wins.
- Citation preflight failure: verification returns citation-blocking `needs_revision` and uses the existing bounded revision flow.
- Citation failure at final verification round: run fails with `ReportCitationError`; report is not streamed or marked verified.

## Testing

Tests are written before production changes.

### Model-call guard

- valid default, positive override, disabled nonpositive values, and malformed/non-finite fallback;
- synchronous handler returns before deadline;
- synchronous handler times out with the dedicated safe error;
- asynchronous handler returns before deadline;
- asynchronous handler is cancelled and awaited on timeout;
- external async cancellation remains `CancelledError`;
- default Ollama timeout performs no unload;
- explicit Ollama opt-in performs one bounded unload with the active model and configured base URL;
- Ollama cancellation also invokes opt-in unload;
- unload is never called for cloud providers;
- unload failure preserves the original timeout/cancellation exception;
- timeout is registered for root and delegated compiled graph paths; and
- completion-continuation counters do not advance on timeout.

### Citation preflight

- web-enabled report with a concrete URL passes preflight;
- report with no URL fails before the LLM judge;
- `Conceptual Source` and other placeholder variants fail;
- unresolved numbered citation fails with exact reference numbers;
- numbered references backed by URL-bearing source entries pass;
- markdown links and bare URLs pass;
- duplicate URLs do not create a false failure;
- no-web request is exempt;
- document-only request is exempt;
- sync and async verification paths receive identical strictness; and
- revision feedback remains ephemeral and respects the configured round limit;
- citation-blocking verdict cannot use accepted-at-limit finalization; and
- final-round citation failure checkpoints safe defect metadata, raises `ReportCitationError`, and does not stream the report.

Run focused guard, model-factory, citation, verification, completion, and agent-contract suites first, followed by Ruff, compile checks, and the full test suite. Provider/network calls are replaced with local fakes; tests must not require a running Ollama server.

## Operational Notes

Recommended local settings:

```bash
export MODEL_CALL_TIMEOUT_SECONDS=300
export OLLAMA_FORCE_UNLOAD_ON_CANCEL=true
```

The force-unload flag is useful on a single-user local Ollama instance where releasing a stuck model is preferable to preserving concurrent calls. Shared Ollama servers should keep the default `false` value.

A backend restart is required after changing either variable because local development runs with `--no-reload`.
