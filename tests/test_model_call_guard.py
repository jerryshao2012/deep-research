import asyncio
import contextvars
import logging
import pickle
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from functools import wraps
from math import inf, nan
from typing import Any

import httpx
import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import var_child_runnable_config
from pydantic import PrivateAttr

from research_agent.model_call_guard import (
    DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
    MODEL_CANCEL_GRACE_SECONDS,
    OLLAMA_UNLOAD_TIMEOUT_SECONDS,
    ModelCallGuardMiddleware,
    ModelCallGuardMixin,
    ModelCallPolicy,
    ModelCallTimeoutError,
    ModelRuntimeMetadata,
    UnsupportedModelOverrideError,
    _maybe_unload_ollama,
    _run_with_deadline,
    adapt_model_override,
    cancel_model_call_scope,
    guard_model,
)

_TEST_CONTEXT = contextvars.ContextVar(
    "test_model_call_bridge_context", default="missing"
)


class _AsyncOnlyChatModel(BaseChatModel):
    """Network-free provider fake whose synchronous path must never run."""

    delay: float = 0.0
    chunk_delay: float = 0.0
    chunks: list[AIMessageChunk] = []
    model_runtime_metadata: Any = None
    suppress_invoke_cancel: bool = False
    suppress_stream_cancel: bool = False
    failures_before_success: int = 0
    _cancelled: threading.Event = PrivateAttr(default_factory=threading.Event)
    _cancel_count: int = PrivateAttr(default=0)
    _calls: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _release_stream: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _release_invoke: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _stream_started: threading.Event = PrivateAttr(default_factory=threading.Event)
    _context_values: list[str] = PrivateAttr(default_factory=list)
    _failure_count: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "async-only-test-provider"

    def _generate(self, *_args: Any, **_kwargs: Any) -> ChatResult:
        raise AssertionError("guarded sync methods must use provider async path")

    async def _agenerate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._calls.append({"messages": messages, "stop": stop, "kwargs": kwargs})
        self._context_values.append(_TEST_CONTEXT.get())
        if self._failure_count < self.failures_before_success:
            self._failure_count += 1
            raise RuntimeError("retryable provider failure")
        try:
            await asyncio.sleep(self.delay)
        except (asyncio.CancelledError, GeneratorExit):
            self._cancel_count += 1
            self._cancelled.set()
            if self.suppress_invoke_cancel:
                await self._release_invoke.wait()
            raise
        message = AIMessage(
            content="complete",
            id="message-id",
            response_metadata={"provider": "fake", "trace": "kept"},
            usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={"native": "kept"},
        )

    async def _astream(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        self._calls.append({"messages": messages, "stop": stop, "kwargs": kwargs})
        self._stream_started.set()
        try:
            for chunk in self.chunks:
                await asyncio.sleep(self.chunk_delay)
                yield ChatGenerationChunk(message=chunk)
        except (asyncio.CancelledError, GeneratorExit):
            self._cancel_count += 1
            self._cancelled.set()
            if self.suppress_stream_cancel:
                await self._release_stream.wait()
            raise

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any):
        return self.bind(tools=tools, tool_choice=tool_choice, **kwargs)


class _IndependentBindToolsChatModel(_AsyncOnlyChatModel):
    """Provider fake whose tool binding is independent from the model runnable."""

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any):
        del tools, tool_choice, kwargs

        async def slow_tool_runnable(_input: Any) -> AIMessage:
            await asyncio.sleep(0.08)
            return AIMessage(content="independent")

        return RunnableLambda(slow_tool_runnable)


class _DetachedBindToolsChatModel(_AsyncOnlyChatModel):
    """Provider fake that starts independent work from a guarded operation."""

    _detached_task: asyncio.Task[Any] | None = PrivateAttr(default=None)
    _release_detached: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any):
        del tools, tool_choice, kwargs

        async def spawn_detached(_input: Any) -> AIMessage:
            async def invoke_later() -> AIMessage:
                await self._release_detached.wait()
                return await self.ainvoke("detached")

            self._detached_task = asyncio.create_task(invoke_later())
            return AIMessage(content="spawned")

        return RunnableLambda(spawn_detached)


class _CleanupDetachedBindToolsChatModel(_DetachedBindToolsChatModel):
    """Provider fake retaining owner cleanup while detached work wakes."""

    _cleanup_started: threading.Event = PrivateAttr(default_factory=threading.Event)
    _finish_cleanup: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any):
        del tools, tool_choice, kwargs

        async def block_with_detached(_input: Any) -> AIMessage:
            async def invoke_later() -> AIMessage:
                await self._release_detached.wait()
                return await self.ainvoke("detached")

            self._detached_task = asyncio.create_task(invoke_later())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._cleanup_started.set()
                await self._finish_cleanup.wait()
                raise
            return AIMessage(content="unreachable")

        return RunnableLambda(block_with_detached)


class _ConcurrentDetachedBindToolsChatModel(_DetachedBindToolsChatModel):
    """Provider fake starting detached guarded work before outer timeout."""

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any):
        del tools, tool_choice, kwargs

        async def block_after_detaching(_input: Any) -> AIMessage:
            self._detached_task = asyncio.create_task(self.ainvoke("detached"))
            while not self._calls:
                await asyncio.sleep(0)
            await asyncio.Event().wait()
            return AIMessage(content="unreachable")

        return RunnableLambda(block_after_detaching)


class _PickleableChatModel(BaseChatModel):
    """Minimal provider whose native instances support deep copy and pickle."""

    payload: list[str]

    @property
    def _llm_type(self) -> str:
        return "pickleable-test-provider"

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="pickleable"))]
        )


def _rebuild_native_reduction_model(
    payload: list[str],
) -> "_NativeReductionChatModel":
    return _NativeReductionChatModel(payload=payload, native_state="pending")


class _NativeReductionChatModel(_PickleableChatModel):
    """Provider whose valid native pickle contract does not use dict state."""

    native_state: str

    def __getstate__(self) -> str:
        return self.native_state

    def __setstate__(self, state: str) -> None:
        self.native_state = state

    def __reduce_ex__(self, protocol: int) -> tuple[Any, ...]:
        del protocol
        return (
            _rebuild_native_reduction_model,
            (self.payload,),
            self.__getstate__(),
        )


def _rebuild_slot_reduction_model(
    payload: list[str], native_slot: str
) -> "_SlotReductionChatModel":
    model = _SlotReductionChatModel(payload=payload)
    object.__setattr__(model, "native_slot", native_slot)
    return model


class _SlotReductionChatModel(_PickleableChatModel):
    """Provider whose native reconstruction reads non-Pydantic slot state."""

    __slots__ = ("native_slot",)

    def __reduce_ex__(self, protocol: int) -> tuple[Any, ...]:
        del protocol
        return (_rebuild_slot_reduction_model, (self.payload, self.native_slot))


class _RecordingCallback(BaseCallbackHandler):
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.ends: list[Any] = []

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], **kwargs: Any
    ) -> None:
        self.starts.append({"serialized": serialized, "messages": messages, **kwargs})

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.ends.append((response, kwargs))


def _policy(timeout: float = 0.03, *, unload: bool = False) -> ModelCallPolicy:
    return ModelCallPolicy(timeout_seconds=timeout, force_ollama_unload=unload)


def _ollama_metadata(
    base_url: str | None = "http://localhost:11434",
) -> ModelRuntimeMetadata:
    return ModelRuntimeMetadata(
        provider="ollama", model_name="gemma4:latest", base_url=base_url
    )


def _async_test(test: Any) -> Any:
    @wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


@_async_test
async def test_run_with_deadline_returns_exact_async_result_before_deadline():
    async def complete() -> dict[str, str]:
        return {"result": "exact"}

    result = await _run_with_deadline(
        complete, policy=_policy(), metadata=_ollama_metadata(), unload=None
    )

    assert result == {"result": "exact"}


@_async_test
async def test_run_with_deadline_cancels_request_within_bounded_cleanup_grace(
    monkeypatch,
):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.03)
    cancelled = asyncio.Event()

    async def pending() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending, policy=_policy(), metadata=_ollama_metadata(), unload=None
        )

    assert cancelled.is_set()
    assert time.monotonic() - started < 0.03 + 0.03 + 0.08


@_async_test
async def test_run_with_deadline_propagates_external_cancellation_unchanged(
    monkeypatch,
):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.03)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    caller = asyncio.create_task(
        _run_with_deadline(
            pending, policy=_policy(1), metadata=_ollama_metadata(), unload=None
        )
    )
    await started.wait()
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert cancelled.is_set()


@_async_test
async def test_cancellation_suppressing_handler_cannot_exceed_cleanup_grace(
    monkeypatch,
):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.03)
    release = asyncio.Event()

    async def suppress_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            suppress_cancel, policy=_policy(), metadata=_ollama_metadata(), unload=None
        )

    assert time.monotonic() - started < 0.03 + 0.03 + 0.08
    release.set()
    await asyncio.sleep(0)


@_async_test
async def test_late_task_exception_is_consumed_after_bounded_cleanup(monkeypatch):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.02)
    release = asyncio.Event()
    loop_errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    async def late_failure() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError("late task failure")

    try:
        with pytest.raises(ModelCallTimeoutError):
            await _run_with_deadline(
                late_failure, policy=_policy(), metadata=_ollama_metadata(), unload=None
            )
        release.set()
        await asyncio.sleep(0.02)
        assert not loop_errors
    finally:
        loop.set_exception_handler(original_handler)


@_async_test
async def test_timeout_cancels_streaming_http_transport_request(monkeypatch):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.03)
    disconnected = asyncio.Event()

    class BlockingStreamTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            del request
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                disconnected.set()
                raise
            raise AssertionError("unreachable")

    async def streaming_request() -> None:
        async with httpx.AsyncClient(transport=BlockingStreamTransport()) as client:
            await client.get("http://testserver/stream", timeout=None)

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            streaming_request,
            policy=_policy(),
            metadata=_ollama_metadata(),
            unload=None,
        )
    assert disconnected.is_set()


@_async_test
async def test_default_policy_never_calls_ollama_unload():
    unload_calls = 0

    async def unload() -> None:
        nonlocal unload_calls
        unload_calls += 1

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending,
            policy=_policy(unload=False),
            metadata=_ollama_metadata(),
            unload=unload,
        )
    assert unload_calls == 0


@_async_test
async def test_timeout_unload_lifecycle_is_bounded_including_client_close(monkeypatch):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "OLLAMA_UNLOAD_TIMEOUT_SECONDS", 0.03)
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class HangingCloseClient:
        async def __aenter__(self):
            return self

        async def post(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            close_started.set()
            await release_close.wait()

    monkeypatch.setattr(httpx, "AsyncClient", HangingCloseClient)

    async def pending() -> None:
        await asyncio.Event().wait()

    caller = asyncio.create_task(
        _run_with_deadline(
            pending,
            policy=_policy(unload=True),
            metadata=_ollama_metadata(),
            unload=None,
        )
    )
    try:
        await close_started.wait()
        done, _ = await asyncio.wait({caller}, timeout=0.03 + 0.03 + 0.08)
        assert done
        with pytest.raises(ModelCallTimeoutError):
            await caller
    finally:
        release_close.set()
        if not caller.done():
            with pytest.raises(ModelCallTimeoutError):
                await caller


@_async_test
async def test_external_cancellation_during_timeout_unload_attempts_once(monkeypatch):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(guard, "OLLAMA_UNLOAD_TIMEOUT_SECONDS", 0.03)
    unload_started = asyncio.Event()
    release_unload = asyncio.Event()
    unload_calls = 0

    async def unload() -> None:
        nonlocal unload_calls
        unload_calls += 1
        unload_started.set()
        await release_unload.wait()

    async def pending() -> None:
        await asyncio.Event().wait()

    caller = asyncio.create_task(
        _run_with_deadline(
            pending,
            policy=_policy(0.02, unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )
    )
    try:
        await unload_started.wait()
        cancelled_at = time.monotonic()
        caller.cancel("caller")
        with pytest.raises(asyncio.CancelledError) as raised:
            await caller
        assert raised.value.args == ("caller",)
        assert unload_calls == 1
        assert time.monotonic() - cancelled_at < 0.03 + 0.03 + 0.08
    finally:
        release_unload.set()
        await asyncio.sleep(0)


@_async_test
async def test_unload_failure_log_excludes_integration_exception(caplog):
    secret = "integration-secret-that-must-not-log"
    caplog.set_level(logging.WARNING, logger="research_agent.model_call_guard")

    async def unload() -> None:
        raise RuntimeError(secret)

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending,
            policy=_policy(unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )

    assert secret not in caplog.text
    assert any(
        record.message == "Ollama unload provider=ollama action=unload status=failed"
        and record.exc_info is None
        for record in caplog.records
    )


@_async_test
@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        ("http://localhost:11434/api/", "http://localhost:11434/api/generate"),
        ("http://localhost:11434/api/generate/", "http://localhost:11434/api/generate"),
    ],
)
async def test_opt_in_ollama_unloads_once_with_safe_generate_payload(
    base_url, expected_url
):
    requests: list[tuple[str, dict[str, object]]] = []

    async def post(url: str, *, json: dict[str, object]) -> None:
        requests.append((url, json))

    await _maybe_unload_ollama(
        metadata=_ollama_metadata(base_url),
        policy=_policy(unload=True),
        post=post,
    )

    assert requests == [(expected_url, {"model": "gemma4:latest", "keep_alive": 0})]


@_async_test
async def test_cloud_provider_never_unloads_even_when_enabled():
    called = False

    async def post(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    await _maybe_unload_ollama(
        metadata=ModelRuntimeMetadata(
            provider="openai", model_name="gpt", base_url="https://api.openai.com"
        ),
        policy=_policy(unload=True),
        post=post,
    )
    assert called is False


@_async_test
async def test_unload_failure_preserves_timeout_error():
    async def unload() -> None:
        raise RuntimeError("unload failed")

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending,
            policy=_policy(unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )


@_async_test
async def test_sync_raising_unload_failure_preserves_timeout_error():
    def unload() -> Any:
        raise RuntimeError("unload failed before creating awaitable")

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending,
            policy=_policy(unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )


@_async_test
async def test_sync_cancelling_unload_failure_preserves_timeout_error():
    def unload() -> Any:
        raise asyncio.CancelledError("sync-unload")

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending,
            policy=_policy(unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )


@_async_test
async def test_self_cancelling_unload_failure_preserves_timeout_error():
    async def unload() -> None:
        raise asyncio.CancelledError

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending,
            policy=_policy(unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )


@_async_test
async def test_unload_failure_preserves_external_cancellation():
    started = asyncio.Event()

    async def unload() -> None:
        raise RuntimeError("unload failed")

    async def pending() -> None:
        started.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(
        _run_with_deadline(
            pending,
            policy=_policy(1, unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )
    )
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller


@_async_test
async def test_sync_raising_unload_failure_preserves_external_cancellation():
    started = asyncio.Event()

    def unload() -> Any:
        raise RuntimeError("unload failed before creating awaitable")

    async def pending() -> None:
        started.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(
        _run_with_deadline(
            pending,
            policy=_policy(1, unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )
    )
    await started.wait()
    caller.cancel("caller")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert raised.value.args == ("caller",)


@_async_test
async def test_sync_cancelling_unload_preserves_external_cancellation_marker():
    started = asyncio.Event()

    def unload() -> Any:
        raise asyncio.CancelledError("sync-unload")

    async def pending() -> None:
        started.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(
        _run_with_deadline(
            pending,
            policy=_policy(1, unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )
    )
    await started.wait()
    caller.cancel("caller")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert raised.value.args == ("caller",)


@_async_test
async def test_self_cancelling_unload_preserves_external_cancellation():
    started = asyncio.Event()

    async def unload() -> None:
        raise asyncio.CancelledError

    async def pending() -> None:
        started.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(
        _run_with_deadline(
            pending,
            policy=_policy(1, unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )
    )
    await started.wait()
    caller.cancel("caller")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert raised.value.args == ("caller",)


@_async_test
async def test_repeated_external_cancellation_interrupts_unload_promptly():
    started = asyncio.Event()
    unload_started = asyncio.Event()
    release_unload = asyncio.Event()

    async def unload() -> None:
        unload_started.set()
        await release_unload.wait()

    async def pending() -> None:
        started.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(
        _run_with_deadline(
            pending,
            policy=_policy(1, unload=True),
            metadata=_ollama_metadata(),
            unload=unload,
        )
    )
    await started.wait()
    caller.cancel("first")
    await unload_started.wait()

    cancelled_at = time.monotonic()
    caller.cancel("second")
    with pytest.raises(asyncio.CancelledError) as raised:
        await caller

    assert raised.value.args == ("second",)
    assert time.monotonic() - cancelled_at < 0.1
    release_unload.set()
    await asyncio.sleep(0)


@_async_test
@pytest.mark.parametrize("model_name", [None, 42, "   "])
async def test_malformed_ollama_model_name_skips_unload_and_preserves_timeout(
    model_name,
):
    unload_calls = 0

    async def unload() -> None:
        nonlocal unload_calls
        unload_calls += 1

    async def pending() -> None:
        await asyncio.Event().wait()

    metadata = _ollama_metadata()
    object.__setattr__(metadata, "model_name", model_name)

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending, policy=_policy(unload=True), metadata=metadata, unload=unload
        )
    assert unload_calls == 0


@_async_test
async def test_invalid_ollama_unload_metadata_skips_network_without_leaking_model_name():
    calls = 0

    async def post(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1

    metadata = ModelRuntimeMetadata(
        provider="ollama", model_name="secret model", base_url=None
    )
    await _maybe_unload_ollama(
        metadata=metadata, policy=_policy(unload=True), post=post
    )
    assert calls == 0


@pytest.mark.parametrize(
    "value",
    [None, "", "bad", "nan", "inf", "0", "-1"],
)
def test_invalid_timeout_uses_safe_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MODEL_CALL_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", value)

    policy = ModelCallPolicy.from_env()

    assert policy.timeout_seconds == DEFAULT_MODEL_CALL_TIMEOUT_SECONDS == 300.0


@pytest.mark.parametrize("value", [nan, inf, 0.0, -1.0])
def test_direct_policy_construction_rejects_invalid_timeout(value):
    with pytest.raises(ValueError, match="finite and positive"):
        ModelCallPolicy(timeout_seconds=value, force_ollama_unload=False)


@pytest.mark.parametrize("value", ["0.001", "3", "300.5", "1e6"])
def test_finite_positive_timeout_is_accepted(monkeypatch, value):
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", value)

    assert ModelCallPolicy.from_env().timeout_seconds == float(value)


def test_non_finite_numeric_timeout_uses_safe_default(monkeypatch):
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", str(nan))
    assert (
        ModelCallPolicy.from_env().timeout_seconds == DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    )
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", str(inf))
    assert (
        ModelCallPolicy.from_env().timeout_seconds == DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    )


def test_cancellation_constants_are_bounded():
    assert MODEL_CANCEL_GRACE_SECONDS == 2.0
    assert OLLAMA_UNLOAD_TIMEOUT_SECONDS == 2.0


@pytest.mark.parametrize("value", ["true", "TRUE", " true "])
def test_ollama_unload_requires_explicit_true(monkeypatch, value):
    monkeypatch.setenv("OLLAMA_FORCE_UNLOAD_ON_CANCEL", value)

    assert ModelCallPolicy.from_env().force_ollama_unload is True


@pytest.mark.parametrize("value", [None, "", "false", "yes", "1", "on", "invalid"])
def test_ollama_unload_defaults_false_for_non_true_values(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OLLAMA_FORCE_UNLOAD_ON_CANCEL", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_FORCE_UNLOAD_ON_CANCEL", value)

    assert ModelCallPolicy.from_env().force_ollama_unload is False


def test_policy_and_metadata_are_immutable():
    policy = ModelCallPolicy.from_env({})
    metadata = ModelRuntimeMetadata(
        provider="ollama", model_name="llama3", base_url="http://localhost:11434/"
    )

    with pytest.raises(FrozenInstanceError):
        policy.timeout_seconds = 1.0
    with pytest.raises(FrozenInstanceError):
        metadata.base_url = "http://example.com"


def test_runtime_metadata_normalizes_base_url_without_sensitive_parts():
    metadata = ModelRuntimeMetadata(
        provider="ollama",
        model_name="llama3",
        base_url="HTTPS://user:secret@example.com:11434/api/?token=secret#fragment",
    )

    assert metadata.base_url == "https://example.com:11434/api"


@pytest.mark.parametrize(
    "base_url",
    [
        "user:secret@example.com:11434/api?token=secret#fragment",
        "//user:secret@example.com:11434/api?token=secret#fragment",
        "ftp://user:secret@example.com:11434/api?token=secret#fragment",
        "http:///api?token=secret#fragment",
    ],
)
def test_runtime_metadata_rejects_unsafe_base_url_forms(base_url):
    with pytest.raises(ValueError) as raised:
        ModelRuntimeMetadata(provider="ollama", model_name="llama3", base_url=base_url)

    assert "secret" not in str(raised.value)
    assert "secret" not in repr(raised.value)


def test_safe_errors_are_direct_runtime_errors():
    timeout_error = ModelCallTimeoutError(
        provider="ollama", timeout_seconds=3, unload_requested=True
    )
    override_error = UnsupportedModelOverrideError(provider="ollama")

    assert ModelCallTimeoutError.__bases__ == (RuntimeError,)
    assert UnsupportedModelOverrideError.__bases__ == (RuntimeError,)
    assert isinstance(timeout_error, RuntimeError)
    assert isinstance(override_error, RuntimeError)
    assert not isinstance(timeout_error, TimeoutError)
    assert not isinstance(override_error, ValueError)


def test_timeout_error_is_safe_and_describes_deadline():
    error = ModelCallTimeoutError(
        provider="ollama", timeout_seconds=3, unload_requested=True
    )

    rendered = str(error)

    assert "ollama" in rendered
    assert "deadline" in rendered.lower()
    assert "3" in rendered
    assert "request content" not in rendered
    assert "prompt content" not in rendered
    assert "request content" not in repr(error)
    assert "prompt content" not in repr(error)


def test_unsupported_override_error_does_not_retain_arbitrary_content():
    error = UnsupportedModelOverrideError(
        provider="ollama", model_name="prompt content that must not leak"
    )

    assert "prompt content" not in str(error)
    assert not hasattr(error, "model_name")


@pytest.mark.parametrize(
    "model_name",
    [
        "sk-proj-secret-token-123",
        "x" * 10000,
    ],
)
def test_unsupported_override_error_does_not_retain_model_name(model_name):
    error = UnsupportedModelOverrideError(provider="ollama", model_name=model_name)

    assert model_name not in str(error)
    assert model_name not in repr(error)
    assert model_name not in repr(error.args)
    assert model_name not in repr(vars(error))
    assert not hasattr(error, "model_name")


def _fake_metadata() -> ModelRuntimeMetadata:
    return ModelRuntimeMetadata(provider="openai", model_name="fake-model")


def _guarded_fake(
    *,
    timeout: float = 0.1,
    delay: float = 0.0,
    chunk_delay: float = 0.0,
    chunks: list[AIMessageChunk] | None = None,
) -> BaseChatModel:
    return guard_model(
        _AsyncOnlyChatModel(
            delay=delay,
            chunk_delay=chunk_delay,
            chunks=[] if chunks is None else chunks,
        ),
        metadata=_fake_metadata(),
        policy=_policy(timeout),
    )


@pytest.mark.parametrize("decorator", ["direct", "bind", "config", "retry"])
@pytest.mark.parametrize("method", ["invoke", "ainvoke", "stream", "astream"])
def test_logical_guarded_operation_attempts_ollama_unload_once(
    monkeypatch,
    decorator,
    method,
):
    import research_agent.model_call_guard as guard

    unload_calls = 0

    async def count_unload(**_kwargs: Any) -> None:
        nonlocal unload_calls
        unload_calls += 1

    monkeypatch.setattr(guard, "_maybe_unload_ollama", count_unload)
    guarded = guard_model(
        _AsyncOnlyChatModel(
            delay=0.1,
            chunk_delay=0.1,
            chunks=[AIMessageChunk(content="too-late")],
        ),
        metadata=_ollama_metadata(),
        policy=_policy(0.02, unload=True),
    )
    if decorator == "bind":
        runnable = guarded.bind(extra="kept")
    elif decorator == "config":
        runnable = guarded.with_config({"tags": ["kept"]})
    elif decorator == "retry":
        runnable = guarded.with_retry(wait_exponential_jitter=False)
    else:
        runnable = guarded

    with pytest.raises(ModelCallTimeoutError):
        if method == "invoke":
            runnable.invoke("hello")
        elif method == "ainvoke":
            asyncio.run(runnable.ainvoke("hello"))
        elif method == "stream":
            next(runnable.stream("hello"))
        else:

            async def consume() -> None:
                await anext(runnable.astream("hello"))

            asyncio.run(consume())

    assert unload_calls == 1
    assert guarded._cancel_count == 1


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_guarded_retry_backoff_is_inside_outer_total_deadline(
    monkeypatch,
    method,
):
    import research_agent.model_call_guard as guard

    unload_calls = 0

    async def count_unload(**_kwargs: Any) -> None:
        nonlocal unload_calls
        unload_calls += 1

    monkeypatch.setattr(guard, "_maybe_unload_ollama", count_unload)
    guarded = guard_model(
        _AsyncOnlyChatModel(failures_before_success=1),
        metadata=_ollama_metadata(),
        policy=_policy(0.02, unload=True),
    )
    runnable = guarded.with_retry(
        exponential_jitter_params={
            "initial": 0.05,
            "max": 0.05,
            "jitter": 0.0,
        },
        stop_after_attempt=2,
    )

    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        if method == "invoke":
            runnable.invoke("hello")
        else:
            asyncio.run(runnable.ainvoke("hello"))

    assert time.monotonic() - started < 0.05
    assert unload_calls == 1
    assert guarded._failure_count == 1


@pytest.mark.parametrize("method", ["invoke", "ainvoke", "stream", "astream"])
def test_independent_bind_tools_runnable_is_inside_outer_total_deadline(
    monkeypatch,
    method,
):
    import research_agent.model_call_guard as guard

    unload_calls = 0

    async def count_unload(**_kwargs: Any) -> None:
        nonlocal unload_calls
        unload_calls += 1

    monkeypatch.setattr(guard, "_maybe_unload_ollama", count_unload)
    guarded = guard_model(
        _IndependentBindToolsChatModel(),
        metadata=_ollama_metadata(),
        policy=_policy(0.02, unload=True),
    )
    runnable = guarded.bind_tools([])

    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        if method == "invoke":
            runnable.invoke("hello")
        elif method == "ainvoke":
            asyncio.run(runnable.ainvoke("hello"))
        elif method == "stream":
            next(runnable.stream("hello"))
        else:

            async def consume() -> None:
                await anext(runnable.astream("hello"))

            asyncio.run(consume())

    assert time.monotonic() - started < 0.06
    assert unload_calls == 1


@_async_test
async def test_outer_operation_context_resets_after_timeout():
    import research_agent.model_call_guard as guard

    guarded = guard_model(
        _IndependentBindToolsChatModel(),
        metadata=_fake_metadata(),
        policy=_policy(0.02),
    )

    with pytest.raises(ModelCallTimeoutError):
        await guarded.bind_tools([]).ainvoke("hello")

    assert guard._ACTIVE_OPERATION.get() is None
    assert guard._ACTIVE_DEADLINE.get() is None
    assert guard._ACTIVE_SCOPE_ID.get() is None


@_async_test
async def test_detached_task_cannot_reuse_stale_completed_operation_context():
    guarded = guard_model(
        _DetachedBindToolsChatModel(delay=0.2),
        metadata=_fake_metadata(),
        policy=_policy(0.08),
    )

    result = await guarded.bind_tools([]).ainvoke("hello")
    assert result.content == "spawned"
    assert guarded._detached_task is not None

    guarded._release_detached.set()
    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        await guarded._detached_task

    assert time.monotonic() - started < 0.14
    assert guarded._cancel_count == 1


@_async_test
async def test_concurrent_detached_task_keeps_shared_absolute_deadline(monkeypatch):
    import research_agent.model_call_guard as guard

    unload_calls = 0

    async def count_unload(**_kwargs: Any) -> None:
        nonlocal unload_calls
        unload_calls += 1

    monkeypatch.setattr(guard, "_maybe_unload_ollama", count_unload)
    guarded = guard_model(
        _ConcurrentDetachedBindToolsChatModel(delay=0.15),
        metadata=_ollama_metadata(),
        policy=_policy(0.02, unload=True),
    )
    scope_id = "concurrent-detached"

    with pytest.raises(ModelCallTimeoutError):
        await guarded.bind_tools([]).ainvoke(
            "hello",
            config={"configurable": {"model_call_scope_id": scope_id}},
        )
    assert guarded._detached_task is not None

    with pytest.raises(ModelCallTimeoutError):
        await guarded._detached_task

    assert guarded._calls
    assert guarded._cancel_count == 1
    assert unload_calls == 1
    assert guarded._bridge_registry.active_count(scope_id) == 0
    assert guard._ACTIVE_OPERATION.get() is None
    assert guard._ACTIVE_DEADLINE.get() is None
    assert guard._ACTIVE_SCOPE_ID.get() is None


@_async_test
async def test_detached_task_cannot_reuse_operation_during_post_deadline_cleanup(
    monkeypatch,
):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.3)
    guarded = guard_model(
        _CleanupDetachedBindToolsChatModel(delay=0.2),
        metadata=_fake_metadata(),
        policy=_policy(0.02),
    )
    caller = asyncio.create_task(guarded.bind_tools([]).ainvoke("hello"))
    while not guarded._cleanup_started.is_set():
        await asyncio.sleep(0)
    assert guarded._detached_task is not None

    try:
        guarded._release_detached.set()
        with pytest.raises(ModelCallTimeoutError):
            await guarded._detached_task
    finally:
        guarded._finish_cleanup.set()
        with pytest.raises(ModelCallTimeoutError):
            await caller


@_async_test
async def test_detached_task_cannot_reuse_operation_during_cancel_cleanup(
    monkeypatch,
):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.3)
    guarded = guard_model(
        _CleanupDetachedBindToolsChatModel(delay=0.2),
        metadata=_fake_metadata(),
        policy=_policy(0.08),
    )
    caller = asyncio.create_task(guarded.bind_tools([]).ainvoke("hello"))
    while guarded._detached_task is None:
        await asyncio.sleep(0)
    caller.cancel("caller")
    while not guarded._cleanup_started.is_set():
        await asyncio.sleep(0)

    try:
        guarded._release_detached.set()
        with pytest.raises(ModelCallTimeoutError):
            await guarded._detached_task
    finally:
        guarded._finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await caller
        assert raised.value.args == ("caller",)


@_async_test
async def test_bound_external_cancellation_owns_cleanup_once(monkeypatch):
    import research_agent.model_call_guard as guard

    unload_calls = 0

    async def count_unload(**_kwargs: Any) -> None:
        nonlocal unload_calls
        unload_calls += 1

    monkeypatch.setattr(guard, "_maybe_unload_ollama", count_unload)
    guarded = guard_model(
        _AsyncOnlyChatModel(delay=1),
        metadata=_ollama_metadata(),
        policy=_policy(1, unload=True),
    )
    caller = asyncio.create_task(guarded.bind(extra="kept").ainvoke("hello"))
    while not guarded._calls:
        await asyncio.sleep(0)

    caller.cancel("caller")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert raised.value.args == ("caller",)
    assert unload_calls == 1
    assert guarded._cancel_count == 1


def test_guard_model_creates_cached_provider_preserving_dynamic_class():
    first = _guarded_fake()
    second = _guarded_fake()

    assert isinstance(first, BaseChatModel)
    assert isinstance(first, _AsyncOnlyChatModel)
    assert isinstance(first, ModelCallGuardMixin)
    assert type(first) is type(second)
    assert type(first).__mro__[:3] == (
        type(first),
        ModelCallGuardMixin,
        _AsyncOnlyChatModel,
    )


def test_generated_private_attrs_are_initialized_values_and_copy_is_independent():
    guarded = _guarded_fake()
    copied = guarded.model_copy()

    for name in (
        "_model_call_policy",
        "_runtime_metadata",
        "_bridge_registry",
        "_retry_controller",
    ):
        assert getattr(guarded, name).__class__.__name__ != "ModelPrivateAttr"
        assert getattr(copied, name).__class__.__name__ != "ModelPrivateAttr"
    assert copied is not guarded
    assert type(copied) is type(guarded)
    assert copied._bridge_registry is not guarded._bridge_registry
    assert copied._model_call_policy == guarded._model_call_policy
    assert copied._runtime_metadata == guarded._runtime_metadata


def test_guarded_model_deep_copy_excludes_registry_lock_and_reinitializes_state():
    raw = _PickleableChatModel(payload=["original"])
    raw_copy = raw.model_copy(deep=True)
    guarded = guard_model(raw, metadata=_fake_metadata(), policy=_policy(0.2))

    class ActiveControl:
        scope_id = "copy-scope"

    active = ActiveControl()
    assert guarded._bridge_registry.register(active)
    try:
        copied = guarded.model_copy(deep=True)
    finally:
        guarded._bridge_registry.unregister(active)

    assert raw_copy.payload == raw.payload
    assert raw_copy.payload is not raw.payload
    assert isinstance(copied, _PickleableChatModel)
    assert type(copied) is type(guarded)
    assert copied.payload == guarded.payload
    assert copied.payload is not guarded.payload
    assert copied._bridge_registry is not guarded._bridge_registry
    assert copied._bridge_registry._lock is not guarded._bridge_registry._lock
    assert copied._bridge_registry.active_count("copy-scope") == 0
    assert copied._model_call_policy == guarded._model_call_policy
    assert copied._model_call_policy is not guarded._model_call_policy
    assert copied._runtime_metadata == guarded._runtime_metadata
    assert copied._runtime_metadata is not guarded._runtime_metadata
    assert copied._retry_controller is None


def test_guard_pickle_reconstructs_through_stable_factory():
    guarded = guard_model(
        _PickleableChatModel(payload=["pickle"]),
        metadata=_fake_metadata(),
        policy=_policy(0.2),
    )
    guarded_class = type(guarded)

    restored = pickle.loads(pickle.dumps(guarded))

    assert type(restored) is guarded_class
    assert isinstance(restored, _PickleableChatModel)
    assert isinstance(restored, ModelCallGuardMixin)
    assert restored.payload == guarded.payload
    assert restored._model_call_policy == guarded._model_call_policy
    assert restored._model_call_policy is not guarded._model_call_policy
    assert restored._runtime_metadata == guarded._runtime_metadata
    assert restored._runtime_metadata is not guarded._runtime_metadata
    assert restored._bridge_registry is not guarded._bridge_registry
    assert restored._retry_controller is None
    assert restored.invoke("hello").content == "pickleable"


def test_guard_pickle_preserves_provider_native_custom_reduction():
    raw = _NativeReductionChatModel(payload=["native"], native_state="non-dict")
    guarded = guard_model(raw, metadata=_fake_metadata(), policy=_policy(0.2))

    raw_restored = pickle.loads(pickle.dumps(raw))
    restored = pickle.loads(pickle.dumps(guarded))

    assert raw_restored.payload == ["native"]
    assert raw_restored.native_state == "non-dict"
    assert isinstance(restored, _NativeReductionChatModel)
    assert isinstance(restored, ModelCallGuardMixin)
    assert restored.payload == raw_restored.payload
    assert restored.native_state == raw_restored.native_state
    assert restored._bridge_registry is not guarded._bridge_registry


def test_guard_pickle_preserves_provider_native_slot_state():
    raw = _SlotReductionChatModel(payload=["slot"])
    object.__setattr__(raw, "native_slot", "provider-slot")
    guarded = guard_model(raw, metadata=_fake_metadata(), policy=_policy(0.2))
    object.__setattr__(guarded, "native_slot", raw.native_slot)

    raw_restored = pickle.loads(pickle.dumps(raw))
    restored = pickle.loads(pickle.dumps(guarded))

    assert raw_restored.native_slot == "provider-slot"
    assert isinstance(restored, _SlotReductionChatModel)
    assert isinstance(restored, ModelCallGuardMixin)
    assert restored.native_slot == raw_restored.native_slot


def test_pickle_first_guard_survives_distinct_provider_with_same_import_identity():
    collision_name = "_SameIdentityPickleProvider"
    first_provider = type(
        collision_name,
        (_PickleableChatModel,),
        {"__module__": __name__, "__qualname__": collision_name},
    )
    second_provider = type(
        collision_name,
        (_PickleableChatModel,),
        {"__module__": __name__, "__qualname__": collision_name},
    )
    module = sys.modules[__name__]
    setattr(module, collision_name, first_provider)
    try:
        first = guard_model(
            first_provider(payload=["first"]),
            metadata=_fake_metadata(),
            policy=_policy(0.2),
        )
        second = guard_model(
            second_provider(payload=["second"]),
            metadata=_fake_metadata(),
            policy=_policy(0.2),
        )

        assert type(first) is not type(second)
        restored = pickle.loads(pickle.dumps(first))
    finally:
        delattr(module, collision_name)

    assert isinstance(restored, first_provider)
    assert restored.payload == ["first"]
    assert restored.invoke("hello").content == "pickleable"


def test_guard_pickle_reconstructs_in_fresh_python_process(tmp_path):
    guarded = guard_model(
        _PickleableChatModel(payload=["subprocess"]),
        metadata=_fake_metadata(),
        policy=_policy(0.2),
    )
    payload_path = tmp_path / "guarded-model.pkl"
    payload_path.write_bytes(pickle.dumps(guarded))
    code = """
import pickle
import sys
sys.path.insert(0, "tests")
from test_model_call_guard import _PickleableChatModel
from research_agent.model_call_guard import ModelCallGuardMixin

with open(sys.argv[1], "rb") as payload:
    restored = pickle.load(payload)
assert isinstance(restored, _PickleableChatModel)
assert isinstance(restored, ModelCallGuardMixin)
assert restored.payload == ["subprocess"]
assert restored._bridge_registry.active_count("unused") == 0
assert restored.invoke("hello").content == "pickleable"
"""

    completed = subprocess.run(
        [sys.executable, "-c", code, str(payload_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_guard_pickle_preserves_native_nonpickleable_provider_failure():
    raw = _AsyncOnlyChatModel()
    guarded = guard_model(raw, metadata=_fake_metadata(), policy=_policy())

    with pytest.raises(TypeError, match="cannot pickle"):
        pickle.dumps(raw)
    with pytest.raises(TypeError, match="cannot pickle"):
        pickle.dumps(guarded)


@_async_test
async def test_ainvoke_captures_total_deadline_before_coroutine_is_awaited():
    guarded = _guarded_fake(timeout=0.03)

    pending = guarded.ainvoke("hello")
    await asyncio.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        await pending
    assert time.monotonic() - started < 0.04


@_async_test
async def test_ainvoke_external_cancellation_reaches_provider():
    guarded = _guarded_fake(timeout=1, delay=1)

    caller = asyncio.create_task(guarded.ainvoke("hello"))
    await asyncio.sleep(0.01)
    caller.cancel("caller")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert raised.value.args == ("caller",)
    assert guarded._cancelled.is_set()


@_async_test
async def test_astream_captures_deadline_before_first_pull():
    guarded = _guarded_fake(
        timeout=0.03,
        chunk_delay=0.1,
        chunks=[AIMessageChunk(content="too-late")],
    )

    stream = guarded.astream("hello")
    await asyncio.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        await anext(stream)
    assert time.monotonic() - started < 0.04
    assert not guarded._calls


@_async_test
async def test_astream_uses_one_eager_deadline_across_slow_trickle():
    chunks = [
        AIMessageChunk(content="a", id="chunk-id"),
        AIMessageChunk(content="b", id="chunk-id"),
        AIMessageChunk(content="", id="chunk-id"),
    ]
    chunks[-1].tool_call_chunks = [
        {"name": "search", "args": '{"q":"x"}', "id": "tool-1", "index": 0}
    ]
    chunks[-1].usage_metadata = {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
    }
    guarded = _guarded_fake(timeout=0.045, chunk_delay=0.02, chunks=chunks)

    stream = guarded.astream("hello")
    received = []
    with pytest.raises(ModelCallTimeoutError):
        async for chunk in stream:
            received.append(chunk)

    assert received
    assert [chunk.content for chunk in received] in (["a"], ["a", "b"])
    assert guarded._cancelled.is_set()


@_async_test
async def test_bound_astream_operation_context_keeps_one_deadline_across_chunks():
    guarded = _guarded_fake(
        timeout=0.045,
        chunk_delay=0.02,
        chunks=[
            AIMessageChunk(content="a"),
            AIMessageChunk(content="b"),
            AIMessageChunk(content="c"),
        ],
    )
    received: list[str] = []

    with pytest.raises(ModelCallTimeoutError):
        async for chunk in guarded.bind(extra="kept").astream("hello"):
            received.append(str(chunk.content))

    assert received
    assert received == ["a", "b", "c"][: len(received)]
    assert len(received) < 3
    assert guarded._cancel_count == 1


@_async_test
async def test_astream_cancel_suppression_cannot_replace_or_extend_timeout(monkeypatch):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.02)
    raw = _AsyncOnlyChatModel(
        chunk_delay=1,
        chunks=[AIMessageChunk(content="too-late")],
        suppress_stream_cancel=True,
    )
    guarded = guard_model(raw, metadata=_fake_metadata(), policy=_policy(0.02))

    started = time.monotonic()
    try:
        with pytest.raises(ModelCallTimeoutError):
            await anext(guarded.astream("hello"))
        assert time.monotonic() - started < 0.1
    finally:
        guarded._release_stream.set()
        await asyncio.sleep(0)


@_async_test
async def test_astream_preserves_message_chunk_metadata_tool_calls_and_usage():
    chunk = AIMessageChunk(
        content="",
        id="chunk-id",
        response_metadata={"trace": "kept"},
        tool_call_chunks=[
            {"name": "search", "args": '{"q":"x"}', "id": "tool-1", "index": 0}
        ],
        usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    guarded = _guarded_fake(chunks=[chunk])

    received = [item async for item in guarded.astream("hello")]

    # BaseChatModel adds a terminal empty chunk; provider chunk remains unchanged.
    assert len(received) == 2
    assert received[0].id == "chunk-id"
    assert received[0].response_metadata == {"trace": "kept"}
    assert received[0].tool_call_chunks == chunk.tool_call_chunks
    assert received[0].usage_metadata == chunk.usage_metadata


def test_sync_invoke_uses_async_provider_path_and_preserves_message_metadata():
    guarded = _guarded_fake()

    message = guarded.invoke("hello", stop=["stop"], configurable_key="kept")

    assert message.content == "complete"
    assert message.id == "message-id"
    assert message.response_metadata["provider"] == "fake"
    assert message.usage_metadata == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
    }
    assert guarded._calls[0]["stop"] == ["stop"]
    assert guarded._calls[0]["kwargs"]["configurable_key"] == "kept"


def test_sync_bridge_copies_calling_contextvars():
    guarded = _guarded_fake()
    token = _TEST_CONTEXT.set("caller-context")
    try:
        guarded.invoke("hello")
    finally:
        _TEST_CONTEXT.reset(token)

    assert guarded._context_values == ["caller-context"]


def test_callbacks_tags_metadata_and_configurable_config_survive_guard():
    callback = _RecordingCallback()
    guarded = _guarded_fake()
    config = {
        "callbacks": [callback],
        "tags": ["guarded-tag"],
        "metadata": {"request": "kept"},
        "configurable": {"model_call_scope_id": "callback-scope", "tenant": "kept"},
    }

    result = guarded.invoke("hello", config=config)

    assert result.content == "complete"
    assert len(callback.starts) == 1
    assert len(callback.ends) == 1
    assert callback.starts[0]["serialized"]["id"] == _AsyncOnlyChatModel.lc_id()
    assert callback.starts[0]["serialized"]["name"] == "_AsyncOnlyChatModel"
    assert callback.starts[0]["tags"] == ["guarded-tag"]
    assert callback.starts[0]["metadata"]["request"] == "kept"
    assert config["configurable"] == {
        "model_call_scope_id": "callback-scope",
        "tenant": "kept",
    }


def test_sync_stream_close_cancels_async_provider_and_cleans_bridge():
    guarded = _guarded_fake(
        timeout=1,
        chunk_delay=0.01,
        chunks=[AIMessageChunk(content="a"), AIMessageChunk(content="b")],
    )

    stream = guarded.stream(
        "hello", config={"configurable": {"model_call_scope_id": "close-scope"}}
    )
    assert next(stream).content == "a"
    stream.close()

    assert guarded._cancelled.wait(0.2)
    assert guarded._bridge_registry.active_count("close-scope") == 0


def test_sync_stream_without_scope_retains_private_scope_on_control():
    guarded = _guarded_fake(
        timeout=1,
        chunk_delay=0.05,
        chunks=[AIMessageChunk(content="a")],
    )

    stream = guarded.stream("hello")
    scope_id = stream._control.scope_id
    try:
        assert scope_id.startswith("private-")
        assert guarded._bridge_registry.active_count(scope_id) == 1
    finally:
        stream.close()


def test_sync_stream_delayed_consumption_observes_original_total_deadline():
    guarded = _guarded_fake(
        timeout=0.03,
        chunk_delay=0.1,
        chunks=[AIMessageChunk(content="too-late")],
    )

    stream = guarded.stream("hello")
    time.sleep(0.06)
    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        next(stream)
    assert time.monotonic() - started < 0.04


def test_sync_bridge_cleans_or_drains_late_cancel_on_daemon_loop(monkeypatch):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.02)
    raw = _AsyncOnlyChatModel(
        delay=1,
        suppress_invoke_cancel=True,
    )
    guarded = guard_model(raw, metadata=_fake_metadata(), policy=_policy(0.05))
    controls = []
    original_start_bridge = guard._start_bridge

    def capture_bridge(*args, **kwargs):
        control = original_start_bridge(*args, **kwargs)
        controls.append(control)
        return control

    monkeypatch.setattr(guard, "_start_bridge", capture_bridge)
    with pytest.raises(ModelCallTimeoutError):
        guarded.invoke(
            "hello", config={"configurable": {"model_call_scope_id": "late-cleanup"}}
        )

    control = controls[0]
    assert control._thread.daemon
    assert control._thread.name.startswith("model-call-bridge-late-cleanup")
    unregister_deadline = time.monotonic() + 0.1
    while guarded._bridge_registry.active_count("late-cleanup"):
        assert time.monotonic() < unregister_deadline
        time.sleep(0.002)
    assert guarded._bridge_registry.active_count("late-cleanup") == 0
    if control._thread.is_alive():
        loop = control._loop
        assert loop is not None
        loop.call_soon_threadsafe(guarded._release_invoke.set)
    control.join(0.2)
    assert not control._thread.is_alive()


def test_bind_and_bind_tools_return_eager_guarded_bound_runnables():
    guarded = _guarded_fake(timeout=0.03)

    for bound in (guarded.bind(extra="kept"), guarded.bind_tools([], extra="kept")):
        pending = bound.ainvoke("hello")
        time.sleep(0.05)
        with pytest.raises(ModelCallTimeoutError):
            asyncio.run(pending)


@_async_test
async def test_bound_stream_and_astream_preserve_chunk_parity():
    chunk = AIMessageChunk(
        content="tool",
        id="bound-chunk-id",
        response_metadata={"bound": "kept"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    guarded = _guarded_fake(chunks=[chunk])
    bound = guarded.bind(marker="kept")

    sync_chunks = list(bound.stream("hello"))
    async_chunks = [item async for item in bound.astream("hello")]

    assert [item.content for item in sync_chunks] == [
        item.content for item in async_chunks
    ]
    assert len(sync_chunks) == len(async_chunks) == 2
    assert sync_chunks[0].id == "bound-chunk-id"
    assert sync_chunks[0].response_metadata == {"bound": "kept"}
    assert sync_chunks[0].usage_metadata == chunk.usage_metadata


def test_bound_sync_stream_deadline_includes_bridge_start_delay(monkeypatch):
    original_start = threading.Thread.start

    def delayed_start(thread: threading.Thread) -> None:
        time.sleep(0.05)
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", delayed_start)
    guarded = _guarded_fake(
        timeout=0.03,
        chunk_delay=0.1,
        chunks=[AIMessageChunk(content="too-late")],
    )

    stream = guarded.bind(extra="kept").stream("hello")
    started = time.monotonic()
    with pytest.raises(ModelCallTimeoutError):
        next(stream)
    assert time.monotonic() - started < 0.02


def test_bound_with_config_ainvoke_captures_deadline_at_outer_call():
    guarded = _guarded_fake(timeout=0.03)
    configured = guarded.bind(extra="kept").with_config(
        {"configurable": {"model_call_scope_id": "configured-invoke"}}
    )

    pending = configured.ainvoke("hello")
    time.sleep(0.05)

    with pytest.raises(ModelCallTimeoutError):
        asyncio.run(pending)


def test_model_with_config_ainvoke_captures_deadline_at_outer_call():
    guarded = _guarded_fake(timeout=0.03)
    configured = guarded.with_config(
        {"configurable": {"model_call_scope_id": "model-configured-invoke"}}
    )

    pending = configured.ainvoke("hello")
    time.sleep(0.05)

    with pytest.raises(ModelCallTimeoutError):
        asyncio.run(pending)


@_async_test
async def test_model_with_config_astream_captures_deadline_at_outer_call():
    guarded = _guarded_fake(
        timeout=0.03,
        chunks=[AIMessageChunk(content="too-late")],
    )
    configured = guarded.with_config(
        {"configurable": {"model_call_scope_id": "model-configured-astream"}}
    )

    stream = configured.astream("hello")
    await asyncio.sleep(0.05)

    with pytest.raises(ModelCallTimeoutError):
        await anext(stream)


def test_bound_with_config_stream_captures_deadline_at_outer_call():
    guarded = _guarded_fake(
        timeout=0.03,
        chunk_delay=0.1,
        chunks=[AIMessageChunk(content="too-late")],
    )
    configured = guarded.bind(extra="kept").with_config(
        {"configurable": {"model_call_scope_id": "configured-stream"}}
    )

    stream = configured.stream("hello")
    time.sleep(0.05)

    with pytest.raises(ModelCallTimeoutError):
        next(stream)


@_async_test
async def test_bound_with_config_astream_captures_deadline_at_outer_call():
    guarded = _guarded_fake(
        timeout=0.03,
        chunks=[AIMessageChunk(content="too-late")],
    )
    configured = guarded.bind(extra="kept").with_config(
        {"configurable": {"model_call_scope_id": "configured-astream"}}
    )

    stream = configured.astream("hello")
    await asyncio.sleep(0.05)

    with pytest.raises(ModelCallTimeoutError):
        await anext(stream)


def test_bound_runnable_transformations_remain_guard_owned():
    bound = _guarded_fake().bind(extra="kept")
    wrapper_type = type(bound)
    typed = bound.with_types(input_type=str, output_type=AIMessage)
    decorators = [
        bound.bind(other="kept"),
        bound.with_config({"tags": ["kept"]}),
        bound.with_retry(wait_exponential_jitter=False),
        bound.with_listeners(),
        bound.with_alisteners(),
        typed,
    ]

    assert decorators
    assert all(isinstance(decorator, wrapper_type) for decorator in decorators)
    assert typed.InputType is str
    assert typed.OutputType is AIMessage
    assert typed.get_name() == typed.bound.get_name()
    assert typed.config_specs == typed.bound.config_specs


def test_model_call_decorators_remain_guard_owned():
    guarded = _guarded_fake()
    wrapper_type = type(guarded.bind(extra="kept"))

    decorated = [
        guarded.with_config({"tags": ["kept"]}),
        guarded.with_retry(wait_exponential_jitter=False),
        guarded.with_listeners(),
        guarded.with_alisteners(),
        guarded.with_types(input_type=str, output_type=AIMessage),
    ]

    assert all(isinstance(runnable, wrapper_type) for runnable in decorated)


def test_bound_compositions_are_native_and_do_not_guard_downstream_work():
    guarded = _guarded_fake(timeout=0.03)
    bound = guarded.bind(extra="kept")
    wrapper_type = type(bound)
    identity = RunnableLambda(lambda value: value)
    compositions = [
        bound.assign(extra=lambda value: value),
        bound.pick("content"),
        bound.map(),
        bound.pipe(identity),
        bound | identity,
        bound.__ror__(identity),
        bound.with_fallbacks([identity]),
    ]

    assert all(
        not isinstance(composition, wrapper_type) for composition in compositions
    )

    def slow_parser(message: AIMessage) -> str:
        time.sleep(0.06)
        return f"{message.content}-parsed"

    pipeline = bound.pipe(RunnableLambda(slow_parser))
    started = time.monotonic()

    assert pipeline.invoke("hello") == "complete-parsed"
    assert time.monotonic() - started >= 0.06


def test_native_bound_composition_still_bounds_model_segment():
    guarded = _guarded_fake(timeout=0.03, delay=0.1)
    pipeline = guarded.bind(extra="kept").pipe(RunnableLambda(lambda value: value))

    with pytest.raises(ModelCallTimeoutError):
        pipeline.invoke("hello")


def test_cancel_model_call_scope_cancels_all_sync_bridges_with_one_grace(monkeypatch):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.05)
    guarded = _guarded_fake(timeout=2, delay=2)
    scope_id = "shared-run"
    errors: list[BaseException] = []

    def call() -> None:
        try:
            guarded.invoke(
                "hello", config={"configurable": {"model_call_scope_id": scope_id}}
            )
        except BaseException as exc:
            errors.append(exc)

    callers = [threading.Thread(target=call) for _ in range(2)]
    for caller in callers:
        caller.start()
    deadline = time.monotonic() + 0.5
    while guarded._bridge_registry.active_count(scope_id) != 2:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    started = time.monotonic()
    cancel_model_call_scope(scope_id)
    elapsed = time.monotonic() - started
    for caller in callers:
        caller.join(0.2)

    assert elapsed < 0.05 + 0.08
    assert all(not caller.is_alive() for caller in callers)
    assert len(errors) == 2
    assert all(isinstance(exc, asyncio.CancelledError) for exc in errors)
    assert guarded._bridge_registry.active_count(scope_id) == 0


def test_ambient_runnable_config_supplies_model_call_scope():
    guarded = _guarded_fake(
        timeout=1,
        chunk_delay=0.1,
        chunks=[AIMessageChunk(content="later")],
    )
    token = var_child_runnable_config.set(
        {"configurable": {"model_call_scope_id": "ambient-scope"}}
    )
    try:
        stream = guarded.stream("hello", config=None)
    finally:
        var_child_runnable_config.reset(token)

    try:
        assert stream._control.scope_id == "ambient-scope"
        assert guarded._bridge_registry.active_count("ambient-scope") == 1
    finally:
        stream.close()


def test_bound_with_config_scope_reaches_outer_bridge_registry():
    guarded = _guarded_fake(
        timeout=1,
        chunk_delay=0.1,
        chunks=[AIMessageChunk(content="later")],
    )
    configured = guarded.bind(extra="kept").with_config(
        {"configurable": {"model_call_scope_id": "bound-config-scope"}}
    )

    stream = configured.stream("hello", config=None)

    try:
        assert stream._control.scope_id == "bound-config-scope"
        assert guarded._bridge_registry.active_count("bound-config-scope") == 1
    finally:
        stream.close()


def test_model_with_config_scope_reaches_outer_bridge_registry():
    guarded = _guarded_fake(
        timeout=1,
        chunk_delay=0.1,
        chunks=[AIMessageChunk(content="later")],
    )
    configured = guarded.with_config(
        {"configurable": {"model_call_scope_id": "model-config-scope"}}
    )

    stream = configured.stream("hello", config=None)

    try:
        assert stream._control.scope_id == "model-config-scope"
        assert guarded._bridge_registry.active_count("model-config-scope") == 1
    finally:
        stream.close()


def test_scope_cancel_is_atomic_with_concurrent_bridge_registration(monkeypatch):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.02)
    guarded = _guarded_fake(timeout=1, delay=0.2)
    scope_id = "registration-race-scope"
    local_register_entered = threading.Event()
    release_local_register = threading.Event()
    original_register = guarded._bridge_registry.register
    errors: list[BaseException] = []

    def paused_local_register(control):
        local_register_entered.set()
        assert release_local_register.wait(0.5)
        return original_register(control)

    monkeypatch.setattr(guarded._bridge_registry, "register", paused_local_register)

    def call() -> None:
        try:
            guarded.invoke(
                "hello", config={"configurable": {"model_call_scope_id": scope_id}}
            )
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=call)
    caller.start()
    assert local_register_entered.wait(0.2)

    cancel_model_call_scope(scope_id)
    release_local_register.set()
    caller.join(0.5)

    assert not caller.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    assert not guarded._calls
    assert guarded._bridge_registry.active_count(scope_id) == 0

    result = guarded.invoke(
        "hello", config={"configurable": {"model_call_scope_id": scope_id}}
    )
    assert result.content == "complete"
    assert len(guarded._calls) == 1
    cleanup_deadline = time.monotonic() + 0.1
    while guarded._bridge_registry.active_count(scope_id):
        assert time.monotonic() < cleanup_deadline
        time.sleep(0.002)
    assert guarded._bridge_registry.active_count(scope_id) == 0


def test_scope_cancellation_does_not_retain_inactive_tombstones():
    import research_agent.model_call_guard as guard

    registry = guard.BridgeRegistry()

    for index in range(1000):
        registry.cancel_scope(f"inactive-{index}")

    assert registry._cancelling == {}
    assert registry._controls == {}


def test_registration_during_scope_cancellation_is_rejected_and_cancelled(
    monkeypatch,
):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.2)
    registry = guard.BridgeRegistry()
    monkeypatch.setattr(guard, "_GLOBAL_BRIDGE_REGISTRY", registry)
    scope_id = "during-cancel-scope"
    join_entered = threading.Event()
    release_join = threading.Event()

    class BlockingControl:
        def __init__(self) -> None:
            self.scope_id = scope_id

        def cancel(self) -> None:
            pass

        def join(self, timeout: float) -> None:
            join_entered.set()
            release_join.wait(timeout)
            registry.unregister(self)

    active = BlockingControl()
    assert registry.register(active)
    cancellation = threading.Thread(
        target=guard.cancel_model_call_scope, args=(scope_id,)
    )
    cancellation.start()
    assert join_entered.wait(0.1)

    factory_started = threading.Event()

    async def late_factory() -> str:
        factory_started.set()
        return "escaped"

    late_registry = guard.BridgeRegistry()
    late = guard._BridgeControl(
        late_factory,
        scope_id=scope_id,
        registry=late_registry,
    )
    late.start()

    assert late._cancel_requested
    assert not factory_started.is_set()
    kind, error = late.results.get(timeout=0.1)
    assert kind == "error"
    assert isinstance(error, asyncio.CancelledError)
    assert late_registry._controls == {}
    release_join.set()
    cancellation.join(0.5)
    assert not cancellation.is_alive()
    assert registry._cancelling == {}
    assert registry._controls == {}

    reusable = BlockingControl()
    assert registry.register(reusable)
    registry.unregister(reusable)


def test_unknown_raw_override_fails_before_invocation_without_unload():
    raw = _AsyncOnlyChatModel()

    with pytest.raises(UnsupportedModelOverrideError) as raised:
        adapt_model_override(raw, policy=_policy(unload=True))

    assert raised.value.provider == "unknown"
    assert not raw._calls


def test_custom_runtime_descriptor_allows_safe_override_adaptation():
    raw = _AsyncOnlyChatModel()
    raw.model_runtime_metadata = _fake_metadata()

    adapted = adapt_model_override(raw, policy=_policy())

    assert isinstance(adapted, _AsyncOnlyChatModel)
    assert isinstance(adapted, ModelCallGuardMixin)
    assert adapt_model_override(adapted, policy=_policy()) is adapted


def test_middleware_adapts_override_exactly_once_for_sync_handler():
    raw = _AsyncOnlyChatModel()
    raw.model_runtime_metadata = _fake_metadata()
    middleware = ModelCallGuardMiddleware(policy=_policy())
    request = ModelRequest(model=raw, messages=[])
    seen: list[BaseChatModel] = []

    def handler(adapted_request: ModelRequest) -> AIMessage:
        seen.append(adapted_request.model)
        return AIMessage(content="ok")

    result = middleware.wrap_model_call(request, handler)

    assert result.content == "ok"
    assert len(seen) == 1
    assert isinstance(seen[0], ModelCallGuardMixin)
    assert adapt_model_override(seen[0]) is seen[0]


@_async_test
async def test_middleware_adapts_override_exactly_once_for_async_handler():
    raw = _AsyncOnlyChatModel()
    raw.model_runtime_metadata = _fake_metadata()
    middleware = ModelCallGuardMiddleware(policy=_policy())
    request = ModelRequest(model=raw, messages=[])
    seen: list[BaseChatModel] = []

    async def handler(adapted_request: ModelRequest) -> AIMessage:
        seen.append(adapted_request.model)
        return AIMessage(content="ok")

    result = await middleware.awrap_model_call(request, handler)

    assert result.content == "ok"
    assert len(seen) == 1
    assert isinstance(seen[0], ModelCallGuardMixin)


def _installed_provider_cases() -> list[
    tuple[type[BaseChatModel], dict[str, Any], str, str]
]:
    cases: list[tuple[type[BaseChatModel], dict[str, Any], str, str]] = []
    try:
        from langchain_ollama import ChatOllama

        cases.append(
            (
                ChatOllama,
                {"model": "ollama-explicit", "base_url": "http://localhost:11434"},
                "ollama",
                "model",
            )
        )
    except ImportError:
        pass
    try:
        from langchain_openai import AzureChatOpenAI, ChatOpenAI

        cases.extend(
            [
                (
                    ChatOpenAI,
                    {"model": "openai-explicit", "api_key": "sk-test"},
                    "openai",
                    "model_name",
                ),
                (
                    AzureChatOpenAI,
                    {
                        "model": "azure-explicit",
                        "azure_deployment": "deployment-explicit",
                        "azure_endpoint": "https://explicit.openai.azure.com",
                        "api_version": "2024-01-01",
                        "api_key": "sk-test",
                    },
                    "azure_openai",
                    "model_name",
                ),
            ]
        )
    except ImportError:
        pass
    try:
        from langchain_anthropic import ChatAnthropic

        cases.append(
            (
                ChatAnthropic,
                {"model_name": "anthropic-explicit", "api_key": "sk-test"},
                "anthropic",
                "model",
            )
        )
    except ImportError:
        pass
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        cases.append(
            (
                ChatGoogleGenerativeAI,
                {"model": "gemini-explicit", "api_key": "test"},
                "google",
                "model",
            )
        )
    except ImportError:
        pass
    return cases


@pytest.mark.parametrize(
    ("provider_class", "constructor", "provider", "model_field"),
    _installed_provider_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_known_provider_guard_preserves_identity_fields_profile_and_copy_state(
    monkeypatch, provider_class, constructor, provider, model_field
):
    monkeypatch.setenv("MODEL_NAME", "environment-must-not-win")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-must-not-win")
    raw = provider_class(**constructor)
    guarded = adapt_model_override(raw, policy=_policy(0.2))

    assert isinstance(guarded, provider_class)
    assert isinstance(guarded, BaseChatModel)
    assert isinstance(guarded, ModelCallGuardMixin)
    assert type(guarded).__mro__[1] is ModelCallGuardMixin
    assert getattr(guarded, model_field) == getattr(raw, model_field)
    assert guarded.profile == raw.profile
    assert guarded.lc_id() == raw.lc_id()
    guarded_serialized = guarded.to_json()
    raw_serialized = raw.to_json()
    identity_fields = ("lc", "type", "id", "name")
    assert {key: guarded_serialized.get(key) for key in identity_fields} == {
        key: raw_serialized.get(key) for key in identity_fields
    }
    if "repr" in guarded_serialized:
        assert guarded_serialized["repr"].startswith(f"{provider_class.__name__}(")
    timeout_keys = {"timeout", "request_timeout", "default_request_timeout"}
    assert {
        key: value
        for key, value in guarded._identifying_params.items()
        if key not in timeout_keys
    } == {
        key: value
        for key, value in raw._identifying_params.items()
        if key not in timeout_keys
    }
    assert guarded._runtime_metadata.provider == provider
    assert "environment-must-not-win" not in guarded._runtime_metadata.model_name
    for name in (
        "_model_call_policy",
        "_runtime_metadata",
        "_bridge_registry",
        "_retry_controller",
    ):
        assert getattr(guarded, name).__class__.__name__ != "ModelPrivateAttr"

    copied = guarded.model_copy()
    assert isinstance(copied, provider_class)
    assert copied._bridge_registry is not guarded._bridge_registry
    assert copied._model_call_policy == guarded._model_call_policy

    updated = guarded.model_copy(update={model_field: "copy-explicit"})
    assert isinstance(updated, provider_class)
    assert updated._runtime_metadata.model_name == "copy-explicit"
    assert updated._bridge_registry is not guarded._bridge_registry


@pytest.mark.parametrize(
    ("provider_class", "constructor", "provider", "model_field"),
    _installed_provider_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_known_provider_guard_invoke_and_bind_use_provider_async_path(
    monkeypatch, provider_class, constructor, provider, model_field
):
    del provider, model_field

    async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="provider-result",
                        id="provider-id",
                        response_metadata={"kept": True},
                    )
                )
            ]
        )

    monkeypatch.setattr(provider_class, "_agenerate", fake_agenerate)
    guarded = adapt_model_override(provider_class(**constructor), policy=_policy(0.2))

    direct = guarded.invoke("hello")
    bound = guarded.bind(extra="value").invoke("hello")

    assert direct.content == bound.content == "provider-result"
    assert direct.id == bound.id == "provider-id"
    assert direct.response_metadata == bound.response_metadata == {"kept": True}


@pytest.mark.parametrize(
    ("provider_class", "constructor", "provider", "model_field"),
    _installed_provider_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_known_provider_rebuild_applies_native_timeout_without_env_influence(
    monkeypatch, provider_class, constructor, provider, model_field
):
    del model_field
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", "999")
    guarded = adapt_model_override(provider_class(**constructor), policy=_policy(0.2))

    if provider == "ollama":
        assert guarded.client_kwargs["timeout"] == 0.2
        assert guarded.async_client_kwargs["timeout"] == 0.2
        assert guarded.sync_client_kwargs["timeout"] == 0.2
    elif provider in {"openai", "azure_openai"}:
        assert guarded.request_timeout == 0.2
    elif provider == "anthropic":
        assert guarded.default_request_timeout == 0.2
    else:
        assert guarded.timeout == 0.2


def _openai_provider_cases():
    from langchain_openai import AzureChatOpenAI, ChatOpenAI

    return [
        (
            ChatOpenAI,
            {"model": "openai-explicit", "api_key": "sk-test"},
        ),
        (
            AzureChatOpenAI,
            {
                "model": "azure-explicit",
                "azure_deployment": "deployment-explicit",
                "azure_endpoint": "https://explicit.openai.azure.com",
                "api_version": "2024-01-01",
                "api_key": "sk-test",
            },
        ),
    ]


def _timeout_components(timeout: Any) -> list[float]:
    if isinstance(timeout, httpx.Timeout):
        values = [timeout.connect, timeout.read, timeout.write, timeout.pool]
    elif isinstance(timeout, tuple):
        values = list(timeout)
    else:
        values = [timeout]
    return [float(value) for value in values if value is not None]


@pytest.mark.parametrize(
    ("provider_class", "constructor"),
    _openai_provider_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
@pytest.mark.parametrize(
    "native_timeout",
    [
        17.0,
        (17.0, 9.0),
        httpx.Timeout(connect=17.0, read=9.0, write=0.1, pool=4.0),
    ],
    ids=["float", "tuple", "httpx"],
)
def test_openai_provider_rebuilds_distinct_clients_with_all_timeouts_bounded(
    provider_class, constructor, native_timeout
):
    raw = provider_class(**constructor, timeout=native_timeout)

    guarded = adapt_model_override(raw, policy=_policy(0.2))

    assert guarded.client is not raw.client
    assert guarded.async_client is not raw.async_client
    assert guarded.root_client is not raw.root_client
    assert guarded.root_async_client is not raw.root_async_client
    for timeout in (
        guarded.request_timeout,
        guarded.root_client.timeout,
        guarded.root_async_client.timeout,
        guarded.root_client._client.timeout,
        guarded.root_async_client._client.timeout,
    ):
        components = _timeout_components(timeout)
        assert len(components) == 4 or not isinstance(timeout, httpx.Timeout)
        assert components
        assert max(components) <= 0.2


@pytest.mark.parametrize(
    ("provider_class", "constructor"),
    _openai_provider_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_openai_provider_rebuilds_explicit_http_clients_without_owning_transport(
    provider_class, constructor
):
    sync_requests: list[httpx.Request] = []
    async_requests: list[httpx.Request] = []

    class SyncTransport(httpx.BaseTransport):
        closed = False

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            sync_requests.append(request)
            return httpx.Response(200, request=request)

        def close(self) -> None:
            self.closed = True

    class AsyncTransport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            async_requests.append(request)
            return httpx.Response(200, request=request)

        async def aclose(self) -> None:
            self.closed = True

    sync_transport = SyncTransport()
    async_transport = AsyncTransport()
    sync_mount_transport = SyncTransport()
    async_mount_transport = AsyncTransport()
    sync_client = httpx.Client(
        timeout=17,
        transport=sync_transport,
        mounts={
            "all://*bypass.test": None,
            "https://mounted.test": sync_mount_transport,
        },
    )
    async_client = httpx.AsyncClient(
        timeout=17,
        transport=async_transport,
        mounts={
            "all://*bypass.test": None,
            "https://mounted.test": async_mount_transport,
        },
    )
    raw = provider_class(
        **constructor,
        timeout=17,
        http_client=sync_client,
        http_async_client=async_client,
    )

    guarded = adapt_model_override(raw, policy=_policy(0.2))

    assert guarded.http_client is not sync_client
    assert guarded.http_async_client is not async_client
    assert guarded.root_client._client is guarded.http_client
    assert guarded.root_async_client._client is guarded.http_async_client
    assert max(_timeout_components(guarded.http_client.timeout)) <= 0.2
    assert max(_timeout_components(guarded.http_async_client.timeout)) <= 0.2
    assert {pattern.pattern for pattern in guarded.http_client._mounts} == {
        pattern.pattern for pattern in sync_client._mounts
    }
    assert {pattern.pattern for pattern in guarded.http_async_client._mounts} == {
        pattern.pattern for pattern in async_client._mounts
    }
    assert (
        next(
            transport
            for pattern, transport in guarded.http_client._mounts.items()
            if pattern.pattern == "all://*bypass.test"
        )
        is None
    )
    assert (
        next(
            transport
            for pattern, transport in guarded.http_async_client._mounts.items()
            if pattern.pattern == "all://*bypass.test"
        )
        is None
    )
    assert guarded.http_client.get("https://example.test/sync").status_code == 200
    assert guarded.http_client.get("https://mounted.test/sync").status_code == 200
    assert (
        asyncio.run(
            guarded.http_async_client.get("https://example.test/async")
        ).status_code
        == 200
    )
    assert (
        asyncio.run(
            guarded.http_async_client.get("https://mounted.test/async")
        ).status_code
        == 200
    )

    guarded.root_client.close()
    asyncio.run(guarded.root_async_client.close())
    assert not sync_transport.closed
    assert not async_transport.closed
    assert not sync_mount_transport.closed
    assert not async_mount_transport.closed
    assert sync_client.get("https://example.test/raw").status_code == 200
    assert sync_client.get("https://mounted.test/raw").status_code == 200
    assert asyncio.run(async_client.get("https://example.test/raw")).status_code == 200
    assert asyncio.run(async_client.get("https://mounted.test/raw")).status_code == 200
    assert len(sync_requests) == 4
    assert len(async_requests) == 4
    sync_client.close()
    asyncio.run(async_client.aclose())


def test_guarded_provider_keeps_langchain_provider_strategy_identity():
    from langchain.agents.middleware.provider_tool_search import _get_model_provider
    from langchain_anthropic import ChatAnthropic
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_ollama import ChatOllama
    from langchain_openai import ChatOpenAI

    models = [
        (ChatOllama(model="ollama-explicit"), "ollama"),
        (ChatOpenAI(model="openai-explicit", api_key="sk-test"), "openai"),
        (
            ChatAnthropic(model_name="anthropic-explicit", api_key="sk-test"),
            "anthropic",
        ),
        (
            ChatGoogleGenerativeAI(model="gemini-explicit", api_key="test"),
            "google_genai",
        ),
    ]

    for raw, expected in models:
        guarded = adapt_model_override(raw, policy=_policy())
        assert _get_model_provider(guarded, runtime=None) == expected


def test_guarded_openai_and_google_keep_multimodal_file_handling_identity():
    from deepagents.middleware.filesystem import _model_tolerates_non_pdf_files
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_openai import ChatOpenAI

    guarded_openai = adapt_model_override(
        ChatOpenAI(model="openai-explicit", api_key="sk-test"), policy=_policy()
    )
    guarded_google = adapt_model_override(
        ChatGoogleGenerativeAI(model="gemini-explicit", api_key="test"),
        policy=_policy(),
    )

    assert _model_tolerates_non_pdf_files(guarded_openai)
    assert _model_tolerates_non_pdf_files(guarded_google)


def test_anthropic_prompt_caching_middleware_recognizes_guarded_model():
    from langchain_anthropic import ChatAnthropic
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
    from langchain_core.messages import HumanMessage, SystemMessage

    guarded = adapt_model_override(
        ChatAnthropic(model_name="anthropic-explicit", api_key="sk-test"),
        policy=_policy(),
    )
    request = ModelRequest(
        model=guarded,
        messages=[HumanMessage(content="hello")],
        system_message=SystemMessage(content="system"),
    )
    seen: list[ModelRequest] = []

    def handler(adapted: ModelRequest) -> AIMessage:
        seen.append(adapted)
        return AIMessage(content="ok")

    middleware = AnthropicPromptCachingMiddleware(unsupported_model_behavior="raise")
    middleware.wrap_model_call(request, handler)

    assert seen[0].model is guarded
    assert seen[0].model_settings["cache_control"] == {
        "type": "ephemeral",
        "ttl": "5m",
    }
