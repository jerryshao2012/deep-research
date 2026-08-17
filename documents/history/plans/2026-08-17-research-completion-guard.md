# Research Completion Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one LangGraph research run active until its current task plan, final report, and verification are complete, or fail safely after a bounded number of automatic continuations.

**Architecture:** Add a focused `CompletionGuardMiddleware` that owns request generation, artifact ownership, continuation routing, finalization, and exhaustion. Keep report verification in `ResearchStateMiddleware`, but make revision routing explicit and prompt feedback ephemeral for Ollama-compatible message ordering. Use run-scoped state and correlated tool results so stale thread artifacts cannot satisfy a new request.

**Tech Stack:** Python 3.13, LangChain agent middleware, LangGraph, DeepAgents filesystem state, pytest, pytest-asyncio, Ruff.

---

## File Structure

- Create `research_agent/completion_guard.py`: completion state schema, bounded configuration, artifact inspection, tool-result correlation, continuation middleware, finalization, and safe exhaustion error.
- Modify `research_agent/agent.py`: inherit completion state, gate verification/eval, remove premature streaming, inject verification feedback ephemerally, register middleware in correct order.
- Create `tests/test_completion_guard.py`: pure policy, hook, tool-correlation, exhaustion, serialization, and compiled sync/async graph tests.
- Modify `tests/test_verification.py`: verification jump, ownership, accepted-report version, and no-provisional-stream tests.
- Modify `tests/test_verification_progress.py`: update direct-hook fixtures for current plan/report ownership and explicit routing.
- Modify `tests/test_agent_contracts.py`: compiled middleware order and `write_todos` registry contracts.
- Modify `.env.example`: document bounded `MAX_COMPLETION_ATTEMPTS`.

### Task 1: Preserve Current Approved Runtime Baseline

**Files:**
- Modify: `research_agent/agent.py`
- Modify: `tests/test_agent_contracts.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Record the existing scoped diff before new edits**

Run:

```bash
git diff -- research_agent/agent.py tests/test_agent_contracts.py tests/test_tools.py
```

Expected: only previously approved Ollama request-scoped configuration, document-context gating, and explicit `TodoListMiddleware(system_prompt="")` changes. Do not include documentation moves, `uv.lock`, or duplicate `* 2.py` files.

- [ ] **Step 2: Verify the prerequisite regressions**

Run:

```bash
uv run pytest tests/test_agent_contracts.py tests/test_tools.py -q
```

Expected: PASS, including compiled `write_todos` registration and strict Ollama system-message ordering.

- [ ] **Step 3: Commit only the prerequisite runtime files**

```bash
git add research_agent/agent.py tests/test_agent_contracts.py tests/test_tools.py
git commit -m "fix: preserve Ollama research tool contracts"
```

Expected: unrelated staged documentation moves, `uv.lock`, and duplicate files remain outside the commit.

### Task 2: Add Pure Completion Policy

**Files:**
- Create: `research_agent/completion_guard.py`
- Create: `tests/test_completion_guard.py`

- [ ] **Step 1: Write failing configuration and artifact-inspection tests**

Cover:

```python
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 3), ("bad", 3), ("0", 3), ("-1", 3), ("1", 1), ("2", 2), ("3", 3), ("4", 3)],
)
def test_get_max_completion_attempts_is_bounded(monkeypatch, raw, expected): ...

def test_completed_plan_requires_every_item_to_be_valid_and_completed(): ...
def test_malformed_or_unknown_todo_is_incomplete(): ...
def test_report_inspection_rejects_missing_empty_malformed_and_stale_files(): ...
def test_report_inspection_accepts_changed_nonempty_owned_report(): ...
```

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
uv run pytest tests/test_completion_guard.py -q
```

Expected: FAIL because `research_agent.completion_guard` does not exist.

- [ ] **Step 3: Implement minimal pure policy**

Add:

```python
DEFAULT_MAX_COMPLETION_ATTEMPTS = 3
MAX_ALLOWED_COMPLETION_ATTEMPTS = 3

def get_max_completion_attempts() -> int:
    raw = os.getenv("MAX_COMPLETION_ATTEMPTS")
    try:
        parsed = int(raw) if raw is not None else DEFAULT_MAX_COMPLETION_ATTEMPTS
    except (TypeError, ValueError):
        return DEFAULT_MAX_COMPLETION_ATTEMPTS
    if parsed <= 0:
        return DEFAULT_MAX_COMPLETION_ATTEMPTS
    return min(parsed, MAX_ALLOWED_COMPLETION_ATTEMPTS)
```

Define `CompletionInspection` with plan-active, incomplete/malformed counts, report reason, and `ready` property. Normalize file content through `file_data_to_string`; conversion failures remain incomplete.

- [ ] **Step 4: Run policy tests and confirm GREEN**

Run:

```bash
uv run pytest tests/test_completion_guard.py -q
```

Expected: PASS for pure policy cases.

- [ ] **Step 5: Commit**

```bash
git add research_agent/completion_guard.py tests/test_completion_guard.py
git commit -m "feat: define bounded research completion policy"
```

### Task 3: Add Request Generation and Tool-Result Ownership

**Files:**
- Modify: `research_agent/completion_guard.py`
- Modify: `tests/test_completion_guard.py`

- [ ] **Step 1: Write failing lifecycle tests**

Test `before_agent` with API-shaped `{"run_id": UUID(...), "configurable": {...}}` and direct calls without a run ID. Assert:

- ordinary run resets attempts/exhaustion, clears stale todos, records report baseline, clears ownership and streamed files;
- explicit resume resets attempts/exhaustion but preserves prior plan, baseline, report ownership, and verification ownership;
- identical user text in two distinct run IDs creates distinct generations;
- exhausted run followed by explicit resume can finish without stale `after_agent` failure.

- [ ] **Step 2: Run lifecycle tests and confirm RED**

Run:

```bash
uv run pytest tests/test_completion_guard.py -q -k "generation or resume or exhaustion"
```

Expected: FAIL because lifecycle hooks/state are absent.

- [ ] **Step 3: Implement completion state and `before_agent`**

Define `CompletionState(FilesystemState)` fields for run ID, generation, attempts, plan/report ownership, report baseline, verification ownership, and exhaustion metadata. Implement `CompletionGuardMiddleware.before_agent` using top-level `get_config().get("run_id")` with UUID-to-string normalization and UUID fallback.

- [ ] **Step 4: Write failing correlated tool-result tests**

Use real `AIMessage.tool_calls` and `ToolMessage` objects. Cover:

- `write_todos` activates only after matching successful result and non-empty resulting todos;
- failed, mismatched, malformed, or empty results do not activate;
- `write_file` with omitted path or `/final_report.md` activates ownership only after matching success, changed `modified_at`, valid data, and non-empty content;
- failed, mismatched, malformed, stale timestamp, and non-final paths do not activate.

- [ ] **Step 5: Run correlation tests and confirm RED**

Run:

```bash
uv run pytest tests/test_completion_guard.py -q -k "write_todos or write_file"
```

Expected: FAIL because `before_model`/`abefore_model` activation is absent.

- [ ] **Step 6: Implement minimal sync/async activation hooks**

Implement one pure correlator used by both hooks. Match `AIMessage.tool_calls[*]["id"]` to `ToolMessage.tool_call_id`, require `status != "error"`, then validate resulting state. Content-only `write_file` resolves to `/final_report.md`.

- [ ] **Step 7: Run lifecycle/correlation tests and confirm GREEN**

Run:

```bash
uv run pytest tests/test_completion_guard.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add research_agent/completion_guard.py tests/test_completion_guard.py
git commit -m "feat: scope completion artifacts to each run"
```

### Task 4: Add Automatic Continuation and Safe Exhaustion

**Files:**
- Modify: `research_agent/completion_guard.py`
- Modify: `tests/test_completion_guard.py`

- [ ] **Step 1: Write failing continuation tests**

Assert terminal `AIMessage` plus active incomplete plan:

- increments attempts and returns `jump_to: "model"`;
- tags message with `response_metadata.resume_intermediate=True` without losing ID, content blocks, usage, or tool metadata;
- injects attempt/reason guidance only through `ModelRequest.system_message`;
- preserves files/todos/messages;
- ignores model responses containing tool calls;
- consumes exactly configured limits `1`, `2`, and `3`.

- [ ] **Step 2: Run continuation tests and confirm RED**

Run:

```bash
uv run pytest tests/test_completion_guard.py -q -k "continue or attempt or guidance"
```

Expected: FAIL because guard routing is absent.

- [ ] **Step 3: Implement continuation hooks**

Decorate sync/async hooks with:

```python
@hook_config(can_jump_to=["model", "end"])
```

Return a tagged replacement message, incremented attempt state, and `jump_to="model"` while below limit. Add model-request wrapper that appends ephemeral `<CompletionGuard>` guidance to the leading system message.

- [ ] **Step 4: Write failing exhaustion tests**

Assert limit exhaustion returns a checkpointable tagged message, current exhausted run ID, safe counts/reason, and `jump_to="end"`. Assert `after_agent` raises only for matching current run ID. Assert `langgraph_api.serde.default()` exposes safe `ResearchIncompleteError` text and no todo labels/research content.

- [ ] **Step 5: Run exhaustion tests and confirm RED**

Run:

```bash
uv run pytest tests/test_completion_guard.py -q -k "exhaust or serialize"
```

Expected: FAIL because exhaustion handling is absent.

- [ ] **Step 6: Implement checkpoint-before-error exhaustion**

Create `ResearchIncompleteError(RuntimeError)`. Persist safe metadata in `after_model`, jump to end, then raise from `after_agent` only when stored and current run IDs match.

- [ ] **Step 7: Run guard tests and confirm GREEN**

Run:

```bash
uv run pytest tests/test_completion_guard.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add research_agent/completion_guard.py tests/test_completion_guard.py
git commit -m "feat: continue incomplete research within one run"
```

### Task 5: Make Verification Routing Ollama-Safe

**Files:**
- Modify: `research_agent/agent.py:150-170`
- Modify: `research_agent/agent.py:390-930`
- Modify: `research_agent/agent.py:1180-1220`
- Modify: `tests/test_verification.py`
- Modify: `tests/test_verification_progress.py`

- [ ] **Step 1: Write failing verification routing tests**

Cover sync and async paths:

- verification runs only for current owned report after all research todos complete;
- non-final `needs_revision` stores feedback and returns `jump_to="model"`;
- triggering terminal AI message receives `resume_intermediate` metadata;
- no persisted `SystemMessage` is added;
- next `ModelRequest.system_message` contains feedback with valid Ollama role order;
- pass records verified report `modified_at`;
- final revision-limit verdict records accepted-at-limit without another jump;
- no completion attempt is consumed.

- [ ] **Step 2: Run verification tests and confirm RED**

Run:

```bash
uv run pytest tests/test_verification.py tests/test_verification_progress.py -q
```

Expected: FAIL on missing jump, persisted `SystemMessage`, and absent ownership/version state.

- [ ] **Step 3: Implement minimal verification changes**

Make `ResearchState` inherit `CompletionState`. Gate verification through completion policy. Change both hooks to `can_jump_to=["model", "end"]`. On non-final revision, set `verification_feedback`, tag terminal response, and return `jump_to="model"`; rely on existing `_build_system_instruction` request-time feedback injection. Record verified report timestamp or accepted-at-limit state.

- [ ] **Step 4: Run verification tests and confirm GREEN**

Run:

```bash
uv run pytest tests/test_verification.py tests/test_verification_progress.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add research_agent/agent.py tests/test_verification.py tests/test_verification_progress.py
git commit -m "fix: route report verification explicitly"
```

### Task 6: Finalize and Stream the Accepted Report Once

**Files:**
- Modify: `research_agent/completion_guard.py`
- Modify: `research_agent/agent.py:390-930`
- Modify: `tests/test_completion_guard.py`
- Modify: `tests/test_verification.py`

- [ ] **Step 1: Write failing finalization tests**

Cover:

- pending-todo report triggers continuation and emits no cited/final report messages;
- verification revision emits no provisional report and does not mark `_streamed_files`;
- accepted report emits cited responses, separator, and final report exactly once;
- subsequent hooks do not duplicate output;
- edited report invalidates prior verification timestamp;
- eval logging begins only after the same readiness predicate passes.

- [ ] **Step 2: Run finalization tests and confirm RED**

Run:

```bash
uv run pytest tests/test_completion_guard.py tests/test_verification.py -q -k "stream or finaliz or eval"
```

Expected: FAIL because `ResearchStateMiddleware` currently streams before completion/verification.

- [ ] **Step 3: Move finalization behind guard acceptance**

Remove premature cited/final report streaming from both `ResearchStateMiddleware` hooks. Add a shared finalization helper in `completion_guard.py`; call it only when plan, owned report, and verification readiness pass. Gate eval logging with the same predicate.

- [ ] **Step 4: Run finalization and verification tests and confirm GREEN**

Run:

```bash
uv run pytest tests/test_completion_guard.py tests/test_verification.py tests/test_verification_progress.py -q
```

Expected: PASS with one final report emission.

- [ ] **Step 5: Commit**

```bash
git add research_agent/completion_guard.py research_agent/agent.py tests/test_completion_guard.py tests/test_verification.py
git commit -m "fix: stream only accepted research reports"
```

### Task 7: Register Middleware and Prove Full Graph Routing

**Files:**
- Modify: `research_agent/agent.py:1310-1338`
- Modify: `tests/test_completion_guard.py`
- Modify: `tests/test_agent_contracts.py`

- [ ] **Step 1: Write failing compiled-graph tests**

Build small sync and async agents with deterministic model responses and real todo/file tools. Prove:

- middleware registration yields after-model order Research → Resume → Completion;
- inactive plan → real `write_todos` → matching successful tool result → non-empty plan → terminal response → second model call;
- real content-only `write_file` owns `/final_report.md` only after success;
- one graph invocation/run ID spans all automatic continuations;
- files, todos, tool events, and message IDs survive the loop;
- exhaustion reaches `after_agent` and fails;
- accepted plan/report exits normally.

- [ ] **Step 2: Run compiled tests and confirm RED**

Run:

```bash
uv run pytest tests/test_completion_guard.py tests/test_agent_contracts.py -q -k "compiled or middleware_order"
```

Expected: FAIL because middleware is not registered/routed.

- [ ] **Step 3: Register middleware in required order**

Use:

```python
middleware=[
    TodoListMiddleware(system_prompt=""),
    ClarificationMiddleware(),
    CompletionGuardMiddleware(),
    ResumeMiddleware(),
    ResearchStateMiddleware(),
    ...,
]
```

- [ ] **Step 4: Run compiled tests and confirm GREEN**

Run:

```bash
uv run pytest tests/test_completion_guard.py tests/test_agent_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add research_agent/agent.py tests/test_completion_guard.py tests/test_agent_contracts.py
git commit -m "feat: enforce research completion in compiled graph"
```

### Task 8: Document, Verify, and Review

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Document configuration**

Add:

```bash
# Automatic same-run continuations when a planned report is incomplete (1-3)
MAX_COMPLETION_ATTEMPTS=3
```

- [ ] **Step 2: Run focused suites**

```bash
uv run pytest tests/test_completion_guard.py tests/test_agent_contracts.py tests/test_resume.py tests/test_verification.py tests/test_verification_progress.py tests/test_tools.py -q
```

Expected: PASS.

- [ ] **Step 3: Run quality checks**

```bash
uv run ruff check research_agent/completion_guard.py research_agent/agent.py tests/test_completion_guard.py tests/test_agent_contracts.py tests/test_verification.py tests/test_verification_progress.py
git diff --check
```

Expected: PASS with no whitespace errors.

- [ ] **Step 4: Run broader regression suite**

```bash
uv run pytest tests/ -q
```

Expected: PASS, or document only verified pre-existing/environmental failures with exact test names and unchanged reproduction on the branch base.

- [ ] **Step 5: Inspect Threadroot evidence**

```bash
threadroot score latest
```

Expected: score available if the preflight run recorded successfully; otherwise document the existing `.codex/threadroot` permission failure.

- [ ] **Step 6: Commit documentation**

```bash
git add .env.example
git commit -m "docs: configure bounded completion attempts"
```

- [ ] **Step 7: Request final code review**

Review the complete feature range against `documents/history/specs/2026-08-17-research-completion-guard-design.md`, with emphasis on run ownership, middleware routing, stale state, report visibility, error safety, and unrelated working-tree preservation.
