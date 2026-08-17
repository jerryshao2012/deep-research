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
- finish normally only when an active todo plan is complete and `/final_report.md` exists with non-empty content;
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

### Completion state

Add a run-state counter for automatic completion attempts. Reset it when `ResearchStateMiddleware` detects a fresh user request. Explicit resume rounds retain the existing counter and state.

Completion assessment runs only after an `AIMessage` with no tool calls. Tool-producing model responses continue through the ordinary tools loop without consuming a completion attempt.

For an active, non-empty todo plan, a run is complete only when:

1. no todo has `pending` or `in_progress` status; and
2. `/final_report.md` exists in filesystem state and its normalized content is non-empty.

The guard is deliberately tied to an active todo plan. Clarification-only turns and other terminal responses that have not established a research plan remain governed by existing middleware rather than being converted into completion loops.

### Automatic continuation

When a terminal response fails the completion assessment and fewer than three attempts have been used, middleware:

1. increments the completion-attempt counter;
2. returns `jump_to: "model"`;
3. preserves existing messages, todos, files, document context, and verification state; and
4. supplies an ephemeral system-prompt block on the next model request.

The prompt block states the current attempt number, identifies whether unfinished todos, a missing report, or an empty report blocked completion, and requires the next response to perform a concrete tool action. It is injected at model-request time so strict Ollama templates never receive a persisted system message in the middle of conversation history.

The counter is total per user request, not consecutive. Successful tool calls do not reset it, preventing a model from alternating one tool call with one terminal response indefinitely.

### Interaction with verification

Existing report verification remains authoritative once `/final_report.md` exists. Completion enforcement runs after the current report-streaming and verification decisions and does not override an existing verification jump.

The guard evaluates the effective state produced by the hook, including todo updates from verification. A report is not considered fully complete while any research or verification todo remains incomplete.

### Retry exhaustion

On the fourth incomplete terminal response—the initial stop plus three automatic continuation attempts—middleware raises a dedicated `ResearchIncompleteError` containing:

- attempts used;
- count and labels of remaining todos; and
- whether `/final_report.md` is missing or empty.

This makes LangGraph mark the run failed rather than successful. The error is logged with run and thread context through existing LangGraph logging. The frontend's existing stream-error path remains responsible for presenting the failed run; no separate hidden run is created.

## Testing

Tests must be written before production changes and must prove both synchronous and asynchronous hook behavior.

Required cases:

- terminal response plus incomplete todos and missing report increments attempt and jumps to model;
- continuation request contains ephemeral guidance without inserting a persisted mid-conversation `SystemMessage`;
- model response with tool calls does not consume an attempt;
- completed todos plus a non-empty final report finishes without a jump;
- completed todos plus missing or empty final report continues;
- report present plus unfinished todo continues;
- existing verification jump takes precedence over completion enforcement;
- third automatic continuation remains allowed;
- next incomplete terminal response raises `ResearchIncompleteError`;
- fresh user request resets the attempt counter;
- explicit resume behavior and registered `write_todos` tool remain unchanged.

Focused middleware and contract tests run first, followed by relevant verification and resume suites, Ruff, and diff checks.

## Operational Notes

`MAX_COMPLETION_ATTEMPTS` defaults to `3` and may be configured through an environment variable using the project's existing bounded-integer parsing conventions. A server restart is required after deployment because development is run with `--no-reload`.
