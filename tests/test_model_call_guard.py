from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from research_agent.model_call_guard import (
    DEFAULT_MODEL_CALL_TIMEOUT_SECONDS,
    ModelCallPolicy,
    ModelCallTimeoutError,
    ModelRuntimeMetadata,
    UnsupportedModelOverrideError,
)


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


@pytest.mark.parametrize("value", ["0.001", "3", "300.5", "1e6"])
def test_finite_positive_timeout_is_accepted(monkeypatch, value):
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", value)

    assert ModelCallPolicy.from_env().timeout_seconds == float(value)


def test_non_finite_numeric_timeout_uses_safe_default(monkeypatch):
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", str(nan))
    assert ModelCallPolicy.from_env().timeout_seconds == DEFAULT_MODEL_CALL_TIMEOUT_SECONDS

    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", str(inf))
    assert ModelCallPolicy.from_env().timeout_seconds == DEFAULT_MODEL_CALL_TIMEOUT_SECONDS


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
    assert error.model_name is None
