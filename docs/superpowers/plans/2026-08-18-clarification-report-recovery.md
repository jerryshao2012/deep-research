# Clarification and Report Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept Gemma's shorthand clarification tool call deterministically so research can interrupt, resume, and reach existing `/final_report.md` completion enforcement.

**Architecture:** Add a copy-on-normalize compatibility adapter at `ClarificationBatch`'s model-validation boundary while keeping its serialized output canonical and strict. Keep `run_clarification`, response contracts, completion guard, and file-writing ownership unchanged; improve the model-facing tool description and prove the complete shorthand interrupt/resume boundary with tests.

**Tech Stack:** Python 3.12+, Pydantic v2, LangChain tools, LangGraph interrupts, pytest, Ruff.

---

## File map

- Modify `research_agent/research_subagent/clarification/contracts.py`: normalize exact whole-batch shorthand before existing strict validation.
- Modify `research_agent/research_subagent/clarification/tool.py`: add one compact canonical input example to model-facing tool description.
- Modify `tests/test_clarification.py`: contract, replay, args-schema, interrupt/resume, and prompt-description regressions.
- Verify only, no expected production edit: `research_agent/research_subagent/tools.py`, `research_agent/completion_guard.py`, `tests/test_write_file.py`, and `tests/test_completion_guard.py`.

### Task 1: Prove shorthand failure and immutable normalization contract

**Files:**
- Modify: `tests/test_clarification.py`
- Modify: `research_agent/research_subagent/clarification/contracts.py:1-62`

- [ ] **Step 1: Add failing exact-shorthand and replay tests**

Add a fixture matching the complete observed Gemma call, with all three
questions and string options. Preserve a deep copy and validate twice. Assert
deterministic `question_1`/`question_2`/`question_3` IDs and per-question option
IDs, including removal of the standalone Other choice only where at least two
concrete choices remain:

```python
def _gemma_shorthand() -> dict[str, object]:
    return {
        "questions": [
            {
                "question": "What is the intended audience?",
                "options": [
                    "Researchers",
                    "Industry professionals",
                    "Other (please specify)",
                ],
            },
            {
                "question": "What is the primary focus?",
                "options": [
                    "Theory and algorithms",
                    "Practical implementation",
                    "Other (please specify)",
                ],
            },
            {
                "question": "What depth is required?",
                "options": [
                    "Comprehensive review",
                    "Executive summary",
                    "Other (please specify)",
                ],
            },
        ]
    }


def test_gemma_shorthand_normalizes_without_mutating_replay_input() -> None:
    raw = _gemma_shorthand()
    original = copy.deepcopy(raw)

    first = ClarificationBatch.model_validate(raw)
    second = ClarificationBatch.model_validate(raw)

    assert raw == original
    assert first == second
    dumped = first.model_dump()
    assert [question["id"] for question in dumped["questions"]] == [
        "question_1", "question_2", "question_3"
    ]
    assert [option["id"] for option in dumped["questions"][0]["options"]] == [
        "option_1", "option_2"
    ]
    assert all(
        option["label"] != "Other (please specify)"
        for question in dumped["questions"]
        for option in question["options"]
    )
```

- [ ] **Step 2: Add failing rejection tests for ambiguous shapes**

Cover a hybrid batch containing one canonical and one shorthand question, a
single item mixing `question` with canonical keys, non-string options,
shorthand item extras, and an unexpected top-level batch field. Each must raise
`ValidationError`; normalization must never hide `extra="forbid"` errors. Add
parametrized exact Other matching for whitespace/case and prove `Another
option` is retained.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
uv run pytest \
  tests/test_clarification.py::test_gemma_shorthand_normalizes_without_mutating_replay_input \
  tests/test_clarification.py -q
```

Expected: new shorthand normalization test fails with missing canonical fields;
existing tests remain green up to that failure.

- [ ] **Step 4: Implement minimal copy-on-normalize adapter**

In `contracts.py`, add `Any`/`Mapping` imports and a before validator on
`ClarificationBatch`. Keep the helper private and return newly allocated data:

```python
_OTHER_LABELS = {"other", "other (please specify)"}


def _normalise_shorthand_batch(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    if set(value) != {"questions"}:
        return value
    raw_questions = value.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return value

    shorthand_flags = [
        isinstance(question, Mapping)
        and set(question) == {"question", "options"}
        for question in raw_questions
    ]
    if not any(shorthand_flags):
        return value
    if not all(shorthand_flags):
        return value

    questions: list[dict[str, Any]] = []
    for question_index, raw_question in enumerate(raw_questions, start=1):
        prompt = raw_question["question"]
        raw_options = raw_question["options"]
        if not isinstance(raw_options, list) or not all(
            isinstance(option, str) for option in raw_options
        ):
            return value
        concrete = [
            option
            for option in raw_options
            if option.strip().casefold() not in _OTHER_LABELS
        ]
        labels = concrete if len(concrete) >= 2 else list(raw_options)
        questions.append(
            {
                "id": f"question_{question_index}",
                "prompt": prompt,
                "type": "single_select",
                "options": [
                    {"id": f"option_{index}", "label": label}
                    for index, label in enumerate(labels, start=1)
                ],
            }
        )
    return {"questions": questions}
```

Call it from `@model_validator(mode="before")`. Do not mutate `value`, question
mappings, or option lists. Existing field validators remain authoritative.

- [ ] **Step 5: Run focused contract tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_clarification.py -q
```

Expected: all clarification tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add research_agent/research_subagent/clarification/contracts.py \
  tests/test_clarification.py
git commit -m "fix: normalize local clarification tool calls"
```

### Task 2: Prove model schema and real checkpointed interrupt/resume boundary

**Files:**
- Modify: `tests/test_clarification.py`
- Modify: `research_agent/research_subagent/clarification/tool.py:72-96`

- [ ] **Step 1: Add failing real LangGraph interrupt/resume regression**

Build on `test_interrupt_pauses_and_resumes_same_checkpoint`: compile a real
`StateGraph` with `InMemorySaver`, and make its node validate the same preserved
shorthand object through `clarify_requirements.args_schema` before calling
`run_clarification`. Invoke once to obtain the real `__interrupt__`, then resume
the same checkpoint with `Command(resume=...)`:

```python
def test_shorthand_replays_and_resumes_same_checkpoint_canonically() -> None:
    raw = _gemma_shorthand()
    original = copy.deepcopy(raw)
    executions = 0

    def clarify_node(state: _InterruptState) -> Command:
        nonlocal executions
        executions += 1
        batch = clarify_requirements.args_schema.model_validate(raw)
        return run_clarification(batch, tool_call_id="tool-call-1")

    graph = (
        StateGraph(_InterruptState)
        .add_node("clarify", clarify_node)
        .add_edge(START, "clarify")
        .add_edge("clarify", END)
        .compile(checkpointer=InMemorySaver())
    )
    config = {"configurable": {"thread_id": "shorthand-clarification"}}

    interrupted = graph.invoke({"messages": []}, config)
    payload = interrupted["__interrupt__"][0].value
    assert payload["questions"][0]["id"] == "question_1"

    resumed = graph.invoke(Command(resume=_matching_response(payload)), config)
    result = json.loads(resumed["messages"][-1].content)
    assert result["status"] == "answered"
    assert result["requirements"][0]["selected_labels"] == ["Researchers"]
    assert executions == 2
    assert raw == original
```

Use a small response helper to answer every question in the three-question
payload. Adapt indexing to the concrete typed payload returned by LangGraph.
This is intentionally not a fake `interrupt_fn`: it proves node replay,
checkpoint correlation, canonical payload serialization, and immutable raw
tool-call args together.

- [ ] **Step 2: Add failing model-facing description test**

Assert `clarify_requirements.description` contains all canonical keys (`id`,
`prompt`, `type`, option `id`, `label`) and `single_select`.

- [ ] **Step 3: Run new tests and verify RED**

Run:

```bash
uv run pytest tests/test_clarification.py -q
```

Expected: boundary behavior passes after Task 1; description test fails because
the compact canonical example is absent.

- [ ] **Step 4: Add compact canonical JSON example to tool docstring**

Add one example after usage rules, keeping the automatic Other behavior clear:

```text
Canonical question example:
{"id":"audience","prompt":"Who is this for?","type":"single_select",
 "options":[{"id":"researchers","label":"Researchers"},
            {"id":"leaders","label":"Industry leaders"}]}
Do not add an Other option; the interface provides it automatically.
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_clarification.py -q
uv run ruff check research_agent/research_subagent/clarification \
  tests/test_clarification.py
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit Task 2**

```bash
git add research_agent/research_subagent/clarification/tool.py \
  tests/test_clarification.py
git commit -m "docs: show canonical clarification tool input"
```

### Task 3: Verify report completion remains owned by existing guard

**Files:**
- Verify: `research_agent/research_subagent/tools.py`
- Verify: `research_agent/completion_guard.py`
- Verify: `tests/test_agent_contracts.py`
- Verify: `tests/test_completion_guard.py`
- Verify: `tests/test_write_file.py`

- [ ] **Step 1: Run complete backend regression slice**

```bash
uv run pytest tests/test_clarification.py tests/test_agent_contracts.py \
  tests/test_completion_guard.py tests/test_write_file.py -q
```

Expected: all tests pass, including content-only `write_file` defaulting to
`/final_report.md` and completion readiness rejecting a missing report.

- [ ] **Step 2: Run static checks**

```bash
uv run ruff check research_agent/research_subagent/clarification \
  tests/test_clarification.py tests/test_agent_contracts.py
uv run python -m compileall -q research_agent/research_subagent/clarification
git diff --check main...HEAD
git status --short
```

Expected: Ruff/compile/diff exit 0; status is clean.

- [ ] **Step 3: Record code-area memory**

Record `contracts.py` as the deterministic local-model compatibility boundary
and `tool.py` as the canonical model-facing schema example.
