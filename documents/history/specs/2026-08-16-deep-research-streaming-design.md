# Deep research tool gating and live subagent trace design

## Purpose

Restore expected deep-research behavior when a thread has no uploaded documents and make delegated research activity visible in the companion frontend.

Current behavior exposes `llm_wiki_query` to the orchestrator even when document and wiki endpoints report no content. A local model can therefore select document-only tools, fail to delegate web research, and stop without a report. The UI can also render two adjacent `Starting research…` messages from one run and can miss live nested subagent tool updates because derived subagent data is memoized against stable references.

## Scope

This change spans two repositories:

- Backend: `/Users/jerryshao/Documents/projects/IBM/ai/deep-research`
- Frontend: `/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui`

It changes document-availability state, model-visible tool eligibility, no-document orchestration guidance, adjacent progress-message presentation, and live subagent trace derivation. It does not change upload payloads, ingest, wiki query results, Tavily, or LangGraph transport APIs.

## Evidence and root causes

The supplied backend trace shows `/documents/list` and `/wiki/tree` returning `404` for the active thread before its run. The run was created once, executed with `run_attempt=1`, and completed successfully, which rules out frontend double submission and backend run retry as causes of the duplicated start status.

Backend graph registration currently includes `llm_wiki_query` and `read_docs_folder` for every model request. The dynamic instruction says to use wiki search "if documents are available" but does not state availability explicitly. A weaker local model can still choose the exposed wiki and filesystem tools.

Frontend submission already sets `streamSubgraphs: true`, and SDK `useStream` is configured with `filterSubagentMessages: true`. `ChatMessage` obtains nested calls through `stream.getSubagent(taskCall.id)`, but wraps the complete subagent projection in `useMemo` whose dependencies are stable during SDK manager updates. The component can rerender without recomputing the nested calls.

## Backend design

### Authoritative document availability

Add `has_documents: bool | None` to graph state and keep matching tri-state frontend state. Initial loading and list network/5xx errors remain `unknown` and omit the field from run updates; they must never be converted to false. A successful non-empty `/documents/list` result or upload success sets it true. A confirmed `404`, a successful empty list, or deletion of the last known document sets it false and clears stale `doc_folder` state.

Add one fail-closed predicate shared by middleware and `llm_wiki_query`:

- Explicit `has_documents=false` always means unavailable, even when `doc_folder` is stale.
- Explicit `has_documents=true` requires at least one supported source in the normalized local `doc_folder` or the physical Thread Wiki raw directory resolved for the same LangGraph thread ID; truthiness alone is insufficient.
- When the flag is absent for CLI compatibility, the same physical-source check applies. Virtual agent state paths such as `/raw/` or `/docs/` do not count as upload evidence.
- The Thread Wiki raw directory is `<wiki-base>/docs/threads-wiki/<thread-id>/raw`, resolved through `ThreadWikiPaths`; `<thread-id>` is the existing LangGraph deep-agent thread ID already used by upload, ingest, and chat state.
- Supported evidence uses one shared Thread Wiki source policy: Markdown/text, JSON, CSV, YAML, supported office documents, and supported source-code formats. Generated wiki/report files do not count.
- Whitespace, malformed paths, empty or missing folders, generated research files, and agent-writable `/wiki/` paths do not count.

Resolve current-turn parameter extraction before progress wording and evaluate the predicate against merged state (`state` plus extracted updates). Use the same predicate for progress wording, model-request configuration, and the wiki tool guard so these behaviors cannot drift.

### Model-visible tool gating

Keep wiki and document tools registered on the graph. During `configure_request`, filter `llm_wiki_query` and `read_docs_folder` from `request.tools` when the predicate is false. Preserve all other tools and current `tool_choice` behavior.

When document context exists, expose the registered tools unchanged. This keeps existing uploaded-document and explicit wiki workflows intact.

`llm_wiki_query` also evaluates the predicate before resolving wiki paths or calling `run_query`. This execution-time defense handles stale checkpoints or malformed model output. It returns a source-constraint error without invoking wiki retrieval. Model-visible filtering remains the primary mechanism preventing an unwanted tool row.

### Dynamic orchestration guidance

Append an explicit document-context block to the request-scoped system instruction:

- With documents: tell the orchestrator document retrieval tools are available and should ground relevant claims.
- Without documents: state no uploaded document context exists, prohibit wiki/document-folder calls, and direct web-enabled research through `task` using `research-agent`.
- Without documents and with `no_web=true`: state that neither document nor web research is available so the agent must report the source constraint rather than invent evidence.

The instruction remains ephemeral in `ModelRequest.system_message`, preserving strict Ollama role ordering and avoiding new persisted system messages. Prompt guidance is not treated as proof of delegation: acceptance requires a representative local-model run that calls `task` with `subagent_type="research-agent"`, does not call document-only tools, and produces `/final_report.md`. No synthetic task call is inserted into model history.

## Progress-message design

Keep the backend’s deterministic initial progress message so every run gives immediate feedback. Do not depend on local-model narration for this status.

In the frontend message-processing boundary, inspect raw message order and collapse only consecutive assistant messages that:

- canonicalize `Starting research…` and `Starting research...` to the same recognized initial status;
- have no tool calls; and
- have no intervening raw human, tool, or non-status assistant message.

Keep the first status and suppress later consecutive duplicates. Do not deduplicate arbitrary assistant prose or non-adjacent statuses. A raw tool message resets the duplicate candidate even though tool messages are consumed into rendered tool-call state. This fixes existing threads as well as new streams while preserving backend state for debugging.

## Live subagent trace design

Continue using SDK-native subgraph streaming and `stream.getSubagent(taskCall.id)` as the source of truth. Remove memoization around the full `subAgents` projection and remove the outer `React.memo` from `ChatMessage`. Parent stream updates must therefore rerender the component and read the mutable SDK manager again even when message, task-call, and stream object identities remain stable.

For each expanded `research-agent` card, render nested calls in arrival order through the existing `ToolCallBox` component. Expected calls include `tavily_search`, `fetch_webpage_content`, and `think_tool`, with pending, completed, interrupted, and error states normalized by the existing adapter.

Completed historical runs may fall back to nested calls parsed from the task result when SDK subagent history is unavailable. Live SDK data takes precedence when present.

## Data flow

1. Frontend submits a human message with `streamSubgraphs: true`.
2. Frontend synchronizes confirmed `has_documents` state, omits unknown/error state from runs, and clears stale `doc_folder` after the last source disappears.
3. Backend middleware validates document availability against merged current-turn state.
4. Backend removes document-only tools when unavailable and injects matching request guidance.
5. Orchestrator plans and delegates web research through `task`.
6. LangGraph streams namespaced subgraph messages.
7. SDK associates the namespace with the parent task call and updates its subagent manager.
8. `ChatMessage` rerenders, re-reads the subagent snapshot, and displays nested tools.
9. Raw-order message processing suppresses only a consecutive duplicate initial status in the rendered view.

## Error handling

- Tool gating is fail-closed for missing, stale, empty, or malformed document state: document-only tools remain hidden.
- A stale or hallucinated wiki tool call cannot reach `run_query` without validated document context.
- Unknown tool objects retain their current visibility.
- Missing `getSubagent` data yields an empty live trace and uses the existing completed-result fallback.
- Nested tool calls without stable IDs remain excluded by the adapter rather than creating unstable React keys.
- Wiki endpoint `404` responses remain valid empty-state behavior and are not converted to application errors.

## Tests

Backend tests must prove:

- no-document requests hide `llm_wiki_query` and `read_docs_folder`;
- validated non-empty upload folders or physical `docs/threads-wiki/<thread-id>/raw` sources expose them, including JSON, CSV, YAML, and supported code;
- stale, whitespace, missing, empty, and explicitly cleared document state hides them;
- generated research files alone do not expose them;
- the guarded wiki tool never invokes `run_query` without valid document context;
- request-scoped instruction describes the correct document/web path;
- progress wording uses current-turn extracted document state;
- existing Ollama system-message ordering tests remain green.

Frontend tests must prove:

- document availability starts unknown, stays unknown on list network/5xx errors, becomes true after upload or a non-empty list, and becomes false only after confirmed `404`, an empty successful list, or last-document deletion;
- run submission includes only confirmed boolean availability and omits unknown state;
- adjacent recognized start statuses render once;
- mixed ellipsis forms collapse while human, tool, and non-status barriers preserve later statuses;
- unrelated or non-adjacent identical assistant content is preserved;
- a stable stream object whose subagent snapshot gains a nested call causes that call to appear after rerender;
- nested calls remain associated with the correct parallel task;
- submission continues to set `streamSubgraphs: true`.

Verification uses the narrowest relevant backend pytest files, focused Node tests for message processing and subagent rendering, frontend lint/build, and `git diff --check` in both repositories. Final acceptance also runs the configured local Ollama model against a fresh no-document thread and verifies observed tool history contains `task`/`research-agent`, contains neither document-only tool, exposes at least one nested research tool in the frontend, and writes `/final_report.md`. If this representative-model check fails, the change is not complete and prompt/tool policy must be revisited.

## Non-goals

- Removing explicit wiki retrieval from document-backed research.
- Querying document or wiki HTTP endpoints inside every model call.
- Fabricating a `task` tool call on behalf of the model.
- Replacing LangGraph SDK subagent state with a custom event protocol.
- Changing report format, verification passes, upload behavior, or authentication.
- Deduplicating general assistant messages.
