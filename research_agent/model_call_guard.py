"""Configuration and safe metadata for bounded model calls."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import hashlib
import inspect
import logging
import math
import os
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from concurrent.futures import Future as ConcurrentFuture
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

import httpx
from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.callbacks.manager import AsyncCallbackManager
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.runnables.config import ensure_config, merge_configs
from pydantic import PrivateAttr

DEFAULT_MODEL_CALL_TIMEOUT_SECONDS = 300.0
MODEL_CANCEL_GRACE_SECONDS = 2.0
OLLAMA_UNLOAD_TIMEOUT_SECONDS = 2.0

ModelProvider = Literal[
    "aws_bedrock",
    "azure_openai",
    "openai",
    "google",
    "anthropic",
    "ollama",
    "unknown",
]

_KNOWN_PROVIDERS = frozenset(ModelProvider.__args__)
_LOGGER = logging.getLogger(__name__)
_ACTIVE_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "model_call_deadline", default=None
)
_ACTIVE_SCOPE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "model_call_scope_id", default=None
)
_DYNAMIC_CALLBACK_TARGET = object()


class _CallbackTargetRoute:
    """Revocable callback destination copied safely into child task contexts."""

    def __init__(self, target: Any) -> None:
        self._target = target
        self._active = threading.Event()
        self._active.set()

    def resolve(self) -> Any | None:
        return self._target if self._active.is_set() else None

    def close(self) -> None:
        self._active.clear()


_ACTIVE_CALLBACK_TARGET: contextvars.ContextVar[
    _CallbackTargetRoute | None
] = contextvars.ContextVar("model_call_callback_target", default=None)


@dataclass
class _ModelCallOperation:
    """Identity shared by one outer deadline owner and its nested model calls."""

    deadline: float
    scope_id: str
    cancellation: _OperationCancellationCoordinator
    owner_task_id: int | None = None
    active: bool = True
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
        compare=False,
    )

    def set_owner(self, task: asyncio.Task[Any]) -> None:
        """Associate bypass rights with the exact deadline worker task."""
        with self._lock:
            self.owner_task_id = id(task)

    def close(self) -> None:
        """Stop all inherited contexts from bypassing a new guard."""
        with self._lock:
            self.active = False

    def is_active(self) -> bool:
        """Return whether the owner still accepts inherited deadlines."""
        with self._lock:
            return self.active

    def is_owned_by(self, task: asyncio.Task[Any] | None) -> bool:
        """Return whether task is the sole deadline worker for this operation."""
        if task is None:
            return False
        with self._lock:
            return self.active and self.owner_task_id == id(task)


class _OperationCancellationCoordinator:
    """Coordinate one optional unload across same-deadline watchdogs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._unload_claimed = False

    async def unload(
        self,
        *,
        metadata: ModelRuntimeMetadata,
        policy: ModelCallPolicy,
    ) -> None:
        """Run the logical operation's optional unload at most once."""
        with self._lock:
            if self._unload_claimed:
                return
            self._unload_claimed = True
        await _maybe_unload_ollama(metadata=metadata, policy=policy)


_ACTIVE_OPERATION: contextvars.ContextVar[_ModelCallOperation | None] = (
    contextvars.ContextVar("model_call_operation", default=None)
)


def _active_operation() -> _ModelCallOperation | None:
    """Return operation until its owner finishes bounded cleanup."""
    operation = _ACTIVE_OPERATION.get()
    return operation if operation is not None and operation.is_active() else None


def _owned_active_operation() -> _ModelCallOperation | None:
    """Return operation only during its callable absolute deadline."""
    operation = _active_operation()
    if operation is None or time.monotonic() >= operation.deadline:
        return None
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        return None
    return operation if operation.is_owned_by(current_task) else None


def _safe_provider(provider: str) -> ModelProvider:
    value = str(provider).strip().lower()
    return value if value in _KNOWN_PROVIDERS else "unknown"  # type: ignore[return-value]


def _normalize_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None

    value = str(base_url).strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError(
            "base_url must be an absolute HTTP(S) URL with a hostname"
        ) from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL with a hostname")
    try:
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ValueError(
            "base_url must be an absolute HTTP(S) URL with a hostname"
        ) from None
    if not hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL with a hostname")

    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _parse_timeout(value: str | None) -> float:
    try:
        timeout = (
            float(value) if value is not None else DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    return (
        timeout
        if math.isfinite(timeout) and timeout > 0
        else DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    )


@dataclass(frozen=True)
class ModelCallPolicy:
    """Immutable limits controlling model-call cancellation behavior."""

    timeout_seconds: float
    force_ollama_unload: bool

    def __post_init__(self) -> None:
        """Reject invalid direct construction values without normalization."""
        try:
            valid_timeout = (
                not isinstance(self.timeout_seconds, bool)
                and math.isfinite(self.timeout_seconds)
                and self.timeout_seconds > 0
            )
        except (TypeError, ValueError):
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout_seconds must be finite and positive")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ModelCallPolicy:
        """Build policy from environment values, falling back safely."""
        source = os.environ if environ is None else environ
        force_unload = (
            source.get("OLLAMA_FORCE_UNLOAD_ON_CANCEL", "").strip().lower() == "true"
        )
        return cls(
            timeout_seconds=_parse_timeout(source.get("MODEL_CALL_TIMEOUT_SECONDS")),
            force_ollama_unload=force_unload,
        )


@dataclass(frozen=True)
class ModelRuntimeMetadata:
    """Immutable, non-secret identity and endpoint metadata for a model."""

    provider: ModelProvider
    model_name: str
    base_url: str | None = None

    def __post_init__(self) -> None:
        """Normalize provider and endpoint after dataclass initialization."""
        object.__setattr__(self, "provider", _safe_provider(self.provider))
        object.__setattr__(self, "base_url", _normalize_base_url(self.base_url))


class ModelCallTimeoutError(RuntimeError):
    """Safe timeout error that contains no request or prompt content."""

    def __init__(
        self, provider: str, timeout_seconds: float, unload_requested: bool
    ) -> None:
        """Create an error containing only bounded-call metadata."""
        self.provider = _safe_provider(provider)
        self.timeout_seconds = _parse_timeout(str(timeout_seconds))
        self.unload_requested = bool(unload_requested)
        unload_note = "; Ollama unload requested" if self.unload_requested else ""
        super().__init__(
            f"Model call deadline exceeded for provider {self.provider!r} "
            f"after {self.timeout_seconds:g} seconds{unload_note}"
        )


class UnsupportedModelOverrideError(RuntimeError):
    """Safe error for a model override that cannot be applied."""

    def __init__(self, provider: str, model_name: str | None = None) -> None:
        """Create an error without retaining or rendering model name input."""
        del model_name
        self.provider = _safe_provider(provider)
        super().__init__(f"Unsupported model override for provider {self.provider!r}")


def _consume_task_exception(task: asyncio.Future[Any]) -> None:
    """Consume a detached task exception to prevent loop warning output."""
    if not task.cancelled():
        try:
            task.exception()
        except asyncio.CancelledError:
            pass


async def _bounded_task_cleanup(task: asyncio.Task[Any]) -> None:
    """Wait briefly for a cancelled task, then detach it safely."""
    if task.done():
        _consume_task_exception(task)
        return

    try:
        done, _ = await asyncio.wait({task}, timeout=MODEL_CANCEL_GRACE_SECONDS)
    except asyncio.CancelledError:
        task.add_done_callback(_consume_task_exception)
        raise

    if done:
        _consume_task_exception(task)
    else:
        task.add_done_callback(_consume_task_exception)


def _ollama_generate_url(base_url: str | None) -> str | None:
    """Return Ollama generate endpoint for a normalized model base URL."""
    if not base_url:
        return None
    base = base_url.rstrip("/")
    if base.endswith("/api/generate"):
        return base
    if base.endswith("/api"):
        return f"{base}/generate"
    return f"{base}/api/generate"


def _has_valid_ollama_model_name(metadata: ModelRuntimeMetadata) -> bool:
    """Return whether runtime metadata has a nonblank string Ollama model name."""
    return isinstance(metadata.model_name, str) and bool(metadata.model_name.strip())


def _log_ollama_unload(status: str) -> None:
    """Log fixed, non-secret metadata for best-effort Ollama unloads."""
    _LOGGER.warning("Ollama unload provider=ollama action=unload status=%s", status)


async def _unload_ollama_with_client(
    endpoint: str,
    payload: dict[str, object],
    client_factory: Callable[[], Any],
) -> None:
    """Post an unload request and close its client as one bounded operation."""
    async with client_factory() as client:
        await client.post(endpoint, json=payload)


async def _maybe_unload_ollama(
    *,
    metadata: ModelRuntimeMetadata,
    policy: ModelCallPolicy,
    post: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Best-effort unload an eligible Ollama model without request content."""
    if not policy.force_ollama_unload or metadata.provider != "ollama":
        return

    endpoint = _ollama_generate_url(metadata.base_url)
    if endpoint is None or not _has_valid_ollama_model_name(metadata):
        _log_ollama_unload("skipped")
        return
    model_name = metadata.model_name.strip()

    payload = {"model": model_name, "keep_alive": 0}
    try:
        if post is not None:
            await _bounded_shielded_unload(post(endpoint, json=payload))
            return

        import httpx

        await _bounded_shielded_unload(
            _unload_ollama_with_client(endpoint, payload, httpx.AsyncClient)
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log_ollama_unload("failed")


async def _bounded_shielded_unload(operation: Awaitable[Any]) -> None:
    """Run unload with an independent deadline and consume a late failure."""
    task = asyncio.ensure_future(operation)
    current_task = asyncio.current_task()
    parent_cancellation_count = (
        current_task.cancelling() if current_task is not None else 0
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(task), timeout=OLLAMA_UNLOAD_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        if not task.done():
            task.add_done_callback(_consume_task_exception)
        if (
            current_task is not None
            and current_task.cancelling() > parent_cancellation_count
        ):
            raise
        _log_ollama_unload("cancelled")
    except Exception:
        if task.done():
            _consume_task_exception(task)
        else:
            task.add_done_callback(_consume_task_exception)
        _log_ollama_unload("failed")


async def _run_optional_unload(
    *,
    metadata: ModelRuntimeMetadata,
    policy: ModelCallPolicy,
    unload: Callable[[], Awaitable[Any]] | None,
) -> None:
    """Run injected or HTTP unload only for opt-in Ollama cancellation."""
    if (
        not policy.force_ollama_unload
        or metadata.provider != "ollama"
        or not _has_valid_ollama_model_name(metadata)
    ):
        return
    current_task = asyncio.current_task()
    parent_cancellation_count = (
        current_task.cancelling() if current_task is not None else 0
    )
    try:
        if unload is None:
            await _maybe_unload_ollama(metadata=metadata, policy=policy)
            return
        try:
            operation = unload()
        except asyncio.CancelledError:
            if (
                current_task is not None
                and current_task.cancelling() > parent_cancellation_count
            ):
                raise
            _log_ollama_unload("cancelled")
            return
        await _bounded_shielded_unload(operation)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log_ollama_unload("failed")


async def _run_with_deadline[Result](
    factory: Callable[[], Awaitable[Result]],
    *,
    policy: ModelCallPolicy,
    metadata: ModelRuntimeMetadata,
    unload: Callable[[], Awaitable[Any]] | None,
    on_cancel: Callable[[], None] | None = None,
    on_task_created: Callable[[asyncio.Task[Any]], None] | None = None,
) -> Result:
    """Run a model request under a total deadline and bounded cancellation cleanup."""
    task = asyncio.create_task(factory())
    if on_task_created is not None:
        on_task_created(task)
    unload_attempted = False

    async def attempt_unload() -> None:
        """Run at most one best-effort unload for this model call."""
        nonlocal unload_attempted
        if unload_attempted:
            return
        unload_attempted = True
        await _run_optional_unload(metadata=metadata, policy=policy, unload=unload)

    try:
        done, _ = await asyncio.wait({task}, timeout=policy.timeout_seconds)
        if done:
            return task.result()

        if on_cancel is not None:
            on_cancel()
        task.cancel()
        await _bounded_task_cleanup(task)
        await attempt_unload()
        raise ModelCallTimeoutError(
            provider=metadata.provider,
            timeout_seconds=policy.timeout_seconds,
            unload_requested=policy.force_ollama_unload
            and metadata.provider == "ollama",
        )
    except asyncio.CancelledError as original_cancel:
        if on_cancel is not None:
            on_cancel()
        if not task.done():
            task.cancel()
        try:
            await _bounded_task_cleanup(task)
            await attempt_unload()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning("Model cancellation cleanup failed")
        raise original_cancel


@runtime_checkable
class ModelRuntimeDescriptor(Protocol):
    """Protocol for custom chat models that expose safe runtime identity."""

    @property
    def model_runtime_metadata(self) -> ModelRuntimeMetadata:
        """Return non-secret provider metadata used by cancellation policy."""


@runtime_checkable
class ModelHTTPTransportCloner(Protocol):
    """Optional capability for independently cloning custom HTTP transports."""

    def clone_model_http_transport(
        self,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport,
        *,
        asynchronous: bool,
    ) -> httpx.BaseTransport | httpx.AsyncBaseTransport:
        """Return an independent transport preserving custom routing semantics."""


class BridgeRegistry:
    """Thread-safe collection of synchronous bridges grouped by run scope."""

    def __init__(self) -> None:
        """Create an empty registry guarded by a standard thread lock."""
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._controls: dict[str, set[_BridgeControl[Any]]] = {}
        self._cancelling: dict[str, int] = {}

    def _ensure_process(self) -> None:
        """Discard inherited locks and controls after a process fork."""
        current_pid = os.getpid()
        if current_pid == self._pid:
            return
        self._pid = current_pid
        self._lock = threading.Lock()
        self._controls = {}
        self._cancelling = {}

    def register(self, control: _BridgeControl[Any]) -> bool:
        """Register unless cancellation is currently active for the scope."""
        self._ensure_process()
        with self._lock:
            if control.scope_id in self._cancelling:
                return False
            self._controls.setdefault(control.scope_id, set()).add(control)
            return True

    def unregister(self, control: _BridgeControl[Any]) -> None:
        """Forget a completed bridge and prune its empty scope."""
        self._ensure_process()
        with self._lock:
            controls = self._controls.get(control.scope_id)
            if controls is None:
                return
            controls.discard(control)
            if not controls:
                self._controls.pop(control.scope_id, None)

    def active_count(self, scope_id: str) -> int:
        """Return active controls for tests and lifecycle diagnostics."""
        self._ensure_process()
        with self._lock:
            return len(self._controls.get(scope_id, ()))

    def cancel_scope(self, scope_id: str) -> None:
        """Cancel every bridge in a scope against one shared join grace."""
        self._ensure_process()
        with self._lock:
            self._cancelling[scope_id] = self._cancelling.get(scope_id, 0) + 1
            controls = tuple(self._controls.get(scope_id, ()))
        try:
            for control in controls:
                control.cancel()
            join_deadline = time.monotonic() + MODEL_CANCEL_GRACE_SECONDS
            for control in controls:
                control.join(max(0.0, join_deadline - time.monotonic()))
        finally:
            with self._lock:
                remaining = self._cancelling[scope_id] - 1
                if remaining:
                    self._cancelling[scope_id] = remaining
                else:
                    self._cancelling.pop(scope_id, None)


_GLOBAL_BRIDGE_REGISTRY = BridgeRegistry()


class _BridgeRuntime:
    """Process-wide daemon event loop for all synchronous model bridges."""

    def __init__(self) -> None:
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = self._new_thread()

    def _new_thread(self) -> threading.Thread:
        return threading.Thread(
            target=self._thread_main,
            name="model-call-bridge-runtime",
            daemon=True,
        )

    def _ensure_process(self) -> None:
        """Install fresh synchronization state in a forked child."""
        current_pid = os.getpid()
        if current_pid == self._pid:
            return
        self._pid = current_pid
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop = None
        self._thread = self._new_thread()

    def owns_current_loop(self) -> bool:
        """Return whether provider code is already on the transport loop."""
        self._ensure_process()
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def submit(self, control: _BridgeControl[Any]) -> None:
        """Schedule one per-call control on the persistent bridge loop."""
        self._ensure_process()
        with self._lock:
            if not self._thread.is_alive():
                self._thread.start()
        self._ready.wait()
        with self._lock:
            loop = self._loop
        if loop is None:
            raise RuntimeError("model call bridge runtime failed to start")
        loop.call_soon_threadsafe(control._schedule, loop)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        self._ready.set()
        loop.run_forever()


_GLOBAL_BRIDGE_RUNTIME = _BridgeRuntime()
_MODEL_PROCESS_RESET_LOCK = threading.Lock()
_GUARDED_PROVIDER_CLASSES_LOCK = threading.Lock()


def _reset_global_bridge_state_after_fork() -> None:
    """Reset inherited thread state without touching parent-owned locks."""
    global _GUARDED_PROVIDER_CLASSES_LOCK, _MODEL_PROCESS_RESET_LOCK
    _MODEL_PROCESS_RESET_LOCK = threading.Lock()
    _GUARDED_PROVIDER_CLASSES_LOCK = threading.Lock()
    _GLOBAL_BRIDGE_REGISTRY._ensure_process()
    _GLOBAL_BRIDGE_RUNTIME._ensure_process()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_global_bridge_state_after_fork)


class _BridgeControl[ResultT]:
    """One cancellable task scheduled on the process-wide bridge runtime."""

    def __init__(
        self,
        factory: Callable[[], Awaitable[ResultT]],
        *,
        scope_id: str,
        registry: BridgeRegistry,
    ) -> None:
        self.scope_id = scope_id
        self.results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.future: ConcurrentFuture[ResultT] = ConcurrentFuture()
        self._factory = factory
        self._registry = registry
        self._lock = threading.Lock()
        self._cancel_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._globally_registered = False
        self._locally_registered = False
        self._completed = threading.Event()
        self._context = contextvars.copy_context()

    def start(self) -> None:
        """Register before making the bridge observable as a live thread."""
        globally_registered = _GLOBAL_BRIDGE_REGISTRY.register(self)
        locally_registered = False
        if globally_registered and self._registry is not _GLOBAL_BRIDGE_REGISTRY:
            locally_registered = self._registry.register(self)
        accepted = globally_registered and (
            self._registry is _GLOBAL_BRIDGE_REGISTRY or locally_registered
        )
        self._globally_registered = globally_registered
        self._locally_registered = locally_registered
        if not accepted:
            if globally_registered:
                _GLOBAL_BRIDGE_REGISTRY.unregister(self)
            if locally_registered:
                self._registry.unregister(self)
            self.cancel()
        try:
            _GLOBAL_BRIDGE_RUNTIME.submit(self)
        except BaseException:
            if self._locally_registered:
                self._registry.unregister(self)
            if self._globally_registered:
                _GLOBAL_BRIDGE_REGISTRY.unregister(self)
            raise
        if not accepted:
            self.join(MODEL_CANCEL_GRACE_SECONDS)

    def cancel(self) -> None:
        """Request task cancellation safely before or after loop creation."""
        with self._lock:
            self._cancel_requested = True
            loop = self._loop
            task = self._task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def join(self, timeout: float) -> None:
        """Join without ever extending the caller's grace period."""
        self._completed.wait(max(0.0, timeout))

    def _schedule(self, loop: asyncio.AbstractEventLoop) -> None:
        """Create one task with caller context from inside the bridge loop."""
        coroutine = self._context.run(self._factory)
        task = self._context.run(loop.create_task, coroutine)
        task.add_done_callback(self._task_done)
        with self._lock:
            self._loop = loop
            self._task = task
            cancel_requested = self._cancel_requested
        if cancel_requested:
            task.cancel()

    def _task_done(self, task: asyncio.Task[ResultT]) -> None:
        """Publish one terminal result and unregister completed control state."""
        try:
            if task.cancelled():
                outcome: tuple[str, Any] = ("error", asyncio.CancelledError())
            else:
                error = task.exception()
                outcome = ("error", error) if error is not None else ("result", task.result())
        finally:
            with self._lock:
                self._task = None
            if self._locally_registered:
                self._registry.unregister(self)
            if self._globally_registered:
                _GLOBAL_BRIDGE_REGISTRY.unregister(self)
            self.results.put(outcome)
            if not self.future.done():
                if outcome[0] == "error":
                    self.future.set_exception(outcome[1])
                else:
                    self.future.set_result(outcome[1])
            self._completed.set()


def cancel_model_call_scope(scope_id: str) -> None:
    """Cancel all active synchronous model bridges in a run scope."""
    _GLOBAL_BRIDGE_REGISTRY.cancel_scope(str(scope_id))


def _scope_id_from_config(
    config: RunnableConfig | None,
    bound_config: RunnableConfig | None = None,
) -> str:
    resolved = ensure_config(config)
    if bound_config is not None:
        resolved = merge_configs(bound_config, resolved)
    configurable = resolved.get("configurable", {})
    requested = (
        configurable.get("model_call_scope_id")
        if isinstance(configurable, Mapping)
        else None
    )
    if requested is not None and str(requested).strip():
        return str(requested)
    operation = _active_operation()
    return operation.scope_id if operation is not None else f"private-{uuid.uuid4().hex}"


def _capture_deadline(policy: ModelCallPolicy) -> float:
    operation = _active_operation()
    if operation is not None:
        return operation.deadline
    return time.monotonic() + policy.timeout_seconds


def _timeout_error(
    policy: ModelCallPolicy, metadata: ModelRuntimeMetadata
) -> ModelCallTimeoutError:
    return ModelCallTimeoutError(
        provider=metadata.provider,
        timeout_seconds=policy.timeout_seconds,
        unload_requested=policy.force_ollama_unload and metadata.provider == "ollama",
    )


async def _run_with_absolute_deadline[ResultT](
    factory: Callable[[], Awaitable[ResultT]],
    *,
    deadline: float,
    scope_id: str,
    policy: ModelCallPolicy,
    metadata: ModelRuntimeMetadata,
) -> ResultT:
    """Run only the outermost logical model operation as deadline owner."""
    if _owned_active_operation() is not None:
        return await factory()

    inherited = _ACTIVE_OPERATION.get()
    cancellation = (
        inherited.cancellation
        if inherited is not None and inherited.deadline == deadline
        else _OperationCancellationCoordinator()
    )

    async def unload_once() -> None:
        await cancellation.unload(metadata=metadata, policy=policy)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        await _run_optional_unload(
            metadata=metadata,
            policy=policy,
            unload=unload_once,
        )
        raise _timeout_error(policy, metadata)
    deadline_policy = ModelCallPolicy(
        timeout_seconds=remaining,
        force_ollama_unload=policy.force_ollama_unload,
    )
    operation = _ModelCallOperation(
        deadline=deadline,
        scope_id=scope_id,
        cancellation=cancellation,
    )
    operation_token = _ACTIVE_OPERATION.set(operation)
    deadline_token = _ACTIVE_DEADLINE.set(deadline)
    scope_token = _ACTIVE_SCOPE_ID.set(scope_id)

    def close_operation() -> None:
        operation.close()

    try:
        return await _run_with_deadline(
            factory,
            policy=deadline_policy,
            metadata=metadata,
            unload=unload_once,
            on_cancel=close_operation,
            on_task_created=operation.set_owner,
        )
    except ModelCallTimeoutError:
        raise _timeout_error(policy, metadata) from None
    finally:
        operation.close()
        _ACTIVE_SCOPE_ID.reset(scope_token)
        _ACTIVE_DEADLINE.reset(deadline_token)
        _ACTIVE_OPERATION.reset(operation_token)


async def _bounded_async_iterator_close(iterator: AsyncIterator[Any]) -> None:
    """Close a provider iterator without replacing or extending call failure."""
    task = asyncio.create_task(iterator.aclose())
    try:
        done, _ = await asyncio.wait({task}, timeout=MODEL_CANCEL_GRACE_SECONDS)
    except asyncio.CancelledError:
        task.add_done_callback(_consume_task_exception)
        raise
    if done:
        _consume_task_exception(task)
        return
    task.cancel()
    task.add_done_callback(_consume_task_exception)


async def _run_with_callback_target[ResultT](
    factory: Callable[[], Awaitable[ResultT]],
    target: Any,
) -> ResultT:
    """Expose caller callback routing to nested guarded model boundaries."""
    route = _CallbackTargetRoute(target)
    token = _ACTIVE_CALLBACK_TARGET.set(route)
    try:
        return await factory()
    finally:
        route.close()
        _ACTIVE_CALLBACK_TARGET.reset(token)


async def _stream_with_callback_target[OutputT](
    factory: Callable[[], AsyncIterator[OutputT]],
    target: Any,
) -> AsyncIterator[OutputT]:
    """Keep callback routing active through every nested stream pull."""
    iterator = factory()
    try:
        while True:
            if isinstance(target, _CallbackTargetRoute):
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    return
                yield item
                continue
            route = _CallbackTargetRoute(target)
            token = _ACTIVE_CALLBACK_TARGET.set(route)
            try:
                item = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                route.close()
                _ACTIVE_CALLBACK_TARGET.reset(token)
            yield item
    finally:
        await _bounded_async_iterator_close(iterator)


def _start_bridge[ResultT](
    factory: Callable[[], Awaitable[ResultT]],
    *,
    scope_id: str,
    registry: BridgeRegistry,
) -> _BridgeControl[ResultT]:
    control = _BridgeControl(factory, scope_id=scope_id, registry=registry)
    control.start()
    return control


def _bridge_result[ResultT](
    control: _BridgeControl[ResultT],
    deadline: float,
    *,
    policy: ModelCallPolicy,
    metadata: ModelRuntimeMetadata,
    callback_pump: _SyncCallerCallbackPump | None = None,
) -> ResultT:
    """Wait for one bridge result while retaining bounded interrupt cleanup."""
    try:
        wait = max(0.0, deadline - time.monotonic()) + MODEL_CANCEL_GRACE_SECONDS
        if policy.force_ollama_unload and metadata.provider == "ollama":
            wait += OLLAMA_UNLOAD_TIMEOUT_SECONDS
        kind, value = _sync_wait_queue(
            control.results,
            wait,
            callback_pump=callback_pump,
        )
        if callback_pump is not None:
            callback_pump.drain()
    except KeyboardInterrupt:
        control.cancel()
        control.join(MODEL_CANCEL_GRACE_SECONDS)
        raise
    except queue.Empty:
        control.cancel()
        control.join(MODEL_CANCEL_GRACE_SECONDS)
        raise _timeout_error(policy, metadata) from None
    control.join(0)
    if kind == "error":
        raise value
    return value


def _bridge_wait_seconds(
    deadline: float,
    *,
    policy: ModelCallPolicy,
    metadata: ModelRuntimeMetadata,
) -> float:
    """Bound cross-thread result delivery without extending provider policy."""
    wait = max(0.0, deadline - time.monotonic()) + MODEL_CANCEL_GRACE_SECONDS
    if policy.force_ollama_unload and metadata.provider == "ollama":
        wait += OLLAMA_UNLOAD_TIMEOUT_SECONDS
    return wait


async def _await_bridge_cleanup(
    control: _BridgeControl[Any],
    wrapped: asyncio.Future[Any] | None = None,
) -> None:
    """Yield caller loop while remote cancellation receives bounded cleanup."""
    if control.future.done():
        control.join(0)
        return
    pending = wrapped if wrapped is not None else asyncio.wrap_future(control.future)
    pending.add_done_callback(_consume_task_exception)
    try:
        async with asyncio.timeout(MODEL_CANCEL_GRACE_SECONDS):
            await asyncio.shield(pending)
    except BaseException:
        pass
    control.join(0)


async def _await_bridge_result[ResultT](
    control: _BridgeControl[ResultT],
    deadline: float,
    *,
    policy: ModelCallPolicy,
    metadata: ModelRuntimeMetadata,
) -> ResultT:
    """Await a bridge from any caller loop and propagate cancellation inward."""
    wrapped = asyncio.wrap_future(control.future)
    try:
        async with asyncio.timeout(
            _bridge_wait_seconds(deadline, policy=policy, metadata=metadata)
        ):
            return await asyncio.shield(wrapped)
    except TimeoutError:
        control.cancel()
        await _await_bridge_cleanup(control, wrapped)
        raise _timeout_error(policy, metadata) from None
    except asyncio.CancelledError as original_cancel:
        current_task = asyncio.current_task()
        if current_task is None or not current_task.cancelling():
            raise
        control.cancel()
        await _await_bridge_cleanup(control, wrapped)
        raise original_cancel


class _AsyncBridgeStreamIterator[OutputT](AsyncIterator[OutputT]):
    """Backpressured async iterator whose provider stays on transport loop."""

    def __init__(
        self,
        factory: Callable[
            [asyncio.AbstractEventLoop], AsyncIterator[OutputT]
        ],
        *,
        deadline: float,
        scope_id: str,
        registry: BridgeRegistry,
        policy: ModelCallPolicy,
        metadata: ModelRuntimeMetadata,
    ) -> None:
        self._factory = factory
        self._deadline = deadline
        self._scope_id = scope_id
        self._registry = registry
        self._policy = policy
        self._metadata = metadata
        self._iterator: AsyncIterator[OutputT] | None = None
        self._current: _BridgeControl[OutputT] | None = None
        self._lock = threading.Lock()
        self._closed = False

    def __aiter__(self) -> _AsyncBridgeStreamIterator[OutputT]:
        return self

    async def __anext__(self) -> OutputT:
        if self._closed:
            raise StopAsyncIteration
        caller_loop = asyncio.get_running_loop()

        async def advance() -> OutputT:
            if self._iterator is None:
                self._iterator = self._factory(caller_loop)
            return await anext(self._iterator)

        control = _start_bridge(
            advance,
            scope_id=self._scope_id,
            registry=self._registry,
        )
        with self._lock:
            self._current = control
        try:
            return await _await_bridge_result(
                control,
                self._deadline,
                policy=self._policy,
                metadata=self._metadata,
            )
        except StopAsyncIteration:
            self._closed = True
            raise
        except BaseException:
            self._closed = True
            raise
        finally:
            with self._lock:
                if self._current is control:
                    self._current = None

    async def aclose(self) -> None:
        """Cancel an in-flight pull and close provider iterator on its loop."""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            current = self._current
        if current is not None:
            current.cancel()
            await _await_bridge_cleanup(current)
        iterator = self._iterator
        if iterator is None:
            return
        control = _start_bridge(
            lambda: _bounded_async_iterator_close(iterator),
            scope_id=self._scope_id,
            registry=self._registry,
        )
        try:
            await _await_bridge_result(
                control,
                time.monotonic() + MODEL_CANCEL_GRACE_SECONDS,
                policy=self._policy,
                metadata=self._metadata,
            )
        except BaseException:
            control.cancel()
            control.join(MODEL_CANCEL_GRACE_SECONDS)

    def __del__(self) -> None:
        with self._lock:
            current = self._current
        if current is not None:
            current.cancel()


class _SyncStreamIterator[OutputT](Iterator[OutputT]):
    """Synchronous iterator backed by one cancellable async bridge."""

    def __init__(
        self,
        factory: Callable[[], AsyncIterator[OutputT]],
        *,
        deadline: float,
        scope_id: str,
        registry: BridgeRegistry,
        policy: ModelCallPolicy,
        metadata: ModelRuntimeMetadata,
        callback_pump: _SyncCallerCallbackPump | None = None,
    ) -> None:
        self._deadline = deadline
        self._closed = False
        self._policy = policy
        self._metadata = metadata
        self._callback_pump = callback_pump

        async def pump() -> None:
            try:
                async for item in factory():
                    self._control.results.put(("item", item))
                self._control.results.put(("done", None))
            except BaseException as exc:
                self._control.results.put(("error", exc))

        self._control = _start_bridge(pump, scope_id=scope_id, registry=registry)

    def __iter__(self) -> _SyncStreamIterator[OutputT]:
        return self

    def __next__(self) -> OutputT:
        if self._closed:
            raise StopIteration
        try:
            wait = (
                max(0.0, self._deadline - time.monotonic()) + MODEL_CANCEL_GRACE_SECONDS
            )
            if (
                self._policy.force_ollama_unload
                and self._metadata.provider == "ollama"
            ):
                wait += OLLAMA_UNLOAD_TIMEOUT_SECONDS
            kind, value = _sync_wait_queue(
                self._control.results,
                wait,
                callback_pump=self._callback_pump,
            )
            if self._callback_pump is not None:
                self._callback_pump.drain()
        except KeyboardInterrupt:
            self.close()
            raise
        except queue.Empty:
            self.close()
            raise _timeout_error(self._policy, self._metadata) from None
        if kind == "item":
            return value
        self._closed = True
        self._control.join(MODEL_CANCEL_GRACE_SECONDS)
        if kind == "error":
            raise value
        raise StopIteration

    def close(self) -> None:
        """Cancel provider stream and wait only for configured cleanup grace."""
        if self._closed:
            return
        self._closed = True
        self._control.cancel()
        self._control.join(MODEL_CANCEL_GRACE_SECONDS)
        if self._callback_pump is not None:
            self._callback_pump.drain()

    def __del__(self) -> None:
        self.close()


class _AttemptVisibility:
    """Thread-safe record of output exposed by one logical model call."""

    def __init__(self) -> None:
        self._visible = threading.Event()

    def mark_visible(self) -> None:
        self._visible.set()

    def can_retry(self) -> bool:
        return not self._visible.is_set()


@dataclass
class _SyncCallbackRecord:
    """One callback invocation waiting for its original sync caller thread."""

    callback: Callable[[], Any]
    context: contextvars.Context
    future: ConcurrentFuture[Any]


class _SyncCallerCallbackPump:
    """Move bridge callbacks onto the thread blocked on a synchronous API."""

    def __init__(self) -> None:
        self._records: queue.Queue[_SyncCallbackRecord] = queue.Queue()

    async def dispatch(
        self,
        event: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Queue callback and suspend bridge work until caller executes it."""
        future: ConcurrentFuture[Any] = ConcurrentFuture()
        self._records.put(
            _SyncCallbackRecord(
                callback=functools.partial(event, *args, **kwargs),
                context=contextvars.copy_context(),
                future=future,
            )
        )
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    def run_one(self, timeout: float = 0.0) -> bool:
        """Execute at most one queued callback on the current caller thread."""
        try:
            record = self._records.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return False
        if record.future.cancelled():
            return True

        def invoke() -> Any:
            result = record.callback()
            return asyncio.run(result) if inspect.isawaitable(result) else result

        try:
            result = record.context.run(invoke)
        except BaseException as exc:
            if not record.future.done():
                record.future.set_exception(exc)
        else:
            if not record.future.done():
                record.future.set_result(result)
        return True

    def drain(self) -> None:
        """Execute all callbacks already visible to the caller."""
        while self.run_one():
            pass


def _sync_wait_queue(
    results: queue.Queue[tuple[str, Any]],
    timeout: float,
    *,
    callback_pump: _SyncCallerCallbackPump | None,
) -> tuple[str, Any]:
    """Wait for bridge output while servicing sync-thread callback records."""
    if callback_pump is None:
        return results.get(timeout=timeout)
    wait_deadline = time.monotonic() + timeout
    while True:
        callback_pump.drain()
        remaining = wait_deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        try:
            return results.get(timeout=min(remaining, 0.002))
        except queue.Empty:
            if time.monotonic() >= wait_deadline:
                raise


class _CallerLoopCallbackProxy(BaseCallbackHandler):
    """Dispatch a callback on its async loop or synchronous caller thread."""

    def __init__(
        self,
        handler: BaseCallbackHandler,
        target: asyncio.AbstractEventLoop | _SyncCallerCallbackPump | object,
    ) -> None:
        self._handler = handler
        self._target = target
        self._loop = target if isinstance(target, asyncio.AbstractEventLoop) else None

    @property
    def __class__(self) -> type[BaseCallbackHandler]:
        """Retain handler identity for LangChain tracer de-duplication."""
        return self._handler.__class__

    @property
    def raise_error(self) -> bool:
        return self._handler.raise_error

    @property
    def run_inline(self) -> bool:
        return self._handler.run_inline

    @property
    def ignore_llm(self) -> bool:
        return self._handler.ignore_llm

    @property
    def ignore_retry(self) -> bool:
        return self._handler.ignore_retry

    @property
    def ignore_chain(self) -> bool:
        return self._handler.ignore_chain

    @property
    def ignore_agent(self) -> bool:
        return self._handler.ignore_agent

    @property
    def ignore_retriever(self) -> bool:
        return self._handler.ignore_retriever

    @property
    def ignore_chat_model(self) -> bool:
        return self._handler.ignore_chat_model

    @property
    def ignore_custom_event(self) -> bool:
        return self._handler.ignore_custom_event

    def copy_with_metadata_defaults(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> BaseCallbackHandler:
        """Keep caller routing when LangChain clones a V2 tracing handler."""
        copied = self._handler.copy_with_metadata_defaults(
            metadata=metadata,
            tags=tags,
        )
        return _CallerLoopCallbackProxy(copied, self._target)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("on_"):
            handler = object.__getattribute__(self, "_handler")
            target = object.__getattribute__(self, "_target")
            event = getattr(handler, name)

            async def dispatch(*args: Any, **kwargs: Any) -> Any:
                resolved_target = (
                    _ACTIVE_CALLBACK_TARGET.get()
                    if target is _DYNAMIC_CALLBACK_TARGET
                    else target
                )
                while isinstance(resolved_target, _CallbackTargetRoute):
                    resolved_target = resolved_target.resolve()
                if isinstance(resolved_target, _SyncCallerCallbackPump):
                    return await resolved_target.dispatch(event, *args, **kwargs)
                if resolved_target is None:
                    result = event(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result

                async def invoke_on_caller_loop() -> Any:
                    if inspect.iscoroutinefunction(event):
                        return await event(*args, **kwargs)
                    if handler.run_inline:
                        return event(*args, **kwargs)
                    callback = functools.partial(event, *args, **kwargs)
                    context = contextvars.copy_context()
                    return await resolved_target.run_in_executor(
                        None,
                        context.run,
                        callback,
                    )

                callback_coroutine = invoke_on_caller_loop()
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        callback_coroutine,
                        resolved_target,
                    )
                except BaseException:
                    callback_coroutine.close()
                    raise
                try:
                    return await asyncio.wrap_future(future)
                except asyncio.CancelledError:
                    future.cancel()
                    raise

            return dispatch
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            handler = object.__getattribute__(self, "_handler")
            return getattr(handler, name)


def _callback_proxy(
    handler: BaseCallbackHandler,
    target: asyncio.AbstractEventLoop | _SyncCallerCallbackPump | object,
) -> BaseCallbackHandler:
    """Create one caller-specific proxy, unwrapping an existing bridge proxy."""
    if isinstance(handler, _CallerLoopCallbackProxy):
        if handler._target is target:
            return handler
        handler = handler._handler
    return _CallerLoopCallbackProxy(handler, target)


def _callback_manager_on_caller_loop(
    callbacks: Any,
    loop: asyncio.AbstractEventLoop,
) -> AsyncCallbackManager | None:
    """Resolve callbacks on caller loop and proxy every resulting handler."""
    manager = AsyncCallbackManager.configure(callbacks)
    if not manager.handlers:
        return None
    return _proxy_callback_manager(manager, loop)


def _proxy_callback_manager(
    manager: Any,
    target: asyncio.AbstractEventLoop | _SyncCallerCallbackPump | object,
    *,
    proxies: dict[int, BaseCallbackHandler] | None = None,
    excluded: set[int] | None = None,
) -> AsyncCallbackManager:
    """Copy callback manager context while proxying each unique handler."""
    proxy_cache = proxies if proxies is not None else {}
    excluded_ids = excluded or set()
    included_ids: set[int] = set()

    def proxy(handler: BaseCallbackHandler) -> BaseCallbackHandler:
        original = (
            handler._handler
            if isinstance(handler, _CallerLoopCallbackProxy)
            else handler
        )
        identity = id(original)
        existing = proxy_cache.get(identity)
        if existing is not None:
            return existing
        created = _callback_proxy(original, target)
        proxy_cache[identity] = created
        return created

    handlers: list[BaseCallbackHandler] = []
    for handler in manager.handlers:
        original = (
            handler._handler
            if isinstance(handler, _CallerLoopCallbackProxy)
            else handler
        )
        identity = id(original)
        if identity in excluded_ids or identity in included_ids:
            continue
        included_ids.add(identity)
        handlers.append(proxy(original))

    inheritable: list[BaseCallbackHandler] = []
    inherited_ids: set[int] = set()
    for handler in manager.inheritable_handlers:
        original = (
            handler._handler
            if isinstance(handler, _CallerLoopCallbackProxy)
            else handler
        )
        identity = id(original)
        if identity in excluded_ids or identity in inherited_ids:
            continue
        inherited_ids.add(identity)
        inheritable.append(proxy(original))

    return AsyncCallbackManager(
        handlers=handlers,
        inheritable_handlers=inheritable,
        parent_run_id=manager.parent_run_id,
        tags=manager.tags.copy(),
        inheritable_tags=manager.inheritable_tags.copy(),
        metadata=manager.metadata.copy(),
        inheritable_metadata=manager.inheritable_metadata.copy(),
    )


def _config_on_caller_callback_loop(
    config: RunnableConfig | None,
    loop: asyncio.AbstractEventLoop,
) -> RunnableConfig:
    """Copy config while making explicit and contextual callbacks loop-safe."""
    resolved = ensure_config(config)
    manager = _callback_manager_on_caller_loop(resolved.get("callbacks"), loop)
    if manager is not None:
        resolved["callbacks"] = manager
    return resolved


def _provider_on_caller_callback_loop(
    model: BaseChatModel,
    loop: asyncio.AbstractEventLoop,
) -> BaseChatModel:
    """Make a shallow provider view whose model callbacks use caller loop."""
    callbacks = getattr(model, "callbacks", None)
    if not callbacks:
        return model
    proxied_callbacks = (
        [_callback_proxy(handler, loop) for handler in callbacks]
        if isinstance(callbacks, list)
        else _proxy_callback_manager(callbacks, loop)
    )
    return super(ModelCallGuardMixin, model).model_copy(
        update={"callbacks": proxied_callbacks},
        deep=False,
    )


def _provider_callbacks_on_caller(
    model: BaseChatModel,
    config: RunnableConfig | None,
    target: asyncio.AbstractEventLoop | _SyncCallerCallbackPump,
) -> tuple[BaseChatModel, RunnableConfig]:
    """Proxy and identity-dedupe config/model callbacks as one ordered set."""
    del target
    resolved = ensure_config(config)
    proxy_cache = _ensure_model_callback_proxies(model)
    config_manager = AsyncCallbackManager.configure(resolved.get("callbacks"))
    proxied_config = _proxy_callback_manager(
        config_manager,
        _DYNAMIC_CALLBACK_TARGET,
        proxies=proxy_cache,
    )
    if proxied_config.handlers or proxied_config.inheritable_handlers:
        resolved["callbacks"] = proxied_config
    return model, resolved


@contextmanager
def _without_duplicate_ambient_callback_hooks(
    callbacks: Any,
) -> Iterator[None]:
    """Hide ambient hooks already represented by bridge callback proxies."""
    from langchain_core.tracers.context import _configure_hooks  # noqa: PLC0415

    handlers = (
        [*callbacks.handlers, *callbacks.inheritable_handlers]
        if hasattr(callbacks, "handlers")
        and hasattr(callbacks, "inheritable_handlers")
        else list(callbacks or [])
    )
    represented: dict[int, BaseCallbackHandler] = {}
    for handler in handlers:
        original = (
            handler._handler
            if isinstance(handler, _CallerLoopCallbackProxy)
            else handler
        )
        represented.setdefault(id(original), handler)
    tokens: list[tuple[contextvars.ContextVar[Any], contextvars.Token[Any]]] = []
    try:
        for variable, _inheritable, handler_class, _env_var in _configure_hooks:
            ambient = variable.get()
            replacement = represented.get(id(ambient)) if ambient is not None else None
            if (
                handler_class is None
                and replacement is not None
                and replacement is not ambient
            ):
                tokens.append((variable, variable.set(replacement)))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def _ensure_model_callback_proxies(
    model: BaseChatModel,
) -> dict[int, BaseCallbackHandler]:
    """Install loop-neutral callback proxies once on the real provider instance."""
    callbacks = getattr(model, "callbacks", None)
    if not callbacks:
        return {}
    manager = (
        AsyncCallbackManager(handlers=list(callbacks))
        if isinstance(callbacks, list)
        else callbacks
    )
    proxy_cache = {
        id(handler._handler): handler
        for handler in [*manager.handlers, *manager.inheritable_handlers]
        if isinstance(handler, _CallerLoopCallbackProxy)
        and handler._target is _DYNAMIC_CALLBACK_TARGET
    }
    proxied = _proxy_callback_manager(
        manager,
        _DYNAMIC_CALLBACK_TARGET,
        proxies=proxy_cache,
    )
    model.__dict__["callbacks"] = (
        proxied.handlers if isinstance(callbacks, list) else proxied
    )
    return proxy_cache


class _VisibilityCallbackHandler(BaseCallbackHandler):
    """Mark provider tokens before retry classification can observe failure."""

    run_inline = True

    def __init__(self, visibility: _AttemptVisibility) -> None:
        self._visibility = visibility

    def on_llm_new_token(self, _token: str, **_kwargs: Any) -> None:
        self._visibility.mark_visible()

    def on_stream_event(self, _event: Any, **_kwargs: Any) -> None:
        self._visibility.mark_visible()


def _config_with_visibility_callback(
    config: RunnableConfig | None,
    visibility: _AttemptVisibility,
    local_callbacks: Any = None,
) -> RunnableConfig:
    """Prepend tracking when callbacks can expose provider output."""
    resolved = ensure_config(config)
    configured_callbacks = resolved.get("callbacks")
    if configured_callbacks is None and not local_callbacks:
        return resolved
    visibility_handler = _VisibilityCallbackHandler(visibility)
    if configured_callbacks is None:
        resolved["callbacks"] = [visibility_handler]
    else:
        callback_manager = AsyncCallbackManager.configure(configured_callbacks)
        callback_manager.handlers.insert(0, visibility_handler)
        callback_manager.inheritable_handlers.insert(0, visibility_handler)
        resolved["callbacks"] = callback_manager
    return resolved


class _GuardedBoundRunnable[InputT, OutputT](Runnable[InputT, OutputT]):
    """Runnable binding whose public call boundaries capture deadlines eagerly."""

    def __init__(
        self,
        target: Runnable[InputT, OutputT],
        owner: ModelCallGuardMixin,
        boundary_config: RunnableConfig | None = None,
    ) -> None:
        self.bound = target
        self._owner = owner
        self._boundary_config = boundary_config

    def _rewrap(self, target: Runnable[Any, Any]) -> _GuardedBoundRunnable[Any, Any]:
        return _GuardedBoundRunnable(
            target,
            self._owner,
            boundary_config=self._boundary_config,
        )

    def get_name(self, suffix: str | None = None, *, name: str | None = None) -> str:
        return self.bound.get_name(suffix, name=name)

    @property
    def InputType(self) -> type[InputT]:
        return self.bound.InputType

    @property
    def OutputType(self) -> type[OutputT]:
        return self.bound.OutputType

    def get_input_schema(self, config: RunnableConfig | None = None) -> Any:
        return self.bound.get_input_schema(config)

    def get_output_schema(self, config: RunnableConfig | None = None) -> Any:
        return self.bound.get_output_schema(config)

    @property
    def config_specs(self) -> list[Any]:
        return self.bound.config_specs

    def get_graph(self, config: RunnableConfig | None = None) -> Any:
        return self.bound.get_graph(config)

    def invoke(
        self, input: InputT, config: RunnableConfig | None = None, **kwargs: Any
    ) -> OutputT:
        deadline = _capture_deadline(self._owner._model_call_policy)
        scope_id = _scope_id_from_config(config, self._boundary_config)
        callback_pump = _SyncCallerCallbackPump()
        control = _start_bridge(
            lambda: _run_with_callback_target(
                lambda: _run_with_absolute_deadline(
                    lambda: self.bound.ainvoke(input, config=config, **kwargs),
                    deadline=deadline,
                    scope_id=scope_id,
                    policy=self._owner._model_call_policy,
                    metadata=self._owner._runtime_metadata,
                ),
                callback_pump,
            ),
            scope_id=scope_id,
            registry=self._owner._bridge_registry,
        )
        return _bridge_result(
            control,
            deadline,
            policy=self._owner._model_call_policy,
            metadata=self._owner._runtime_metadata,
            callback_pump=callback_pump,
        )

    def ainvoke(
        self, input: InputT, config: RunnableConfig | None = None, **kwargs: Any
    ) -> Awaitable[OutputT]:
        deadline = _capture_deadline(self._owner._model_call_policy)
        scope_id = _scope_id_from_config(config, self._boundary_config)

        async def run() -> OutputT:
            return await _run_with_absolute_deadline(
                lambda: self.bound.ainvoke(input, config=config, **kwargs),
                deadline=deadline,
                scope_id=scope_id,
                policy=self._owner._model_call_policy,
                metadata=self._owner._runtime_metadata,
            )

        return run()

    def stream(
        self, input: InputT, config: RunnableConfig | None = None, **kwargs: Any
    ) -> Iterator[OutputT]:
        deadline = _capture_deadline(self._owner._model_call_policy)
        scope_id = _scope_id_from_config(config, self._boundary_config)
        callback_pump = _SyncCallerCallbackPump()
        return _SyncStreamIterator(
            lambda: _stream_with_callback_target(
                lambda: self._astream_with_deadline(
                    input,
                    config=config,
                    deadline=deadline,
                    scope_id=scope_id,
                    **kwargs,
                ),
                callback_pump,
            ),
            deadline=deadline,
            scope_id=scope_id,
            registry=self._owner._bridge_registry,
            policy=self._owner._model_call_policy,
            metadata=self._owner._runtime_metadata,
            callback_pump=callback_pump,
        )

    def astream(
        self, input: InputT, config: RunnableConfig | None = None, **kwargs: Any
    ) -> AsyncIterator[OutputT]:
        deadline = _capture_deadline(self._owner._model_call_policy)
        scope_id = _scope_id_from_config(config, self._boundary_config)
        return self._astream_with_deadline(
            input,
            config=config,
            deadline=deadline,
            scope_id=scope_id,
            **kwargs,
        )

    def _astream_with_deadline(
        self,
        input: InputT,
        *,
        config: RunnableConfig | None,
        deadline: float,
        scope_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[OutputT]:
        async def iterate() -> AsyncIterator[OutputT]:
            async def create_iterator() -> AsyncIterator[OutputT]:
                return self.bound.astream(input, config=config, **kwargs)

            iterator = await _run_with_absolute_deadline(
                create_iterator,
                deadline=deadline,
                scope_id=scope_id,
                policy=self._owner._model_call_policy,
                metadata=self._owner._runtime_metadata,
            )
            try:
                while True:
                    try:
                        item = await _run_with_absolute_deadline(
                            lambda: anext(iterator),
                            deadline=deadline,
                            scope_id=scope_id,
                            policy=self._owner._model_call_policy,
                            metadata=self._owner._runtime_metadata,
                        )
                    except StopAsyncIteration:
                        return
                    yield item
            finally:
                await _bounded_async_iterator_close(iterator)

        return iterate()

    def bind(self, **kwargs: Any) -> _GuardedBoundRunnable[InputT, OutputT]:
        return self._rewrap(self.bound.bind(**kwargs))

    def with_config(
        self,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> _GuardedBoundRunnable[InputT, OutputT]:
        """Apply config while retaining eager outer call boundaries."""
        explicit_config = {**(config or {}), **kwargs}
        return _GuardedBoundRunnable(
            self.bound.with_config(config, **kwargs),
            self._owner,
            boundary_config=merge_configs(self._boundary_config, explicit_config),
        )

    def with_retry(
        self,
        *,
        retry_if_exception_type: tuple[type[BaseException], ...] = (Exception,),
        wait_exponential_jitter: bool = True,
        exponential_jitter_params: Any = None,
        stop_after_attempt: int = 3,
    ) -> _GuardedBoundRunnable[InputT, OutputT]:
        """Apply retries inside one guard-owned total deadline."""
        return self._rewrap(
            self.bound.with_retry(
                retry_if_exception_type=retry_if_exception_type,
                wait_exponential_jitter=wait_exponential_jitter,
                exponential_jitter_params=exponential_jitter_params,
                stop_after_attempt=stop_after_attempt,
            )
        )

    def with_listeners(
        self,
        *,
        on_start: Any = None,
        on_end: Any = None,
        on_error: Any = None,
    ) -> _GuardedBoundRunnable[InputT, OutputT]:
        """Attach synchronous listeners without exposing a lazy raw binding."""
        return self._rewrap(
            self.bound.with_listeners(
                on_start=on_start,
                on_end=on_end,
                on_error=on_error,
            )
        )

    def with_alisteners(
        self,
        *,
        on_start: Any = None,
        on_end: Any = None,
        on_error: Any = None,
    ) -> _GuardedBoundRunnable[InputT, OutputT]:
        """Attach async listeners without exposing a lazy raw binding."""
        return self._rewrap(
            self.bound.with_alisteners(
                on_start=on_start,
                on_end=on_end,
                on_error=on_error,
            )
        )

    def with_types(
        self,
        *,
        input_type: type[InputT] | None = None,
        output_type: type[OutputT] | None = None,
    ) -> _GuardedBoundRunnable[InputT, OutputT]:
        """Apply input/output types while retaining guarded boundaries."""
        return self._rewrap(
            self.bound.with_types(input_type=input_type, output_type=output_type)
        )


class ModelCallGuardMixin:
    """Provider-preserving public model boundaries with strict total deadlines."""

    def invoke(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Invoke through provider async transport from a bounded daemon bridge."""
        _ensure_model_process(self)
        policy = self._model_call_policy
        deadline = _capture_deadline(policy)
        scope_id = _scope_id_from_config(config)
        callback_pump = _SyncCallerCallbackPump()
        control = _start_bridge(
            lambda: _run_with_callback_target(
                lambda: self._guarded_ainvoke(
                    input,
                    config=config,
                    stop=stop,
                    deadline=deadline,
                    scope_id=scope_id,
                    callback_target=callback_pump,
                    **kwargs,
                ),
                callback_pump,
            ),
            scope_id=scope_id,
            registry=self._bridge_registry,
        )
        return _bridge_result(
            control,
            deadline,
            policy=self._model_call_policy,
            metadata=self._runtime_metadata,
            callback_pump=callback_pump,
        )

    def ainvoke(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Awaitable[Any]:
        """Capture deadline eagerly and keep provider I/O on transport loop."""
        _ensure_model_process(self)
        deadline = _capture_deadline(self._model_call_policy)
        scope_id = _scope_id_from_config(config)

        async def run() -> Any:
            caller_loop = asyncio.get_running_loop()
            if _GLOBAL_BRIDGE_RUNTIME.owns_current_loop():
                return await self._guarded_ainvoke(
                    input,
                    config=config,
                    stop=stop,
                    deadline=deadline,
                    scope_id=scope_id,
                    callback_target=_ACTIVE_CALLBACK_TARGET.get(),
                    **kwargs,
                )
            control = _start_bridge(
                lambda: _run_with_callback_target(
                    lambda: self._guarded_ainvoke(
                        input,
                        config=config,
                        stop=stop,
                        deadline=deadline,
                        scope_id=scope_id,
                        callback_target=caller_loop,
                        **kwargs,
                    ),
                    caller_loop,
                ),
                scope_id=scope_id,
                registry=self._bridge_registry,
            )
            return await _await_bridge_result(
                control,
                deadline,
                policy=self._model_call_policy,
                metadata=self._runtime_metadata,
            )

        return run()

    async def _guarded_ainvoke(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None,
        *,
        stop: list[str] | None,
        deadline: float,
        scope_id: str,
        callback_target: asyncio.AbstractEventLoop | _SyncCallerCallbackPump | None,
        **kwargs: Any,
    ) -> Any:
        provider_model = self
        provider_config = ensure_config(config)
        if callback_target is not None:
            provider_model, provider_config = _provider_callbacks_on_caller(
                self,
                config,
                callback_target,
            )
        controller = self._retry_controller
        visibility = _AttemptVisibility() if controller is not None else None
        if visibility is not None:
            provider_config = _config_with_visibility_callback(
                provider_config,
                visibility,
                local_callbacks=getattr(provider_model, "callbacks", None),
            )

        async def provider_call() -> Any:
            with _without_duplicate_ambient_callback_hooks(
                provider_config.get("callbacks")
            ):
                return await super(ModelCallGuardMixin, provider_model).ainvoke(
                    input, config=provider_config, stop=stop, **kwargs
                )

        if controller is None:
            operation = provider_call
        else:
            assert visibility is not None  # noqa: S101

            async def operation() -> Any:
                return await controller.ainvoke(
                    provider_call,
                    deadline=deadline,
                    input=input,
                    max_tokens=kwargs.get("max_tokens", 1000) or 1000,
                    can_retry=visibility.can_retry,
                )
        return await _run_with_absolute_deadline(
            operation,
            deadline=deadline,
            scope_id=scope_id,
            policy=self._model_call_policy,
            metadata=self._runtime_metadata,
        )

    def stream(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Stream provider async chunks through a bounded daemon bridge."""
        _ensure_model_process(self)
        deadline = _capture_deadline(self._model_call_policy)
        scope_id = _scope_id_from_config(config)
        callback_pump = _SyncCallerCallbackPump()
        return _SyncStreamIterator(
            lambda: _stream_with_callback_target(
                lambda: self._guarded_astream(
                    input,
                    config=config,
                    stop=stop,
                    deadline=deadline,
                    scope_id=scope_id,
                    callback_target=callback_pump,
                    **kwargs,
                ),
                callback_pump,
            ),
            deadline=deadline,
            scope_id=scope_id,
            registry=self._bridge_registry,
            policy=self._model_call_policy,
            metadata=self._runtime_metadata,
            callback_pump=callback_pump,
        )

    def astream(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Capture one deadline and keep async transport on its owner loop."""
        _ensure_model_process(self)
        deadline = _capture_deadline(self._model_call_policy)
        scope_id = _scope_id_from_config(config)

        def provider_stream(
            callback_target: asyncio.AbstractEventLoop | None,
        ) -> AsyncIterator[Any]:
            return _stream_with_callback_target(
                lambda: self._guarded_astream(
                    input,
                    config=config,
                    stop=stop,
                    deadline=deadline,
                    scope_id=scope_id,
                    callback_target=callback_target,
                    **kwargs,
                ),
                callback_target,
            )

        if _GLOBAL_BRIDGE_RUNTIME.owns_current_loop():
            return provider_stream(_ACTIVE_CALLBACK_TARGET.get())
        return _AsyncBridgeStreamIterator(
            provider_stream,
            deadline=deadline,
            scope_id=scope_id,
            registry=self._bridge_registry,
            policy=self._model_call_policy,
            metadata=self._runtime_metadata,
        )

    async def _guarded_astream(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None,
        *,
        stop: list[str] | None,
        deadline: float,
        scope_id: str,
        callback_target: asyncio.AbstractEventLoop | _SyncCallerCallbackPump | None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        provider_model = self
        provider_config = ensure_config(config)
        if callback_target is not None:
            provider_model, provider_config = _provider_callbacks_on_caller(
                self,
                config,
                callback_target,
            )

        async def base_provider_stream() -> AsyncIterator[Any]:
            with _without_duplicate_ambient_callback_hooks(
                provider_config.get("callbacks")
            ):
                async for item in super(
                    ModelCallGuardMixin,
                    provider_model,
                ).astream(
                    input,
                    config=provider_config,
                    stop=stop,
                    **kwargs,
                ):
                    yield item

        controller = self._retry_controller
        if controller is None:
            iterator = base_provider_stream()
        else:
            visibility = _AttemptVisibility()
            provider_config = _config_with_visibility_callback(
                provider_config,
                visibility,
                local_callbacks=getattr(provider_model, "callbacks", None),
            )

            iterator = controller.astream(
                base_provider_stream,
                deadline=deadline,
                input=input,
                max_tokens=kwargs.get("max_tokens", 1000) or 1000,
                can_retry=visibility.can_retry,
                mark_visible=visibility.mark_visible,
            )
        try:
            while True:
                try:
                    item = await _run_with_absolute_deadline(
                        lambda: anext(iterator),
                        deadline=deadline,
                        scope_id=scope_id,
                        policy=self._model_call_policy,
                        metadata=self._runtime_metadata,
                    )
                except StopAsyncIteration:
                    return
                yield item
        finally:
            await _bounded_async_iterator_close(iterator)

    def bind(self: BaseChatModel, **kwargs: Any) -> Runnable[Any, Any]:
        """Bind model arguments while retaining eager guarded boundaries."""
        bound = super().bind(**kwargs)
        return _GuardedBoundRunnable(bound, self)

    def with_config(
        self: BaseChatModel,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """Bind model config behind an eager guarded boundary."""
        explicit_config = {**(config or {}), **kwargs}
        return _GuardedBoundRunnable(
            super().with_config(config, **kwargs),
            self,
            boundary_config=merge_configs(None, explicit_config),
        )

    def with_retry(
        self: BaseChatModel,
        *,
        retry_if_exception_type: tuple[type[BaseException], ...] = (Exception,),
        wait_exponential_jitter: bool = True,
        exponential_jitter_params: Any = None,
        stop_after_attempt: int = 3,
    ) -> Runnable[Any, Any]:
        """Decorate model retries inside one eager guarded boundary."""
        return _GuardedBoundRunnable(
            super().with_retry(
                retry_if_exception_type=retry_if_exception_type,
                wait_exponential_jitter=wait_exponential_jitter,
                exponential_jitter_params=exponential_jitter_params,
                stop_after_attempt=stop_after_attempt,
            ),
            self,
        )

    def with_listeners(
        self: BaseChatModel,
        *,
        on_start: Any = None,
        on_end: Any = None,
        on_error: Any = None,
    ) -> Runnable[Any, Any]:
        """Attach sync listeners behind an eager guarded boundary."""
        return _GuardedBoundRunnable(
            super().with_listeners(
                on_start=on_start,
                on_end=on_end,
                on_error=on_error,
            ),
            self,
        )

    def with_alisteners(
        self: BaseChatModel,
        *,
        on_start: Any = None,
        on_end: Any = None,
        on_error: Any = None,
    ) -> Runnable[Any, Any]:
        """Attach async listeners behind an eager guarded boundary."""
        return _GuardedBoundRunnable(
            super().with_alisteners(
                on_start=on_start,
                on_end=on_end,
                on_error=on_error,
            ),
            self,
        )

    def with_types(
        self: BaseChatModel,
        *,
        input_type: type[Any] | None = None,
        output_type: type[Any] | None = None,
    ) -> Runnable[Any, Any]:
        """Apply model input/output types behind an eager guarded boundary."""
        return _GuardedBoundRunnable(
            super().with_types(input_type=input_type, output_type=output_type),
            self,
        )

    def bind_tools(
        self: BaseChatModel,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any]],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """Resolve provider tools before wrapping resulting runnable once."""
        bound = super().bind_tools(tools, tool_choice=tool_choice, **kwargs)
        if isinstance(bound, _GuardedBoundRunnable):
            return bound
        return _GuardedBoundRunnable(bound, self)

    def __getstate__(self: BaseChatModel) -> dict[str, Any]:
        """Serialize provider state without non-pickleable guard runtime state."""
        state = dict(super().__getstate__())
        private = dict(state.get("__pydantic_private__") or {})
        private.pop("_bridge_registry", None)
        private.pop("_retry_controller", None)
        state["__pydantic_private__"] = private
        return state

    def __setstate__(self: BaseChatModel, state: dict[str, Any]) -> None:
        """Restore provider state and initialize a fresh guard runtime."""
        super().__setstate__(state)
        _initialize_guard_state(
            self,
            policy=self._model_call_policy,
            metadata=self._runtime_metadata,
        )

    def __reduce_ex__(self: BaseChatModel, protocol: int) -> tuple[Any, tuple[Any, ...]]:
        """Pickle through provider-native state, never a generated class global."""
        del protocol
        retry_settings = (
            self._retry_controller.settings
            if self._retry_controller is not None
            else None
        )
        return (
            rebuild_guarded_model,
            (
                _provider_native_pickle_view(self),
                self._runtime_metadata,
                self._model_call_policy,
                retry_settings,
            ),
        )

    def model_copy(
        self: BaseChatModel,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> BaseChatModel:
        """Copy provider fields while initializing independent guard state."""
        _ensure_model_process(self)
        copied = super().model_copy(update=update, deep=False)
        if deep:
            private = dict(copied.__pydantic_private__ or {})
            for name in _GUARD_PRIVATE_ATTR_NAMES:
                private.pop(name, None)
            object.__setattr__(copied, "__pydantic_private__", private)
            copied = super(ModelCallGuardMixin, copied).model_copy(deep=True)
        metadata = _extract_runtime_metadata(copied) or self._runtime_metadata
        policy = self._model_call_policy
        retry_controller = self._retry_controller
        if deep:
            metadata = copy.deepcopy(metadata)
            policy = copy.deepcopy(policy)
        _initialize_guard_state(
            copied,
            policy=policy,
            metadata=metadata,
        )
        if retry_controller is not None:
            copied._retry_controller = retry_controller.clone().bind(copied)
        return copied


_GUARD_PRIVATE_ATTR_NAMES = (
    "_model_call_policy",
    "_runtime_metadata",
    "_bridge_registry",
    "_retry_controller",
    "_guard_pid",
)
_GUARDED_PROVIDER_CLASSES: dict[type[BaseChatModel], type[BaseChatModel]] = {}


def _copy_provider_slots(
    source: BaseChatModel,
    target: BaseChatModel,
    provider_class: type[BaseChatModel],
) -> None:
    """Copy provider-declared slot storage without guard runtime attributes."""
    excluded_slots = {
        "__dict__",
        "__weakref__",
        "__pydantic_fields_set__",
        "__pydantic_extra__",
        "__pydantic_private__",
        *_GUARD_PRIVATE_ATTR_NAMES,
    }
    for owner in provider_class.__mro__:
        declared_slots = owner.__dict__.get("__slots__", ())
        slots = (declared_slots,) if isinstance(declared_slots, str) else declared_slots
        for slot in slots:
            if slot in excluded_slots:
                continue
            storage_name = slot
            if slot.startswith("__") and not slot.endswith("__"):
                storage_name = f"_{owner.__name__.lstrip('_')}{slot}"
            try:
                value = object.__getattribute__(source, storage_name)
            except AttributeError:
                continue
            object.__setattr__(target, storage_name, value)


def _provider_native_pickle_view(model: BaseChatModel) -> BaseChatModel:
    """Create raw provider storage whose own pickle protocol remains authoritative."""
    provider_class = type(model).__mro__[2]
    provider = object.__new__(provider_class)
    provider_dict = {
        name: value
        for name, value in model.__dict__.items()
        if name not in _GUARD_PRIVATE_ATTR_NAMES
    }
    provider_private = dict(model.__pydantic_private__ or {})
    for name in _GUARD_PRIVATE_ATTR_NAMES:
        provider_private.pop(name, None)

    object.__setattr__(provider, "__dict__", provider_dict)
    object.__setattr__(
        provider,
        "__pydantic_fields_set__",
        set(model.__pydantic_fields_set__),
    )
    object.__setattr__(provider, "__pydantic_extra__", model.__pydantic_extra__)
    object.__setattr__(provider, "__pydantic_private__", provider_private or None)
    _copy_provider_slots(model, provider, provider_class)
    return provider


def _guarded_provider_class(provider_class: type[BaseChatModel]) -> type[BaseChatModel]:
    with _GUARDED_PROVIDER_CLASSES_LOCK:
        cached = _GUARDED_PROVIDER_CLASSES.get(provider_class)
        if cached is not None:
            return cached

        def provider_lc_id(cls: type[BaseChatModel]) -> list[str]:
            del cls
            return provider_class.lc_id()

        def provider_to_json(self: BaseChatModel) -> dict[str, Any]:
            serialized = dict(provider_class.to_json(self))
            serialized["id"] = provider_class.lc_id()
            if "name" in serialized and not self.name:
                serialized["name"] = provider_class.__name__
            representation = serialized.get("repr")
            dynamic_prefix = f"{type(self).__name__}("
            if isinstance(representation, str) and representation.startswith(
                dynamic_prefix
            ):
                serialized["repr"] = (
                    f"{provider_class.__name__}("
                    f"{representation.removeprefix(dynamic_prefix)}"
                )
            return serialized

        identity = f"{provider_class.__module__}:{provider_class.__qualname__}"
        identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        generated_name = f"Guarded{provider_class.__name__}_{identity_digest}"
        namespace = {
            "__module__": __name__,
            "__qualname__": generated_name,
            "lc_id": classmethod(provider_lc_id),
            "to_json": provider_to_json,
            "_model_call_policy": PrivateAttr(),
            "_runtime_metadata": PrivateAttr(),
            "_bridge_registry": PrivateAttr(),
            "_retry_controller": PrivateAttr(default=None),
            "_guard_pid": PrivateAttr(default_factory=os.getpid),
        }
        guarded = type(
            generated_name,
            (ModelCallGuardMixin, provider_class),
            namespace,
        )
        _GUARDED_PROVIDER_CLASSES[provider_class] = guarded
        return guarded


def _snapshot_lazy_provider_clients(
    model: BaseChatModel,
    metadata: ModelRuntimeMetadata,
) -> None:
    """Resolve lazy transports while parent process environment is safe."""
    if metadata.provider != "anthropic":
        return
    try:
        from langchain_anthropic import ChatAnthropic  # noqa: PLC0415
    except ImportError:
        return
    if isinstance(model, ChatAnthropic):
        model._client
        model._async_client


def _initialize_guard_state(
    model: BaseChatModel,
    *,
    policy: ModelCallPolicy,
    metadata: ModelRuntimeMetadata,
) -> None:
    model._model_call_policy = policy
    model._runtime_metadata = metadata
    model._bridge_registry = BridgeRegistry()
    model._retry_controller = None
    model._guard_pid = os.getpid()
    _ensure_model_callback_proxies(model)
    _snapshot_lazy_provider_clients(model, metadata)


def rebuild_guarded_model(
    provider: BaseChatModel,
    metadata: ModelRuntimeMetadata,
    policy: ModelCallPolicy,
    retry_settings: Any | None = None,
) -> BaseChatModel:
    """Reconstruct a guarded model from one natively serialized provider."""
    guarded = guard_model(provider, metadata=metadata, policy=policy)
    if retry_settings is not None:
        from research_agent.retry_utils import ModelRetryController

        guarded._retry_controller = ModelRetryController.from_settings(
            retry_settings
        ).bind(guarded)
    guard_private = {
        name: getattr(guarded, name) for name in _GUARD_PRIVATE_ATTR_NAMES
    }
    provider_private = dict(provider.__pydantic_private__ or {})
    provider_private.update(guard_private)

    object.__setattr__(guarded, "__dict__", dict(provider.__dict__))
    object.__setattr__(
        guarded,
        "__pydantic_fields_set__",
        set(provider.__pydantic_fields_set__),
    )
    object.__setattr__(guarded, "__pydantic_extra__", provider.__pydantic_extra__)
    object.__setattr__(guarded, "__pydantic_private__", provider_private)
    _copy_provider_slots(provider, guarded, type(provider))
    return guarded


def _known_provider_metadata(model: BaseChatModel) -> ModelRuntimeMetadata | None:
    try:
        from langchain_openai import AzureChatOpenAI, ChatOpenAI

        if isinstance(model, AzureChatOpenAI):
            return ModelRuntimeMetadata(
                provider="azure_openai",
                model_name=model.model_name or model.deployment_name or "",
                base_url=model.azure_endpoint,
            )
        if isinstance(model, ChatOpenAI):
            return ModelRuntimeMetadata(
                provider="openai",
                model_name=model.model_name,
                base_url=model.openai_api_base,
            )
    except ImportError:
        pass
    try:
        from langchain_anthropic import ChatAnthropic

        if isinstance(model, ChatAnthropic):
            return ModelRuntimeMetadata(
                provider="anthropic",
                model_name=model.model,
                base_url=model.anthropic_api_url,
            )
    except ImportError:
        pass
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        if isinstance(model, ChatGoogleGenerativeAI):
            return ModelRuntimeMetadata(
                provider="google",
                model_name=model.model,
                base_url=model.base_url,
            )
    except ImportError:
        pass
    try:
        from langchain_ollama import ChatOllama

        if isinstance(model, ChatOllama):
            return ModelRuntimeMetadata(
                provider="ollama",
                model_name=model.model,
                base_url=model.base_url,
            )
    except ImportError:
        pass
    return None


def _extract_runtime_metadata(model: BaseChatModel) -> ModelRuntimeMetadata | None:
    known = _known_provider_metadata(model)
    if known is not None:
        return known
    descriptor = getattr(model, "model_runtime_metadata", None)
    if callable(descriptor):
        descriptor = descriptor()
    if isinstance(descriptor, ModelRuntimeMetadata):
        return descriptor
    getter = getattr(model, "get_model_runtime_metadata", None)
    if callable(getter):
        described = getter()
        if isinstance(described, ModelRuntimeMetadata):
            return described
    return None


def _bounded_native_timeout(current: Any, policy: ModelCallPolicy) -> Any:
    if current is None:
        return policy.timeout_seconds
    if isinstance(current, bool):
        return policy.timeout_seconds
    if isinstance(current, (int, float)):
        try:
            valid = math.isfinite(current) and current >= 0
        except (TypeError, ValueError):
            valid = False
        return (
            min(float(current), policy.timeout_seconds)
            if valid
            else policy.timeout_seconds
        )
    if isinstance(current, (tuple, httpx.Timeout)):
        try:
            parsed = httpx.Timeout(current)
        except (TypeError, ValueError):
            return policy.timeout_seconds

        def bounded_component(value: Any) -> float:
            if isinstance(value, bool):
                return policy.timeout_seconds
            try:
                valid = value is not None and math.isfinite(value) and value >= 0
            except (TypeError, ValueError):
                valid = False
            return (
                min(float(value), policy.timeout_seconds)
                if valid
                else policy.timeout_seconds
            )

        return httpx.Timeout(
            connect=bounded_component(parsed.connect),
            read=bounded_component(parsed.read),
            write=bounded_component(parsed.write),
            pool=bounded_component(parsed.pool),
        )
    return policy.timeout_seconds


def _validated_provider_fields(
    model: BaseChatModel,
    metadata: ModelRuntimeMetadata,
    policy: ModelCallPolicy,
    *,
    clone_http_transports: bool = True,
) -> dict[str, Any]:
    fields = {
        name: getattr(model, name)
        for name in type(model).model_fields
        if hasattr(model, name)
    }
    if metadata.provider in {
        "aws_bedrock",
        "openai",
        "azure_openai",
        "anthropic",
        "google",
    }:
        fields["max_retries"] = 0
    if metadata.provider == "ollama":
        for name in ("client_kwargs", "async_client_kwargs", "sync_client_kwargs"):
            native = dict(fields.get(name) or {})
            native["timeout"] = _bounded_native_timeout(native.get("timeout"), policy)
            fields[name] = native
        if clone_http_transports:
            _install_fresh_ollama_transport_fields(model, fields, metadata, policy)
    elif metadata.provider in {"aws_bedrock", "openai", "azure_openai"}:
        fields["request_timeout"] = _bounded_native_timeout(
            fields.get("request_timeout"), policy
        )
        for name in (
            "client",
            "async_client",
            "root_client",
            "root_async_client",
            "http_client",
            "http_async_client",
        ):
            fields.pop(name, None)
        http_client = getattr(model, "http_client", None)
        http_async_client = getattr(model, "http_async_client", None)
        if not isinstance(http_client, httpx.Client):
            http_client = getattr(getattr(model, "root_client", None), "_client", None)
        if not isinstance(http_async_client, httpx.AsyncClient):
            http_async_client = getattr(
                getattr(model, "root_async_client", None),
                "_client",
                None,
            )
        fresh_http_client: httpx.Client | None = None
        if clone_http_transports and isinstance(http_client, httpx.Client):
            fresh_http_client = _fresh_httpx_client_after_fork(
                http_client,
                policy,
                owner=model,
                metadata=metadata,
            )
            fields["http_client"] = fresh_http_client
        if clone_http_transports and isinstance(http_async_client, httpx.AsyncClient):
            try:
                fields["http_async_client"] = _fresh_httpx_async_client_after_fork(
                    http_async_client,
                    policy,
                    owner=model,
                    metadata=metadata,
                )
            except BaseException:
                if fresh_http_client is not None:
                    fresh_http_client.close()
                raise
    elif metadata.provider == "anthropic":
        fields["default_request_timeout"] = _bounded_native_timeout(
            fields.get("default_request_timeout"), policy
        )
    elif metadata.provider == "google":
        fields["timeout"] = _bounded_native_timeout(fields.get("timeout"), policy)
    return fields


def _httpx_transport_ssl_context(client: Any) -> Any:
    """Recover safe TLS configuration without reusing a live transport pool."""
    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    return getattr(pool, "_ssl_context", True)


def _unwrapped_httpx_transport(transport: Any) -> Any:
    """Return provider transport without borrowing caller-owned wrappers."""
    return transport


def _resolved_httpx_proxy(pool: Any) -> httpx.Proxy | None:
    """Recover proxy settings already resolved before a process fork."""
    proxy_url = getattr(pool, "_proxy_url", None)
    if proxy_url is None:
        return None
    proxy_auth = getattr(pool, "_proxy_auth", None)
    if proxy_auth is not None:
        proxy_auth = tuple(
            value.decode("ascii") if isinstance(value, bytes) else value
            for value in proxy_auth
        )
    return httpx.Proxy(
        bytes(proxy_url).decode("ascii"),
        auth=proxy_auth,
        headers=getattr(pool, "_proxy_headers", None),
        ssl_context=getattr(pool, "_proxy_ssl_context", None),
    )


def _httpx_limits_from_pool(pool: Any) -> httpx.Limits:
    """Copy connection bounds without carrying inherited pool locks."""
    return httpx.Limits(
        max_connections=getattr(pool, "_max_connections", None),
        max_keepalive_connections=getattr(
            pool,
            "_max_keepalive_connections",
            None,
        ),
        keepalive_expiry=getattr(pool, "_keepalive_expiry", None),
    )


def _fresh_httpx_transport_after_fork(
    transport: Any,
    *,
    asynchronous: bool,
    owner: BaseChatModel | None = None,
    metadata: ModelRuntimeMetadata | None = None,
) -> Any:
    """Recreate standard transports or use an explicit model clone capability."""
    transport = _unwrapped_httpx_transport(transport)
    expected_type = httpx.AsyncHTTPTransport if asynchronous else httpx.HTTPTransport
    if type(transport) is expected_type:
        pool = transport._pool
        transport_class = (
            httpx.AsyncHTTPTransport if asynchronous else httpx.HTTPTransport
        )
        return transport_class(
            verify=getattr(pool, "_ssl_context", True),
            trust_env=False,
            http1=getattr(pool, "_http1", True),
            http2=getattr(pool, "_http2", False),
            limits=_httpx_limits_from_pool(pool),
            proxy=_resolved_httpx_proxy(pool),
            uds=getattr(pool, "_uds", None),
            local_address=getattr(pool, "_local_address", None),
            retries=getattr(pool, "_retries", 0),
            socket_options=getattr(pool, "_socket_options", None),
        )
    clone = getattr(owner, "clone_model_http_transport", None)
    if callable(clone):
        try:
            fresh = clone(transport, asynchronous=asynchronous)
        except Exception:
            raise UnsupportedModelOverrideError(
                provider=(metadata.provider if metadata is not None else "unknown")
            ) from None
        valid_type = httpx.AsyncBaseTransport if asynchronous else httpx.BaseTransport
        if isinstance(fresh, valid_type) and fresh is not transport:
            return fresh
    raise UnsupportedModelOverrideError(
        provider=(metadata.provider if metadata is not None else "unknown")
    )


def _fresh_httpx_mounts_after_fork(
    client: Any,
    *,
    asynchronous: bool,
    owner: BaseChatModel | None = None,
    metadata: ModelRuntimeMetadata | None = None,
) -> dict[str, Any]:
    """Clone resolved proxy/no-proxy mounts without inherited transports."""
    mounts: dict[str, Any] = {}
    for pattern, transport in client._mounts.items():
        if transport is None:
            mounts[pattern.pattern] = None
            continue
        fresh_transport = _fresh_httpx_transport_after_fork(
            transport,
            asynchronous=asynchronous,
            owner=owner,
            metadata=metadata,
        )
        mounts[pattern.pattern] = fresh_transport
    return mounts


def _fresh_httpx_client_after_fork(
    client: httpx.Client,
    policy: ModelCallPolicy,
    *,
    owner: BaseChatModel | None = None,
    metadata: ModelRuntimeMetadata | None = None,
) -> httpx.Client:
    """Clone stable client settings around a new process-local transport."""
    transport = _fresh_httpx_transport_after_fork(
        client._transport,
        asynchronous=False,
        owner=owner,
        metadata=metadata,
    )
    return httpx.Client(
        auth=client._auth,
        params=client.params,
        headers=client.headers,
        cookies=client.cookies,
        verify=_httpx_transport_ssl_context(client),
        # macOS system proxy discovery is not safe after a multithreaded fork.
        trust_env=False,
        mounts=_fresh_httpx_mounts_after_fork(
            client,
            asynchronous=False,
            owner=owner,
            metadata=metadata,
        ),
        timeout=_bounded_native_timeout(client.timeout, policy),
        follow_redirects=client.follow_redirects,
        max_redirects=client.max_redirects,
        event_hooks={name: list(hooks) for name, hooks in client.event_hooks.items()},
        base_url=client.base_url,
        transport=transport,
        default_encoding=client._default_encoding,
    )


def _fresh_httpx_async_client_after_fork(
    client: httpx.AsyncClient,
    policy: ModelCallPolicy,
    *,
    owner: BaseChatModel | None = None,
    metadata: ModelRuntimeMetadata | None = None,
) -> httpx.AsyncClient:
    """Clone stable async settings around a new process-local transport."""
    transport = _fresh_httpx_transport_after_fork(
        client._transport,
        asynchronous=True,
        owner=owner,
        metadata=metadata,
    )
    return httpx.AsyncClient(
        auth=client._auth,
        params=client.params,
        headers=client.headers,
        cookies=client.cookies,
        verify=_httpx_transport_ssl_context(client),
        # macOS system proxy discovery is not safe after a multithreaded fork.
        trust_env=False,
        mounts=_fresh_httpx_mounts_after_fork(
            client,
            asynchronous=True,
            owner=owner,
            metadata=metadata,
        ),
        timeout=_bounded_native_timeout(client.timeout, policy),
        follow_redirects=client.follow_redirects,
        max_redirects=client.max_redirects,
        event_hooks={name: list(hooks) for name, hooks in client.event_hooks.items()},
        base_url=client.base_url,
        transport=transport,
        default_encoding=client._default_encoding,
    )


def _install_fresh_ollama_transport_fields(
    model: BaseChatModel,
    fields: dict[str, Any],
    metadata: ModelRuntimeMetadata,
    policy: ModelCallPolicy,
) -> None:
    """Clone Ollama HTTP settings without sharing caller-owned loop state."""
    del policy
    for name in ("client_kwargs", "sync_client_kwargs", "async_client_kwargs"):
        client_kwargs = dict(fields.get(name) or {})
        client_kwargs["trust_env"] = False
        client_kwargs.pop("mounts", None)
        client_kwargs.pop("transport", None)
        fields[name] = client_kwargs
    clients = (
        (
            "sync_client_kwargs",
            getattr(getattr(model, "_client", None), "_client", None),
            False,
        ),
        (
            "async_client_kwargs",
            getattr(getattr(model, "_async_client", None), "_client", None),
            True,
        ),
    )
    for field_name, client, asynchronous in clients:
        expected_client_type = httpx.AsyncClient if asynchronous else httpx.Client
        if not isinstance(client, expected_client_type):
            continue
        fields[field_name]["mounts"] = _fresh_httpx_mounts_after_fork(
            client,
            asynchronous=asynchronous,
            owner=model,
            metadata=metadata,
        )
        fields[field_name]["transport"] = _fresh_httpx_transport_after_fork(
            client._transport,
            asynchronous=asynchronous,
            owner=model,
            metadata=metadata,
        )


def _provider_fields_after_fork(model: BaseChatModel) -> dict[str, Any]:
    """Build constructor fields without inherited loop-bound SDK clients."""
    metadata = model._runtime_metadata
    policy = model._model_call_policy
    fields = _validated_provider_fields(
        model,
        metadata,
        policy,
        clone_http_transports=False,
    )
    if metadata.provider == "ollama":
        for name in ("client_kwargs", "sync_client_kwargs", "async_client_kwargs"):
            client_kwargs = dict(fields.get(name) or {})
            client_kwargs["trust_env"] = False
            client_kwargs.pop("mounts", None)
            client_kwargs.pop("transport", None)
            fields[name] = client_kwargs
        sync_client = getattr(getattr(model, "_client", None), "_client", None)
        async_client = getattr(
            getattr(model, "_async_client", None),
            "_client",
            None,
        )
        if isinstance(sync_client, httpx.Client):
            fields["sync_client_kwargs"]["mounts"] = (
                _fresh_httpx_mounts_after_fork(
                    sync_client,
                    asynchronous=False,
                    owner=model,
                    metadata=metadata,
                )
            )
            sync_transport = _fresh_httpx_transport_after_fork(
                sync_client._transport,
                asynchronous=False,
                owner=model,
                metadata=metadata,
            )
            fields["sync_client_kwargs"]["transport"] = sync_transport
        if isinstance(async_client, httpx.AsyncClient):
            fields["async_client_kwargs"]["mounts"] = (
                _fresh_httpx_mounts_after_fork(
                    async_client,
                    asynchronous=True,
                    owner=model,
                    metadata=metadata,
                )
            )
            async_transport = _fresh_httpx_transport_after_fork(
                async_client._transport,
                asynchronous=True,
                owner=model,
                metadata=metadata,
            )
            fields["async_client_kwargs"]["transport"] = async_transport
    elif metadata.provider in {"aws_bedrock", "openai", "azure_openai"}:
        model_fields = type(model).model_fields
        root_sync_client = getattr(
            getattr(model, "root_client", None),
            "_client",
            None,
        )
        root_async_client = getattr(
            getattr(model, "root_async_client", None),
            "_client",
            None,
        )
        sync_client = (
            root_sync_client
            if isinstance(root_sync_client, httpx.Client)
            else getattr(model, "http_client", None)
        )
        async_client = (
            root_async_client
            if isinstance(root_async_client, httpx.AsyncClient)
            else getattr(model, "http_async_client", None)
        )
        fields.pop("http_client", None)
        fields.pop("http_async_client", None)
        if "http_client" in model_fields:
            fields["http_client"] = (
                _fresh_httpx_client_after_fork(
                    sync_client,
                    policy,
                    owner=model,
                    metadata=metadata,
                )
                if isinstance(sync_client, httpx.Client)
                else httpx.Client(
                    timeout=policy.timeout_seconds,
                    trust_env=False,
                )
            )
        if "http_async_client" in model_fields:
            fields["http_async_client"] = (
                _fresh_httpx_async_client_after_fork(
                    async_client,
                    policy,
                    owner=model,
                    metadata=metadata,
                )
                if isinstance(async_client, httpx.AsyncClient)
                else httpx.AsyncClient(
                    timeout=policy.timeout_seconds,
                    trust_env=False,
                )
            )
        if "http_socket_options" in model_fields:
            fields["http_socket_options"] = ()
    elif metadata.provider == "google":
        fields.pop("client", None)
        client_args = dict(fields.get("client_args") or {})
        client_args["trust_env"] = False
        fields["client_args"] = client_args
    return fields


def _install_fork_safe_anthropic_clients(
    source: BaseChatModel,
    target: BaseChatModel,
    policy: ModelCallPolicy,
) -> None:
    """Preload process-local Anthropic clients without environment proxies."""
    import anthropic  # noqa: PLC0415

    client_params = dict(target._client_params)
    http_client_params: dict[str, Any] = {
        "base_url": client_params["base_url"],
        "timeout": client_params.get("timeout", policy.timeout_seconds),
        "trust_env": False,
    }
    anthropic_proxy = getattr(target, "anthropic_proxy", None)
    if anthropic_proxy:
        http_client_params["proxy"] = anthropic_proxy
    source_client = source.__dict__.get("_client")
    source_async_client = source.__dict__.get("_async_client")
    source_http_client = getattr(source_client, "_client", None)
    source_http_async_client = getattr(source_async_client, "_client", None)
    http_client = (
        _fresh_httpx_client_after_fork(
            source_http_client,
            policy,
            owner=source,
            metadata=source._runtime_metadata,
        )
        if isinstance(source_http_client, httpx.Client)
        else httpx.Client(**http_client_params)
    )
    http_async_client = (
        _fresh_httpx_async_client_after_fork(
            source_http_async_client,
            policy,
            owner=source,
            metadata=source._runtime_metadata,
        )
        if isinstance(source_http_async_client, httpx.AsyncClient)
        else httpx.AsyncClient(**http_client_params)
    )
    target.__dict__["_client"] = anthropic.Anthropic(
        **client_params,
        http_client=http_client,
    )
    target.__dict__["_async_client"] = anthropic.AsyncAnthropic(
        **client_params,
        http_client=http_async_client,
    )


def _install_fork_safe_google_clients(
    source: BaseChatModel,
    target: BaseChatModel,
    policy: ModelCallPolicy,
) -> None:
    """Replace Google child clients with clones of resolved HTTP settings."""
    source_api_client = source.client._api_client
    target_api_client = target.client._api_client
    source_http_client = source_api_client._httpx_client
    source_http_async_client = source_api_client._async_httpx_client
    target_api_client._httpx_client = _fresh_httpx_client_after_fork(
        source_http_client,
        policy,
        owner=source,
        metadata=source._runtime_metadata,
    )
    target_api_client._async_httpx_client = _fresh_httpx_async_client_after_fork(
        source_http_async_client,
        policy,
        owner=source,
        metadata=source._runtime_metadata,
    )


def _ensure_model_process(model: BaseChatModel) -> None:
    """Recreate provider runtime state once after crossing a process fork."""
    current_pid = os.getpid()
    if model._guard_pid == current_pid:
        return
    with _MODEL_PROCESS_RESET_LOCK:
        if model._guard_pid == current_pid:
            return
        provider_class = next(
            base for base in type(model).__bases__ if base is not ModelCallGuardMixin
        )
        fresh = provider_class(**_provider_fields_after_fork(model))
        if model._runtime_metadata.provider == "anthropic":
            _install_fork_safe_anthropic_clients(
                model,
                fresh,
                model._model_call_policy,
            )
        elif model._runtime_metadata.provider == "google":
            _install_fork_safe_google_clients(
                model,
                fresh,
                model._model_call_policy,
            )
        controller = model._retry_controller
        registry = model._bridge_registry
        registry._ensure_process()
        guard_private = {
            "_model_call_policy": model._model_call_policy,
            "_runtime_metadata": model._runtime_metadata,
            "_bridge_registry": registry,
            "_retry_controller": (
                None if controller is None else controller.clone().bind(model)
            ),
            "_guard_pid": current_pid,
        }
        fresh_private = dict(fresh.__pydantic_private__ or {})
        fresh_private.update(guard_private)
        object.__setattr__(model, "__dict__", dict(fresh.__dict__))
        object.__setattr__(
            model,
            "__pydantic_fields_set__",
            set(fresh.__pydantic_fields_set__),
        )
        object.__setattr__(model, "__pydantic_extra__", fresh.__pydantic_extra__)
        object.__setattr__(model, "__pydantic_private__", fresh_private)
        _copy_provider_slots(fresh, model, provider_class)


def guard_model(
    model: BaseChatModel,
    metadata: ModelRuntimeMetadata | None = None,
    policy: ModelCallPolicy | None = None,
) -> BaseChatModel:
    """Build a provider-preserving guarded model from validated field values."""
    if isinstance(model, ModelCallGuardMixin):
        return model
    resolved_policy = policy or ModelCallPolicy.from_env()
    resolved_metadata = metadata or _extract_runtime_metadata(model)
    if resolved_metadata is None:
        raise UnsupportedModelOverrideError(provider="unknown")
    guarded_class = _guarded_provider_class(type(model))
    fields = _validated_provider_fields(model, resolved_metadata, resolved_policy)
    guarded = guarded_class(**fields)
    _initialize_guard_state(
        guarded,
        policy=resolved_policy,
        metadata=resolved_metadata,
    )
    return guarded


def build_guarded_provider_model(
    provider_class: type[BaseChatModel],
    provider_kwargs: Mapping[str, Any],
    metadata: ModelRuntimeMetadata,
    policy: ModelCallPolicy | None = None,
) -> BaseChatModel:
    """Construct a provider-native guarded model without a raw public interval."""
    if not issubclass(provider_class, BaseChatModel):
        raise TypeError("provider_class must inherit BaseChatModel")
    resolved_policy = policy or ModelCallPolicy.from_env()
    guarded_class = _guarded_provider_class(provider_class)
    guarded = guarded_class(**dict(provider_kwargs))
    _initialize_guard_state(
        guarded,
        policy=resolved_policy,
        metadata=metadata,
    )
    return guarded


def adapt_model_override(
    model: BaseChatModel,
    *,
    policy: ModelCallPolicy | None = None,
) -> BaseChatModel:
    """Adapt a raw model override once or return an existing guard unchanged."""
    if isinstance(model, ModelCallGuardMixin):
        return model
    metadata = _extract_runtime_metadata(model)
    if metadata is None:
        raise UnsupportedModelOverrideError(provider="unknown")
    return guard_model(model, metadata=metadata, policy=policy)


class ModelCallGuardMiddleware(AgentMiddleware):
    """Apply model-call protection to middleware model overrides exactly once."""

    def __init__(self, *, policy: ModelCallPolicy | None = None) -> None:
        """Create middleware with explicit or environment-derived policy."""
        self.policy = policy or ModelCallPolicy.from_env()

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        """Adapt sync request model exactly once before dispatch."""
        guarded = adapt_model_override(request.model, policy=self.policy)
        return handler(request.override(model=guarded))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        """Adapt async request model exactly once before dispatch."""
        guarded = adapt_model_override(request.model, policy=self.policy)
        return await handler(request.override(model=guarded))
