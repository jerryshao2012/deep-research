# Research Flow Recovery Design

## Context

One local-model run exposed three failures across backend and frontend:

1. Gemma emitted `clarify_requirements` arguments with `question` and string
   `options`. The tool accepts only canonical questions containing `id`,
   `prompt`, `type`, and structured options, so LangChain rejected the call
   before the tool could interrupt.
2. Because clarification never completed, orchestration did not reach report
   synthesis. The existing `write_file` default for `/final_report.md` was not
   exercised; the missing report is a downstream symptom, not a separate file
   tool failure.
3. Frontend passively calls `threads.updateState` to persist document
   availability without `asNode`. Deep-agent checkpoints can have multiple
   possible last writers, so LangGraph returns `Ambiguous update, specify
   as_node`. Choosing an arbitrary node is unsafe because node attribution also
   controls future graph scheduling.
4. Thread list derives titles from `thread.values.messages`, but its search and
   fallback retrieval do not guarantee state values. Completed threads can
   therefore retain `Untitled Thread` even when the first human message exists
   in checkpoint state.

## Goals

- Accept the observed Gemma clarification shape without weakening the canonical
  interrupt and response contracts.
- Preserve deterministic replay across LangGraph interrupt and resume.
- Allow the resumed run to continue through existing completion enforcement and
  `/final_report.md` generation.
- Eliminate passive graph-state mutations for document availability.
- Derive a useful thread title from checkpoint messages, with a stable fallback.

## Non-goals

- Do not disable clarification for local models.
- Do not silently repair partially formed canonical clarification payloads.
- Do not assign an arbitrary LangGraph `asNode` merely to suppress the 400.
- Do not change report verification, citation policy, or task-completion rules.
- Do not introduce model-based title generation.

## Backend design

### Clarification compatibility boundary

`ClarificationBatch` remains the canonical normalized model. A before-validation
adapter accepts only the exact legacy shorthand used by Gemma:

```json
{
  "questions": [
    {
      "question": "What is the intended audience?",
      "options": ["Researchers", "Industry professionals", "Other"]
    }
  ]
}
```

Each shorthand question is converted deterministically:

- question ID: `question_1`, `question_2`, ...
- prompt: original `question`
- type: `single_select`
- option IDs: `option_1`, `option_2`, ... within each question
- option labels: original strings
- a conventional standalone `Other` option is removed only when at least two
  concrete options remain, because the clarification response already supports
  free-form `other_text`

After conversion, existing strict Pydantic validation still enforces batch,
length, identifier, uniqueness, and extra-field limits. Any item containing a
mix of canonical and shorthand keys is rejected instead of guessed.

The model-facing tool description gains one compact canonical JSON example.
This keeps canonical output preferred while the boundary adapter handles local
model deviations.

### Report completion

No new report-writing path is added. Once clarification produces a valid
interrupt and the user resumes it, the existing completion guard remains the
single owner of completion: all todos must finish and the owned report must be
written to `/final_report.md`. Tests prove the shorthand call reaches an
interrupt rather than a tool-validation error and that resume produces the
canonical clarification result used by the continuing graph.

## Frontend design

### Document availability

`useThreadDocumentAvailability` keeps confirmed availability and documents in
local React state but stops persisting them through `threads.updateState`.
Upload, delete, and list refreshes therefore cannot create ambiguous graph
checkpoints or conflict with active runs.

Run submission remains authoritative: every new research run includes
`has_documents` and canonical `doc_folder` derived for the active LangGraph
thread. Existing pending-upload fallback remains thread-owned until confirmed
list/delete evidence supersedes it. Passive polling never mutates graph state.

The hook drops its state-write queue, busy-status deferral, and 409 retry logic;
those mechanisms exist only to support the unsafe passive write. Tests retain
race, navigation, unmount, upload, deletion, and submit-boundary coverage while
asserting that no graph-state write occurs.

### Thread titles

Thread search explicitly requests `values` along with identity, timestamps,
status, and metadata. For a completed nonbusy item still lacking a custom title
or first human message, the repository calls `threads.getState(threadId)` and
recomputes the preview from checkpoint `values`.

Title priority remains:

1. user-defined `metadata.custom_title`
2. first human message, truncated to the existing 50-character limit
3. stable `Thread <first-eight-id-characters>` fallback

This avoids model latency and preserves manual titles.

## Error handling

- Legacy clarification normalization never logs or echoes model payloads.
- Canonical validation errors remain bounded tool errors.
- Document list failures leave availability unknown; they do not persist false.
- Thread-state title lookup failure returns the stable ID fallback and does not
  block the thread list.

## Testing

### Backend

- RED/GREEN contract tests for exact shorthand payload and mixed-shape rejection.
- Tool invocation test proving shorthand arguments pass `args_schema`, interrupt,
  and resume into canonical result.
- Existing canonical clarification, completion guard, and write-file tests.

### Frontend

- Hook tests proving refresh/upload/delete update local state without
  `updateState`.
- Submission tests proving document flags and canonical folder still reach each
  run payload.
- Repository tests for explicit search selection, checkpoint title recovery,
  manual-title precedence, and stable ID fallback.
- Focused tests, lint, formatting, and production build in the frontend branch.

## Delivery

- Backend branch: `codex/clarification-report-recovery`
- Frontend branch: `codex/research-flow-ui-fixes`
- Each repository is verified and merged independently after review.
