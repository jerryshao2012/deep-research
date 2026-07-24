# Resume Incomplete Research Todos

Date: 2026-07-23
Status: Approved design

## Problem

A research run can end while its persisted todo list still contains pending or
in-progress work. The frontend already permits another message on the same
thread, but phrases such as `continue` have no deterministic resume contract.
A single follow-up run may also stop before all remaining tasks finish.

## Goals

- Let users type a short resume phrase in the existing chat composer.
- Recognize common resume-only variants without requiring a special UI command.
- Resume only when the thread has incomplete persisted todos.
- Preserve original research goal, selected skill, files, todo list, and visible
  chat transcript.
- Keep running bounded agent rounds until every todo is complete or a defined
  stop condition occurs.
- Work for every client that uses the backend, not only the current frontend.

## Non-goals

- Interpret longer follow-up instructions as implicit resume commands.
- Automatically resume a run without a user message.
- Guarantee completion through unavailable services, exhausted model limits, or
  user cancellation.
- Replace existing run enqueue, interrupt, or cancellation behavior.
- Add a dedicated resume button in the initial implementation.

## User Contract

A message is a resume request when it is a short, standalone continuation
phrase accepted by this deterministic grammar:

- Base phrase is one of `continue`, `go on`, `keep going`, `resume`, `proceed`,
  `finish the remaining tasks`, or `complete the remaining tasks`.
- Optional politeness is either `please <base>`, `<base> please`, or
  `<base>, please`.
- Matching uses Unicode NFKC normalization, case-folding, trimmed surrounding
  whitespace, collapsed internal whitespace, and removal of trailing `.` or
  `!` characters only.
- A message containing `?` or punctuation other than the optional comma before
  suffix `please` does not match.

Matching is deterministic, not LLM-classified. New synonyms require an explicit
base-phrase addition and accompanying positive and negative tests.

Messages that contain negation, ask a question, or add new research direction
remain ordinary follow-ups. Examples:

- `do not continue`
- `should we continue?`
- `continue researching security`
- `go on, but compare the vendors first`

A matched phrase enters resume mode only if the latest persisted state contains
at least one incomplete todo. With no incomplete todos, the message follows the
normal chat path unchanged.

## Architecture

### Resume-intent classifier

Add a pure function that implements the grammar above. Keep accepted phrases
explicit and testable. Do not use substring or LLM matching because either
would misclassify longer instructions and negations.

### Shared todo-state helper

Centralize incomplete-todo detection so CLI fallback behavior and web resume
behavior use one definition. A todo is complete only when its normalized status
is `completed`. Known pending and in-progress states are incomplete. Malformed
todo containers do not trigger resume mode; malformed entries are ignored and
logged.

### Ephemeral resume instruction

Keep the user's original phrase in visible chat history. Capture that exact
run-triggering message as ephemeral run context when the request is accepted;
never infer intent from whichever message is latest when a queued run begins.
At execution time, classify the captured message, check current todos, and
inject a non-persisted instruction into model context:

- preserve original user goal, selected skill, files, and valid existing todos;
- inspect and execute pending and in-progress items;
- do not replace the plan merely because the run resumed;
- mark an item complete only after its work is done;
- synthesize the requested final output after all items finish.

Internal continuation instructions must not appear as user-authored chat
messages or pollute persisted transcript history.

The agent's tool calls, tool results, todo updates, files, and model messages
from hidden rounds remain in internal checkpoint state for reasoning, audit, and
recovery. Terminal assistant messages from non-final rounds are tagged
`resume_intermediate=true`. Standard chat streaming and thread-history
serialization omit tagged messages and emit only additive resume-progress
metadata. The final completed, failed, cancelled, or safety-limited assistant
message is untagged and remains the single user-visible response for the run.

### Bounded resume coordinator

The backend run coordinator owns the loop. After each agent round it reads the
latest persisted todo state:

1. All todos complete: finish the run normally.
2. Incomplete todos remain: start another hidden resume round.
3. User cancellation: stop immediately with existing cancellation semantics.
4. Unrecoverable agent/tool error: stop and expose the error through the
   existing run result.
5. Safety limit reached: stop cleanly and report remaining tasks.

Use `MAX_RESUME_ROUNDS` as a positive integer configuration value with default
`3`. Invalid values fall back to the default and emit a warning. Each hidden
round belongs to the same frontend-visible run so the user sees one continuous
operation.

Resume intent is tied to the exact message that created the run. Todo state is
checked when that queued run actually executes, not only when the request is
accepted. This avoids both selecting a later queued message and deciding from
stale todos while another run is still active.

## Data Flow

1. Frontend sends the user's text through its existing message submission path.
2. Backend persists the original message, binds its text to the created run as
   ephemeral resume-candidate context, and applies existing multitask policy.
3. At execution time, backend classifies that run's bound candidate text and
   checks current todo state.
4. If both conditions match, backend activates ephemeral resume context.
5. Agent performs a round using existing messages, files, skill, and todos.
6. Backend persists normal agent state updates and rechecks todos.
7. Backend repeats hidden rounds while incomplete work remains and the safety
   conditions allow it.
8. Final response is returned when work completes or explains why work remains.

## Frontend Behavior

No special command syntax or required frontend mutation is needed. Existing
composer behavior remains valid. The frontend may optionally render progress
such as `Completing remaining tasks — round 2 of 3` when backend stream metadata
announces resume rounds.

The optional progress event must be additive so older clients can ignore it.

## Stop and Failure Behavior

- No todos or all todos complete: process phrase as ordinary chat.
- Malformed todo list: process phrase normally and log diagnostic context.
- Agent round returns without todo progress: continue until round limit, then
  stop with remaining-task summary.
- Model/tool exception: preserve latest successfully persisted state and expose
  existing run failure semantics.
- Cancellation: do not launch another hidden round.
- Server restart: rely on current thread/checkpoint persistence; user can send
  another resume phrase if the interrupted visible run is not recovered.
- Round limit: final visible response states that safety limit was reached and
  lists pending/in-progress todo labels and statuses.

## Observability

Add structured log fields for:

- resume intent matched;
- resume mode activated or skipped;
- visible run ID and thread ID;
- current and maximum resume round;
- incomplete todo count before and after each round;
- stop reason: completed, cancelled, error, or round limit.

Do not log user message bodies or todo contents by default.

## Testing

### Unit tests

- Accept every base phrase with case, whitespace, supported politeness, and
  trailing `.` or `!` variants.
- Reject negations, `?`, unsupported punctuation, substrings, and longer
  follow-up instructions.
- Detect pending and in-progress todos.
- Ignore malformed containers and entries safely.
- Parse valid and invalid `MAX_RESUME_ROUNDS` values.

### Coordinator tests

- Skip resume mode when no todos are incomplete.
- Complete in one round and stop.
- Require multiple rounds, then stop when all todos complete.
- Reach maximum rounds and return remaining-task summary.
- Preserve original visible user message and omit hidden instructions from
  transcript state.
- Persist intermediate model/tool state internally, tag non-final terminal
  assistant messages, and omit tagged messages from chat stream and serialized
  thread history.
- Preserve files, skill, original goal, and existing valid todos.
- Stop after cancellation without another round.
- Preserve state and surface agent errors.
- Evaluate todo state at execution time for queued runs.
- Bind resume intent to the message that created each queued run.
- Respect existing enqueue and interrupt strategies.

### Contract and integration tests

- Submit `Please continue!` through the run endpoint on an existing thread with
  incomplete todos and verify bounded multi-round behavior.
- Submit the same phrase with completed todos and verify normal message flow.
- Verify optional progress metadata remains compatible with existing stream
  consumers.
- Verify CLI incomplete-todo fallback still uses the shared helper.

## Acceptance Criteria

- Common standalone resume phrases reliably activate resume mode.
- Longer or negated instructions are not misclassified.
- Resume mode never activates without incomplete persisted todos.
- One visible run performs up to three agent rounds by default.
- Run stops immediately when all todos are complete.
- User transcript contains the original phrase but no internal prompts.
- Safety-limit and failure outcomes identify remaining work.
- Existing run concurrency, cancellation, and ordinary follow-up behavior pass
  regression tests.
