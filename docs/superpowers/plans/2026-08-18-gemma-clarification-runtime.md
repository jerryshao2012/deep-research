# Gemma Clarification Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Gemma shorthand cross the real LangChain tool boundary and make `gemma4` Ollama orchestration emit visible, actionable output by default.

**Architecture:** Preserve strict canonical contracts while moving legacy shorthand compatibility into an annotated field validator that survives LangChain subset-schema construction. Add a provider-local Ollama reasoning policy in the shared model factory; exact Gemma repository names default to `reasoning=False`, explicit boolean environment input wins, and other providers/models retain existing behavior.

**Tech Stack:** Python 3.13, Pydantic v2, LangChain structured tools/ToolNode, LangGraph interrupts/checkpoints, `langchain-ollama`, pytest, Ruff.

---

## File map

- `research_agent/research_subagent/clarification/contracts.py`: canonical clarification contracts plus exact legacy-shorthand normalization.
- `tests/test_clarification.py`: real `tool_call_schema`, ToolNode, interrupt/resume, replay, immutability, and advertised-schema regressions.
- `research_agent/model_factory.py`: provider selection and Ollama constructor policy.
- `tests/test_model_factory_timeout.py`: isolated provider environment and constructor/runtime policy tests.
- `.env.example`: operator-facing `OLLAMA_REASONING` configuration and restart note.

### Task 1: Normalize shorthand at the actual ToolNode boundary

**Files:**
- Modify: `tests/test_clarification.py:631-700`
- Modify: `research_agent/research_subagent/clarification/contracts.py:1-105`

- [ ] **Step 1: Write failing real-boundary tests**

Add tests that use the exact observed payload shape and validate through
`clarify_requirements.tool_call_schema`:

```python
def test_tool_call_schema_normalizes_exact_gemma_shorthand(
    gemma_shorthand_batch: dict[str, Any],
) -> None:
    raw = deepcopy(gemma_shorthand_batch)

    parsed = clarify_requirements.tool_call_schema.model_validate(raw)

    assert parsed.questions[0].id == "question_1"
    assert parsed.questions[0].prompt == "Who is this report for?"
    assert raw == gemma_shorthand_batch


def test_tool_call_json_schema_advertises_only_canonical_contract() -> None:
    schema = clarify_requirements.tool_call_schema.model_json_schema()
    question = schema["$defs"]["ClarificationQuestion"]["properties"]

    assert set(question) == {"id", "prompt", "type", "options"}
    assert "question" not in question
```

Update `test_real_checkpointed_clarification_replays_node_and_preserves_shorthand`
to validate with `clarify_requirements.tool_call_schema`, not `args_schema`.
Add a compiled ToolNode/StateGraph test whose AI tool call contains exact
shorthand; assert the first run interrupts with canonical questions, resume
returns the correlated ToolMessage, and replay does not mutate raw arguments.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
/Users/jerryshao/Documents/projects/IBM/ai/deep-research/.venv/bin/pytest \
  tests/test_clarification.py -q
```

Expected: new `tool_call_schema` and ToolNode tests fail with missing canonical
question fields; existing direct `args_schema` tests still pass.

- [ ] **Step 3: Implement field-level normalization**

In `contracts.py`, import `Annotated` and `BeforeValidator`. Move normalization
into a standalone field function receiving the raw questions list:

```python
def _normalize_shorthand_questions(value: Any) -> Any:
    if not isinstance(value, list) or not value:
        return value

    normalized_questions: list[dict[str, Any]] = []
    for question_index, raw_question in enumerate(value, start=1):
        if not isinstance(raw_question, Mapping) or set(raw_question) != {
            "question",
            "options",
        }:
            return value
        raw_options = raw_question["options"]
        if not isinstance(raw_options, list) or not all(
            isinstance(option, str) for option in raw_options
        ):
            return value
        concrete_options = [
            option
            for option in raw_options
            if option.strip().casefold()
            not in {"other", "other (please specify)"}
        ]
        options = concrete_options if len(concrete_options) >= 2 else list(raw_options)
        normalized_questions.append(
            {
                "id": f"question_{question_index}",
                "prompt": raw_question["question"],
                "type": "single_select",
                "options": [
                    {"id": f"option_{option_index}", "label": option}
                    for option_index, option in enumerate(options, start=1)
                ],
            }
        )
    return normalized_questions


ClarificationQuestions = Annotated[
    list[ClarificationQuestion],
    BeforeValidator(_normalize_shorthand_questions),
]


class ClarificationBatch(StrictContract):
    questions: ClarificationQuestions = Field(min_length=1, max_length=3)
```

Remove only the old batch-level `normalize_shorthand` model validator. Keep the
after-validator for unique canonical question IDs unchanged.

- [ ] **Step 4: Run clarification tests and verify GREEN**

Run the command from Step 2. Expected: all clarification tests pass, including
real ToolNode interrupt/resume/replay and canonical JSON schema assertions.

- [ ] **Step 5: Commit Task 1**

```bash
git add research_agent/research_subagent/clarification/contracts.py \
  tests/test_clarification.py
git commit -m "fix: normalize clarification at tool call boundary"
```

### Task 2: Make Gemma Ollama reasoning policy explicit

**Files:**
- Modify: `tests/test_model_factory_timeout.py:15-45,173-250,402-430`
- Modify: `research_agent/model_factory.py:280-430`

- [ ] **Step 1: Write failing model-factory tests**

Add `OLLAMA_REASONING` to `_PROVIDER_ENV`, then add constructor-policy tests.
Use real guarded model objects and inspect their inherited ChatOllama fields:

```python
@pytest.mark.parametrize(
    "model_name",
    ["gemma4", " gemma4:latest ", "team/gemma4:27b", "REGISTRY/TEAM/GEMMA4:LATEST"],
)
def test_gemma4_defaults_ollama_reasoning_off(monkeypatch, model_name):
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")
    monkeypatch.setenv("MODEL_NAME", model_name)
    model = model_factory.get_configured_model(bypass_cache=True)
    assert model.reasoning is False


@pytest.mark.parametrize("model_name", ["gemma40", "my-gemma4", "gemma4x", "qwen3:latest"])
def test_non_gemma4_omits_default_reasoning(monkeypatch, model_name):
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")
    monkeypatch.setenv("MODEL_NAME", model_name)
    model = model_factory.get_configured_model(bypass_cache=True)
    assert "reasoning" not in model.model_fields_set


@pytest.mark.parametrize("raw", ["1", " true ", "YES", "on"])
def test_explicit_ollama_reasoning_true_wins(monkeypatch, raw): ...


@pytest.mark.parametrize("raw", ["0", " false ", "NO", "off"])
def test_explicit_ollama_reasoning_false_wins(monkeypatch, raw): ...


@pytest.mark.parametrize("raw", ["", " ", "sometimes", "2"])
def test_invalid_ollama_reasoning_fails_safely(monkeypatch, raw):
    ...
    with pytest.raises(ValueError, match="^OLLAMA_REASONING must be a boolean$"):
        model_factory.get_configured_model(bypass_cache=True)
```

Add a mixed-provider test with valid AWS configuration plus invalid
`OLLAMA_REASONING`; assert AWS is selected and no Ollama parsing occurs. Include
explicit override tests on a non-Gemma Ollama name to prove family-independent
override precedence.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
/Users/jerryshao/Documents/projects/IBM/ai/deep-research/.venv/bin/pytest \
  tests/test_model_factory_timeout.py -q
```

Expected: reasoning-policy assertions fail because ChatOllama currently receives
no `reasoning` keyword and invalid values are ignored.

- [ ] **Step 3: Implement exact policy helpers**

Add focused helpers near `_build_configured_model`:

```python
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _is_gemma4_model(model_name: str) -> bool:
    repository = model_name.strip().casefold().rsplit("/", 1)[-1]
    repository = repository.rsplit(":", 1)[0]
    return repository == "gemma4"


def _ollama_reasoning_kwargs(model_name: str) -> dict[str, bool]:
    raw = os.getenv("OLLAMA_REASONING")
    if raw is None:
        return {"reasoning": False} if _is_gemma4_model(model_name) else {}
    normalized = raw.strip().casefold()
    if normalized in _TRUE_VALUES:
        return {"reasoning": True}
    if normalized in _FALSE_VALUES:
        return {"reasoning": False}
    raise ValueError("OLLAMA_REASONING must be a boolean")
```

Call `_ollama_reasoning_kwargs(model_name)` only inside the selected Ollama
branch and merge its result into the ChatOllama constructor kwargs. Do not read
or validate the variable in higher-precedence branches.

- [ ] **Step 4: Run model-factory tests and verify GREEN**

Run the command from Step 2. Expected: all provider/factory tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add research_agent/model_factory.py tests/test_model_factory_timeout.py
git commit -m "fix: disable Gemma reasoning for tool orchestration"
```

### Task 3: Document and verify the integrated behavior

**Files:**
- Modify: `.env.example:10-25,105-130`

- [ ] **Step 1: Document operator behavior**

Add near Ollama configuration:

```dotenv
# Optional Ollama reasoning override. When unset, exact gemma4 repositories
# default to false so tool calls and final output remain visible.
# Accepted values: true/false, 1/0, yes/no, on/off.
# Restart LangGraph (or clear the model cache) after changing this value.
# OLLAMA_REASONING=false
```

- [ ] **Step 2: Run focused regression suite**

```bash
/Users/jerryshao/Documents/projects/IBM/ai/deep-research/.venv/bin/pytest \
  tests/test_clarification.py \
  tests/test_model_factory_timeout.py \
  tests/test_agent_contracts.py \
  tests/test_completion_guard.py \
  tests/test_write_file.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run static checks**

```bash
/Users/jerryshao/Documents/projects/IBM/ai/deep-research/.venv/bin/ruff check \
  research_agent/research_subagent/clarification/contracts.py \
  research_agent/model_factory.py \
  tests/test_clarification.py \
  tests/test_model_factory_timeout.py
/Users/jerryshao/Documents/projects/IBM/ai/deep-research/.venv/bin/python -m compileall -q \
  research_agent/research_subagent/clarification/contracts.py \
  research_agent/model_factory.py
git diff --check main...HEAD
```

If Ruff is not installed, record that exact environment limitation; do not call
the check successful.

- [ ] **Step 4: Commit documentation**

```bash
git add -f .env.example
git commit -m "docs: describe Ollama reasoning override"
```

- [ ] **Step 5: Independent code review**

Request review of the cumulative production/test diff. Resolve Critical and
Important findings, rerun the focused suite and static checks, and keep each
review correction in a separate commit.

### Task 4: Integrate and prove the local runtime

**Files:** None beyond prior tasks.

- [ ] **Step 1: Fast-forward local main**

With both worktrees clean, fast-forward `main` to the reviewed feature head.
Preserve the feature branch until post-merge verification passes.

- [ ] **Step 2: Verify merged main**

Rerun the focused suite from Task 3 against the project-root `main` checkout and
confirm clean `git status --short --branch`.

- [ ] **Step 3: Restart LangGraph from merged main**

Stop the existing localhost:2024 process, start:

```bash
.venv/bin/langgraph dev --no-reload --no-browser
```

Verify `http://127.0.0.1:2024/docs` returns HTTP 200.

- [ ] **Step 4: Run fresh Gemma acceptance flow**

Use exact `MODEL_NAME=gemma4:latest` with `OLLAMA_REASONING` unset. In a fresh
thread, require:

1. exact shorthand crosses the real tool boundary;
2. interrupt payload is canonical;
3. user response resumes the correlated call;
4. follow-up model responses contain visible tool calls;
5. todos reach completed;
6. generation-owned `/final_report.md` exists and is non-empty.

Do not treat a historical failed thread as acceptance evidence.

- [ ] **Step 5: Clean feature branch/worktree**

After merged-main verification and live acceptance pass, remove only the clean
feature worktree and delete the merged local feature branch. Do not push.
