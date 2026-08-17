# Model-Call Timeout and Ollama Cancellation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound every application-owned model call by a total wall-clock deadline, cancel active requests, and optionally unload an Ollama model without affecting cloud providers.

**Architecture:** Add a focused `BaseChatModel`-compatible guard module that owns timeout parsing, immutable provider metadata, async cancellation, sync-to-async bridging, bound-runnable streaming preservation, override adaptation, and Ollama unload. Model factory returns guarded models after existing retry/rate shaping; request-boundary middleware is installed in root, explicit research, and explicit general-purpose subagents. Verification judges become truly async and CLI sync entrypoints use the guarded bridge.

**Tech Stack:** Python 3.12+, asyncio, threading, httpx, LangChain Runnable/BaseChatModel APIs, LangGraph middleware, pytest/pytest-asyncio.

---

## File map

- Create `research_agent/model_call_guard.py` — timeout configuration, runtime metadata, `BaseChatModel` proxy, guarded bound runnable, sync bridge, override registry, unload helper, and safe errors.
- Modify `research_agent/model_factory.py:272-384` — construct provider metadata/native HTTP deadlines and guard the selected model after retry wrapping.
- Modify `research_agent/agent.py:54,693-1066,1361-1433` — propagate control errors and register request-boundary guards for root/explicit/general-purpose agents.
- Modify `research_agent/research_subagent/utils/verification.py:117-235` — propagate timeout/cancellation from direct sufficiency and adversarial judge calls.
- Modify `.deepagents/skills/golden-dataset/scripts/skill_model_factory.py` — reuse the shared guarded factory for active skill judge calls.
- Modify `research_agent/cli.py:107-131,388-568` — preserve model-control failures and cancel active bridge on CLI interruption.
- Create `tests/test_model_call_guard.py` — unit/integration coverage for deadlines, cancellation, unload, binding, streaming, and overrides.
- Create `tests/test_model_factory_timeout.py` — provider precedence, metadata, and native client deadline contracts.
- Modify `tests/test_agent_contracts.py` — compiled root/subagent registration and exactly-once wrapping.
- Modify `tests/test_verification.py` — direct judge timeout/cancellation pass-through.
- Modify `tests/test_research_agent_cli_e2e.py` — CLI interruption and timeout behavior.
- Modify `.env.example`, `documents/guides/configuration.md`, and `documents/guides/reliability.md` — public configuration and operator guidance.
- Modify `scripts/render_azure_containerapp_config.py:97-136`, `tests/test_azure_persistence_scripts.py`, `deploy-aws.sh`, and `tests/test_aws_persistence_scripts.py` — deployment defaults.

### Task 1: Configuration, metadata, and safe errors

**Files:**
- Create: `research_agent/model_call_guard.py`
- Create: `tests/test_model_call_guard.py`

- [ ] **Step 1: Write failing configuration and metadata tests**

```python
@pytest.mark.parametrize("raw", [None, "", "bad", "nan", "inf", "0", "-1"])
def test_timeout_falls_back_to_safe_default(raw, monkeypatch):
    if raw is None:
        monkeypatch.delenv("MODEL_CALL_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", raw)
    assert ModelCallPolicy.from_env().timeout_seconds == 300.0


def test_force_unload_requires_explicit_true(monkeypatch):
    monkeypatch.setenv("OLLAMA_FORCE_UNLOAD_ON_CANCEL", "true")
    assert ModelCallPolicy.from_env().force_ollama_unload is True


def test_timeout_error_is_safe():
    error = ModelCallTimeoutError(provider="ollama", timeout_seconds=3, unload_requested=True)
    assert "ollama" in str(error)
    assert "3" in str(error)
    assert "prompt" not in str(error).lower()
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest tests/test_model_call_guard.py -q`

Expected: collection failure because `research_agent.model_call_guard` does not exist.

- [ ] **Step 3: Implement minimal immutable policy and metadata types**

```python
DEFAULT_MODEL_CALL_TIMEOUT_SECONDS = 300.0
MODEL_CANCEL_GRACE_SECONDS = 2.0
OLLAMA_UNLOAD_TIMEOUT_SECONDS = 2.0

@dataclass(frozen=True)
class ModelCallPolicy:
    timeout_seconds: float
    force_ollama_unload: bool

    @classmethod
    def from_env(cls) -> "ModelCallPolicy": ...

@dataclass(frozen=True)
class ModelRuntimeMetadata:
    provider: Literal["aws_bedrock", "azure_openai", "openai", "google", "anthropic", "ollama", "unknown"]
    model_name: str
    base_url: str | None = None

class ModelCallTimeoutError(RuntimeError): ...
class UnsupportedModelOverrideError(RuntimeError): ...
```

Parsing must accept finite positive floats only, use `str2bool(..., False)` for unload, normalize base URLs without credentials/query/fragment, and never include request content in errors.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_model_call_guard.py -q`

Expected: all initial policy/error tests pass.

- [ ] **Step 5: Commit**

```bash
git add research_agent/model_call_guard.py tests/test_model_call_guard.py
git commit -m "feat: define safe model call policy"
```

### Task 2: Async total deadline and bounded Ollama cleanup

**Files:**
- Modify: `research_agent/model_call_guard.py`
- Modify: `tests/test_model_call_guard.py`

- [ ] **Step 1: Add RED tests for async success, timeout, cancellation, and cleanup suppression**

Use `asyncio.Event` fakes and a local `httpx`/ASGI streaming endpoint. Required assertions:

```python
async def test_async_timeout_cancels_request_and_returns_within_grace(): ...
async def test_external_cancel_preserves_cancelled_error(): ...
async def test_cancellation_suppressing_handler_cannot_block_caller(): ...
async def test_late_exception_is_consumed_without_loop_warning(): ...
async def test_streaming_server_observes_disconnect_on_timeout(): ...
```

Set test timeouts to 20–50 ms and assert elapsed time is less than configured deadline plus cleanup grace plus a small scheduler margin.

- [ ] **Step 2: Run async slice and confirm RED**

Run: `uv run pytest tests/test_model_call_guard.py -q -k 'async or disconnect or late_exception'`

Expected: failures because guarded async invocation is absent.

- [ ] **Step 3: Implement `_run_with_deadline`**

```python
async def _run_with_deadline(factory, *, policy, metadata, unload):
    task = asyncio.create_task(factory())
    try:
        done, _ = await asyncio.wait({task}, timeout=policy.timeout_seconds)
        if done:
            return task.result()
        task.cancel()
        await _bounded_task_cleanup(task)
        await _maybe_unload_ollama(policy, metadata, unload)
        raise ModelCallTimeoutError(...)
    except asyncio.CancelledError:
        task.cancel()
        await _bounded_task_cleanup(task)
        await _maybe_unload_ollama(policy, metadata, unload)
        raise
```

`_bounded_task_cleanup` uses `asyncio.wait`, never `wait_for(task)`; a pending task receives a done callback that calls `task.exception()` unless cancelled. Shield unload only long enough to apply its own two-second timeout.

- [ ] **Step 4: Add RED unload tests**

```python
async def test_ollama_default_never_unloads(): ...
async def test_ollama_opt_in_unloads_once_with_active_model(): ...
async def test_cloud_provider_never_unloads_even_when_flag_true(): ...
async def test_unload_failure_preserves_timeout(): ...
async def test_unload_failure_preserves_cancelled_error(): ...
```

Assert POST payload is `{"model": "gemma4:latest", "keep_alive": 0}` and target is normalized `<base>/api/generate`; no prompt field is sent.

- [ ] **Step 5: Implement bounded unload helper**

Use injected `httpx.AsyncClient`/callable for tests. Catch/log unload failures and preserve original exception precedence.

- [ ] **Step 6: Run async/unload tests and confirm GREEN**

Run: `uv run pytest tests/test_model_call_guard.py -q -k 'async or disconnect or unload or cancel'`

Expected: pass with no leaked-task warnings.

- [ ] **Step 7: Commit**

```bash
git add research_agent/model_call_guard.py tests/test_model_call_guard.py
git commit -m "feat: cancel timed out model requests"
```

### Task 3: BaseChatModel proxy, tool binding, streaming, and sync bridge

**Files:**
- Modify: `research_agent/model_call_guard.py`
- Modify: `tests/test_model_call_guard.py`

- [ ] **Step 1: Add RED tests for runnable parity**

Cover `invoke`, `ainvoke`, `stream`, `astream`, `bind`, and `bind_tools`. A fake runnable emits `AIMessageChunk` tool-call chunks and usage metadata. Assert:

```python
assert isinstance(guarded, BaseChatModel)
bound = guarded.bind_tools([fake_tool])
assert is_guarded_model(bound)
assert bound.runtime_metadata == guarded.runtime_metadata
assert list(bound.stream(...)) == expected_chunks
assert await collect(bound.astream(...)) == expected_chunks
```

Callbacks, tags, configurable values, response metadata, message IDs, tool-call chunks, and usage metadata must be byte/equality-identical.

- [ ] **Step 2: Add RED slow-trickle sync tests**

Run an async stream in which each chunk arrives faster than the native inactivity timeout but total duration exceeds the guard deadline. Assert sync `stream()`/`invoke()` raises `ModelCallTimeoutError`, local server sees disconnect, elapsed time is bounded, and the named bridge daemon exits for supported fakes.

- [ ] **Step 3: Implement `GuardedChatModel`, bound-runnable guard, and sync bridge**

Required public surface:

```python
class GuardedChatModel(BaseChatModel):
    inner: BaseChatModel
    runtime_metadata: ModelRuntimeMetadata
    policy: ModelCallPolicy
    @property
    def _llm_type(self) -> str: return f"guarded-{self.inner._llm_type}"
    @property
    def profile(self): return self.inner.profile
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs): ...
    def _generate(self, messages, stop=None, run_manager=None, **kwargs): ...
    async def _astream(self, messages, stop=None, run_manager=None, **kwargs): ...
    def _stream(self, messages, stop=None, run_manager=None, **kwargs): ...
    def bind(self, **kwargs): return GuardedBoundRunnable(self.inner.bind(**kwargs), ...)
    def bind_tools(self, tools, **kwargs): return GuardedBoundRunnable(self.inner.bind_tools(tools, **kwargs), ...)

class GuardedBoundRunnable(Runnable):
    def invoke(self, input, config=None, **kwargs): ...
    async def ainvoke(self, input, config=None, **kwargs): ...
    def stream(self, input, config=None, **kwargs): ...
    async def astream(self, input, config=None, **kwargs): ...
    def bind(self, **kwargs): ...
```

`GuardedChatModel` must remain a real `BaseChatModel` so DeepAgents `resolve_model()` accepts it. Delegate identifying params, profile, name, callbacks/cache configuration, and model metadata required by LangChain. `bind_tools()` may return `GuardedBoundRunnable` only after model resolution.

The sync bridge must copy `contextvars.copy_context()`, create one daemon thread/event loop, expose loop/task handles through a thread-safe control object, cancel on timeout/`KeyboardInterrupt`/generator close, join only for cleanup grace, consume late exceptions, and never create a `ThreadPoolExecutor`. Capture a stream deadline before returning the iterator, not at first iteration; test delayed first consumption.

- [ ] **Step 4: Add RED override-adapter tests**

Test already guarded passthrough; known ChatOllama/OpenAI/Azure/Anthropic/Google class metadata extraction; custom `ModelRuntimeDescriptor` protocol; unknown raw override rejection; mixed-provider environment does not affect concrete override metadata; unknown override never unloads.

- [ ] **Step 5: Implement idempotent adapter registry and middleware**

```python
def guard_model(model: BaseChatModel, *, metadata=None, policy=None) -> GuardedChatModel: ...
def adapt_model_override(model) -> GuardedChatModel: ...

class ModelCallGuardMiddleware(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        return handler(request.override(model=adapt_model_override(request.model)))
    async def awrap_model_call(self, request, handler): ...
```

Known raw providers must be rebuilt with native timeout when required; unknown raw providers raise `UnsupportedModelOverrideError` before invocation.

- [ ] **Step 6: Run complete guard suite and confirm GREEN**

Run: `uv run pytest tests/test_model_call_guard.py -q`

Expected: pass; no task/thread leak warnings.

- [ ] **Step 7: Commit**

```bash
git add research_agent/model_call_guard.py tests/test_model_call_guard.py
git commit -m "feat: preserve guarded model runnable behavior"
```

### Task 4: Model factory and compiled graph coverage

**Files:**
- Modify: `research_agent/model_factory.py:272-384`
- Modify: `research_agent/agent.py:54,1361-1433`
- Modify: `.deepagents/skills/golden-dataset/scripts/skill_model_factory.py`
- Create: `tests/test_model_factory_timeout.py`
- Modify: `tests/test_agent_contracts.py`

- [ ] **Step 1: Write RED provider-factory tests**

Patch constructors, set mixed provider environments, and assert the first selected provider supplies exact `ModelRuntimeMetadata`, native HTTP timeout, async client where supported, and outer guard. Cover AWS Bedrock-compatible OpenAI, legacy/new Azure, Google, Anthropic, and Ollama.

- [ ] **Step 2: Run provider tests and confirm RED**

Run: `uv run pytest tests/test_model_factory_timeout.py -q`

Expected: failures because factory returns raw/retry-mutated models without metadata guard.

- [ ] **Step 3: Guard each selected model after existing retry/rate shaping**

Refactor repeated returns to:

```python
def _finalize_model(model, metadata, policy):
    retry_model = wrap_model_with_rate_limiting(model)
    return guard_model(retry_model, metadata=metadata, policy=policy)
```

Use installed provider APIs exactly:

- `ChatOpenAI`/`AzureChatOpenAI`: `request_timeout=timeout`, `http_client=httpx.Client(timeout=timeout, verify=verify_ssl)`, and `http_async_client=httpx.AsyncClient(timeout=timeout, verify=verify_ssl)`;
- `ChatAnthropic`: `timeout=timeout` (stored as `default_request_timeout`); preserve current CA behavior through existing environment/SDK configuration;
- `ChatGoogleGenerativeAI`: `timeout=timeout` plus current custom client when non-default SSL is configured;
- `ChatOllama`: `client_kwargs={"timeout": timeout}`, `sync_client_kwargs={"timeout": timeout}`, and `async_client_kwargs={"timeout": timeout}`; and
- Bedrock-compatible/Azure OpenAI constructors retain selected endpoint/deployment metadata from their winning branch.

Keep provider precedence and model cache behavior unchanged; cached object includes resolved policy and therefore requires restart/config-cache clear after env changes. Route the golden-dataset skill factory through `research_agent.model_factory.get_configured_model(bypass_cache=True)` so it cannot construct an unguarded active judge.

- [ ] **Step 4: Write RED compiled graph contract tests**

Compile root with fake guarded model and a tool-calling response. Assert root tool call executes; explicit `research-agent` and an explicitly supplied `general-purpose` spec both use guarded model plus `ModelCallGuardMiddleware`; live subgraph updates include nested tool calls; and no response chunk/metadata changes. Force a known and unknown late raw override inside each of the three compiled graphs and assert guarded success/fail-closed behavior.

- [ ] **Step 5: Register `ModelCallGuardMiddleware`**

Import it in `research_agent/agent.py` and add it at the root request boundary without changing Todo/Clarification/Completion/Resume/Research after-hook order. Add `model` and `middleware=[ModelCallGuardMiddleware()]` to `research-agent`. Define a local explicit `general-purpose` `SubAgent` with the documented general-purpose role, guarded root model, and same middleware; omit its `tools` key so DeepAgents inherits root tools. Its name suppresses automatic GP creation. Contract tests must prove exactly one guard application after tool binding in every graph.

- [ ] **Step 6: Run factory/graph tests and confirm GREEN**

Run: `uv run pytest tests/test_model_factory_timeout.py tests/test_model_call_guard.py tests/test_agent_contracts.py -q`

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add research_agent/model_factory.py research_agent/agent.py .deepagents/skills/golden-dataset/scripts/skill_model_factory.py tests/test_model_factory_timeout.py tests/test_agent_contracts.py
git commit -m "feat: guard every research model path"
```

### Task 5: Judge and CLI control-flow propagation

**Files:**
- Modify: `research_agent/agent.py:693-1066`
- Modify: `research_agent/research_subagent/utils/verification.py:117-235`
- Modify: `research_agent/cli.py:107-131,388-568`
- Modify: `tests/test_verification.py`
- Modify: `tests/test_research_agent_cli_e2e.py`

- [ ] **Step 1: Write RED judge pass-through tests**

Convert sufficiency and adversarial helpers to async functions that call `await model.ainvoke()`. Tests must re-raise `ModelCallTimeoutError` and `asyncio.CancelledError` rather than return neutral scores/gaps. Ordinary parse/provider failures retain current fallback.

- [ ] **Step 2: Write RED CLI interruption tests**

Patch a guarded async provider that blocks. Trigger `KeyboardInterrupt` during verbose stream, fallback invoke, non-verbose invoke, and title generation. Assert spinner stops, bridge task is cancelled, server disconnect/cancel event fires before configured deadline, no fallback retry starts for timeout/cancellation, and process exit remains interrupt semantics.

- [ ] **Step 3: Run slices and confirm RED**

Run: `uv run pytest tests/test_verification.py tests/test_research_agent_cli_e2e.py -q -k 'timeout or cancel or interrupt'`

- [ ] **Step 4: Implement explicit control-exception branches**

Remove `ThreadPoolExecutor.result(timeout=60)` from both judge helpers. Make `verify_report()` await them directly. In synchronous `ResearchStateMiddleware.after_model`, run the async verifier through the shared cancellable sync bridge without a second competing 120-second executor timeout; in `aafter_model`, await it normally. Both hooks catch `ModelCallTimeoutError` only to re-raise, and never catch `CancelledError` under generic fail-open fallback. In CLI, use `except KeyboardInterrupt: spinner.stop(); cancel_active_sync_bridge(); raise` before stream fallback logic. Timeout is a run failure and must not call `agent.invoke` fallback or completion continuation.

- [ ] **Step 5: Run judge/CLI and completion regressions**

Run: `uv run pytest tests/test_verification.py tests/test_research_agent_cli_e2e.py tests/test_completion_guard.py -q`

Expected: pass; completion-attempt counters unchanged on model timeout.

- [ ] **Step 6: Commit**

```bash
git add research_agent/agent.py research_agent/research_subagent/utils/verification.py research_agent/cli.py tests/test_verification.py tests/test_research_agent_cli_e2e.py
git commit -m "fix: propagate model timeout and cancellation"
```

### Task 6: Configuration and deployment documentation

**Files:**
- Modify: `.env.example`
- Modify: `documents/guides/configuration.md`
- Modify: `documents/guides/reliability.md`
- Modify: `scripts/render_azure_containerapp_config.py:97-136`
- Modify: `tests/test_azure_persistence_scripts.py`
- Modify: `deploy-aws.sh`
- Modify: `tests/test_aws_persistence_scripts.py`

- [ ] **Step 1: Write RED deployment contract tests**

Assert Azure and AWS rendered environments contain `MODEL_CALL_TIMEOUT_SECONDS=300` and `OLLAMA_FORCE_UNLOAD_ON_CANCEL=false`; no cloud deployment enables unload. Preserve existing exact env ordering/snapshot conventions.

- [ ] **Step 2: Add configuration defaults and guidance**

Document:

```dotenv
MODEL_CALL_TIMEOUT_SECONDS=300
OLLAMA_FORCE_UNLOAD_ON_CANCEL=false
```

Explain total wall-clock semantics, fallback for invalid/nonpositive values, restart/cache-clear requirement, safe default, and single-user local opt-in:

```bash
export OLLAMA_FORCE_UNLOAD_ON_CANCEL=true
```

- [ ] **Step 3: Run deployment/document checks**

Run: `uv run pytest tests/test_azure_persistence_scripts.py tests/test_aws_persistence_scripts.py -q`

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add .env.example documents/guides/configuration.md documents/guides/reliability.md scripts/render_azure_containerapp_config.py tests/test_azure_persistence_scripts.py deploy-aws.sh tests/test_aws_persistence_scripts.py
git commit -m "docs: configure model call deadlines"
```

### Task 7: Final verification

**Files:**
- Verify only; no planned production changes.

- [ ] **Step 1: Run focused feature suite**

Run:

```bash
uv run pytest \
  tests/test_model_call_guard.py \
  tests/test_model_factory_timeout.py \
  tests/test_agent_contracts.py \
  tests/test_verification.py \
  tests/test_research_agent_cli_e2e.py \
  tests/test_completion_guard.py \
  tests/test_azure_persistence_scripts.py \
  tests/test_aws_persistence_scripts.py -q
```

Expected: all pass.

- [ ] **Step 2: Run static checks**

```bash
uv run ruff check research_agent/model_call_guard.py research_agent/model_factory.py research_agent/agent.py research_agent/cli.py research_agent/research_subagent/utils/verification.py tests/test_model_call_guard.py tests/test_model_factory_timeout.py
uv run python -m compileall -q research_agent
git diff --check main...HEAD
```

Expected: no output/errors.

- [ ] **Step 3: Run full suite**

Run: `uv run pytest tests/ -q`

Expected: all project tests pass; classify any known environment-only cloud fixture failures against clean `main` before changing feature code.

- [ ] **Step 4: Inspect worktree and commit any test-only corrections**

Run: `git status --short && git log --oneline main..HEAD`

Expected: clean worktree and intentional commits only.
