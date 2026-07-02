"""Tests for rate limit retry utilities."""

from unittest.mock import patch

import pytest

from retry_utils import (
    RetryConfig,
    calculate_backoff,
    is_rate_limit_error,
    retry_on_rate_limit,
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
        import retry_utils
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
