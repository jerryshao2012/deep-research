# Research Completion Guard Design

## Goal

Prevent a research run from ending successfully when the model returns a terminal response while its task plan is unfinished or `/final_report.md` has not been produced.

The immediate failure mode is an Ollama `gemma4:latest` run that completed one of four todos, called `think_tool`, stated that it would proceed to synthesis, then emitted no further tool call. LangGraph treated that terminal model response as a successful run even though no final report existed.

## Scope

This change applies to the root research graph served by `langgraph dev`.

It must:

- preserve the current LangGraph thread, files, todo list, source context, and live nested-tool streaming;
- continue within the same run rather than creating hidden follow-up runs;
- allow at most three automatic continuation attempts;
- finish normally only when the current request's active todo plan is complete and its `/final_report.md` exists with non-empty content;
- fail clearly after the retry limit instead of silently reporting success;
- retain explicit incomplete-todo resume behavior for later user-driven runs.

It does not change frontend historical nested-tool restoration, document availability behavior, research prompts unrelated to completion, or subagent tool ownership.

## Considered Approaches

### 1. Prompt-only enforcement

Strengthen the main prompt to say that the model must continue until every task is complete and the final report is written.

This is low cost but not deterministic. The observed Gemma response already described the correct next action and still stopped. Prompt text cannot prevent a terminal response.

### 2. External run coordinator

After a run reports success, have the frontend or custom server inspect state and submit hidden `continue` runs until completion.

This matches the older custom-server resume pattern but is a poor fit for the current `langgraph dev` runtime. It creates multiple runs, exposes temporary successful states, and reintroduces state-update and in-flight-run races such as HTTP 409 responses.

### 3. In-graph completion guard

Inspect terminal model responses inside root graph middleware. If work is incomplete, inject ephemeral continuation guidance and jump back to the model node. Stop after a bounded number of attempts.

This is the selected approach because it keeps one run, one thread, and one authoritative state transition path.

## Design

### Request-scoped completion state

Add a dedicated `CompletionGuardMiddleware` and persist its small amount of control state in `ResearchState`:

- current LangGraph run identifier;
- automatic continuation-attempt count;
- current request's plan owner;
- current request's report-ownership flag;
- whether an explicit resume adopted the previous request generation; and
- the ordinary request's baseline `/final_report.md` `modified_at` value.

`before_agent` runs once for each visible LangGraph run. It uses top-level `get_config()["run_id"]`, normalized to a string, as request identity, with a generated invocation token only for direct graph calls that do not provide a run ID. This matches LangGraph API-shaped configuration and avoids content-hash ambiguity when a user submits identical text twice. Automatic `jump_to: "model"` continuations bypass `before_agent`, so they retain the same counter.

Every visible run receives a fresh three-attempt budget, including an explicit resume after a previous exhausted run. An ordinary new research run clears stale todos, marks its plan and report unowned, records the prior report's `modified_at` baseline, and resets streamed-file ownership. An explicit `resume_incomplete_todos` run preserves the prior plan, baseline, and report-ownership flag while resetting only the automatic-attempt budget. Resume never promotes a report to owned merely because a non-empty canonical report exists; request B therefore cannot adopt request A's stale report.

`CompletionGuardMiddleware.before_model` and `abefore_model` activate artifacts after the tools node. They correlate the latest `AIMessage` tool-call IDs with successful `ToolMessage.tool_call_id` values:

- successful `write_todos` plus a resulting non-empty todo list activates the plan for the current request;
- successful `write_file` targeting canonical `/final_report.md` plus valid file data, non-empty content, and a `modified_at` value different from the ordinary-run baseline marks the report owned.

Content-only `write_file` calls use the tool's existing default `/final_report.md` path. Failed, missing, mismatched, or malformed tool results activate nothing. Explicit resume retains only ownership already established by the request it resumes. These rules prevent a new request from passing with a prior completed plan/report or looping on a prior incomplete plan.

Completion assessment runs only after an `AIMessage` with no tool calls. Tool-producing model responses continue through the ordinary tools loop without consuming a completion attempt.

For the current request's active, non-empty todo plan, a run is complete only when:

1. every todo is a valid mapping whose normalized status is exactly `completed`; and
2. the current request owns `/final_report.md`, the file converts successfully, and its normalized content is non-empty.

Unknown statuses, malformed todo entries, malformed file data, and file-conversion failures are incomplete states rather than successful completion.

The guard is deliberately tied to an active todo plan. Clarification-only turns and other terminal responses that have not established a research plan remain governed by existing middleware rather than being converted into completion loops.

### Automatic continuation

When a terminal response fails the completion assessment and fewer than three attempts have been used, middleware:

1. increments the completion-attempt counter;
2. returns `jump_to: "model"`;
3. preserves existing messages, todos, files, document context, and verification state; and
4. supplies an ephemeral system-prompt block on the next model request.

The prompt block states the current attempt number, identifies whether unfinished todos, a missing report, or an empty report blocked completion, and requires the next response to perform a concrete tool action. It is injected at model-request time so strict Ollama templates never receive a persisted system message in the middle of conversation history.

The counter is total per user request, not consecutive. Successful tool calls do not reset it, preventing a model from alternating one tool call with one terminal response indefinitely.

Each incomplete terminal `AIMessage` that causes an automatic continuation is tagged with the existing `resume_intermediate` response metadata. This preserves the frontend's current suppression contract so promises such as “I will proceed” are not presented as final answers.

Both sync and async completion hooks declare `@hook_config(can_jump_to=["model"])`. Compiled-graph tests must prove that the returned jump actually reaches a second model invocation.

Middleware registration order is:

1. todo and clarification middleware;
2. `CompletionGuardMiddleware`;
3. `ResumeMiddleware`;
4. `ResearchStateMiddleware`.

LangChain executes `after_model` hooks in reverse registration order. This produces Research → Resume → Completion, allowing report verification and explicit-resume tagging to run before completion enforcement. A completion jump remains inside the same graph run and therefore keeps the same run ID.

### Interaction with verification

Report streaming, verification, evaluation logging, and final completion are gated on current request ownership. Existing report verification runs only after the current plan is valid and every research todo is complete. `ResearchStateMiddleware.after_model` and `aafter_model` declare `can_jump_to=["model", "end"]`. A non-final `needs_revision` verdict stores `verification_feedback`, tags its triggering terminal `AIMessage` as `resume_intermediate`, and explicitly returns `jump_to: "model"`; it no longer persists a mid-history `SystemMessage`. A pass records the verified report's `modified_at`. A final-round `needs_revision` retains existing revision-limit behavior and records an accepted-at-limit state without another model jump.

`ResearchStateMiddleware.configure_request` already derives its leading system prompt from state. It injects `verification_feedback` there alongside completion guidance, keeping Ollama message roles valid. A verification-owned model jump occurs before `ResumeMiddleware` and `CompletionGuardMiddleware`, so it does not consume completion budget.

Move cited-response and final-report streaming out of the beginning of `ResearchStateMiddleware.after_model` and into completion finalization after verification acceptance. Completion finalization occurs only when the current plan is complete, the current report is owned and non-empty, and verification is either disabled, passed for the report's current `modified_at`, or accepted at the configured verification limit. It emits cited responses and `/final_report.md` exactly once, then marks `_streamed_files`. A provisional report with pending todos or a revision verdict is never streamed or marked streamed. Evaluation logging uses the same readiness gate.

The guard evaluates state produced by earlier middleware nodes, including verification todo updates. A report is not considered fully complete while any research or verification todo remains incomplete.

### Retry exhaustion

On the fourth incomplete terminal response—the initial stop plus three automatic continuation attempts—middleware raises a dedicated `ResearchIncompleteError` containing:

- the three automatic attempts used;
- count of incomplete or malformed todos; and
- whether the current request's `/final_report.md` is stale, missing, malformed, or empty.

`CompletionGuardMiddleware.after_model` does not raise immediately on exhaustion. It first tags the exhausted terminal `AIMessage` as `resume_intermediate`, stores safe exhaustion metadata, and returns `jump_to: "end"`. That node update is checkpointed before `CompletionGuardMiddleware.after_agent` raises `ResearchIncompleteError`. This ensures live and restored history suppress the partial terminal response while LangGraph still marks the run failed.

`ResearchIncompleteError` subclasses `RuntimeError`, and the installed LangGraph API serializer exposes `RuntimeError` text instead of replacing it with “An internal error occurred.” The message is deliberately safe and contains counts, not todo labels or research text. Existing task state retains the remaining labels for the UI. No separate hidden run is created.

## Testing

Tests must be written before production changes and must prove both synchronous and asynchronous hook behavior.

Required cases:

- terminal response plus incomplete todos and missing report increments attempt and jumps to model;
- continuation request contains ephemeral guidance without inserting a persisted mid-conversation `SystemMessage`;
- model response with tool calls does not consume an attempt;
- completed todos plus a non-empty final report finishes without a jump;
- completed todos plus missing or empty final report continues;
- report present plus unfinished todo continues;
- stale completed plan/report from a prior ordinary request cannot satisfy a new request;
- stale incomplete plan cannot activate a new ordinary request;
- request B followed by explicit resume cannot adopt request A's stale report when B never owned a report;
- explicit resume adopts prior plan/report but receives a fresh retry budget;
- repeated identical user messages receive distinct request generations;
- malformed todo entries, unknown statuses, malformed file data, and file-conversion failures cannot pass;
- verification revision explicitly jumps to model, injects feedback ephemerally, preserves Ollama role order, and consumes no completion attempt;
- pending-todo report continues without streaming, then emits the accepted report exactly once after completion;
- verification revision does not stream or mark a provisional report and tags its triggering terminal response;
- third automatic continuation remains allowed;
- next incomplete terminal response persists intermediate/exhaustion state before `after_agent` raises `ResearchIncompleteError`;
- LangGraph API serialization preserves the safe incomplete error message;
- incomplete terminal messages are tagged as intermediate;
- compiled sync and async graph executions make the second model call after a completion jump;
- compiled sync and async activation starts inactive, calls real `write_todos`, correlates its successful tool result, observes non-empty todo state, and then guards the next incomplete terminal response;
- failed, mismatched, and malformed `write_todos` tool results do not activate a plan;
- graph integration keeps one run ID and preserves files, todos, and tool events across continuations;
- explicit resume behavior and registered `write_todos` tool remain unchanged.

Focused middleware and contract tests run first, followed by relevant verification and resume suites, Ruff, and diff checks.

## Operational Notes

`MAX_COMPLETION_ATTEMPTS` defaults to `3`. Accepted values are integers from `1` through `3`; values above `3` clamp to `3`, while missing, malformed, zero, or negative values fall back to `3`. Tests cover every boundary. A server restart is required after deployment because development is run with `--no-reload`.
