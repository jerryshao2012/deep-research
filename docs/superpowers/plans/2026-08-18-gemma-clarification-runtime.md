# Gemma Clarification Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Gemma shorthand cross the real LangChain tool boundary and make `gemma4` Ollama orchestration emit visible, actionable output by default.

**Architecture:** Preserve strict canonical contracts while moving legacy shorthand compatibility into an annotated field validator that survives LangChain subset-schema construction. Add a provider-local Ollama reasoning policy in the shared model factory; exact Gemma repository names default to `reasoning=False`, explicit boolean environment input wins, and other providers/models retain existing behavior.

**Tech Stack:** Python 3.13, Pydantic v2, LangChain structured tools/ToolNode, LangGraph interrupts/checkpoints, `langchain-ollama`, pytest, Ruff.

---

## File map

- `research_agent/research_subagent/clarification/contracts.py`: canonical clarification contracts plus exact legacy-shorthand normalization.
- `research_agent/research_subagent/clarification/tool.py`: ToolNode adapter and injected tool-call correlation.
- `tests/test_clarification.py`: real `tool_call_schema`, ToolNode, interrupt/resume, replay, immutability, and advertised-schema regressions.
- `research_agent/model_factory.py`: provider selection and Ollama constructor policy.
- `tests/test_model_factory_timeout.py`: isolated provider environment and constructor/runtime policy tests.
- `.env.example`: operator-facing `OLLAMA_REASONING` configuration and restart note.

### Task 1: Normalize shorthand at the actual ToolNode boundary

**Files:**
- Modify: `tests/test_clarification.py:631-700`
- Modify: `research_agent/research_subagent/clarification/contracts.py:1-105`
- Modify: `research_agent/research_subagent/clarification/tool.py:1-95`

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
First add a compiled ToolNode/StateGraph test whose AI tool call is canonical;
assert it interrupts rather than returning a strict-schema error for injected
`runtime`. Add the same test with exact shorthand;
assert the first run interrupts with canonical questions, resume returns the
correlated ToolMessage, and replay does not mutate raw arguments.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
/Users/jerryshao/Documents/projects/IBM/ai/deep-research/.venv/bin/pytest \
  tests/test_clarification.py -q
```

Expected: shorthand `tool_call_schema` and ToolNode tests fail with missing
canonical question fields. The canonical ToolNode test separately fails because
injected `runtime` enters strict argument validation. Existing direct
`args_schema` tests still pass.

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

In `tool.py`, replace `ToolRuntime` injection with the only injected value the
adapter consumes:

```python
from typing import Annotated, Any

from langchain_core.tools import InjectedToolCallId, tool


@tool(args_schema=ClarificationBatch)
def clarify_requirements(
    questions: list[ClarificationQuestion],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    ...
    return run_clarification(
        ClarificationBatch(questions=questions),
        tool_call_id=tool_call_id,
    )
```

- [ ] **Step 4: Run clarification tests and verify GREEN**

Run the command from Step 2. Expected: all clarification tests pass, including
real ToolNode interrupt/resume/replay and canonical JSON schema assertions.

- [ ] **Step 5: Commit Task 1**

```bash
git add research_agent/research_subagent/clarification/contracts.py \
  research_agent/research_subagent/clarification/tool.py \
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
override precedence. Add a cache-lifecycle test: construct cached unset Gemma,
change `OLLAMA_REASONING`, prove cached object/setting remains unchanged, then
prove bypass and `clear_model_cache()` reconstruct with the override. For the
Gemma default assertion, also call `model._chat_params([])` and require the
actual Ollama SDK request params to contain `"think": False`.

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

Replace stale `OLLAMA_BASE_URL`/`OLLAMA_MODEL` example names with the factory's
maintained `OLLAMA_API_BASE`/`MODEL_NAME`, then add:

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
  research_agent/research_subagent/clarification/tool.py \
  research_agent/model_factory.py \
  tests/test_clarification.py \
  tests/test_model_factory_timeout.py
/Users/jerryshao/Documents/projects/IBM/ai/deep-research/.venv/bin/python -m compileall -q \
  research_agent/research_subagent/clarification/contracts.py \
  research_agent/research_subagent/clarification/tool.py \
  research_agent/model_factory.py
git diff --check
```

If Ruff is not installed, record that exact environment limitation; do not call
the check successful.

- [ ] **Step 4: Commit documentation**

```bash
git add -f .env.example
git commit -m "docs: describe Ollama reasoning override"
```

Rerun `git diff --check main...HEAD` after this commit so documentation is
included in cumulative verification.

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

Resolve the listener before stopping anything:

```bash
lsof -nP -iTCP:2024 -sTCP:LISTEN
ps -p <PID> -o pid=,ppid=,etime=,command=
lsof -a -p <PID> -d cwd -Fn
```

Stop it only if command is LangGraph and cwd is this backend project.

Do not rely on exported shell variables: `langgraph.json` loads `.env` and can
replace them. Build a temporary config beside `langgraph.json` so relative graph
paths remain valid, and point it at a permission-restricted temporary env file.
Copy existing non-provider settings (including Tavily/auth settings), remove all
AWS/Azure/Google/Anthropic/OpenAI/Ollama provider selectors, then add only
`MODEL_NAME=gemma4:latest` and
`OLLAMA_API_BASE=http://localhost:11434`. Deliberately omit
`OLLAMA_REASONING`. Launch the CLI under `env -i` with only `PATH`, `HOME`, and
`TMPDIR` inherited, using `--config <TEMP_CONFIG>`. Register a shell trap that
deletes both temporary files on exit; do not print their contents because they
can contain credentials.

Create the controlled files from the project-root `main` checkout:

```bash
acceptance_env="$(mktemp "$PWD/.langgraph-acceptance-env.XXXXXX")"
acceptance_config="$(mktemp "$PWD/.langgraph-acceptance-config.XXXXXX")"
acceptance_key="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
chmod 600 "$acceptance_env" "$acceptance_config"
trap 'rm -f -- "$acceptance_env" "$acceptance_config"' EXIT

ACCEPTANCE_ENV="$acceptance_env" ACCEPTANCE_API_KEY="$acceptance_key" \
  .venv/bin/python - <<'PY'
import json
import os

from dotenv import dotenv_values

provider_keys = {
    "AWS_BEDROCK_ENDPOINT",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_CLIENT_ID",
    "AZURE_OPENAI_SCOPE",
    "AZURE_OPENAI_API_VERSION",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OLLAMA_API_BASE",
    "OLLAMA_REASONING",
    "MODEL_NAME",
}
values = {
    key: value
    for key, value in dotenv_values(".env").items()
    if key not in provider_keys and value is not None
}
values.update(
    MODEL_NAME="gemma4:latest",
    OLLAMA_API_BASE="http://localhost:11434",
    UPLOAD_API_KEY=os.environ["ACCEPTANCE_API_KEY"],
    ALLOW_ALL_THREADS="true",
)
with open(os.environ["ACCEPTANCE_ENV"], "w", encoding="utf-8") as stream:
    for key, value in values.items():
        stream.write(f"{key}={json.dumps(value, ensure_ascii=False)}\n")
PY

jq --arg env "$acceptance_env" '.env = $env' \
  langgraph.json > "$acceptance_config"
```

The generated temporary config otherwise equals `langgraph.json`; only its
absolute `.env` path changes. Start:

```bash
env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
  .venv/bin/langgraph dev --config "$acceptance_config" \
  --no-reload --no-browser
```

Verify `http://127.0.0.1:2024/docs` returns HTTP 200. Before creating a thread,
query the running server and fail unless its own diagnostics report the exact
provider/model and a successful probe:

```bash
curl -sS -H "X-API-Key: $acceptance_key" \
  http://127.0.0.1:2024/storage/info | jq -e '
  .model_factory.detected_provider == "ollama" and
  .model_factory.configuration.MODEL_NAME == "gemma4:latest" and
  .model_factory.configuration.OLLAMA_API_BASE == "http://localhost:11434" and
  .model_factory.test_request.success == true'
```

- [ ] **Step 4: Run fresh Gemma acceptance flow**

Preflight the frontend with
`lsof -nP -iTCP:3000 -sTCP:LISTEN`. If absent, start `yarn dev` from
`/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui` and wait for HTTP
200 before continuing. Open `http://localhost:3000/chat?assistantId=research`, create a fresh thread,
submit `Help to do a research on graph engineering`, and capture the new thread
ID from the URL. Before answering, inspect canonical interrupt state:

```bash
curl -sS -H "X-API-Key: $acceptance_key" \
  http://127.0.0.1:2024/threads/<THREAD_ID>/state | jq \
  '{interrupts, todos:.values.todos, messages:.values.messages[-4:]}'
```

Answer the clarification in the frontend so the same correlated call resumes.
After the run reaches a terminal state, capture exact acceptance evidence:

```bash
curl -sS -H "X-API-Key: $acceptance_key" \
  http://127.0.0.1:2024/threads/<THREAD_ID>/state | jq -e '
  (.values.todos | length > 0) and
  (all(.values.todos[]; .status == "completed")) and
  (.values.files["/final_report.md"].content | type == "string" and length > 0) and
  (.values.completion_report_owned == true)'
```

Also inspect the post-resume message tail and require at least one visible tool
call before completion:

```bash
curl -sS -H "X-API-Key: $acceptance_key" \
  http://127.0.0.1:2024/threads/<THREAD_ID>/state | jq \
  '.values.messages | map({type,name,tool_calls,content}) | .[-20:]'
```

Use exact `MODEL_NAME=gemma4:latest` with `OLLAMA_REASONING` unset. Require:

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
