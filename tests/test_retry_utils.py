"""Tests for rate limit retry utilities."""

# ruff: noqa: T201

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from research_agent import retry_utils
from research_agent.model_call_guard import (
    ModelCallGuardMixin,
    ModelCallPolicy,
    ModelCallTimeoutError,
    ModelRuntimeMetadata,
    guard_model,
)
from research_agent.retry_utils import (
    RetryConfig,
    calculate_backoff,
    is_rate_limit_error,
    retry_on_rate_limit,
    wrap_model_with_rate_limiting,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = time.monotonic()
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)
        await asyncio.sleep(0)


class _RateLimitedFakeModel(FakeMessagesListChatModel):
    attempts: int = 0

    async def _agenerate(self, *args, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("429 rate limit")
        return await super()._agenerate(*args, **kwargs)


class _TokenRecorder(BaseCallbackHandler):
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def on_llm_new_token(self, token: str, **_kwargs) -> None:
        self.tokens.append(token)


class _StreamEventRecorder(BaseCallbackHandler):
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.starts = 0
        self.ends = 0

    def on_chat_model_start(self, *_args: Any, **_kwargs: Any) -> None:
        self.starts += 1

    def on_stream_event(self, event: Any, **_kwargs: Any) -> None:
        self.events.append(event)

    def on_llm_end(self, *_args: Any, **_kwargs: Any) -> None:
        self.ends += 1


class _FailingStreamEventRecorder(_StreamEventRecorder):
    run_inline = True
    raise_error = True

    def on_stream_event(self, event: Any, **kwargs: Any) -> None:
        super().on_stream_event(event, **kwargs)
        raise RuntimeError("429 from visible callback")


class _CallbackThenRateLimitedModel(BaseChatModel):
    attempts: int = 0

    @property
    def _llm_type(self) -> str:
        return "callback-then-rate-limit"

    def _generate(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("guarded sync path must use provider async path")

    async def _agenerate(self, *args, run_manager=None, **kwargs):
        del args, kwargs
        self.attempts += 1
        if self.attempts == 1:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            await run_manager.on_llm_new_token("partial", chunk=chunk)
            raise RuntimeError("429 rate limit after visible token")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="phantom retry"))]
        )


class _StreamEventThenRateLimitedModel(BaseChatModel):
    attempts: int = 0
    emit_before_failure: bool

    @property
    def _llm_type(self) -> str:
        return "stream-event-then-rate-limit"

    def _generate(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("guarded sync path must use provider async path")

    async def _agenerate(self, *args, run_manager=None, **kwargs):
        del args, kwargs
        self.attempts += 1
        event = {"type": "content-block-delta", "delta": {"text": "partial"}}
        if self.attempts == 1:
            if self.emit_before_failure:
                await run_manager.on_stream_event(event)
            raise RuntimeError("429 rate limit around stream event")
        await run_manager.on_stream_event(event)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="recovered"))]
        )


class _StreamingRateLimitedModel(BaseChatModel):
    attempts: int = 0
    fail_after_first_chunk: bool

    @property
    def _llm_type(self) -> str:
        return "streaming-rate-limit"

    def _generate(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("guarded sync path must use provider async path")

    async def _astream(self, *args, **kwargs):
        del args, kwargs
        self.attempts += 1
        if self.attempts == 1 and not self.fail_after_first_chunk:
            raise RuntimeError("429 rate limit before first chunk")
        if self.attempts == 1:
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            raise RuntimeError("429 rate limit after visible chunk")
        yield ChatGenerationChunk(message=AIMessageChunk(content="recovered"))


def _guarded_ollama_for_retry():
    from langchain_ollama import ChatOllama

    return guard_model(
        ChatOllama(model="retry-test", base_url="http://localhost:11434"),
        metadata=ModelRuntimeMetadata(
            provider="ollama",
            model_name="retry-test",
            base_url="http://localhost:11434",
        ),
        policy=ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False),
    )


def _retry_controller(clock: _FakeClock):
    controller_class = getattr(retry_utils, "ModelRetryController")
    return controller_class(
        config=RetryConfig(
            max_retries=2,
            initial_backoff=2.0,
            max_backoff=2.0,
            backoff_multiplier=1.0,
            jitter=False,
        ),
        tpm=0,
        rpm=0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        async_sleep=clock.async_sleep,
    )


def _immediate_retry_controller(clock: _FakeClock):
    controller_class = getattr(retry_utils, "ModelRetryController")
    return controller_class(
        config=RetryConfig(
            max_retries=1,
            initial_backoff=0.0,
            max_backoff=0.0,
            backoff_multiplier=1.0,
            jitter=False,
        ),
        tpm=0,
        rpm=0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        async_sleep=clock.async_sleep,
    )


def _guarded_retry_model(raw: BaseChatModel, clock: _FakeClock):
    model = guard_model(
        raw,
        metadata=ModelRuntimeMetadata(provider="openai", model_name="retry-visible"),
        policy=ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False),
    )
    return wrap_model_with_rate_limiting(
        model,
        controller=_immediate_retry_controller(clock),
    )


class TestIsRateLimitError:
    """Test rate limit error detection."""

    def test_detects_rate_limit_strings(self):
        """Should detect various rate limit error messages."""
        assert is_rate_limit_error(Exception("Rate limit exceeded")) is True
        assert is_rate_limit_error(Exception("rate_limit")) is True
        assert is_rate_limit_error(Exception("Too many requests")) is True
        assert is_rate_limit_error(Exception("429")) is True
        assert is_rate_limit_error(Exception("Quota exceeded")) is True
        assert is_rate_limit_error(Exception("throttled")) is True

    def test_ignores_non_rate_limit_errors(self):
        """Should not retry on non-rate-limit errors."""
        assert is_rate_limit_error(Exception("Connection timeout")) is False
        assert is_rate_limit_error(Exception("Invalid API key")) is False
        assert is_rate_limit_error(Exception("Model not found")) is False

    def test_ignores_content_filter_errors(self):
        """Should NOT retry Azure content filter errors."""
        assert is_rate_limit_error(Exception("Content filter triggered")) is False
        assert is_rate_limit_error(Exception("content_filter violation")) is False
        assert is_rate_limit_error(Exception("ResponsibleAI policy")) is False


class TestCalculateBackoff:
    """Test backoff calculation."""

    def test_exponential_growth(self):
        """Backoff should grow exponentially."""
        b0 = calculate_backoff(0, 1.0, 60.0, 2.0, False)
        b1 = calculate_backoff(1, 1.0, 60.0, 2.0, False)
        b2 = calculate_backoff(2, 1.0, 60.0, 2.0, False)

        assert b0 == 1.0
        assert b1 == 2.0
        assert b2 == 4.0

    def test_respects_max_backoff(self):
        """Backoff should not exceed maximum."""
        backoff = calculate_backoff(10, 1.0, 60.0, 2.0, False)
        assert backoff <= 60.0

    def test_jitter_reduces_backoff(self):
        """With jitter, backoff should be between 50-100% of calculated value."""
        base = 10.0
        with patch("random.random", return_value=0.5):  # Middle of range
            backoff = calculate_backoff(0, base, 60.0, 1.0, True)
            assert base * 0.5 <= backoff <= base


class TestRetryOnRateLimit:
    """Test retry decorator."""

    def test_retries_on_rate_limit(self):
        """Should retry when rate limit error occurs."""
        call_count = 0

        @retry_on_rate_limit(max_retries=3, initial_backoff=0.01, jitter=False)
        def flaky_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Rate limit exceeded")
            return "success"

        result = flaky_function()
        assert result == "success"
        assert call_count == 3

    def test_raises_after_max_retries(self):
        """Should raise exception after exhausting retries."""
        call_count = 0

        @retry_on_rate_limit(max_retries=2, initial_backoff=0.01, jitter=False)
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise Exception("Rate limit exceeded")

        with pytest.raises(Exception, match="Rate limit exceeded"):
            always_fails()

        assert call_count == 3  # Initial + 2 retries

    def test_does_not_retry_non_rate_limit_errors(self):
        """Should immediately raise non-rate-limit errors."""
        call_count = 0

        @retry_on_rate_limit(max_retries=3, initial_backoff=0.01)
        def other_error():
            nonlocal call_count
            call_count += 1
            raise Exception("Invalid API key")

        with pytest.raises(Exception, match="Invalid API key"):
            other_error()

        assert call_count == 1  # Only called once, no retries

    @pytest.mark.anyio
    async def test_async_retry(self):
        """Should work with async functions."""
        call_count = 0

        @retry_on_rate_limit(max_retries=2, initial_backoff=0.01, jitter=False)
        async def async_flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Too many requests")
            return "async success"

        result = await async_flaky()
        assert result == "async success"
        assert call_count == 2


class TestRetryConfig:
    """Test configuration class."""

    def test_default_config(self):
        """Should use default values."""
        from research_agent import retry_utils
        config = RetryConfig()
        assert config.max_retries == retry_utils.MAX_RETRIES
        assert config.initial_backoff == retry_utils.INITIAL_BACKOFF
        assert config.max_backoff == retry_utils.MAX_BACKOFF
        assert config.backoff_multiplier == retry_utils.BACKOFF_MULTIPLIER
        assert config.jitter == retry_utils.JITTER_ENABLED

    def test_custom_config(self):
        """Should accept custom values."""
        config = RetryConfig(
            max_retries=10,
            initial_backoff=2.0,
            max_backoff=120.0,
            backoff_multiplier=1.5,
            jitter=False,
        )
        assert config.max_retries == 10
        assert config.initial_backoff == 2.0
        assert config.max_backoff == 120.0
        assert config.backoff_multiplier == 1.5
        assert config.jitter is False

    def test_from_env(self, monkeypatch):
        """Should read from environment variables."""
        monkeypatch.setenv("MODEL_MAX_RETRIES", "8")
        monkeypatch.setenv("MODEL_INITIAL_BACKOFF", "3.0")
        monkeypatch.setenv("MODEL_RETRY_JITTER", "false")

        config = RetryConfig.from_env()
        assert config.max_retries == 8
        assert config.initial_backoff == 3.0
        assert config.jitter is False


def test_wrapper_attaches_controller_without_replacing_guarded_model_methods() -> None:
    model = _guarded_ollama_for_retry()
    invoke_owner = model.invoke.__func__
    ainvoke_owner = model.ainvoke.__func__
    stream_owner = model.stream.__func__
    astream_owner = model.astream.__func__

    wrapped = wrap_model_with_rate_limiting(
        model,
        controller=_retry_controller(_FakeClock()),
    )

    assert wrapped is model
    assert isinstance(wrapped, ModelCallGuardMixin)
    assert wrapped.invoke.__func__ is invoke_owner is ModelCallGuardMixin.invoke
    assert wrapped.ainvoke.__func__ is ainvoke_owner is ModelCallGuardMixin.ainvoke
    assert wrapped.stream.__func__ is stream_owner is ModelCallGuardMixin.stream
    assert wrapped.astream.__func__ is astream_owner is ModelCallGuardMixin.astream
    assert "invoke" not in wrapped.__dict__
    assert "ainvoke" not in wrapped.__dict__
    assert wrapped._retry_controller is not None


def test_sync_retry_backoff_expires_at_original_deadline_without_later_attempt() -> None:
    clock = _FakeClock()
    model = _guarded_ollama_for_retry()
    wrap_model_with_rate_limiting(model, controller=_retry_controller(clock))
    attempts = 0

    def rate_limited() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("429 rate limit")

    with pytest.raises(ModelCallTimeoutError) as raised:
        model._retry_controller.invoke(
            rate_limited,
            deadline=clock.monotonic() + 1.0,
            input="must-not-appear",
        )

    assert attempts == 1
    assert clock.sleeps == [1.0]
    assert clock.monotonic() == pytest.approx(clock.now)
    assert raised.value.timeout_seconds == 1.0
    assert "must-not-appear" not in str(raised.value)


@pytest.mark.anyio
async def test_async_retry_backoff_expires_at_original_deadline_without_later_attempt() -> None:
    clock = _FakeClock()
    model = _guarded_ollama_for_retry()
    wrap_model_with_rate_limiting(model, controller=_retry_controller(clock))
    attempts = 0

    async def rate_limited() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("429 rate limit")

    with pytest.raises(ModelCallTimeoutError) as raised:
        await model._retry_controller.ainvoke(
            rate_limited,
            deadline=clock.monotonic() + 1.0,
            input="must-not-appear",
        )

    assert attempts == 1
    assert clock.sleeps == [1.0]
    assert raised.value.timeout_seconds == 1.0
    assert "must-not-appear" not in str(raised.value)


def test_proactive_rate_shaping_waits_only_for_binding_constraint() -> None:
    clock = _FakeClock()
    controller = retry_utils.ModelRetryController(
        config=RetryConfig(max_retries=0),
        tpm=100_000,
        rpm=60,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        async_sleep=clock.async_sleep,
    )
    model = _guarded_ollama_for_retry()
    wrap_model_with_rate_limiting(model, controller=controller)

    assert controller.invoke(
        lambda: "first",
        deadline=clock.monotonic() + 10,
        input="small",
        max_tokens=1,
    ) == "first"
    assert controller.invoke(
        lambda: "second",
        deadline=clock.monotonic() + 10,
        input="small",
        max_tokens=1,
    ) == "second"

    assert clock.sleeps == [pytest.approx(1.25)]


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_guard_executes_retry_controller_inside_one_public_deadline(method: str) -> None:
    clock = _FakeClock()
    raw = _RateLimitedFakeModel(responses=[AIMessage(content="retried")])
    model = guard_model(
        raw,
        metadata=ModelRuntimeMetadata(provider="openai", model_name="retry-hook"),
        policy=ModelCallPolicy(timeout_seconds=1.0, force_ollama_unload=False),
    )
    controller = retry_utils.ModelRetryController(
        config=RetryConfig(
            max_retries=1,
            initial_backoff=0.1,
            max_backoff=0.1,
            backoff_multiplier=1.0,
            jitter=False,
        ),
        tpm=0,
        rpm=0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        async_sleep=clock.async_sleep,
    )
    wrap_model_with_rate_limiting(model, controller=controller)

    if method == "invoke":
        result = model.invoke("hello")
    else:
        result = asyncio.run(model.ainvoke("hello"))

    assert result.content == "retried"
    assert model.attempts == 2
    assert clock.sleeps == [0.1]


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_visible_callback_token_prevents_non_stream_retry(method: str) -> None:
    clock = _FakeClock()
    callback = _TokenRecorder()
    model = _guarded_retry_model(_CallbackThenRateLimitedModel(), clock)

    with pytest.raises(RuntimeError, match="after visible token"):
        if method == "invoke":
            model.invoke("hello", config={"callbacks": [callback]})
        else:
            asyncio.run(model.ainvoke("hello", config={"callbacks": [callback]}))

    assert model.attempts == 1
    assert callback.tokens == ["partial"]
    assert clock.sleeps == []


def test_model_level_visible_callback_prevents_non_stream_retry() -> None:
    clock = _FakeClock()
    callback = _TokenRecorder()
    model = _guarded_retry_model(
        _CallbackThenRateLimitedModel(callbacks=[callback]),
        clock,
    )

    with pytest.raises(RuntimeError, match="after visible token"):
        model.invoke("hello")

    assert model.attempts == 1
    assert callback.tokens == ["partial"]
    assert clock.sleeps == []


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_visible_stream_event_prevents_non_stream_retry(method: str) -> None:
    clock = _FakeClock()
    callback = _StreamEventRecorder()
    model = _guarded_retry_model(
        _StreamEventThenRateLimitedModel(emit_before_failure=True),
        clock,
    )

    with pytest.raises(RuntimeError, match="around stream event"):
        if method == "invoke":
            model.invoke("hello", config={"callbacks": [callback]})
        else:
            asyncio.run(model.ainvoke("hello", config={"callbacks": [callback]}))

    assert model.attempts == 1
    assert callback.events == [
        {"type": "content-block-delta", "delta": {"text": "partial"}}
    ]
    assert clock.sleeps == []


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_failure_before_stream_event_can_retry_without_duplicate_event(
    method: str,
) -> None:
    clock = _FakeClock()
    callback = _StreamEventRecorder()
    model = _guarded_retry_model(
        _StreamEventThenRateLimitedModel(emit_before_failure=False),
        clock,
    )

    if method == "invoke":
        result = model.invoke("hello", config={"callbacks": [callback]})
    else:
        result = asyncio.run(model.ainvoke("hello", config={"callbacks": [callback]}))

    assert result.content == "recovered"
    assert model.attempts == 2
    assert callback.events == [
        {"type": "content-block-delta", "delta": {"text": "partial"}}
    ]
    assert clock.sleeps == [0.0]


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_v2_callback_shared_by_model_and_config_runs_once_per_event(method: str) -> None:
    clock = _FakeClock()
    callback = _StreamEventRecorder()
    model = _guarded_retry_model(
        _StreamEventThenRateLimitedModel(
            callbacks=[callback],
            emit_before_failure=False,
            attempts=1,
        ),
        clock,
    )

    if method == "invoke":
        result = model.invoke("hello", config={"callbacks": [callback]})
    else:
        result = asyncio.run(
            model.ainvoke("hello", config={"callbacks": [callback]})
        )

    assert result.content == "recovered"
    assert callback.starts == 1
    assert callback.events == [
        {"type": "content-block-delta", "delta": {"text": "partial"}}
    ]
    assert callback.ends == 1


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_stream_event_is_visible_before_user_callback_can_fail(method: str) -> None:
    clock = _FakeClock()
    callback = _FailingStreamEventRecorder()
    model = _guarded_retry_model(
        _StreamEventThenRateLimitedModel(emit_before_failure=True),
        clock,
    )

    with pytest.raises(RuntimeError, match="visible callback"):
        if method == "invoke":
            model.invoke("hello", config={"callbacks": [callback]})
        else:
            asyncio.run(model.ainvoke("hello", config={"callbacks": [callback]}))

    assert model.attempts == 1
    assert callback.events == [
        {"type": "content-block-delta", "delta": {"text": "partial"}}
    ]
    assert clock.sleeps == []


def test_sync_stream_retries_rate_limit_before_first_visible_chunk() -> None:
    clock = _FakeClock()
    model = _guarded_retry_model(
        _StreamingRateLimitedModel(fail_after_first_chunk=False),
        clock,
    )

    chunks = list(model.stream("hello"))

    assert chunks[0].content == "recovered"
    assert model.attempts == 2
    assert clock.sleeps == [0.0]


@pytest.mark.anyio
async def test_async_stream_retries_rate_limit_before_first_visible_chunk() -> None:
    clock = _FakeClock()
    model = _guarded_retry_model(
        _StreamingRateLimitedModel(fail_after_first_chunk=False),
        clock,
    )

    chunks = [chunk async for chunk in model.astream("hello")]

    assert chunks[0].content == "recovered"
    assert model.attempts == 2
    assert clock.sleeps == [0.0]


def test_sync_stream_does_not_retry_after_first_visible_chunk() -> None:
    clock = _FakeClock()
    callback = _TokenRecorder()
    model = _guarded_retry_model(
        _StreamingRateLimitedModel(fail_after_first_chunk=True),
        clock,
    )

    stream = model.stream("hello", config={"callbacks": [callback]})
    assert next(stream).content == "partial"
    with pytest.raises(RuntimeError, match="after visible chunk"):
        next(stream)

    assert model.attempts == 1
    assert callback.tokens == ["partial"]
    assert clock.sleeps == []


@pytest.mark.anyio
async def test_async_stream_does_not_retry_after_first_visible_chunk() -> None:
    clock = _FakeClock()
    callback = _TokenRecorder()
    model = _guarded_retry_model(
        _StreamingRateLimitedModel(fail_after_first_chunk=True),
        clock,
    )

    stream = model.astream("hello", config={"callbacks": [callback]})
    first = await anext(stream)
    assert first.content == "partial"
    with pytest.raises(RuntimeError, match="after visible chunk"):
        await anext(stream)

    assert model.attempts == 1
    assert callback.tokens == ["partial"]
    assert clock.sleeps == []


def run_verification():
    """Run verification tests with detailed output."""
    print("=" * 70)
    print("Rate Limit Retry Utilities - Verification Tests")
    print("=" * 70)

    try:
        # Test 1: Rate limit detection
        print("\nTesting rate limit error detection...")
        assert is_rate_limit_error(Exception("Rate limit exceeded")) is True
        assert is_rate_limit_error(Exception("429 Too Many Requests")) is True
        assert is_rate_limit_error(Exception("Quota exceeded")) is True
        assert is_rate_limit_error(Exception("Invalid API key")) is False
        assert is_rate_limit_error(Exception("Connection timeout")) is False
        assert is_rate_limit_error(Exception("Content filter triggered")) is False
        print("✅ Rate limit detection works correctly")

        # Test 2: Backoff calculation
        print("\nTesting backoff calculation...")
        b0 = calculate_backoff(0, 1.0, 60.0, 2.0, False)
        b1 = calculate_backoff(1, 1.0, 60.0, 2.0, False)
        b2 = calculate_backoff(2, 1.0, 60.0, 2.0, False)
        assert b0 == 1.0
        assert b1 == 2.0
        assert b2 == 4.0
        b_large = calculate_backoff(100, 1.0, 60.0, 2.0, False)
        assert b_large <= 60.0
        print(f"✅ Backoff calculation correct: {b0}s → {b1}s → {b2}s (capped at 60s)")

        # Test 3: Retry decorator
        print("\nTesting retry decorator...")
        call_count = 0

        @retry_on_rate_limit(max_retries=3, initial_backoff=0.01, jitter=False)
        def flaky_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Rate limit exceeded")
            return "success"

        result = flaky_function()
        assert result == "success"
        assert call_count == 3
        print(f"✅ Retry decorator works (retried {call_count - 1} times before success)")

        # Test 4: Non-rate-limit errors
        print("\nTesting non-rate-limit error handling...")
        call_count = 0

        @retry_on_rate_limit(max_retries=3, initial_backoff=0.01)
        def other_error():
            nonlocal call_count
            call_count += 1
            raise Exception("Invalid API key")

        try:
            other_error()
            assert False, "Should have raised exception"
        except Exception as e:
            assert "Invalid API key" in str(e)
            assert call_count == 1
        print("✅ Non-rate-limit errors are not retried (called only once)")

        # Test 5: Configuration
        print("\nTesting configuration...")
        config = RetryConfig(
            max_retries=10,
            initial_backoff=2.0,
            max_backoff=120.0,
            backoff_multiplier=1.5,
            jitter=False,
        )
        assert config.max_retries == 10
        assert config.initial_backoff == 2.0
        assert config.max_backoff == 120.0
        assert config.backoff_multiplier == 1.5
        assert config.jitter is False
        print("✅ Configuration works correctly")

        print("\n" + "=" * 70)
        print("✅ ALL TESTS PASSED!")
        print("=" * 70)
        print("\nThe retry mechanism is working correctly.")
        print("Rate limit errors will now be automatically handled with retries.")
        return 0

    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
