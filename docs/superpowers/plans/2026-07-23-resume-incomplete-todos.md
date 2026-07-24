# Resume Incomplete Research Todos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make common standalone continuation phrases resume incomplete persisted research todos through a bounded, backend-managed completion loop.

**Architecture:** A pure resume policy classifies phrases, inspects todo state, and builds safety-limit output. Per-run private kwargs bind intent to the triggering message. A LangChain middleware injects ephemeral resume instructions and tags incomplete terminal model messages, while both custom-server execution paths coordinate up to `MAX_RESUME_ROUNDS` and expose only the final assistant response.

**Tech Stack:** Python 3.12, FastAPI, LangGraph/Deep Agents, LangChain middleware, SQLite/PostgreSQL/CosmosDB abstraction, pytest.

---

## File Map

- Create `research_agent/resume/__init__.py`: public resume-policy and middleware exports.
- Create `research_agent/resume/policy.py`: deterministic phrase grammar, todo inspection, configuration parsing, hidden-message detection, safety-limit response.
- Create `research_agent/resume/middleware.py`: non-persisted model instruction and intermediate-message tagging.
- Create `tests/test_resume.py`: policy and middleware unit tests.
- Modify `agent.py:8-35, 125-246, 1141-1168`: make existing state setup resume-aware and register resume middleware.
- Modify `db.py:801-874`: persist private per-run kwargs without a schema migration.
- Modify `server.py:196-228, 254-330, 375-460, 465-828, 831-990, 1142-1190, 1193-1332`: bind candidate text, coordinate rounds, filter intermediate messages, preserve todos, and emit progress.
- Modify `research_agent_cli.py:211-223, 309-314`: reuse shared todo inspection.
- Modify `tests/test_server.py`: background-run binding, looping, cancellation, and safety-limit tests.
- Modify `tests/test_frontend_api_contract.py`: streaming loop, progress, transcript filtering, and normal-flow compatibility tests.
- Modify `tests/test_tools.py`: ensure resume rounds preserve original request state and suppress persisted startup messages.
- Modify `.env.example`: document `MAX_RESUME_ROUNDS=3`.

No frontend source change is required; existing composer submissions already reach `/threads/{thread_id}/runs/stream`.

### Task 1: Pure Resume Policy

**Files:**
- Create: `research_agent/resume/__init__.py`
- Create: `research_agent/resume/policy.py`
- Create: `tests/test_resume.py`

- [ ] **Step 1: Write failing phrase-classifier parameter tests**

Add positive cases for every base phrase, supported `please` placement, NFKC/case/whitespace normalization, and trailing `.`/`!`. Add negative cases for `?`, negation, unsupported punctuation, substrings, and added instructions.

```python
@pytest.mark.parametrize(
    "text",
    [
        "continue",
        " Please continue! ",
        "GO ON.",
        "keep going please",
        "resume",
        "please proceed",
        "finish the remaining tasks",
        "complete the remaining tasks, please!",
    ],
)
def test_is_resume_intent_accepts_supported_grammar(text: str) -> None:
    assert is_resume_intent(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "continue?",
        "do not continue",
        "should we continue?",
        "continue researching security",
        "go on, but compare vendors",
        "please; continue",
    ],
)
def test_is_resume_intent_rejects_non_resume_messages(text: str) -> None:
    assert is_resume_intent(text) is False
```

- [ ] **Step 2: Run classifier tests to prove red state**

Run:

```bash
uv run pytest tests/test_resume.py -k "resume_intent" -v
```

Expected: FAIL during import because `research_agent.resume.policy` does not exist.

- [ ] **Step 3: Implement deterministic grammar**

Use an explicit base allowlist; never use substring or model classification.

```python
BASE_RESUME_PHRASES = frozenset(
    {
        "continue",
        "go on",
        "keep going",
        "resume",
        "proceed",
        "finish the remaining tasks",
        "complete the remaining tasks",
    }
)


def _accepted_resume_phrases() -> frozenset[str]:
    phrases: set[str] = set()
    for base in BASE_RESUME_PHRASES:
        phrases.update(
            {
                base,
                f"please {base}",
                f"{base} please",
                f"{base}, please",
            }
        )
    return frozenset(phrases)


ACCEPTED_RESUME_PHRASES = _accepted_resume_phrases()


def normalize_resume_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.rstrip(".!").rstrip()


def is_resume_intent(text: str) -> bool:
    return normalize_resume_text(text) in ACCEPTED_RESUME_PHRASES
```

- [ ] **Step 4: Run classifier tests to prove green state**

Run:

```bash
uv run pytest tests/test_resume.py -k "resume_intent" -v
```

Expected: all classifier tests PASS.

- [ ] **Step 5: Write failing todo-inspection and configuration tests**

Cover `pending`, `in_progress`, `completed`, mixed-case status, malformed containers/entries, and invalid environment values.

```python
def test_inspect_todos_returns_only_known_incomplete_items() -> None:
    inspection = inspect_todos(
        [
            {"content": "A", "status": "pending"},
            {"content": "B", "status": "in_progress"},
            {"content": "C", "status": "completed"},
            {"content": "bad"},
            "bad",
        ]
    )
    assert [todo["content"] for todo in inspection.incomplete] == ["A", "B"]
    assert inspection.malformed_count == 2


@pytest.mark.parametrize(("raw", "expected"), [(None, 3), ("", 3), ("0", 3), ("-1", 3), ("x", 3), ("5", 5)])
def test_get_max_resume_rounds_uses_positive_values_only(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int
) -> None:
    if raw is None:
        monkeypatch.delenv("MAX_RESUME_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("MAX_RESUME_ROUNDS", raw)
    assert get_max_resume_rounds() == expected
```

- [ ] **Step 6: Implement todo inspection, round limit, and final summary**

```python
INCOMPLETE_TODO_STATUSES = frozenset({"pending", "in_progress"})
DEFAULT_MAX_RESUME_ROUNDS = 3


@dataclass(frozen=True)
class TodoInspection:
    incomplete: tuple[dict[str, Any], ...]
    malformed_count: int = 0

    @property
    def has_incomplete(self) -> bool:
        return bool(self.incomplete)


def inspect_todos(value: Any) -> TodoInspection:
    if not isinstance(value, list):
        return TodoInspection(())
    incomplete: list[dict[str, Any]] = []
    malformed = 0
    for item in value:
        if not isinstance(item, dict):
            malformed += 1
            continue
        status = str(item.get("status", "")).strip().casefold()
        if status in INCOMPLETE_TODO_STATUSES:
            incomplete.append(item)
        elif status != "completed":
            malformed += 1
    return TodoInspection(tuple(incomplete), malformed)


def get_max_resume_rounds() -> int:
    raw = os.getenv("MAX_RESUME_ROUNDS", str(DEFAULT_MAX_RESUME_ROUNDS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_RESUME_ROUNDS
    return value if value > 0 else DEFAULT_MAX_RESUME_ROUNDS


def build_round_limit_message(inspection: TodoInspection, rounds: int) -> str:
    lines = [
        f"Resume safety limit reached after {rounds} rounds.",
        "Remaining tasks:",
    ]
    for item in inspection.incomplete:
        label = str(item.get("content") or item.get("task") or "Unnamed task")
        lines.append(f"- [{item.get('status')}] {label}")
    lines.append("Send another resume phrase to continue.")
    return "\n".join(lines)
```

Also add `is_resume_intermediate_message(message)` and `visible_messages(messages)` helpers. They must inspect either dictionary or LangChain `response_metadata` and filter only `resume_intermediate is True`.
For invalid/non-positive `MAX_RESUME_ROUNDS`, emit one warning containing the
setting name and fallback value but not unrelated environment data. Add a
`caplog` assertion for that warning.

- [ ] **Step 7: Run all policy tests**

Run:

```bash
uv run pytest tests/test_resume.py -k "not middleware" -v
```

Expected: PASS.

- [ ] **Step 8: Commit pure policy**

```bash
git add research_agent/resume/__init__.py research_agent/resume/policy.py tests/test_resume.py
git commit -m "feat: add incomplete todo resume policy"
```

### Task 2: Ephemeral Resume Middleware

**Files:**
- Create: `research_agent/resume/middleware.py`
- Modify: `research_agent/resume/__init__.py`
- Modify: `agent.py:68-75, 1141-1168`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write failing middleware instruction tests**

Instantiate middleware with an injected config getter. Assert inactive config leaves `ModelRequest.system_message` unchanged. Assert active config plus pending todos appends resume instructions without adding a persisted state message.

```python
def test_resume_middleware_injects_ephemeral_system_instruction() -> None:
    middleware = ResumeMiddleware(
        config_getter=lambda: {
            "configurable": {
                "resume_incomplete_todos": True,
                "resume_round": 2,
                "resume_max_rounds": 3,
            }
        }
    )
    request = make_model_request(
        todos=[{"content": "Finish report", "status": "pending"}],
        system_text="base",
    )

    configured = middleware.configure_request(request)

    assert configured.system_message.content.startswith("base")
    assert "Resume round 2 of 3" in configured.system_message.content
    assert configured.messages == request.messages
    assert configured.state == request.state
```

- [ ] **Step 2: Write failing terminal-message tagging tests**

Assert an active resume round tags a terminal AI message only when incomplete todos remain. Tool-call AI messages and completed todo states must remain untagged.

```python
def test_after_model_tags_incomplete_terminal_message() -> None:
    message = AIMessage(id="final-1", content="I stopped early")
    updates = middleware.after_model(
        {
            "messages": [message],
            "todos": [{"content": "A", "status": "pending"}],
        },
        runtime=None,
    )
    tagged = updates["messages"][0]
    assert tagged.id == "final-1"
    assert tagged.response_metadata["resume_intermediate"] is True
```

- [ ] **Step 3: Run middleware tests to prove red state**

Run:

```bash
uv run pytest tests/test_resume.py -k "middleware or after_model" -v
```

Expected: FAIL because middleware is not implemented/registered.

- [ ] **Step 4: Implement middleware**

Use `ModelRequest.override(system_message=...)`, preserving the existing system content. Read per-run values only from `get_config()["configurable"]`; do not add resume flags to graph state.

```python
RESUME_INSTRUCTION = """<ResumeIncompleteTodos>
Resume round {round_number} of {max_rounds}. Preserve the original research
goal, selected skill, files, and valid existing todo plan. Execute every pending
or in-progress item. Do not replace the plan merely because this run resumed.
Mark an item completed only after its work is done. Synthesize the requested
final output after all items are complete.
</ResumeIncompleteTodos>"""


class ResumeMiddleware(AgentMiddleware):
    state_schema = PlanningState

    def __init__(self, *, config_getter=get_config) -> None:
        super().__init__()
        self._config_getter = config_getter

    def configure_request(self, request: ModelRequest) -> ModelRequest:
        configurable = self._config_getter().get("configurable", {})
        if configurable.get("resume_incomplete_todos") is not True:
            return request
        if not inspect_todos(request.state.get("todos")).has_incomplete:
            return request
        base = request.system_message.content if request.system_message else ""
        instruction = RESUME_INSTRUCTION.format(
            round_number=configurable.get("resume_round", 1),
            max_rounds=configurable.get("resume_max_rounds", 3),
        )
        return request.override(
            system_message=SystemMessage(content=f"{base}\n\n{instruction}".strip())
        )
```

Implement sync and async wrappers like `ClarificationMiddleware`. Implement `after_model` with `@hook_config(can_jump_to=["end"])`; copy the final `AIMessage` with the same ID and merged `response_metadata` only when resume config is active, no tool calls exist, and `inspect_todos(state.get("todos")).has_incomplete`.

Import `PlanningState` from `langchain.agents.middleware.todo`. Do not import
`ResearchState` from `agent.py`; that would create a circular dependency when
`agent.py` imports `ResumeMiddleware`.

- [ ] **Step 5: Write failing tests for existing state middleware during resume rounds**

In `tests/test_tools.py`, inject resume config into `ResearchStateMiddleware`.
Seed `/research_request.md` with the original goal, set the latest human message
to `Please continue!`, and assert:

- `/research_request.md` remains the original goal;
- no `Starting research…`/document-search AI status message is added;
- verification and research-pass counters are not reset from the resume phrase;
- parameter extraction does not change skill/document/web settings.

- [ ] **Step 6: Make `ResearchStateMiddleware.before_agent` resume-aware**

Add `config_getter=get_config` dependency injection to
`ResearchStateMiddleware.__init__`. Read
`configurable.resume_incomplete_todos`. During an active resume round:

- skip `_seed_research_request_file`;
- skip persisted startup progress messages;
- skip fresh-message hash/reset logic;
- skip parameter extraction from the resume phrase;
- retain output-folder setup and normal system instruction construction from
  existing state.

Do not store resume flags in `ResearchState`. Add `from langgraph.config import
get_config` in `agent.py`.

- [ ] **Step 7: Register middleware after clarification and before research-state middleware**

```python
middleware=[
    ClarificationMiddleware(),
    ResumeMiddleware(),
    ResearchStateMiddleware(),
],
```

Keep resume prompt injection in `wrap_model_call`; do not extend `ResearchState`.

- [ ] **Step 8: Run middleware, state-preservation, and registration tests**

Run:

```bash
uv run pytest \
  tests/test_resume.py \
  tests/test_tools.py -k "resume or middleware_seeds" \
  tests/test_clarification.py::test_agent_registers_clarification_tool_and_middleware -v
```

Expected: PASS. Extend registration assertion to include `ResumeMiddleware()`.

- [ ] **Step 9: Commit middleware**

```bash
git add agent.py research_agent/resume tests/test_resume.py tests/test_tools.py tests/test_clarification.py
git commit -m "feat: inject resume instructions per run"
```

### Task 3: Bind Resume Intent to Run Records

**Files:**
- Modify: `db.py:801-874`
- Modify: `server.py:216-228, 1193-1242, 1258-1302`
- Test: `tests/test_server.py`
- Test: `tests/test_frontend_api_contract.py`

- [ ] **Step 1: Write failing DB and endpoint tests**

Test `db.create_run(..., kwargs={"_resume_candidate": "Please continue!"})` for SQLite and assert `db.get_run()` returns the private value. Test `_api_run()` omits underscore-prefixed kwargs. Test both run endpoints bind the last user message from their own request, not an older/later thread message.

Add focused mocked-backend assertions that PostgreSQL receives serialized
kwargs in the existing `INSERT_RUN_POSTGRES` slot and Cosmos stores the kwargs
mapping in its run item. No live database/cloud service is required.

```python
def test_create_run_persists_private_resume_candidate_without_exposing_it(client) -> None:
    thread_id = client.post("/threads").json()["thread_id"]
    response = client.post(
        f"/threads/{thread_id}/runs",
        json={"input": {"messages": [{"role": "user", "content": "Please continue!"}]}},
    )
    run_id = response.json()["run_id"]
    assert db.get_run(run_id)["kwargs"]["_resume_candidate"] == "Please continue!"
    assert "_resume_candidate" not in response.json()["kwargs"]
```

- [ ] **Step 2: Run binding tests to prove red state**

Run:

```bash
uv run pytest tests/test_server.py -k "resume_candidate" -v
```

Expected: FAIL because `create_run` cannot accept/persist kwargs.

- [ ] **Step 3: Extend `db.create_run` without schema changes**

Add optional `metadata` and `kwargs` parameters defaulting to empty dictionaries. Serialize them in existing SQLite/PostgreSQL `metadata`/`kwargs` columns and store them in Cosmos items.

```python
def create_run(
    run_id: str,
    thread_id: str,
    assistant_id: str,
    created_at: str,
    multitask_strategy: str | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> None:
    ...
```

- [ ] **Step 4: Add exact triggering-message extraction and API privacy filtering**

Add `_last_user_message(raw_messages)` that iterates the request list in reverse. Store it as `kwargs={"_resume_candidate": candidate}` when non-empty. Change `_api_run()` to return only kwargs whose keys do not begin with `_`.

Do not classify from the thread's latest message inside `_execute_run`; it must read this run's stored candidate.

- [ ] **Step 5: Run binding and existing run-contract tests**

Run:

```bash
uv run pytest tests/test_server.py tests/test_frontend_api_contract.py -k "run or resume_candidate" -v
```

Expected: PASS.

- [ ] **Step 6: Commit run binding**

```bash
git add db.py server.py tests/test_server.py tests/test_frontend_api_contract.py
git commit -m "feat: bind resume intent to triggering run"
```

### Task 4: Background Bounded Resume Coordinator

**Files:**
- Modify: `server.py:831-990`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing multi-round background test**

Seed thread values with pending todos. Mock `agent.ainvoke` to return pending on round one and completed on round two. Submit `Please continue!`; wait for terminal status. Assert two calls, per-round config, preserved original phrase, persisted completed todos, and only final untagged assistant output in public thread values.

```python
assert mock_agent.ainvoke.await_count == 2
first_config = mock_agent.ainvoke.await_args_list[0].kwargs["config"]["configurable"]
second_config = mock_agent.ainvoke.await_args_list[1].kwargs["config"]["configurable"]
assert first_config["resume_incomplete_todos"] is True
assert (first_config["resume_round"], second_config["resume_round"]) == (1, 2)
assert thread_values["todos"][0]["status"] == "completed"
assert [m["content"] for m in thread_values["messages"] if m["type"] == "human"][-1] == "Please continue!"
```

- [ ] **Step 2: Write failing fallback tests**

Cover:

- completed/no todos: one ordinary invocation with resume config absent;
- malformed todo state: one ordinary invocation;
- non-resume long message with pending todos: one ordinary invocation;
- three unchanged incomplete results: stop at default limit and append visible remaining-task summary;
- safety-limit path calls `agent.aupdate_state` and checkpoint-backed
  `/state`/history expose the summary;
- `MAX_RESUME_ROUNDS=2`: only two invocations;
- cancellation after first round: no second invocation;
- agent exception: latest state retained and run status `error`;
- first run candidate `continue` remains authoritative after a later user message is appended.

- [ ] **Step 3: Run background tests to prove red state**

Run:

```bash
uv run pytest tests/test_server.py -k "resume" -v
```

Expected: FAIL because `_execute_run` invokes the agent once and does not persist todos.

- [ ] **Step 4: Add current-state and persistence helpers**

Extract focused server helpers:

```python
async def _current_agent_values(thread_id: str, thread: dict[str, Any]) -> dict[str, Any]:
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await agent.aget_state(config)
    except Exception:
        snapshot = None
    values = dict(snapshot.values) if snapshot and snapshot.values else dict(thread.get("values") or {})
    values["messages"] = list(thread.get("messages") or values.get("messages") or [])
    return values


def _agent_input_state(values: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "messages", "files", "todos", "doc_folder", "skill", "no_web",
        "wiki_query_complete", "existing_reports",
    }
    return {key: value for key, value in values.items() if key in allowed and value is not None}
```

Persist `todos` in `serializable_result`; preserve all existing fields.

- [ ] **Step 5: Implement bounded loop in `_execute_run`**

At execution time:

1. Load run record and `_resume_candidate`.
2. Load current agent values/checkpoint.
3. Activate resume only when `is_resume_intent(candidate)` and `inspect_todos(values["todos"]).has_incomplete`.
4. For each round, build config with `thread_id`, recursion limit, and resume keys.
5. Invoke agent, check cancellation, inspect returned todos, and stop immediately on completion.
6. Reinvoke with returned state while incomplete and below limit.
7. At limit, create `AIMessage(content=build_round_limit_message(...))`,
   persist it through `await agent.aupdate_state(config, {"messages":
   [limit_message]})`, reload the checkpoint, and use a local-result fallback
   only when the agent adapter has no update-state support.
8. Persist full non-message state including todos, but serialize only `visible_messages(result["messages"])` into DB/public values.

Keep candidate text out of logs. Log only thread ID, run ID, round/max, incomplete counts, malformed count, and stop reason.

- [ ] **Step 6: Run background resume and regression tests**

Run:

```bash
uv run pytest tests/test_server.py -v
```

Expected: PASS, including existing lifecycle/cancellation/interrupt tests.

- [ ] **Step 7: Commit background coordinator**

```bash
git add server.py tests/test_server.py
git commit -m "feat: complete remaining todos in bounded runs"
```

### Task 5: Streaming Resume Loop and Transcript Visibility

**Files:**
- Modify: `server.py:196-206, 254-330, 375-460, 465-828, 1142-1190, 1258-1332`
- Test: `tests/test_frontend_api_contract.py`

- [ ] **Step 1: Write failing streaming completion-loop test**

Provide a fake agent that supports `astream_events`, `aget_state`, and `aupdate_state`. First round snapshot contains tagged intermediate assistant output plus pending todos; second contains completed todos plus untagged final output.

Assert:

- one initial `metadata` event;
- additive `metadata` event with `resume_round=2`, `resume_max_rounds=3`;
- no intermediate assistant text in SSE body;
- final assistant text appears once;
- final `values` includes completed todos;
- one `end` event with success;
- run record reaches `success`.
- resume activation comes from this run's `_resume_candidate` plus current
  execution-time todos, not the latest thread message.
- structured logs contain run/thread IDs, round/max, incomplete counts, and
  stop reason without message/todo content.

- [ ] **Step 2: Write failing public-history filtering tests**

Seed checkpointer/DB values with:

```python
AIMessage(
    id="hidden",
    content="I stopped early",
    response_metadata={"resume_intermediate": True},
)
AIMessage(id="visible", content="Final report")
```

Assert hidden text is absent from:

- `GET /threads/{thread_id}`;
- `GET /threads/{thread_id}/state`;
- `GET /threads/{thread_id}/history`;
- `POST /threads/{thread_id}/history`;
- final SSE `values`.

Assert tool messages and untagged assistant messages remain visible.

Also submit a recognized resume phrase against completed todos and assert the
stream stays single-round, preserves ordinary token/message events, and emits
no resume-progress metadata.

- [ ] **Step 3: Run streaming tests to prove red state**

Run:

```bash
uv run pytest tests/test_frontend_api_contract.py -k "resume or intermediate" -v
```

Expected: FAIL because stream execution is single-round and serializers expose every message.

- [ ] **Step 4: Centralize public message serialization**

Add:

```python
def _serialize_visible_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        serialize_message(message)
        for message in visible_messages(messages)
    ]
```

Use it in `_api_thread`, `_build_thread_history_item`, `_resolve_thread_history`, `get_thread_state`, DB persistence, and final SSE `values`. Work on copied dictionaries so history filtering never mutates checkpointer values.

- [ ] **Step 5: Refactor `_stream_run_events` into an outer round loop**

Preserve current token streaming byte-for-byte for ordinary runs. For active resume mode:

- load `_resume_candidate` from `db.get_run(run_id)["kwargs"]` when the
  generator begins;
- load latest checkpoint/DB values at generator execution time and include
  `todos` in `input_state`;
- activate resume only when `is_resume_intent(candidate)` and current todo
  inspection is incomplete;
- pass `resume_incomplete_todos`, `resume_round`, and `resume_max_rounds` in
  each round's `configurable` mapping;
- emit metadata/progress and tool `updates` events immediately;
- suppress `on_chat_model_stream`, `on_chat_model_end`, `on_chain_end`, `on_llm_stream`, and `on_llm_end` assistant text during each round;
- after a round, load snapshot and inspect todos;
- if incomplete and below limit, emit additive metadata:

```python
{
    "run_id": run_id,
    "thread_id": thread_id,
    "status": "running",
    "resume_round": next_round,
    "resume_max_rounds": max_rounds,
    "incomplete_todo_count": len(inspection.incomplete),
}
```

- start next round with the same checkpoint/config and incremented `resume_round`;
- when complete, emit only untagged final assistant messages created after the
  run's initial message-ID/count boundary as full `messages` events;
- at safety limit, persist the visible limit message with
  `await agent.aupdate_state(...)`, reload the snapshot, then emit it;
- check `db.get_run(run_id)["status"] == "cancelled"` before each new round;
- emit/persist final values and `end` exactly once.

This intentionally disables token-level assistant text only during resume mode. It prevents intermediate terminal content from flashing in the UI while keeping tool/progress visibility and preserving ordinary-run streaming.

Capture the initial set of message IDs plus initial message count before the
first round. Final selection must prefer messages with IDs not in that set and
fall back to the post-count slice only for messages lacking IDs. Apply
`visible_messages()` and require AI messages without tool calls. This prevents
replaying older untagged assistant responses.

- [ ] **Step 6: Persist recovery state after each hidden round**

After every hidden round, update DB with visible serialized messages plus current files, todos, skill, document settings, and wiki state. Keep tagged intermediate model/tool messages in LangGraph checkpoint. If process exits between rounds, another resume phrase can continue from saved todo/file state.

- [ ] **Step 7: Run streaming and history tests**

Run:

```bash
uv run pytest tests/test_frontend_api_contract.py -v
```

Expected: PASS.

- [ ] **Step 8: Run combined server regressions**

Run:

```bash
uv run pytest tests/test_server.py tests/test_frontend_api_contract.py tests/test_clarification.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit streaming behavior**

```bash
git add server.py tests/test_frontend_api_contract.py
git commit -m "feat: stream bounded todo resumption"
```

### Task 6: Share CLI Detection and Document Configuration

**Files:**
- Modify: `research_agent_cli.py:211-223, 309-314`
- Modify: `tests/test_research_agent_cli_e2e.py`
- Modify: `.env.example`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write failing CLI shared-policy test**

Remove direct tests of private `_has_incomplete_todos` if present. Patch/import `inspect_todos` and assert `should_retry_with_invoke` uses the shared known-status behavior: pending/in-progress retry, completed/unknown malformed statuses do not.

- [ ] **Step 2: Run CLI test to prove red state**

Run:

```bash
uv run pytest tests/test_research_agent_cli_e2e.py -k "incomplete_todos or retry" -v
```

Expected: FAIL until CLI imports the shared helper.

- [ ] **Step 3: Replace duplicate CLI detector**

Delete `_has_incomplete_todos` and change:

```python
def should_retry_with_invoke(result: dict, skill: str | None = None) -> bool:
    if inspect_todos(result.get("todos")).has_incomplete:
        return True
    content = select_output_content(result, skill)
    return _looks_like_incomplete_delegation(content)
```

- [ ] **Step 4: Document environment setting**

Add to `.env.example` near graph/model execution settings:

```bash
# Maximum agent rounds for a user-triggered incomplete-todo resume (default: 3)
MAX_RESUME_ROUNDS=3
```

- [ ] **Step 5: Run focused suite**

Run:

```bash
uv run pytest \
  tests/test_resume.py \
  tests/test_research_agent_cli_e2e.py \
  tests/test_server.py \
  tests/test_frontend_api_contract.py \
  tests/test_clarification.py -v
```

Expected: PASS.

- [ ] **Step 6: Run prompt and packaging regressions**

Run:

```bash
uv run pytest tests/test_prompts_validation.py tests/test_packaging.py -v
```

Expected: PASS.

- [ ] **Step 7: Run lint on changed Python files**

Run:

```bash
uv run ruff check \
  agent.py \
  db.py \
  server.py \
  research_agent_cli.py \
  research_agent/resume \
  tests/test_resume.py \
  tests/test_server.py \
  tests/test_frontend_api_contract.py \
  tests/test_research_agent_cli_e2e.py
```

Expected: PASS. If Ruff is unavailable in the locked environment, record that and rely on pytest plus `python -m compileall`.

- [ ] **Step 8: Verify syntax**

Run:

```bash
uv run python -m compileall \
  agent.py \
  db.py \
  server.py \
  research_agent_cli.py \
  research_agent/resume
```

Expected: exit code 0.

- [ ] **Step 9: Commit shared CLI/config work**

```bash
git add .env.example research_agent_cli.py tests/test_research_agent_cli_e2e.py
git commit -m "chore: document bounded resume configuration"
```

### Task 7: Final Verification and Review

**Files:**
- Review all files changed by Tasks 1-6.

- [ ] **Step 1: Run complete test suite**

Run:

```bash
uv run pytest tests/ -v
```

Expected: all tests PASS. If environment-dependent real API tests are skipped, record skip count and reasons.

- [ ] **Step 2: Inspect Threadroot score**

Because the repository recorded a Threadroot prep attempt, run:

```bash
threadroot score latest --json
```

Expected: score output if a run exists. If no run was recorded because prep failed, record `no latest run` without creating `.threadroot/`.

- [ ] **Step 3: Review diff for privacy and transcript guarantees**

Check:

- no user message/todo contents in structured logs;
- `_resume_candidate` absent from API responses;
- resume config remains per-run and absent from persisted graph state;
- hidden messages remain in checkpointer but not public stream/state/history;
- ordinary runs retain token streaming;
- every DB persistence path includes `todos`.

- [ ] **Step 4: Request code review**

Use `superpowers:requesting-code-review` with the spec and this plan. Fix blocking issues and rerun the narrowest affected tests.

- [ ] **Step 5: Confirm clean worktree and commits**

Run:

```bash
git status --short
git log --oneline --decorate -8
```

Expected: no uncommitted implementation changes; task commits visible.
