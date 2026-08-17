import asyncio
import time
from dataclasses import FrozenInstanceError
from functools import wraps
from math import inf, nan
from typing import Any

import httpx
import pytest

from research_agent.model_call_guard import (
    DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
    MODEL_CANCEL_GRACE_SECONDS,
    OLLAMA_UNLOAD_TIMEOUT_SECONDS,
    ModelCallPolicy,
    ModelCallTimeoutError,
    ModelRuntimeMetadata,
    UnsupportedModelOverrideError,
    _maybe_unload_ollama,
    _run_with_deadline,
)


def _policy(timeout: float = 0.03, *, unload: bool = False) -> ModelCallPolicy:
    return ModelCallPolicy(timeout_seconds=timeout, force_ollama_unload=unload)


def _ollama_metadata(base_url: str | None = "http://localhost:11434") -> ModelRuntimeMetadata:
    return ModelRuntimeMetadata(provider="ollama", model_name="gemma4:latest", base_url=base_url)


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
async def test_run_with_deadline_cancels_request_within_bounded_cleanup_grace(monkeypatch):
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
async def test_run_with_deadline_propagates_external_cancellation_unchanged(monkeypatch):
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
        _run_with_deadline(pending, policy=_policy(1), metadata=_ollama_metadata(), unload=None)
    )
    await started.wait()
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert cancelled.is_set()


@_async_test
async def test_cancellation_suppressing_handler_cannot_exceed_cleanup_grace(monkeypatch):
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
            streaming_request, policy=_policy(), metadata=_ollama_metadata(), unload=None
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
            pending, policy=_policy(unload=False), metadata=_ollama_metadata(), unload=unload
        )
    assert unload_calls == 0


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

    assert requests == [
        (expected_url, {"model": "gemma4:latest", "keep_alive": 0})
    ]


@_async_test
async def test_cloud_provider_never_unloads_even_when_enabled():
    called = False

    async def post(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    await _maybe_unload_ollama(
        metadata=ModelRuntimeMetadata(provider="openai", model_name="gpt", base_url="https://api.openai.com"),
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
            pending, policy=_policy(unload=True), metadata=_ollama_metadata(), unload=unload
        )


@_async_test
async def test_sync_raising_unload_failure_preserves_timeout_error():
    def unload() -> Any:
        raise RuntimeError("unload failed before creating awaitable")

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending, policy=_policy(unload=True), metadata=_ollama_metadata(), unload=unload
        )


@_async_test
async def test_sync_cancelling_unload_failure_preserves_timeout_error():
    def unload() -> Any:
        raise asyncio.CancelledError("sync-unload")

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending, policy=_policy(unload=True), metadata=_ollama_metadata(), unload=unload
        )


@_async_test
async def test_self_cancelling_unload_failure_preserves_timeout_error():
    async def unload() -> None:
        raise asyncio.CancelledError

    async def pending() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelCallTimeoutError):
        await _run_with_deadline(
            pending, policy=_policy(unload=True), metadata=_ollama_metadata(), unload=unload
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
            pending, policy=_policy(1, unload=True), metadata=_ollama_metadata(), unload=unload
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
            pending, policy=_policy(1, unload=True), metadata=_ollama_metadata(), unload=unload
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
            pending, policy=_policy(1, unload=True), metadata=_ollama_metadata(), unload=unload
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
            pending, policy=_policy(1, unload=True), metadata=_ollama_metadata(), unload=unload
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
            pending, policy=_policy(1, unload=True), metadata=_ollama_metadata(), unload=unload
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
async def test_malformed_ollama_model_name_skips_unload_and_preserves_timeout(model_name):
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

    metadata = ModelRuntimeMetadata(provider="ollama", model_name="secret model", base_url=None)
    await _maybe_unload_ollama(metadata=metadata, policy=_policy(unload=True), post=post)
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
    assert ModelCallPolicy.from_env().timeout_seconds == DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", str(inf))
    assert ModelCallPolicy.from_env().timeout_seconds == DEFAULT_MODEL_CALL_TIMEOUT_SECONDS


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


@pytest.mark.parametrize("model_name", [
    "sk-proj-secret-token-123",
    "x" * 10000,
])
def test_unsupported_override_error_does_not_retain_model_name(model_name):
    error = UnsupportedModelOverrideError(provider="ollama", model_name=model_name)

    assert model_name not in str(error)
    assert model_name not in repr(error)
    assert model_name not in repr(error.args)
    assert model_name not in repr(vars(error))
    assert not hasattr(error, "model_name")
