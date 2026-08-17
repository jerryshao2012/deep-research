"""Configuration and safe metadata for bounded model calls."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit, urlunsplit

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
        raise ValueError("base_url must be an absolute HTTP(S) URL with a hostname") from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL with a hostname")
    try:
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ValueError("base_url must be an absolute HTTP(S) URL with a hostname") from None
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
        timeout = float(value) if value is not None else DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    return timeout if math.isfinite(timeout) and timeout > 0 else DEFAULT_MODEL_CALL_TIMEOUT_SECONDS


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
        force_unload = source.get("OLLAMA_FORCE_UNLOAD_ON_CANCEL", "").strip().lower() == "true"
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

    def __init__(self, provider: str, timeout_seconds: float, unload_requested: bool) -> None:
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
    parent_cancellation_count = current_task.cancelling() if current_task is not None else 0
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=OLLAMA_UNLOAD_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        if not task.done():
            task.add_done_callback(_consume_task_exception)
        if current_task is not None and current_task.cancelling() > parent_cancellation_count:
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
    parent_cancellation_count = current_task.cancelling() if current_task is not None else 0
    try:
        if unload is None:
            await _maybe_unload_ollama(metadata=metadata, policy=policy)
            return
        try:
            operation = unload()
        except asyncio.CancelledError:
            if current_task is not None and current_task.cancelling() > parent_cancellation_count:
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
) -> Result:
    """Run a model request under a total deadline and bounded cancellation cleanup."""
    task = asyncio.create_task(factory())
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

        task.cancel()
        await _bounded_task_cleanup(task)
        await attempt_unload()
        raise ModelCallTimeoutError(
            provider=metadata.provider,
            timeout_seconds=policy.timeout_seconds,
            unload_requested=policy.force_ollama_unload and metadata.provider == "ollama",
        )
    except asyncio.CancelledError as original_cancel:
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
