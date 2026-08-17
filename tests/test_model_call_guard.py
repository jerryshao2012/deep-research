import asyncio
import contextvars
import json
import logging
import os
import pickle
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import inf, nan
from types import MethodType
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.callbacks import (
    AsyncCallbackHandler,
    AsyncCallbackManager,
    BaseCallbackHandler,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tracers.context import collect_runs, tracing_v2_callback_var
from langchain_core.tracers.langchain import LangChainTracer
from pydantic import PrivateAttr, SecretStr

from research_agent.model_call_guard import (
    DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
    MODEL_CANCEL_GRACE_SECONDS,
    OLLAMA_UNLOAD_TIMEOUT_SECONDS,
    ModelCallGuardMiddleware,
    ModelCallGuardMixin,
    ModelCallPolicy,
    ModelCallTimeoutError,
    ModelRuntimeDescriptor,
    ModelRuntimeMetadata,
    UnsupportedModelOverrideError,
    _maybe_unload_ollama,
    _run_with_deadline,
    adapt_model_override,
    build_guarded_provider_model,
    cancel_model_call_scope,
    guard_model,
)
from research_agent.retry_utils import (
    ModelRetryController,
    RetryConfig,
    wrap_model_with_rate_limiting,
)

_TEST_CONTEXT = contextvars.ContextVar(
    "test_model_call_bridge_context", default="missing"
)


@contextmanager
def _ollama_compatible_server(
    *,
    request_started: threading.Event | None = None,
    client_disconnected: threading.Event | None = None,
):
    requests: list[dict[str, Any]] = []
    requests_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            with requests_lock:
                requests.append(request)
            if request_started is not None:
                request_started.set()
            if client_disconnected is not None:
                self.close_connection = True
                self.connection.settimeout(0.5)
                try:
                    if self.connection.recv(1, socket.MSG_PEEK) == b"":
                        client_disconnected.set()
                except OSError:
                    pass
                return
            if self.path.endswith("/chat/completions"):
                response_payload = {
                    "id": "chatcmpl-local",
                    "object": "chat.completion",
                    "created": 1,
                    "model": request["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "local-reply",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
                content_type = "application/json"
            else:
                response_payload = {
                    "model": request["model"],
                    "created_at": "2026-08-17T00:00:00Z",
                    "message": {"role": "assistant", "content": "local-reply"},
                    "done": True,
                    "done_reason": "stop",
                    "total_duration": 1,
                    "load_duration": 1,
                    "prompt_eval_count": 1,
                    "prompt_eval_duration": 1,
                    "eval_count": 1,
                    "eval_duration": 1,
                }
                content_type = "application/x-ndjson"
            response = json.dumps(response_payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()

        def log_message(self, _format: str, *args: Any) -> None:
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(1)


def _guarded_local_ollama(base_url: str) -> BaseChatModel:
    from langchain_ollama import ChatOllama

    return guard_model(
        ChatOllama(
            model="local-bridge-test",
            base_url=base_url,
            client_kwargs={"timeout": 1.0, "trust_env": False},
            async_client_kwargs={"timeout": 1.0, "trust_env": False},
        ),
        metadata=ModelRuntimeMetadata(
            provider="ollama",
            model_name="local-bridge-test",
            base_url=base_url,
        ),
        policy=ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False),
    )


def _guarded_local_bedrock_openai(base_url: str) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    policy = ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False)
    return build_guarded_provider_model(
        ChatOpenAI,
        {
            "model": "bedrock-local-test",
            "base_url": f"{base_url}/v1",
            "api_key": SecretStr("test-key"),
            "max_retries": 0,
            "request_timeout": 1.0,
            "http_client": httpx.Client(timeout=1.0),
            "http_async_client": httpx.AsyncClient(timeout=1.0),
        },
        ModelRuntimeMetadata(
            provider="aws_bedrock",
            model_name="bedrock-local-test",
            base_url=f"{base_url}/v1",
        ),
        policy,
    )


def _guarded_default_openai() -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    return guard_model(
        ChatOpenAI(
            model="fork-default-client-test",
            api_key=SecretStr("test-key"),
            http_socket_options=(),
        ),
        metadata=ModelRuntimeMetadata(
            provider="openai",
            model_name="fork-default-client-test",
        ),
        policy=ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False),
    )


def _guarded_default_ollama() -> BaseChatModel:
    from langchain_ollama import ChatOllama

    return guard_model(
        ChatOllama(model="fork-default-ollama-test"),
        metadata=ModelRuntimeMetadata(
            provider="ollama",
            model_name="fork-default-ollama-test",
            base_url="http://localhost:11434",
        ),
        policy=ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False),
    )


def _guarded_default_google() -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return guard_model(
        ChatGoogleGenerativeAI(
            model="fork-default-google-test",
            api_key=SecretStr("test-key"),
        ),
        metadata=ModelRuntimeMetadata(
            provider="google",
            model_name="fork-default-google-test",
        ),
        policy=ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False),
    )


def _guarded_default_anthropic() -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    return guard_model(
        ChatAnthropic(
            model="fork-default-anthropic-test",
            api_key=SecretStr("test-key"),
        ),
        metadata=ModelRuntimeMetadata(
            provider="anthropic",
            model_name="fork-default-anthropic-test",
        ),
        policy=ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False),
    )


def _close_guarded_local_ollama(model: BaseChatModel) -> None:
    import research_agent.model_call_guard as guard

    deadline = time.monotonic() + 1.0
    control = guard._start_bridge(
        model._async_client._client.aclose,
        scope_id="local-client-close",
        registry=model._bridge_registry,
    )
    guard._bridge_result(
        control,
        deadline,
        policy=model._model_call_policy,
        metadata=model._runtime_metadata,
    )
    model._client._client.close()


def _close_guarded_local_openai(model: BaseChatModel) -> None:
    import research_agent.model_call_guard as guard

    deadline = time.monotonic() + 1.0
    control = guard._start_bridge(
        model.http_async_client.aclose,
        scope_id="local-openai-client-close",
        registry=model._bridge_registry,
    )
    guard._bridge_result(
        control,
        deadline,
        policy=model._model_call_policy,
        metadata=model._runtime_metadata,
    )
    model.http_client.close()


def _close_guarded_default_openai(model: BaseChatModel) -> None:
    import research_agent.model_call_guard as guard

    deadline = time.monotonic() + 1.0
    control = guard._start_bridge(
        model.root_async_client.close,
        scope_id="default-openai-client-close",
        registry=model._bridge_registry,
    )
    guard._bridge_result(
        control,
        deadline,
        policy=model._model_call_policy,
        metadata=model._runtime_metadata,
    )
    model.root_client.close()


def _close_guarded_default_google(model: BaseChatModel) -> None:
    import research_agent.model_call_guard as guard

    deadline = time.monotonic() + 1.0
    control = guard._start_bridge(
        model.client.aio.aclose,
        scope_id="default-google-client-close",
        registry=model._bridge_registry,
    )
    guard._bridge_result(
        control,
        deadline,
        policy=model._model_call_policy,
        metadata=model._runtime_metadata,
    )
    model.client.close()


def _close_guarded_default_anthropic(model: BaseChatModel) -> None:
    import research_agent.model_call_guard as guard

    deadline = time.monotonic() + 1.0
    control = guard._start_bridge(
        model._async_client.close,
        scope_id="default-anthropic-client-close",
        registry=model._bridge_registry,
    )
    guard._bridge_result(
        control,
        deadline,
        policy=model._model_call_policy,
        metadata=model._runtime_metadata,
    )
    model._client.close()


def _pending_bridge_tasks() -> list[str]:
    import research_agent.model_call_guard as guard

    async def inspect() -> list[str]:
        current = asyncio.current_task()
        return [
            repr(task)
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]

    loop = guard._GLOBAL_BRIDGE_RUNTIME._loop
    assert loop is not None
    return asyncio.run_coroutine_threadsafe(inspect(), loop).result(0.5)


def _configured_retry_controller() -> ModelRetryController:
    return ModelRetryController(
        config=RetryConfig(
            max_retries=4,
            initial_backoff=0.25,
            max_backoff=3.0,
            backoff_multiplier=1.5,
            jitter=False,
        ),
        tpm=2_000,
        rpm=80,
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


class _ConcurrentStateChatModel(_AsyncOnlyChatModel):
    counter: int = 0
    _started: list[str] = PrivateAttr(default_factory=list)
    _both_started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
        self._started.append("started")
        if len(self._started) == 2:
            self._both_started.set()
        await self._both_started.wait()
        self.counter += 1
        return await super()._agenerate(*args, **kwargs)


class _NestedCallbackChatModel(_AsyncOnlyChatModel):
    async def _agenerate(
        self,
        *args: Any,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        child = AsyncCallbackManager(
            handlers=list(run_manager.inheritable_handlers),
            inheritable_handlers=list(run_manager.inheritable_handlers),
        )
        await child.on_custom_event("nested-provider-event", {"kept": True})
        return await super()._agenerate(
            *args,
            run_manager=run_manager,
            **kwargs,
        )


class _NestedRunnableChatModel(_AsyncOnlyChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
        await RunnableLambda(lambda value: value).ainvoke("nested")
        return await super()._agenerate(*args, **kwargs)


class _IndependentBindToolsChatModel(_AsyncOnlyChatModel):
    """Provider fake whose tool binding is independent from the model runnable."""

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any):
        del tools, tool_choice, kwargs

        async def slow_tool_runnable(_input: Any) -> AIMessage:
            await asyncio.sleep(0.08)
            return AIMessage(content="independent")

        return RunnableLambda(slow_tool_runnable)


class _ForkOnlyChatModel(_AsyncOnlyChatModel):
    """Provider class first guarded inside a forked child."""


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


class _InFlightDetachedBindToolsChatModel(_DetachedBindToolsChatModel):
    """Provider fake whose detached call queues a callback before outer return."""

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any):
        del tools, tool_choice, kwargs

        async def spawn_detached(_input: Any) -> AIMessage:
            self._detached_task = asyncio.create_task(self.ainvoke("detached"))
            await asyncio.sleep(0)
            return AIMessage(content="spawned")

        return RunnableLambda(spawn_detached)


class _InFlightDetachedStreamChatModel(_DetachedBindToolsChatModel):
    """Provider fake queues detached callback before exposing a stream item."""

    async def _astream(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        del messages, stop, run_manager, kwargs
        self._detached_task = asyncio.create_task(self.ainvoke("detached"))
        await asyncio.sleep(0)
        yield ChatGenerationChunk(message=AIMessageChunk(content="chunk"))


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


class _ThreadRecordingCallback(BaseCallbackHandler):
    run_inline = True
    raise_error = True

    def __init__(self) -> None:
        self.events: list[tuple[str, int, Any]] = []

    def _record(self, name: str, payload: Any = None) -> None:
        self.events.append((name, threading.get_ident(), payload))

    def on_chat_model_start(self, *_args: Any, **_kwargs: Any) -> None:
        self._record("start")

    def on_llm_new_token(self, token: str, **_kwargs: Any) -> None:
        self._record("token", token)

    def on_stream_event(self, event: Any, **_kwargs: Any) -> None:
        self._record("event", event)

    def on_custom_event(self, name: str, data: Any, **_kwargs: Any) -> None:
        self._record(name, data)

    def on_llm_end(self, _response: Any, **_kwargs: Any) -> None:
        self._record("end")

    def on_llm_error(self, error: BaseException, **_kwargs: Any) -> None:
        self._record("error", str(error))


class _LoopAffineAsyncCallback(AsyncCallbackHandler):
    raise_error = True

    def __init__(self, gate: asyncio.Future[None]) -> None:
        self.gate = gate
        self.loops: list[asyncio.AbstractEventLoop] = []

    async def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], **kwargs: Any
    ) -> None:
        del serialized, messages, kwargs
        self.loops.append(asyncio.get_running_loop())
        await self.gate


class _ThreadProbeTracer(LangChainTracer):
    threads: ClassVar[list[int]] = []
    copies: ClassVar[int] = 0

    def copy_with_metadata_defaults(self, **kwargs: Any) -> LangChainTracer:
        type(self).copies += 1
        return super().copy_with_metadata_defaults(**kwargs)

    def on_chat_model_start(self, *_args: Any, **_kwargs: Any) -> None:
        type(self).threads.append(threading.get_ident())

    def on_llm_end(self, *_args: Any, **_kwargs: Any) -> None:
        type(self).threads.append(threading.get_ident())


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


def test_runtime_descriptor_does_not_require_optional_transport_clone_capability():
    class MetadataOnlyDescriptor:
        @property
        def model_runtime_metadata(self) -> ModelRuntimeMetadata:
            return _fake_metadata()

    assert isinstance(MetadataOnlyDescriptor(), ModelRuntimeDescriptor)


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


def test_detached_task_cannot_reuse_completed_sync_callback_pump():
    import research_agent.model_call_guard as guard

    caller_thread = threading.get_ident()
    callback = _ThreadRecordingCallback()
    guarded = guard_model(
        _DetachedBindToolsChatModel(delay=0, callbacks=[callback]),
        metadata=_fake_metadata(),
        policy=_policy(0.08),
    )

    result = guarded.bind_tools([]).invoke("hello")
    assert result.content == "spawned"
    assert guarded._detached_task is not None
    assert callback.events == []

    loop = guard._GLOBAL_BRIDGE_RUNTIME._loop
    assert loop is not None
    loop.call_soon_threadsafe(guarded._release_detached.set)

    async def wait_task() -> AIMessage:
        assert guarded._detached_task is not None
        return await guarded._detached_task

    future = asyncio.run_coroutine_threadsafe(wait_task(), loop)
    nested = future.result(0.5)

    assert nested.content == "complete"
    assert [name for name, _, _ in callback.events] == ["start", "end"]
    assert {thread_id for _, thread_id, _ in callback.events} != {caller_thread}


def test_in_flight_detached_callback_is_drained_before_sync_pump_retires():
    import research_agent.model_call_guard as guard

    caller_thread = threading.get_ident()
    callback = _ThreadRecordingCallback()
    guarded = guard_model(
        _InFlightDetachedBindToolsChatModel(delay=0, callbacks=[callback]),
        metadata=_fake_metadata(),
        policy=_policy(0.08),
    )

    result = guarded.bind_tools([]).invoke("hello")
    assert result.content == "spawned"
    assert guarded._detached_task is not None

    loop = guard._GLOBAL_BRIDGE_RUNTIME._loop
    assert loop is not None

    async def wait_task() -> AIMessage:
        assert guarded._detached_task is not None
        return await guarded._detached_task

    future = asyncio.run_coroutine_threadsafe(wait_task(), loop)
    nested = future.result(0.5)

    assert nested.content == "complete"
    assert [name for name, _, _ in callback.events] == ["start", "end"]
    assert callback.events[0][1] == caller_thread


def test_sync_stream_drains_in_flight_callback_before_item_and_close():
    import research_agent.model_call_guard as guard

    caller_thread = threading.get_ident()
    callback = _ThreadRecordingCallback()
    guarded = guard_model(
        _InFlightDetachedStreamChatModel(delay=0, callbacks=[callback]),
        metadata=_fake_metadata(),
        policy=_policy(0.08),
    )

    stream = guarded.stream("hello")
    chunk = next(stream)
    stream.close()
    assert chunk.content == "chunk"
    assert guarded._detached_task is not None

    loop = guard._GLOBAL_BRIDGE_RUNTIME._loop
    assert loop is not None

    async def wait_task() -> AIMessage:
        assert guarded._detached_task is not None
        return await guarded._detached_task

    future = asyncio.run_coroutine_threadsafe(wait_task(), loop)
    nested = future.result(0.5)

    assert nested.content == "complete"
    assert callback.events
    assert any(thread_id == caller_thread for _, thread_id, _ in callback.events)


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


@pytest.mark.parametrize("deep", [False, True])
def test_guarded_model_copy_preserves_independent_retry_controller_policy(deep):
    guarded = guard_model(
        _PickleableChatModel(payload=["copy-controller"]),
        metadata=_fake_metadata(),
        policy=_policy(0.2),
    )
    wrap_model_with_rate_limiting(guarded, controller=_configured_retry_controller())
    guarded._retry_controller._capacity.token_window.append((1.0, 50))
    guarded._retry_controller._capacity.last_request_time = 1.0

    copied = guarded.model_copy(deep=deep)

    original = guarded._retry_controller
    restored = copied._retry_controller
    assert restored is not None
    assert restored is not original
    assert restored.config is not original.config
    assert restored.config.__dict__ == original.config.__dict__
    assert restored._capacity is not original._capacity
    assert restored._capacity._lock is not original._capacity._lock
    assert restored._capacity.safe_tpm == original._capacity.safe_tpm
    assert restored._capacity.min_interval == original._capacity.min_interval
    assert restored._capacity.token_window == []
    assert restored._capacity.last_request_time == 0.0


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


def test_guard_pickle_preserves_independent_retry_controller_policy():
    guarded = guard_model(
        _PickleableChatModel(payload=["pickle-controller"]),
        metadata=_fake_metadata(),
        policy=_policy(0.2),
    )
    wrap_model_with_rate_limiting(guarded, controller=_configured_retry_controller())
    guarded._retry_controller._capacity.token_window.append((1.0, 50))
    guarded._retry_controller._capacity.last_request_time = 1.0

    restored = pickle.loads(pickle.dumps(guarded))

    original = guarded._retry_controller
    controller = restored._retry_controller
    assert controller is not None
    assert controller is not original
    assert controller.config.__dict__ == original.config.__dict__
    assert controller._capacity is not original._capacity
    assert controller._capacity._lock is not original._capacity._lock
    assert controller._capacity.safe_tpm == original._capacity.safe_tpm
    assert controller._capacity.min_interval == original._capacity.min_interval
    assert controller._capacity.token_window == []
    assert controller._capacity.last_request_time == 0.0
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
    scope_id = "async-caller-cancel"

    caller = asyncio.create_task(
        guarded.ainvoke(
            "hello", config={"configurable": {"model_call_scope_id": scope_id}}
        )
    )
    await asyncio.sleep(0.01)
    caller.cancel("caller")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert raised.value.args == ("caller",)
    assert guarded._cancelled.is_set()
    assert guarded._bridge_registry.active_count(scope_id) == 0
    assert _pending_bridge_tasks() == []


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
        timeout=0.03,
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
async def test_astream_aclose_does_not_block_caller_loop_during_bridge_cleanup(
    monkeypatch,
):
    import research_agent.model_call_guard as guard

    monkeypatch.setattr(guard, "MODEL_CANCEL_GRACE_SECONDS", 0.08)
    raw = _AsyncOnlyChatModel(
        chunk_delay=1,
        chunks=[AIMessageChunk(content="too-late")],
        suppress_stream_cancel=True,
    )
    guarded = guard_model(raw, metadata=_fake_metadata(), policy=_policy(1))
    stream = guarded.astream("hello")
    pull = asyncio.create_task(anext(stream))
    while not guarded._stream_started.is_set():
        await asyncio.sleep(0)

    started = time.monotonic()
    close = asyncio.create_task(stream.aclose())
    await asyncio.sleep(0.01)
    elapsed = time.monotonic() - started
    guarded._release_stream.set()
    await asyncio.gather(close, pull, return_exceptions=True)

    assert elapsed < 0.04


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


def test_sync_invoke_reuses_real_provider_keep_alive_client_across_calls():
    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            replies = [guarded.invoke(f"request-{index}") for index in range(3)]
        finally:
            _close_guarded_local_ollama(guarded)

    assert [reply.content for reply in replies] == ["local-reply"] * 3
    assert len(requests) == 3


def test_concurrent_sync_invokes_share_real_provider_bridge_loop_safely():
    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                replies = list(
                    pool.map(guarded.invoke, [f"request-{i}" for i in range(4)])
                )
        finally:
            _close_guarded_local_ollama(guarded)

    assert [reply.content for reply in replies] == ["local-reply"] * 4
    assert len(requests) == 4


def test_real_provider_sync_async_sync_calls_share_one_transport_loop():
    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            first = guarded.invoke("sync-first")
            second = asyncio.run(guarded.ainvoke("async-middle"))
            third = guarded.invoke("sync-last")
        finally:
            _close_guarded_local_ollama(guarded)

    assert [first.content, second.content, third.content] == ["local-reply"] * 3
    assert len(requests) == 3


def test_real_provider_async_sync_async_calls_share_one_transport_loop():
    async def run_sequence(guarded: BaseChatModel) -> list[Any]:
        loop = asyncio.get_running_loop()
        first = await guarded.ainvoke("async-first")
        second = await loop.run_in_executor(None, guarded.invoke, "sync-middle")
        third = await guarded.ainvoke("async-last")
        return [first, second, third]

    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            replies = asyncio.run(run_sequence(guarded))
        finally:
            _close_guarded_local_ollama(guarded)

    assert [reply.content for reply in replies] == ["local-reply"] * 3
    assert len(requests) == 3


def test_real_provider_concurrent_sync_and_async_calls_share_transport_loop():
    async def run_concurrently(guarded: BaseChatModel) -> list[Any]:
        loop = asyncio.get_running_loop()
        sync_call = loop.run_in_executor(None, guarded.invoke, "sync-concurrent")
        async_call = guarded.ainvoke("async-concurrent")
        return list(await asyncio.gather(sync_call, async_call))

    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            replies = asyncio.run(run_concurrently(guarded))
        finally:
            _close_guarded_local_ollama(guarded)

    assert [reply.content for reply in replies] == ["local-reply"] * 2
    assert len(requests) == 2
    assert _pending_bridge_tasks() == []


@_async_test
async def test_real_async_caller_cancellation_disconnects_provider_transport():
    request_started = threading.Event()
    client_disconnected = threading.Event()
    with _ollama_compatible_server(
        request_started=request_started,
        client_disconnected=client_disconnected,
    ) as (base_url, _requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            scope_id = "real-async-cancel"
            caller = asyncio.create_task(
                guarded.ainvoke(
                    "cancel-real-request",
                    config={"configurable": {"model_call_scope_id": scope_id}},
                )
            )
            assert await asyncio.to_thread(request_started.wait, 0.5)
            caller.cancel("real-caller")
            with pytest.raises(asyncio.CancelledError) as raised:
                await caller
            assert raised.value.args == ("real-caller",)
            assert await asyncio.to_thread(client_disconnected.wait, 0.5)
            assert guarded._bridge_registry.active_count(scope_id) == 0
            assert _pending_bridge_tasks() == []
        finally:
            _close_guarded_local_ollama(guarded)


def test_real_provider_sync_and_async_streams_share_transport_loop():
    async def collect(guarded: BaseChatModel) -> list[Any]:
        return [chunk async for chunk in guarded.astream("async-stream")]

    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            sync_first = list(guarded.stream("sync-stream-first"))
            async_middle = asyncio.run(collect(guarded))
            sync_last = list(guarded.stream("sync-stream-last"))
        finally:
            _close_guarded_local_ollama(guarded)

    assert sync_first[0].content == "local-reply"
    assert async_middle[0].content == "local-reply"
    assert sync_last[0].content == "local-reply"
    assert len(requests) == 3


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_bridge_runtime_resets_in_child_and_parent_remains_usable():
    guarded = _guarded_fake(timeout=0.2)
    assert guarded.invoke("parent-before").content == "complete"
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            child_reply = guarded.invoke("child-success")
            slow = _guarded_fake(timeout=0.02, delay=1)
            try:
                slow.invoke("child-cancel")
            except ModelCallTimeoutError:
                cancelled = slow._cancelled.wait(0.2)
            else:
                cancelled = False
            payload = f"{child_reply.content}:{cancelled}".encode()
        except BaseException as exc:
            payload = f"error:{type(exc).__name__}:{exc}".encode()
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    deadline = time.monotonic() + 2.0
    status = None
    while time.monotonic() < deadline:
        completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
        if completed_pid == child_pid:
            break
        time.sleep(0.01)
    else:
        os.kill(child_pid, 9)
        os.waitpid(child_pid, 0)
        pytest.fail("forked child did not exit promptly")

    payload = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    assert os.waitstatus_to_exitcode(status) == 0
    assert payload == "complete:True"
    assert guarded.invoke("parent-after").content == "complete"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_recreates_inherited_real_provider_transport():
    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            assert guarded.invoke("parent-before-fork").content == "local-reply"
            read_fd, write_fd = os.pipe()
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    payload = guarded.invoke("child-after-fork").content.encode()
                except BaseException as exc:
                    payload = f"error:{type(exc).__name__}:{exc}".encode()
                os.write(write_fd, payload)
                os.close(write_fd)
                os._exit(0)

            os.close(write_fd)
            deadline = time.monotonic() + 2.0
            status = None
            while time.monotonic() < deadline:
                completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
                if completed_pid == child_pid:
                    break
                time.sleep(0.01)
            else:
                os.kill(child_pid, 9)
                os.waitpid(child_pid, 0)
                pytest.fail("forked provider child did not exit promptly")
            payload = os.read(read_fd, 4096).decode()
            os.close(read_fd)
            assert os.waitstatus_to_exitcode(status) == 0
            assert payload == "local-reply"
            assert guarded.invoke("parent-after-fork").content == "local-reply"
        finally:
            _close_guarded_local_ollama(guarded)

    assert len(requests) == 3


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_model_copy_recreates_inherited_provider_transport():
    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_ollama(base_url)
        try:
            assert guarded.invoke("parent-before-fork").content == "local-reply"
            read_fd, write_fd = os.pipe()
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    copied = guarded.model_copy()
                    payload = copied.invoke("copied-child-after-fork").content.encode()
                except BaseException as exc:
                    payload = f"error:{type(exc).__name__}:{exc}".encode()
                os.write(write_fd, payload)
                os.close(write_fd)
                os._exit(0)

            os.close(write_fd)
            deadline = time.monotonic() + 2.0
            status = None
            while time.monotonic() < deadline:
                completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
                if completed_pid == child_pid:
                    break
                time.sleep(0.01)
            else:
                os.kill(child_pid, 9)
                os.waitpid(child_pid, 0)
                pytest.fail("forked model-copy child did not exit promptly")
            payload = os.read(read_fd, 4096).decode()
            os.close(read_fd)
            assert os.waitstatus_to_exitcode(status) == 0
            assert payload == "local-reply"
            assert guarded.invoke("parent-after-fork").content == "local-reply"
        finally:
            _close_guarded_local_ollama(guarded)

    assert len(requests) == 3


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_recreates_bedrock_openai_compatible_transport():
    with _ollama_compatible_server() as (base_url, requests):
        guarded = _guarded_local_bedrock_openai(base_url)
        try:
            assert guarded.invoke("parent-before-fork").content == "local-reply"
            read_fd, write_fd = os.pipe()
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    payload = guarded.invoke("child-after-fork").content.encode()
                except BaseException as exc:
                    payload = f"error:{type(exc).__name__}:{exc}".encode()
                os.write(write_fd, payload)
                os.close(write_fd)
                os._exit(0)

            os.close(write_fd)
            deadline = time.monotonic() + 2.0
            status = None
            while time.monotonic() < deadline:
                completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
                if completed_pid == child_pid:
                    break
                time.sleep(0.01)
            else:
                os.kill(child_pid, 9)
                os.waitpid(child_pid, 0)
                pytest.fail("forked Bedrock-compatible child did not exit promptly")
            payload = os.read(read_fd, 4096).decode()
            os.close(read_fd)
            assert os.waitstatus_to_exitcode(status) == 0
            assert payload == "local-reply"
            assert guarded.invoke("parent-after-fork").content == "local-reply"
        finally:
            _close_guarded_local_openai(guarded)

    assert len(requests) == 3


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_recreates_default_openai_clients_without_proxy_discovery():
    import research_agent.model_call_guard as guard

    guarded = _guarded_default_openai()
    assert _guarded_fake(timeout=0.2).invoke("initialize-runtime").content == "complete"
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            guard._ensure_model_process(guarded)
            payload = b"refreshed"
        except BaseException as exc:
            payload = f"error:{type(exc).__name__}:{exc}".encode()
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    deadline = time.monotonic() + 2.0
    status = None
    try:
        while time.monotonic() < deadline:
            completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
            if completed_pid == child_pid:
                break
            time.sleep(0.01)
        else:
            os.kill(child_pid, 9)
            os.waitpid(child_pid, 0)
            pytest.fail("forked default OpenAI child did not exit promptly")
        payload = os.read(read_fd, 4096).decode()
        assert os.waitstatus_to_exitcode(status) == 0
        assert payload == "refreshed"
    finally:
        os.close(read_fd)
        _close_guarded_default_openai(guarded)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_fork_client_reconstruction_preserves_resolved_proxy_mounts(
    asynchronous: bool,
):
    import research_agent.model_call_guard as guard

    proxy_url = "http://fork-proxy.invalid:8443"
    policy = _policy(0.2)
    if asynchronous:
        original = httpx.AsyncClient(proxy=proxy_url, trust_env=False)
        fresh = guard._fresh_httpx_async_client_after_fork(original, policy)
    else:
        original = httpx.Client(proxy=proxy_url, trust_env=False)
        fresh = guard._fresh_httpx_client_after_fork(original, policy)
    try:
        proxy_pools = [
            transport._pool
            for transport in fresh._mounts.values()
            if transport is not None
            and getattr(transport._pool, "_proxy_url", None) is not None
        ]

        assert [bytes(pool._proxy_url) for pool in proxy_pools] == [
            b"http://fork-proxy.invalid:8443/"
        ]
        assert all(
            transport not in original._mounts.values()
            for transport in fresh._mounts.values()
        )
        assert fresh._trust_env is False
    finally:
        if asynchronous:
            asyncio.run(original.aclose())
            asyncio.run(fresh.aclose())
        else:
            original.close()
            fresh.close()


@pytest.mark.parametrize(
    "provider",
    ["openai", "ollama", "google", "anthropic"],
)
def test_provider_reconstruction_preserves_pre_resolved_environment_proxy(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
):
    import research_agent.model_call_guard as guard

    proxy_url = "http://resolved-before-fork.invalid:8443"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    builders = {
        "openai": _guarded_default_openai,
        "ollama": _guarded_default_ollama,
        "google": _guarded_default_google,
        "anthropic": _guarded_default_anthropic,
    }
    closers = {
        "openai": _close_guarded_default_openai,
        "ollama": _close_guarded_local_ollama,
        "google": _close_guarded_default_google,
        "anthropic": _close_guarded_default_anthropic,
    }
    guarded = builders[provider]()
    if provider == "openai":
        guarded.root_client._client.close()
        asyncio.run(guarded.root_async_client._client.aclose())
        guarded.root_client._client = httpx.Client(
            proxy=proxy_url,
            trust_env=False,
        )
        guarded.root_async_client._client = httpx.AsyncClient(
            proxy=proxy_url,
            trust_env=False,
        )
    monkeypatch.delenv("HTTP_PROXY")
    monkeypatch.delenv("HTTPS_PROXY")
    guarded._guard_pid = -1

    guard._ensure_model_process(guarded)
    if provider == "openai":
        clients = (guarded.http_client, guarded.http_async_client)
    elif provider == "ollama":
        clients = (guarded._client._client, guarded._async_client._client)
    elif provider == "google":
        api_client = guarded.client._api_client
        clients = (api_client._httpx_client, api_client._async_httpx_client)
    else:
        clients = (guarded._client._client, guarded._async_client._client)
    try:
        for client in clients:
            resolved_proxies = {
                bytes(transport._pool._proxy_url).decode("ascii")
                for transport in client._mounts.values()
                if transport is not None
                and getattr(transport._pool, "_proxy_url", None) is not None
            }
            assert proxy_url + "/" in resolved_proxies
            assert client._trust_env is False
    finally:
        closers[provider](guarded)


def test_ollama_reconstruction_fails_closed_for_custom_inherited_transports():
    from langchain_ollama import ChatOllama

    import research_agent.model_call_guard as guard

    sync_transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    async_transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    guarded = build_guarded_provider_model(
        ChatOllama,
        {
            "model": "fork-custom-transport-test",
            "sync_client_kwargs": {"transport": sync_transport},
            "async_client_kwargs": {"transport": async_transport},
        },
        ModelRuntimeMetadata(
            provider="ollama",
            model_name="fork-custom-transport-test",
        ),
        _policy(0.2),
    )
    guarded._guard_pid = -1

    with pytest.raises(UnsupportedModelOverrideError) as raised:
        guard._ensure_model_process(guarded)
    try:
        assert raised.value.provider == "ollama"
        assert guarded._client._client._transport is sync_transport
        assert guarded._async_client._client._transport is async_transport
    finally:
        _close_guarded_local_ollama(guarded)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_fails_closed_for_undeclared_custom_transport():
    from langchain_ollama import ChatOllama

    import research_agent.model_call_guard as guard

    guarded = build_guarded_provider_model(
        ChatOllama,
        {
            "model": "fork-unsupported-transport",
            "sync_client_kwargs": {
                "transport": httpx.MockTransport(
                    lambda _request: httpx.Response(200)
                )
            },
            "async_client_kwargs": {
                "transport": httpx.MockTransport(
                    lambda _request: httpx.Response(200)
                )
            },
        },
        ModelRuntimeMetadata(
            provider="ollama",
            model_name="fork-unsupported-transport",
        ),
        _policy(0.2),
    )
    assert _guarded_fake(timeout=0.2).invoke("initialize-runtime").content == "complete"
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            guard._ensure_model_process(guarded)
        except UnsupportedModelOverrideError as exc:
            payload = f"unsupported:{exc.provider}".encode()
        except BaseException as exc:
            payload = f"unsafe:{type(exc).__name__}:{exc}".encode()
        else:
            payload = b"silently-replaced"
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        completed_pid, status = os.waitpid(child_pid, 0)
        assert completed_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == 0
        assert os.read(read_fd, 4096) == b"unsupported:ollama"
    finally:
        os.close(read_fd)
        _close_guarded_local_ollama(guarded)


def test_declared_custom_transport_factory_is_used_during_reconstruction():
    from langchain_ollama import ChatOllama

    import research_agent.model_call_guard as guard

    class CloneableChatOllama(ChatOllama):
        def clone_model_http_transport(self, transport, *, asynchronous):
            del transport, asynchronous
            return httpx.MockTransport(lambda _request: httpx.Response(200))

    sync_transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    async_transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    guarded = build_guarded_provider_model(
        CloneableChatOllama,
        {
            "model": "fork-declared-transport",
            "sync_client_kwargs": {"transport": sync_transport},
            "async_client_kwargs": {"transport": async_transport},
        },
        ModelRuntimeMetadata(
            provider="ollama",
            model_name="fork-declared-transport",
        ),
        _policy(0.2),
    )
    guarded._guard_pid = -1

    guard._ensure_model_process(guarded)
    try:
        assert guarded._client._client._transport is not sync_transport
        assert guarded._async_client._client._transport is not async_transport
    finally:
        _close_guarded_local_ollama(guarded)


@pytest.mark.parametrize("provider", ["ollama", "google", "anthropic"])
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_recreates_default_provider_clients_without_env_proxy(
    provider: str,
):
    import research_agent.model_call_guard as guard

    builders = {
        "ollama": _guarded_default_ollama,
        "google": _guarded_default_google,
        "anthropic": _guarded_default_anthropic,
    }
    closers = {
        "ollama": _close_guarded_local_ollama,
        "google": _close_guarded_default_google,
        "anthropic": _close_guarded_default_anthropic,
    }
    guarded = builders[provider]()
    assert _guarded_fake(timeout=0.2).invoke("initialize-runtime").content == "complete"
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            guard._ensure_model_process(guarded)
            if provider == "ollama":
                trust_env = (
                    guarded._client._client._trust_env,
                    guarded._async_client._client._trust_env,
                )
            elif provider == "google":
                api_client = guarded.client._api_client
                trust_env = (
                    api_client._httpx_client._trust_env,
                    api_client._async_httpx_client._trust_env,
                )
            else:
                trust_env = (
                    guarded._client._client._trust_env,
                    guarded._async_client._client._trust_env,
                )
            payload = repr(trust_env).encode()
        except BaseException as exc:
            payload = f"error:{type(exc).__name__}:{exc}".encode()
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    deadline = time.monotonic() + 2.0
    status = None
    try:
        while time.monotonic() < deadline:
            completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
            if completed_pid == child_pid:
                break
            time.sleep(0.01)
        else:
            os.kill(child_pid, 9)
            os.waitpid(child_pid, 0)
            pytest.fail(f"forked default {provider} child did not exit promptly")
        payload = os.read(read_fd, 4096).decode()
        assert os.waitstatus_to_exitcode(status) == 0
        assert payload == "(False, False)"
    finally:
        os.close(read_fd)
        closers[provider](guarded)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_resets_guarded_provider_class_lock():
    import research_agent.model_call_guard as guard

    parent_holds_lock = threading.Event()
    release_parent_lock = threading.Event()

    def hold_parent_lock() -> None:
        with guard._GUARDED_PROVIDER_CLASSES_LOCK:
            parent_holds_lock.set()
            release_parent_lock.wait(1.0)

    holder = threading.Thread(target=hold_parent_lock)
    holder.start()
    assert parent_holds_lock.wait(0.2)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        acquired = guard._GUARDED_PROVIDER_CLASSES_LOCK.acquire(timeout=0.2)
        if acquired:
            guard._GUARDED_PROVIDER_CLASSES_LOCK.release()
            guarded = guard_model(
                _ForkOnlyChatModel(),
                metadata=_fake_metadata(),
                policy=_policy(0.2),
            )
            payload = str(isinstance(guarded, _ForkOnlyChatModel)).encode()
        else:
            payload = b"inherited-locked"
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        completed_pid, status = os.waitpid(child_pid, 0)
        assert completed_pid == child_pid
        payload = os.read(read_fd, 4096).decode()
        assert os.waitstatus_to_exitcode(status) == 0
        assert payload == "True"
    finally:
        os.close(read_fd)
        release_parent_lock.set()
        holder.join(0.5)


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_bridge_copies_calling_contextvars(method: str):
    guarded = _guarded_fake()
    token = _TEST_CONTEXT.set("caller-context")
    try:
        if method == "invoke":
            guarded.invoke("hello")
        else:
            asyncio.run(guarded.ainvoke("hello"))
    finally:
        _TEST_CONTEXT.reset(token)

    assert guarded._context_values == ["caller-context"]


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_callbacks_tags_metadata_and_configurable_config_survive_guard(method: str):
    callback = _RecordingCallback()
    guarded = _guarded_fake()
    config = {
        "callbacks": [callback],
        "tags": ["guarded-tag"],
        "metadata": {"request": "kept"},
        "configurable": {"model_call_scope_id": "callback-scope", "tenant": "kept"},
    }

    if method == "invoke":
        result = guarded.invoke("hello", config=config)
    else:
        result = asyncio.run(guarded.ainvoke("hello", config=config))

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


@pytest.mark.parametrize("method", ["ainvoke", "astream"])
@pytest.mark.parametrize("callback_source", ["config", "model"])
@_async_test
async def test_async_callbacks_remain_on_caller_loop(
    callback_source: str,
    method: str,
):
    caller_loop = asyncio.get_running_loop()
    gate = caller_loop.create_future()
    callback = _LoopAffineAsyncCallback(gate)
    raw = _AsyncOnlyChatModel(
        callbacks=[callback] if callback_source == "model" else None,
        chunks=[AIMessageChunk(content="complete")],
    )
    guarded = guard_model(raw, metadata=_fake_metadata(), policy=_policy(0.2))
    config = {"callbacks": [callback]} if callback_source == "config" else None
    caller_loop.call_later(0.01, gate.set_result, None)

    if method == "ainvoke":
        result = await guarded.ainvoke("hello", config=config)
        assert result.content == "complete"
    else:
        chunks = [chunk async for chunk in guarded.astream("hello", config=config)]
        assert "".join(str(chunk.content) for chunk in chunks) == "complete"

    assert callback.loops == [caller_loop]


@pytest.mark.parametrize("method", ["invoke", "stream"])
def test_sync_callbacks_run_once_on_original_caller_thread(method: str):
    caller_thread = threading.get_ident()
    callback = _ThreadRecordingCallback()
    guarded = guard_model(
        _AsyncOnlyChatModel(
            callbacks=[callback],
            chunks=[AIMessageChunk(content="chunk")],
        ),
        metadata=_fake_metadata(),
        policy=_policy(0.5),
    )

    if method == "invoke":
        result = guarded.invoke("hello", config={"callbacks": [callback]})
        assert result.content == "complete"
        assert [name for name, _, _ in callback.events] == ["start", "end"]
    else:
        chunks = list(guarded.stream("hello", config={"callbacks": [callback]}))
        assert "".join(str(chunk.content) for chunk in chunks) == "chunk"
        assert [name for name, _, _ in callback.events] == [
            "start",
            "token",
            "token",
            "end",
        ]
    assert {thread_id for _, thread_id, _ in callback.events} == {caller_thread}


@pytest.mark.parametrize("source", ["explicit", "ambient"])
def test_sync_v2_tracer_callbacks_stay_on_original_caller_thread(source: str):
    _ThreadProbeTracer.threads = []
    _ThreadProbeTracer.copies = 0
    tracer = _ThreadProbeTracer(project_name="guard-probe", client=object())
    caller_thread = threading.get_ident()
    config = {"callbacks": [tracer]} if source == "explicit" else None
    token = tracing_v2_callback_var.set(tracer) if source == "ambient" else None
    try:
        result = _guarded_fake(timeout=0.5).invoke("hello", config=config)
    finally:
        if token is not None:
            tracing_v2_callback_var.reset(token)

    assert result.content == "complete"
    assert _ThreadProbeTracer.copies >= 1
    assert _ThreadProbeTracer.threads
    assert set(_ThreadProbeTracer.threads) == {caller_thread}


def test_sync_ambient_run_collector_stays_on_original_caller_thread():
    caller_thread = threading.get_ident()
    persisted_threads: list[int] = []
    with collect_runs() as collector:
        original_persist = collector._persist_run

        def persist_on_probe(self: Any, run: Any) -> None:
            persisted_threads.append(threading.get_ident())
            original_persist(run)

        collector._persist_run = MethodType(persist_on_probe, collector)
        result = _guarded_fake(timeout=0.5).invoke("hello")

    assert result.content == "complete"
    assert persisted_threads == [caller_thread]


def test_sync_ambient_run_collector_preserves_nested_runnable_context():
    guarded = guard_model(
        _NestedRunnableChatModel(),
        metadata=_fake_metadata(),
        policy=_policy(0.5),
    )

    with collect_runs() as collector:
        result = guarded.invoke("hello")

    assert result.content == "complete"
    assert {run.name for run in collector.traced_runs} >= {
        "RunnableLambda",
        "_NestedRunnableChatModel",
    }


@pytest.mark.parametrize("method", ["ainvoke", "astream"])
@_async_test
async def test_async_model_and_config_callback_identity_is_deduplicated(method: str):
    callback = _ThreadRecordingCallback()
    guarded = guard_model(
        _AsyncOnlyChatModel(
            callbacks=[callback],
            chunks=[AIMessageChunk(content="chunk")],
        ),
        metadata=_fake_metadata(),
        policy=_policy(0.5),
    )

    if method == "ainvoke":
        result = await guarded.ainvoke("hello", config={"callbacks": [callback]})
        assert result.content == "complete"
        assert [name for name, _, _ in callback.events] == ["start", "end"]
    else:
        chunks = [
            chunk
            async for chunk in guarded.astream(
                "hello",
                config={"callbacks": [callback]},
            )
        ]
        assert "".join(str(chunk.content) for chunk in chunks) == "chunk"
        assert [name for name, _, _ in callback.events] == [
            "start",
            "token",
            "token",
            "end",
        ]


@_async_test
async def test_concurrent_callback_calls_mutate_one_provider_instance():
    callback = _RecordingCallback()
    guarded = guard_model(
        _ConcurrentStateChatModel(callbacks=[callback]),
        metadata=_fake_metadata(),
        policy=_policy(0.5),
    )

    results = await asyncio.gather(
        guarded.ainvoke("first"),
        guarded.ainvoke("second"),
    )

    assert [result.content for result in results] == ["complete", "complete"]
    assert guarded.counter == 2
    assert len(callback.starts) == 2


def test_sync_error_callback_runs_once_on_original_caller_thread():
    caller_thread = threading.get_ident()
    callback = _ThreadRecordingCallback()
    guarded = guard_model(
        _AsyncOnlyChatModel(callbacks=[callback], failures_before_success=1),
        metadata=_fake_metadata(),
        policy=_policy(0.5),
    )

    with pytest.raises(RuntimeError, match="retryable provider failure"):
        guarded.invoke("hello", config={"callbacks": [callback]})

    assert [name for name, _, _ in callback.events] == ["start", "error"]
    assert {thread_id for _, thread_id, _ in callback.events} == {caller_thread}


def test_sync_callback_can_invoke_another_guarded_model_without_deadlock():
    caller_thread = threading.get_ident()
    inner = _guarded_fake(timeout=0.5)

    class NestedInvokeCallback(_ThreadRecordingCallback):
        def on_chat_model_start(self, *_args: Any, **_kwargs: Any) -> None:
            self._record("start")
            nested = inner.invoke("nested")
            assert nested.content == "complete"

    callback = NestedInvokeCallback()
    outer = _guarded_fake(timeout=0.5)

    result = outer.invoke("outer", config={"callbacks": [callback]})

    assert result.content == "complete"
    assert [name for name, _, _ in callback.events] == ["start", "end"]
    assert {thread_id for _, thread_id, _ in callback.events} == {caller_thread}
    assert len(inner._calls) == 1


@pytest.mark.parametrize("method", ["invoke", "stream"])
def test_bound_sync_model_callbacks_stay_on_original_caller_thread(method: str):
    caller_thread = threading.get_ident()
    callback = _ThreadRecordingCallback()
    guarded = guard_model(
        _AsyncOnlyChatModel(chunks=[AIMessageChunk(content="chunk")]),
        metadata=_fake_metadata(),
        policy=_policy(0.5),
    ).bind(bound_flag="kept")

    if method == "invoke":
        result = guarded.invoke("hello", config={"callbacks": [callback]})
        assert result.content == "complete"
    else:
        chunks = list(guarded.stream("hello", config={"callbacks": [callback]}))
        assert "".join(str(chunk.content) for chunk in chunks) == "chunk"

    assert callback.events
    assert {thread_id for _, thread_id, _ in callback.events} == {caller_thread}


def test_inherited_only_config_callback_runs_nested_event_on_sync_caller_thread():
    caller_thread = threading.get_ident()
    callback = _ThreadRecordingCallback()
    callback_manager = AsyncCallbackManager(
        handlers=[],
        inheritable_handlers=[callback],
    )
    guarded = guard_model(
        _NestedCallbackChatModel(),
        metadata=_fake_metadata(),
        policy=_policy(0.5),
    )

    result = guarded.invoke("hello", config={"callbacks": callback_manager})

    assert result.content == "complete"
    assert callback.events == [
        ("nested-provider-event", caller_thread, {"kept": True})
    ]


@_async_test
async def test_model_callback_manager_context_survives_caller_loop_proxy():
    import research_agent.model_call_guard as guard

    callback = _RecordingCallback()
    inherited = _RecordingCallback()
    parent_run_id = uuid4()
    callback_manager = AsyncCallbackManager(
        handlers=[callback],
        inheritable_handlers=[inherited],
        parent_run_id=parent_run_id,
        tags=["local-tag"],
        inheritable_tags=["inherited-tag"],
        metadata={"local": "value"},
        inheritable_metadata={"inherited": "value"},
    )
    guarded = guard_model(
        _AsyncOnlyChatModel(callbacks=callback_manager),
        metadata=_fake_metadata(),
        policy=_policy(0.2),
    )

    provider_view = guard._provider_on_caller_callback_loop(
        guarded,
        asyncio.get_running_loop(),
    )

    assert isinstance(provider_view.callbacks, AsyncCallbackManager)
    assert provider_view.callbacks.parent_run_id == parent_run_id
    assert provider_view.callbacks.tags == ["local-tag"]
    assert provider_view.callbacks.inheritable_tags == ["inherited-tag"]
    assert provider_view.callbacks.metadata == {"local": "value"}
    assert provider_view.callbacks.inheritable_metadata == {"inherited": "value"}
    assert [proxy._handler for proxy in provider_view.callbacks.handlers] == [callback]
    assert [
        proxy._handler for proxy in provider_view.callbacks.inheritable_handlers
    ] == [inherited]
    assert guarded.callbacks is not callback_manager
    assert callback_manager.handlers == [callback]
    assert callback_manager.inheritable_handlers == [inherited]


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
    runtime = guard._GLOBAL_BRIDGE_RUNTIME
    assert runtime._thread.daemon
    assert runtime._thread.name == "model-call-bridge-runtime"
    assert runtime._thread.is_alive()
    unregister_deadline = time.monotonic() + 0.1
    while guarded._bridge_registry.active_count("late-cleanup"):
        assert time.monotonic() < unregister_deadline
        time.sleep(0.002)
    assert guarded._bridge_registry.active_count("late-cleanup") == 0
    assert control._completed.is_set()
    loop = control._loop
    assert loop is runtime._loop
    assert loop is not None
    loop.call_soon_threadsafe(guarded._release_invoke.set)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.01), loop).result(0.2)
    assert runtime._thread.is_alive()


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
    import research_agent.model_call_guard as guard

    original_submit = guard._GLOBAL_BRIDGE_RUNTIME.submit

    def delayed_submit(control) -> None:
        time.sleep(0.05)
        original_submit(control)

    monkeypatch.setattr(guard._GLOBAL_BRIDGE_RUNTIME, "submit", delayed_submit)
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
    runtime_policy_keys = {
        "timeout",
        "request_timeout",
        "default_request_timeout",
        "max_retries",
    }
    assert {
        key: value
        for key, value in guarded._identifying_params.items()
        if key not in runtime_policy_keys
    } == {
        key: value
        for key, value in raw._identifying_params.items()
        if key not in runtime_policy_keys
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
    if provider != "ollama":
        assert guarded.max_retries == 0


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
def test_openai_provider_rejects_unclonable_explicit_http_transports(
    provider_class, constructor
):
    class SyncTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

    class AsyncTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

    sync_transport = SyncTransport()
    async_transport = AsyncTransport()
    sync_client = httpx.Client(
        timeout=17,
        transport=sync_transport,
    )
    async_client = httpx.AsyncClient(
        timeout=17,
        transport=async_transport,
    )
    raw = provider_class(
        **constructor,
        timeout=17,
        http_client=sync_client,
        http_async_client=async_client,
    )

    try:
        with pytest.raises(UnsupportedModelOverrideError) as raised:
            adapt_model_override(raw, policy=_policy(0.2))

        assert raised.value.provider in {"openai", "azure_openai"}
        assert sync_client.get("https://example.test/raw").status_code == 200
        assert (
            asyncio.run(async_client.get("https://example.test/raw")).status_code
            == 200
        )
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())


@pytest.mark.parametrize(
    ("provider_class", "constructor"),
    _openai_provider_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_openai_provider_rejects_undeclared_standard_transport_subclasses(
    provider_class, constructor
):
    class CustomSyncTransport(httpx.HTTPTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

    class CustomAsyncTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

    sync_client = httpx.Client(transport=CustomSyncTransport())
    async_client = httpx.AsyncClient(transport=CustomAsyncTransport())
    raw = provider_class(
        **constructor,
        http_client=sync_client,
        http_async_client=async_client,
    )

    try:
        with pytest.raises(UnsupportedModelOverrideError) as raised:
            adapt_model_override(raw, policy=_policy(0.2))
        assert raised.value.provider in {"openai", "azure_openai"}
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())


@pytest.mark.parametrize(
    ("provider_class", "constructor"),
    _openai_provider_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_openai_provider_clones_recognized_explicit_clients_without_sharing(
    provider_class, constructor
):
    sync_client = httpx.Client(timeout=17, trust_env=False)
    async_client = httpx.AsyncClient(timeout=17, trust_env=False)
    raw = provider_class(
        **constructor,
        timeout=17,
        http_client=sync_client,
        http_async_client=async_client,
    )

    guarded = adapt_model_override(raw, policy=_policy(0.2))
    try:
        assert guarded.http_client is not sync_client
        assert guarded.http_async_client is not async_client
        assert guarded.http_client._transport is not sync_client._transport
        assert guarded.http_async_client._transport is not async_client._transport
        assert not sync_client.is_closed
        assert not async_client.is_closed
    finally:
        guarded.root_client.close()
        asyncio.run(guarded.root_async_client.close())
        assert not sync_client.is_closed
        assert not async_client.is_closed
        sync_client.close()
        asyncio.run(async_client.aclose())


def test_openai_provider_uses_declared_custom_transport_clone_capability():
    from langchain_openai import ChatOpenAI

    sync_requests: list[str] = []
    async_requests: list[str] = []

    class SyncTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            sync_requests.append(str(request.url))
            return httpx.Response(200, request=request)

    class AsyncTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            async_requests.append(str(request.url))
            return httpx.Response(200, request=request)

    class CloneableChatOpenAI(ChatOpenAI):
        def clone_model_http_transport(self, transport, *, asynchronous):
            del transport
            return AsyncTransport() if asynchronous else SyncTransport()

    sync_transport = SyncTransport()
    async_transport = AsyncTransport()
    raw_sync_client = httpx.Client(transport=sync_transport)
    raw_async_client = httpx.AsyncClient(transport=async_transport)
    raw = CloneableChatOpenAI(
        model="cloneable-openai",
        api_key="sk-test",
        http_client=raw_sync_client,
        http_async_client=raw_async_client,
    )

    guarded = adapt_model_override(raw, policy=_policy(0.2))
    try:
        assert guarded.http_client._transport is not sync_transport
        assert guarded.http_async_client._transport is not async_transport
        assert guarded.http_client.get("https://example.test/guarded").status_code == 200
        assert (
            asyncio.run(guarded.http_async_client.get("https://example.test/guarded"))
            .status_code
            == 200
        )
        assert raw_sync_client.get("https://example.test/raw").status_code == 200
        assert (
            asyncio.run(raw_async_client.get("https://example.test/raw")).status_code
            == 200
        )
        assert len(sync_requests) == 2
        assert len(async_requests) == 2
    finally:
        guarded.root_client.close()
        asyncio.run(guarded.root_async_client.close())
        raw_sync_client.close()
        asyncio.run(raw_async_client.aclose())


def test_used_raw_openai_default_client_stays_on_caller_loop_after_adaptation():
    from langchain_openai import ChatOpenAI

    guarded: BaseChatModel | None = None
    raw: BaseChatModel | None = None

    with _ollama_compatible_server() as (base_url, requests):

        async def exercise() -> None:
            nonlocal guarded, raw
            raw = ChatOpenAI(
                model="raw-caller-loop",
                api_key=SecretStr("test-key"),
                base_url=f"{base_url}/v1",
                max_retries=0,
                http_socket_options=(),
            )
            first = await raw.ainvoke("raw-before")
            guarded = adapt_model_override(raw, policy=_policy(1.0))

            assert (
                raw.root_async_client._client._transport
                is not guarded.root_async_client._client._transport
            )
            guarded_reply = await guarded.ainvoke("guarded")
            final = await raw.ainvoke("raw-after")

            assert first.content == guarded_reply.content == final.content
            await raw.root_async_client.close()
            raw.root_client.close()

        asyncio.run(exercise())
        assert [request["messages"][0]["content"] for request in requests] == [
            "raw-before",
            "guarded",
            "raw-after",
        ]
    assert guarded is not None
    _close_guarded_default_openai(guarded)


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
