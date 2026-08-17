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

### 1. Guarded model plus request-boundary adapter and deterministic verifier

Wrap models at construction, idempotently guard any late request-model override, configure native provider deadlines, and add a deterministic citation preflight to report verification.

This is selected because it covers graph, inherited subagent, override, and direct judge calls, reacts to both timeout and cancellation, preserves provider independence, and makes citation acceptance reproducible.

### 2. ChatOllama client timeout plus prompt guidance

Configure only `ChatOllama` request timeouts and strengthen prompts asking the model to include valid URLs.

This does not cover every model provider or every call path, does not guarantee cancellation propagation, and cannot prevent the LLM judge from accepting placeholder citations.

### 3. External Ollama proxy or process supervisor

Put a proxy or scheduler between LangGraph and Ollama to enforce deadlines and evict models.

This adds deployment complexity and still leaves citation quality inside the application. It is unnecessary for the current local-development failure.

## Model-Call Guard

### Configuration

Add two environment variables:

- `MODEL_CALL_TIMEOUT_SECONDS`: positive finite duration in seconds; default `300`; missing, malformed, non-finite, zero, or negative values fall back to `300`. This change has no timeout-disable mode.
- `OLLAMA_FORCE_UNLOAD_ON_CANCEL`: boolean, default `false`; only explicit true values enable unloading.

Model selection returns authoritative immutable runtime metadata alongside the guarded model: selected provider, active model name, normalized base URL when applicable, and resolved timeout. Metadata comes from the provider branch that actually won model-factory precedence, not from the mere presence of environment variables. A final `ModelRequest.model` override is wrapped from that concrete model's metadata before invocation. Mixed-provider environments therefore cannot cause an OpenAI or Azure request to be treated as Ollama.

Configuration is resolved when the model guard is created. Force-unload logic is reachable only when the final active model is Ollama and the opt-in flag is true.

### Sync and async execution

Introduce a `GuardedChatModel` wrapper plus an idempotent request-boundary adapter. The model factory returns guarded models, and the request adapter wraps a late `ModelRequest.model` override only when it is not already guarded. This combination covers inherited models and final request overrides without applying two deadlines.

Every provider adapter also receives its SDK's native HTTP request deadline, but native inactivity timeouts are only a second line of defense. Total model-call duration is measured from invocation start through final response or final streamed chunk, including retries and token trickle.

All application-owned sync entrypoints, including the packaged CLI and synchronous compiled-graph calls, use a synchronous bridge over the guarded provider's async invocation. The bridge creates a dedicated daemon thread with its own event loop, propagates the current context, and exposes its request task and loop to the caller. At the total wall-clock deadline, the caller schedules task cancellation on that loop, waits only the cleanup grace, performs optional bounded unload, and raises `ModelCallTimeoutError`. Supported provider async transports close their request on cancellation. The daemon fallback prevents a cancellation-suppressing third-party coroutine from blocking interpreter shutdown, while late exceptions are consumed. No application-owned path calls a raw synchronous provider transport.

For asynchronous calls, create a task for the model request and race it against the resolved deadline. On timeout or external cancellation, cancel the request task so the async HTTP client closes the active response/connection. Wait for cleanup only for a short fixed grace period using `asyncio.wait`, which does not wait indefinitely for a cancellation-suppressing handler. If the task remains pending, detach it, attach a callback that consumes any late exception, and preserve the timeout or `CancelledError` as the authoritative result.

Async tests include a local streaming HTTP server whose disconnect event proves timeout and cancellation close the client request. A fake handler that suppresses cancellation proves the cleanup grace is bounded and late exceptions are consumed. Sync-bridge tests use a slow trickle stream whose chunks arrive before SDK inactivity timeout but whose total duration exceeds the configured deadline; they assert bounded end-to-end elapsed time, server disconnect, bridge-thread exit for each supported provider adapter, and normalized `ModelCallTimeoutError`.

The dedicated timeout error subclasses `RuntimeError` and contains only safe operational text: provider, configured duration, and whether unload was requested. It does not include prompts, document content, credentials, or model responses.

### Ollama cancellation and optional unload

Cancellation always targets the active model request first. Default behavior performs no model unload.

When `OLLAMA_FORCE_UNLOAD_ON_CANCEL=true`, final-model metadata confirms Ollama, and a timeout or cancellation occurs, schedule one shielded best-effort unload request for the active model. Use the normalized base URL and exact model name from that metadata, with a two-second independent timeout. The unload request uses Ollama's local generate endpoint with `keep_alive: 0` and no research prompt content.

Unload behavior must:

- never run for Anthropic, OpenAI, Gemini, Azure, or another provider;
- never run for Ollama unless the flag is explicitly enabled;
- be bounded so an unavailable Ollama server cannot delay cancellation indefinitely;
- run at most once for a guarded call; and
- log a warning on failure without replacing the original timeout or cancellation exception.

Concurrent Ollama calls may share a model. Force unload is therefore intentionally an operator opt-in: it can terminate model residency needed by sibling calls, while the safe default only cancels the current request.

### Placement and ordering

Coverage is explicit:

- root research model receives the guarded factory model;
- explicit `research-agent` receives the same guarded model or a separately configured guarded model;
- DeepAgents' automatic `general-purpose` subagent inherits the guarded root model;
- late root or subagent `ModelRequest.model` overrides pass through the idempotent request adapter;
- sufficiency and adversarial verification judges obtain guarded models from the same factory; and
- any skill-local model factory used by the active application path must call the shared guarded factory rather than construct a raw provider client.

Existing request-rewriting middleware still builds the final `ModelRequest`; timeout wrapping changes neither message content nor role order. Strict Ollama templates therefore keep system content first, and no timeout control message is persisted.

The wrapper preserves LangChain runnable behavior:

- `bind_tools()` and `bind()` delegate to the inner model/runnable and return another guarded wrapper carrying the same immutable runtime metadata;
- `invoke()` and `ainvoke()` apply one total deadline;
- `stream()` and `astream()` pass through chunks unchanged while applying one deadline from iterator creation through exhaustion, and close/cancel the underlying iterator on timeout or cancellation; and
- configuration, callbacks, tags, response metadata, tool-call chunks, and usage metadata pass through unchanged.

The idempotent marker survives binding so DeepAgents cannot accidentally unwrap the guard while adding tools. Compiled root and subagent regressions must prove tool binding, complete tool-call messages, and live nested streaming remain unchanged.

Late model overrides follow an explicit adapter registry. Already guarded models are accepted unchanged. Known supported LangChain provider classes are inspected and rebuilt with authoritative provider/model/base-URL metadata plus native timeout, then wrapped. A custom override may implement the documented `ModelRuntimeDescriptor` protocol to supply equivalent immutable metadata and an async cancellable runnable. Unknown raw overrides are rejected before invocation with safe `UnsupportedModelOverrideError`; they are never guessed as Ollama, never unloaded, and never allowed to bypass the deadline.

Timeout does not trigger the completion guard's automatic continuation. It is an execution failure, not a valid terminal model response. Existing state and checkpoints remain available for an explicit user retry or resume.

## Deterministic Web-Citation Gate

### When strict checks apply

Strict checks apply whenever the current request permits web research, independently of `ENABLE_VERIFICATION` and `MAX_VERIFICATION_ROUNDS`. Structural citation validation is an artifact-acceptance invariant; optional LLM sufficiency and adversarial judging remain separately configurable.

Declare raw `no_web` as a LangGraph `EphemeralValue` input channel. It exists only in the step immediately after a caller supplies it and is absent when a later request omits it, even on a checkpointed thread. At `before_agent`, normalize that ephemeral value with the shared boolean parser, default absence to `false`, and copy it into hidden, input-omitted per-generation effective state with current run ID. All later prompt, tool, verification, and metrics logic reads the effective field rather than raw `no_web`. Internal continuation jumps retain effective state; each visible request overwrites it. Tests cover web→no-web, no-web→web, and prior true→omitted transitions on one checkpointed thread.

`document-only` means document context is present **and** normalized `no_web` is true. Documents alone do not exempt a web-enabled run. No-web and document-only runs skip only the public-URL structural gate; they retain existing report sufficiency and document-grounding behavior when LLM verification is enabled.

### Required citation properties

Before invoking the LLM judge, remove fenced code blocks and parse report Markdown under the following grammar:

1. at least one concrete `http://` or `https://` source URL;
2. no placeholder source labels or targets inside a source entry, including case-insensitive forms such as `Conceptual Source`, `placeholder`, `example source`, `source needed`, `citation needed`, or `TBD`; and
3. no unresolved numbered references such as `[1]`, `[2]`, or ranges whose referenced source number has no corresponding concrete URL-bearing source entry.

Source sections begin under Markdown headings named `Sources`, `References`, `Bibliography`, or `Works Cited`, case-insensitively, and end at the next heading of equal or higher level. Numbered entries accept `[1]`, `1.`, or Markdown reference-definition forms. Valid URLs are Markdown link destinations or bare URLs whose parsed scheme is HTTP(S), whose authority is non-empty, and whose hostname is neither `example.com`, `example.org`, `example.net`, nor `localhost`, nor a subdomain of those hosts, nor any host ending in `.example`, `.invalid`, `.test`, or `.localhost`. Trailing punctuation is excluded from the URL.

Inline `[1](https://host/path)` links resolve themselves. Prose citations accept single references, comma/semicolon groups, and ascending ranges such as `[1, 3]` or `[2-4]`; each expanded number must map to a concrete URL-bearing source entry. Reference-like text inside source sections, Markdown links, escaped text, and fenced code is not counted as an unresolved prose citation.

Duplicate URLs are normalized for counting but are not failures. Non-URL document, book, or internal-file references may coexist in a source section and are not placeholders merely because they lack a URL; they do not satisfy the web-enabled run's minimum concrete-URL requirement. Placeholder detection is limited to recognized source entries and link labels/targets so ordinary prose is not falsely rejected.

The gate checks citation structure and resolvability. Existing citation validation remains responsible for URL reachability and claim support when fetched content is available.

### Verdict and revision feedback

Any deterministic defect returns `needs_revision` immediately without calling either LLM judge and marks the verdict as citation-blocking. The structural gate still runs when optional LLM verification is disabled or configured for zero rounds. Feedback lists bounded, actionable defect categories and reference numbers, for example:

- `No concrete HTTP(S) source URL found.`
- `Placeholder source entry found: Conceptual Source.`
- `Unresolved numbered citations: [2], [5].`

Feedback is injected through the existing ephemeral verification-feedback path. It must not persist a mid-history `SystemMessage`. When LLM verification is disabled or has zero rounds, structural citation checking still receives one correction opportunity before hard failure; otherwise the configured verification-round limit bounds correction attempts. A corrected report is rechecked from scratch.

Citation-blocking defects are non-waivable. Final failure uses a two-phase graph transition:

1. `after_model`/`aafter_model` store input-omitted citation-failure state containing current run ID, current report fingerprint, and bounded defect codes/reference numbers; tag the terminal response as intermediate; block streaming/finalization; and return `jump_to: "end"`.
2. A registered `after_agent`/`aafter_agent` hook sees matching run ID and unchanged report fingerprint, then raises safe `ReportCitationError`. Because the state update was checkpointed by the preceding node, restored history retains the failure metadata. A later explicit run clears stale failure state before evaluating a new or revised report.

Existing verification exception handling must explicitly re-raise `ModelCallTimeoutError`, `CancelledError`, and `ReportCitationError`; generic judge failures may retain current fallback behavior. Existing accepted-at-verification-limit behavior remains available only for non-structural LLM-judge findings. This guarantees a web-enabled report with missing, placeholder, or unresolved citations cannot be finalized as verified.

### Request context propagation

Carry the per-generation strict-web-citation snapshot from research state into both sync and async verification calls. Do not infer it from whether Tavily happened to return results: a web-enabled run that failed to fetch sources must still revise its report or fail verification.

## Failure and Cancellation Semantics

- Model timeout: run fails with `ModelCallTimeoutError`; no completion continuation is consumed.
- External async cancellation: request connection closes, optional unload finishes or hits its own deadline, then `CancelledError` propagates unchanged.
- Direct synchronous invocation: sync-to-async bridge enforces the same total deadline and aborts the async provider request; no raw sync provider call is used.
- Default Ollama cancellation: cancel request only; model remains loaded according to Ollama policy.
- Opt-in Ollama cancellation: cancel request, then best-effort bounded unload.
- Unload failure: warning only; original timeout/cancellation outcome wins.
- Citation preflight failure: verification returns citation-blocking `needs_revision` and uses the existing bounded revision flow.
- Citation failure at final correction round: checkpointed two-phase transition raises `ReportCitationError`; report is not streamed or marked verified.

## Testing

Tests are written before production changes.

### Model-call guard

- valid default, positive override, and malformed/non-finite/nonpositive fallback to 300 seconds;
- synchronous handler returns before deadline;
- every supported sync provider adapter runs through the cancellable async bridge, receives a native deadline as secondary protection, and normalizes timeout to the dedicated safe error;
- slow token trickle cannot extend sync or async total duration beyond the configured deadline plus cleanup grace;
- CLI `KeyboardInterrupt` or generator close cancels the sync bridge task and closes the provider request before the configured deadline;
- asynchronous handler returns before deadline;
- asynchronous handler is cancelled with a bounded cleanup grace on timeout;
- cancellation-suppressing async handler cannot block return and its late exception is consumed;
- local streaming-server probes observe client disconnect on timeout and cancellation;
- external async cancellation remains `CancelledError`;
- default Ollama timeout performs no unload;
- explicit Ollama opt-in performs one bounded unload with the active model and configured base URL;
- Ollama cancellation also invokes opt-in unload;
- unload is never called for cloud providers;
- unload failure preserves the original timeout/cancellation exception;
- timeout covers root, explicit `research-agent`, inherited `general-purpose`, late model override, sufficiency judge, and adversarial judge paths;
- `bind_tools`/`bind` retain the guard; `invoke`/`ainvoke` and `stream`/`astream` preserve tool calls, chunks, callbacks, and metadata;
- compiled root and nested subagent paths retain complete tool-call messages and live nested streaming;
- mixed-provider environment precedence and final-model metadata prevent false Ollama unload;
- known late overrides are rebuilt through the adapter registry, descriptor-protocol overrides stay cancellable, and unknown raw overrides fail closed without unload;
- sync timeout cannot leave a non-daemon application-owned worker or block interpreter shutdown; and
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
- document-only request is exempt only when documents are present and web is disabled;
- documents plus web enabled remains strict;
- same thread web→no-web and no-web→web transitions replace stale request context;
- prior `no_web=true` followed by omitted `no_web` restores the public web-enabled default at the graph input boundary;
- source-section boundaries, valid-URL parsing, grouped/ranged references, inline numbered links, code-fence exclusion, exact reserved host set, and non-URL document entries follow the defined grammar;
- sync and async verification paths receive identical strictness; and
- revision feedback remains ephemeral and respects the configured round limit;
- zero-round or disabled LLM verification still enforces structural citations and allows exactly one correction;
- citation-blocking verdict cannot use accepted-at-limit finalization; and
- final-round citation failure checkpoints safe defect metadata and report fingerprint before `after_agent` raises `ReportCitationError` without streaming the report;
- later explicit run clears stale failure metadata; and
- timeout, cancellation, and citation errors bypass generic judge fallback handling.

Run focused guard, model-factory, citation, verification, completion, and agent-contract suites first, followed by Ruff, compile checks, and the full test suite. Provider/network calls are replaced with local fakes; tests must not require a running Ollama server.

## Operational Notes

Recommended local settings:

```bash
export MODEL_CALL_TIMEOUT_SECONDS=300
export OLLAMA_FORCE_UNLOAD_ON_CANCEL=true
```

The force-unload flag is useful on a single-user local Ollama instance where releasing a stuck model is preferable to preserving concurrent calls. Shared Ollama servers should keep the default `false` value.

A backend restart is required after changing either variable because guards resolve configuration when their models are constructed, regardless of reload mode.

Add both settings and their defaults to `.env.example`, the configuration and reliability guides, AWS deployment environment rendering, and Azure Container Apps environment rendering. Deployment examples keep force unload false; the local single-user example may show the explicit true opt-in.
