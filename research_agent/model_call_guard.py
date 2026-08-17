"""Configuration and safe metadata for bounded model calls."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import logging
import math
import os
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

import httpx
from langchain.agents.middleware import AgentMiddleware, ModelRequest
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


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
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


class BridgeRegistry:
    """Thread-safe collection of synchronous bridges grouped by run scope."""

    def __init__(self) -> None:
        """Create an empty registry guarded by a standard thread lock."""
        self._lock = threading.Lock()
        self._controls: dict[str, set[_BridgeControl[Any]]] = {}
        self._cancelling: dict[str, int] = {}

    def register(self, control: _BridgeControl[Any]) -> bool:
        """Register unless cancellation is currently active for the scope."""
        with self._lock:
            if control.scope_id in self._cancelling:
                return False
            self._controls.setdefault(control.scope_id, set()).add(control)
            return True

    def unregister(self, control: _BridgeControl[Any]) -> None:
        """Forget a completed bridge and prune its empty scope."""
        with self._lock:
            controls = self._controls.get(control.scope_id)
            if controls is None:
                return
            controls.discard(control)
            if not controls:
                self._controls.pop(control.scope_id, None)

    def active_count(self, scope_id: str) -> int:
        """Return active controls for tests and lifecycle diagnostics."""
        with self._lock:
            return len(self._controls.get(scope_id, ()))

    def cancel_scope(self, scope_id: str) -> None:
        """Cancel every bridge in a scope against one shared join grace."""
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


class _BridgeControl[ResultT]:
    """One daemon event-loop thread used to execute an async provider path."""

    def __init__(
        self,
        factory: Callable[[], Awaitable[ResultT]],
        *,
        scope_id: str,
        registry: BridgeRegistry,
    ) -> None:
        self.scope_id = scope_id
        self.results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._factory = factory
        self._registry = registry
        self._lock = threading.Lock()
        self._cancel_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._globally_registered = False
        self._locally_registered = False
        self._thread_started = threading.Event()
        self._context = contextvars.copy_context()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"model-call-bridge-{scope_id}",
            daemon=True,
        )

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
            self._thread.start()
            self._thread_started.set()
        except BaseException:
            self._thread_started.set()
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
        if threading.current_thread() is self._thread:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        if not self._thread_started.wait(max(0.0, timeout)):
            return
        if self._thread.ident is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def execute() -> None:
            try:
                self.results.put(("result", await self._factory()))
            except BaseException as exc:
                self.results.put(("error", exc))

        try:
            with self._lock:
                self._loop = loop
                self._task = self._context.run(loop.create_task, execute())
                cancel_requested = self._cancel_requested
                task = self._task
            if cancel_requested:
                task.cancel()
            try:
                loop.run_until_complete(task)
            except BaseException as exc:
                # Cancellation can land before execute() reaches its own handler.
                self.results.put(("error", exc))
        finally:
            with self._lock:
                self._task = None
            if self._locally_registered:
                self._registry.unregister(self)
            if self._globally_registered:
                _GLOBAL_BRIDGE_REGISTRY.unregister(self)
            pending = {
                pending for pending in asyncio.all_tasks(loop) if not pending.done()
            }
            if pending:

                def stop_after_late_cleanup(_task: asyncio.Task[Any]) -> None:
                    if all(task.done() for task in pending):
                        loop.stop()

                for pending_task in pending:
                    pending_task.add_done_callback(stop_after_late_cleanup)
                loop.run_forever()
            with self._lock:
                self._loop = None
            loop.close()


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
) -> ResultT:
    """Wait for one bridge result while retaining bounded interrupt cleanup."""
    try:
        wait = max(0.0, deadline - time.monotonic()) + MODEL_CANCEL_GRACE_SECONDS
        if policy.force_ollama_unload and metadata.provider == "ollama":
            wait += OLLAMA_UNLOAD_TIMEOUT_SECONDS
        kind, value = control.results.get(timeout=wait)
    except KeyboardInterrupt:
        control.cancel()
        control.join(MODEL_CANCEL_GRACE_SECONDS)
        raise
    except queue.Empty:
        control.cancel()
        control.join(MODEL_CANCEL_GRACE_SECONDS)
        raise _timeout_error(policy, metadata) from None
    if not control._thread.is_alive():
        control.join(0)
    if kind == "error":
        raise value
    return value


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
    ) -> None:
        self._deadline = deadline
        self._closed = False
        self._policy = policy
        self._metadata = metadata

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
            kind, value = self._control.results.get(timeout=wait)
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

    def __del__(self) -> None:
        self.close()


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
        control = _start_bridge(
            lambda: _run_with_absolute_deadline(
                lambda: self.bound.ainvoke(input, config=config, **kwargs),
                deadline=deadline,
                scope_id=scope_id,
                policy=self._owner._model_call_policy,
                metadata=self._owner._runtime_metadata,
            ),
            scope_id=scope_id,
            registry=self._owner._bridge_registry,
        )
        return _bridge_result(
            control,
            deadline,
            policy=self._owner._model_call_policy,
            metadata=self._owner._runtime_metadata,
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
        return _SyncStreamIterator(
            lambda: self._astream_with_deadline(
                input,
                config=config,
                deadline=deadline,
                scope_id=scope_id,
                **kwargs,
            ),
            deadline=deadline,
            scope_id=scope_id,
            registry=self._owner._bridge_registry,
            policy=self._owner._model_call_policy,
            metadata=self._owner._runtime_metadata,
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
        policy = self._model_call_policy
        deadline = _capture_deadline(policy)
        scope_id = _scope_id_from_config(config)
        control = _start_bridge(
            lambda: self._guarded_ainvoke(
                input,
                config=config,
                stop=stop,
                deadline=deadline,
                scope_id=scope_id,
                **kwargs,
            ),
            scope_id=scope_id,
            registry=self._bridge_registry,
        )
        return _bridge_result(
            control,
            deadline,
            policy=self._model_call_policy,
            metadata=self._runtime_metadata,
        )

    def ainvoke(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Awaitable[Any]:
        """Capture deadline eagerly and return guarded provider coroutine."""
        deadline = _capture_deadline(self._model_call_policy)
        scope_id = _scope_id_from_config(config)
        return self._guarded_ainvoke(
            input,
            config=config,
            stop=stop,
            deadline=deadline,
            scope_id=scope_id,
            **kwargs,
        )

    async def _guarded_ainvoke(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None,
        *,
        stop: list[str] | None,
        deadline: float,
        scope_id: str,
        **kwargs: Any,
    ) -> Any:
        async def provider_call() -> Any:
            return await super(ModelCallGuardMixin, self).ainvoke(
                input, config=config, stop=stop, **kwargs
            )

        controller = self._retry_controller
        if controller is None:
            operation = provider_call
        else:

            async def operation() -> Any:
                return await controller.ainvoke(
                    provider_call,
                    deadline=deadline,
                    input=input,
                    max_tokens=kwargs.get("max_tokens", 1000) or 1000,
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
        deadline = _capture_deadline(self._model_call_policy)
        scope_id = _scope_id_from_config(config)
        return _SyncStreamIterator(
            lambda: self._guarded_astream(
                input,
                config=config,
                stop=stop,
                deadline=deadline,
                scope_id=scope_id,
                **kwargs,
            ),
            deadline=deadline,
            scope_id=scope_id,
            registry=self._bridge_registry,
            policy=self._model_call_policy,
            metadata=self._runtime_metadata,
        )

    def astream(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Capture one eager deadline for the complete async stream."""
        deadline = _capture_deadline(self._model_call_policy)
        scope_id = _scope_id_from_config(config)
        return self._guarded_astream(
            input,
            config=config,
            stop=stop,
            deadline=deadline,
            scope_id=scope_id,
            **kwargs,
        )

    async def _guarded_astream(
        self: BaseChatModel,
        input: Any,
        config: RunnableConfig | None,
        *,
        stop: list[str] | None,
        deadline: float,
        scope_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        iterator = super().astream(input, config=config, stop=stop, **kwargs)
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
        return (
            rebuild_guarded_model,
            (
                _provider_native_pickle_view(self),
                self._runtime_metadata,
                self._model_call_policy,
            ),
        )

    def model_copy(
        self: BaseChatModel,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> BaseChatModel:
        """Copy provider fields while initializing independent guard state."""
        copied = super().model_copy(update=update, deep=False)
        if deep:
            private = dict(copied.__pydantic_private__ or {})
            for name in _GUARD_PRIVATE_ATTR_NAMES:
                private.pop(name, None)
            object.__setattr__(copied, "__pydantic_private__", private)
            copied = super(ModelCallGuardMixin, copied).model_copy(deep=True)
        metadata = _extract_runtime_metadata(copied) or self._runtime_metadata
        policy = self._model_call_policy
        if deep:
            metadata = copy.deepcopy(metadata)
            policy = copy.deepcopy(policy)
        _initialize_guard_state(
            copied,
            policy=policy,
            metadata=metadata,
        )
        return copied


_GUARD_PRIVATE_ATTR_NAMES = (
    "_model_call_policy",
    "_runtime_metadata",
    "_bridge_registry",
    "_retry_controller",
)
_GUARDED_PROVIDER_CLASSES: dict[type[BaseChatModel], type[BaseChatModel]] = {}
_GUARDED_PROVIDER_CLASSES_LOCK = threading.Lock()


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
        }
        guarded = type(
            generated_name,
            (ModelCallGuardMixin, provider_class),
            namespace,
        )
        _GUARDED_PROVIDER_CLASSES[provider_class] = guarded
        return guarded


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


def rebuild_guarded_model(
    provider: BaseChatModel,
    metadata: ModelRuntimeMetadata,
    policy: ModelCallPolicy,
) -> BaseChatModel:
    """Reconstruct a guarded model from one natively serialized provider."""
    guarded = guard_model(provider, metadata=metadata, policy=policy)
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


class _BorrowedSyncTransport(httpx.BaseTransport):
    """Delegate requests without taking ownership of a caller's transport."""

    def __init__(self, transport: httpx.BaseTransport) -> None:
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._transport.handle_request(request)

    def close(self) -> None:
        pass


class _BorrowedAsyncTransport(httpx.AsyncBaseTransport):
    """Delegate requests without taking ownership of a caller's async transport."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        pass


def _bounded_httpx_client(
    client: httpx.Client, policy: ModelCallPolicy
) -> httpx.Client:
    mounts = {
        pattern.pattern: (
            None if transport is None else _BorrowedSyncTransport(transport)
        )
        for pattern, transport in client._mounts.items()
    }
    return httpx.Client(
        auth=client._auth,
        params=client.params,
        headers=client.headers,
        cookies=client.cookies,
        trust_env=client._trust_env,
        mounts=mounts,
        timeout=_bounded_native_timeout(client.timeout, policy),
        follow_redirects=client.follow_redirects,
        max_redirects=client.max_redirects,
        event_hooks={name: list(hooks) for name, hooks in client.event_hooks.items()},
        base_url=client.base_url,
        transport=_BorrowedSyncTransport(client._transport),
        default_encoding=client._default_encoding,
    )


def _bounded_httpx_async_client(
    client: httpx.AsyncClient, policy: ModelCallPolicy
) -> httpx.AsyncClient:
    mounts = {
        pattern.pattern: (
            None if transport is None else _BorrowedAsyncTransport(transport)
        )
        for pattern, transport in client._mounts.items()
    }
    return httpx.AsyncClient(
        auth=client._auth,
        params=client.params,
        headers=client.headers,
        cookies=client.cookies,
        trust_env=client._trust_env,
        mounts=mounts,
        timeout=_bounded_native_timeout(client.timeout, policy),
        follow_redirects=client.follow_redirects,
        max_redirects=client.max_redirects,
        event_hooks={name: list(hooks) for name, hooks in client.event_hooks.items()},
        base_url=client.base_url,
        transport=_BorrowedAsyncTransport(client._transport),
        default_encoding=client._default_encoding,
    )


def _validated_provider_fields(
    model: BaseChatModel,
    metadata: ModelRuntimeMetadata,
    policy: ModelCallPolicy,
) -> dict[str, Any]:
    fields = {
        name: getattr(model, name)
        for name in type(model).model_fields
        if hasattr(model, name)
    }
    if metadata.provider == "ollama":
        for name in ("client_kwargs", "async_client_kwargs", "sync_client_kwargs"):
            native = dict(fields.get(name) or {})
            native["timeout"] = _bounded_native_timeout(native.get("timeout"), policy)
            fields[name] = native
    elif metadata.provider in {"openai", "azure_openai"}:
        fields["request_timeout"] = _bounded_native_timeout(
            fields.get("request_timeout"), policy
        )
        for name in ("client", "async_client", "root_client", "root_async_client"):
            fields.pop(name, None)
        http_client = getattr(model, "http_client", None)
        http_async_client = getattr(model, "http_async_client", None)
        if isinstance(http_client, httpx.Client):
            fields["http_client"] = _bounded_httpx_client(http_client, policy)
        if isinstance(http_async_client, httpx.AsyncClient):
            fields["http_async_client"] = _bounded_httpx_async_client(
                http_async_client, policy
            )
    elif metadata.provider == "anthropic":
        fields["default_request_timeout"] = _bounded_native_timeout(
            fields.get("default_request_timeout"), policy
        )
    elif metadata.provider == "google":
        fields["timeout"] = _bounded_native_timeout(fields.get("timeout"), policy)
    return fields


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
