"""Contracts for guarded, deadline-aware model construction."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from research_agent import model_factory, retry_utils
from research_agent.model_call_guard import (
    ModelCallGuardMixin,
    ModelRuntimeMetadata,
)


class _MissingRetryController:
    """Sentinel used only while the new controller contract is RED."""


ModelRetryController = getattr(
    retry_utils, "ModelRetryController", _MissingRetryController
)


_PROVIDER_ENV = {
    "AWS_BEDROCK_ENDPOINT",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_AUTH_TYPE",
    "AZURE_CLIENT_ID",
    "AZURE_OPENAI_SCOPE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_URL",
    "ANTHROPIC_BASE_URL",
    "OLLAMA_API_BASE",
    "OLLAMA_REASONING",
    "MODEL_NAME",
    "MODEL_MAX_RETRIES",
}


@pytest.fixture(autouse=True)
def isolated_model_environment(monkeypatch: pytest.MonkeyPatch):
    """Keep provider selection and cache independent from developer env."""
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("MODEL_TPM", "0")
    monkeypatch.setenv("MODEL_RPM", "0")
    monkeypatch.setattr(model_factory, "get_ssl_verify_config", lambda: True)
    model_factory.clear_model_cache()
    yield
    model_factory.clear_model_cache()


def _timeout_values(value: Any) -> list[float]:
    if isinstance(value, httpx.Timeout):
        return [value.connect, value.read, value.write, value.pool]
    if isinstance(value, tuple):
        return [float(item) for item in value]
    return [float(value)]


def _assert_common_model_contract(model: Any, expected: ModelRuntimeMetadata) -> None:
    assert isinstance(model, ModelCallGuardMixin)
    assert model._runtime_metadata == expected
    assert model._model_call_policy.timeout_seconds == 0.2
    assert isinstance(model._retry_controller, ModelRetryController)
    assert "secret-test-value" not in repr(model.model_dump())


@pytest.mark.parametrize(
    ("environment", "provider_class", "metadata", "native_kind"),
    [
        (
            {
                "AWS_BEDROCK_ENDPOINT": "https://bedrock.example.test/v1",
                "AWS_BEARER_TOKEN_BEDROCK": "secret-test-value",
                "MODEL_NAME": "bedrock-model",
            },
            "ChatOpenAI",
            ModelRuntimeMetadata(
                provider="aws_bedrock",
                model_name="bedrock-model",
                base_url="https://bedrock.example.test/v1",
            ),
            "openai",
        ),
        (
            {
                "AZURE_OPENAI_ENDPOINT": "https://legacy.openai.azure.test",
                "AZURE_OPENAI_DEPLOYMENT": "legacy-deployment",
                "AZURE_OPENAI_API_KEY": "secret-test-value",
                "AZURE_OPENAI_API_VERSION": "2024-10-21",
            },
            "AzureChatOpenAI",
            ModelRuntimeMetadata(
                provider="azure_openai",
                model_name="legacy-deployment",
                base_url="https://legacy.openai.azure.test",
            ),
            "openai",
        ),
        (
            {
                "AZURE_OPENAI_ENDPOINT": "https://current.openai.azure.test/v1",
                "AZURE_OPENAI_DEPLOYMENT": "current-deployment",
                "AZURE_OPENAI_API_KEY": "secret-test-value",
            },
            "ChatOpenAI",
            ModelRuntimeMetadata(
                provider="azure_openai",
                model_name="current-deployment",
                base_url="https://current.openai.azure.test/v1",
            ),
            "openai",
        ),
        (
            {
                "OPENAI_API_KEY": "secret-test-value",
                "OPENAI_BASE_URL": "https://openai.example.test/v1",
                "MODEL_NAME": "openai-model",
            },
            "ChatOpenAI",
            ModelRuntimeMetadata(
                provider="openai",
                model_name="openai-model",
                base_url="https://openai.example.test/v1",
            ),
            "openai",
        ),
        (
            {
                "GOOGLE_API_KEY": "secret-test-value",
                "MODEL_NAME": "gemini-test",
            },
            "ChatGoogleGenerativeAI",
            ModelRuntimeMetadata(provider="google", model_name="gemini-test"),
            "google",
        ),
        (
            {
                "ANTHROPIC_API_KEY": "secret-test-value",
                "MODEL_NAME": "claude-test",
            },
            "ChatAnthropic",
            ModelRuntimeMetadata(provider="anthropic", model_name="claude-test"),
            "anthropic",
        ),
        (
            {
                "OLLAMA_API_BASE": "http://ollama.example.test:11434",
                "MODEL_NAME": "ollama-test:latest",
            },
            "ChatOllama",
            ModelRuntimeMetadata(
                provider="ollama",
                model_name="ollama-test:latest",
                base_url="http://ollama.example.test:11434",
            ),
            "ollama",
        ),
    ],
)
def test_factory_builds_provider_identifiable_guard_with_native_timeout(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    provider_class: str,
    metadata: ModelRuntimeMetadata,
    native_kind: str,
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    model = model_factory.get_configured_model(bypass_cache=True)

    assert any(cls.__name__ == provider_class for cls in type(model).__mro__)
    _assert_common_model_contract(model, metadata)
    if native_kind == "openai":
        assert model.max_retries == 0
        assert model.root_client.max_retries == 0
        assert model.root_async_client.max_retries == 0
        assert max(_timeout_values(model.request_timeout)) <= 0.2
        assert max(_timeout_values(model.http_client.timeout)) <= 0.2
        assert max(_timeout_values(model.http_async_client.timeout)) <= 0.2
    elif native_kind == "google":
        assert model.max_retries == 0
        assert model.timeout == 0.2
        assert model.client is not None
        http_options = model.client._api_client._http_options
        assert http_options.client_args["timeout"] == 0.2
        assert http_options.client_args["verify"] is True
        assert http_options.async_client_args["timeout"] == 0.2
        assert http_options.async_client_args["verify"] is True
    elif native_kind == "anthropic":
        assert model.max_retries == 0
        assert model._client.max_retries == 0
        assert model._async_client.max_retries == 0
        assert model.default_request_timeout == 0.2
    else:
        assert model.client_kwargs["timeout"] == 0.2
        assert model.sync_client_kwargs["timeout"] == 0.2
        assert model.async_client_kwargs["timeout"] == 0.2


def test_factory_preserves_provider_precedence_with_mixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mixed = {
        "AWS_BEDROCK_ENDPOINT": "https://bedrock-first.example.test/v1",
        "AWS_BEARER_TOKEN_BEDROCK": "secret-test-value",
        "AZURE_OPENAI_ENDPOINT": "https://azure-second.example.test",
        "AZURE_OPENAI_DEPLOYMENT": "azure-model",
        "AZURE_OPENAI_API_KEY": "secret-test-value",
        "AZURE_OPENAI_API_VERSION": "2024-10-21",
        "OPENAI_API_KEY": "secret-test-value",
        "GOOGLE_API_KEY": "secret-test-value",
        "ANTHROPIC_API_KEY": "secret-test-value",
        "OLLAMA_API_BASE": "http://ollama-last.example.test:11434",
        "MODEL_NAME": "first-model",
    }
    for name, value in mixed.items():
        monkeypatch.setenv(name, value)

    model = model_factory.get_configured_model(bypass_cache=True)

    assert model._runtime_metadata == ModelRuntimeMetadata(
        provider="aws_bedrock",
        model_name="first-model",
        base_url="https://bedrock-first.example.test/v1",
    )


@pytest.mark.parametrize(
    ("provider_environment", "provider_class", "metadata"),
    [
        (
            {
                "AWS_BEDROCK_ENDPOINT": "https://bedrock.example.test/v1",
                "AWS_BEARER_TOKEN_BEDROCK": "secret-test-value",
            },
            "ChatOpenAI",
            ModelRuntimeMetadata(
                provider="aws_bedrock",
                model_name="precedence-model",
                base_url="https://bedrock.example.test/v1",
            ),
        ),
        (
            {
                "AZURE_OPENAI_ENDPOINT": "https://legacy.openai.azure.test",
                "AZURE_OPENAI_DEPLOYMENT": "legacy-deployment",
                "AZURE_OPENAI_API_KEY": "secret-test-value",
                "AZURE_OPENAI_API_VERSION": "2024-10-21",
            },
            "AzureChatOpenAI",
            ModelRuntimeMetadata(
                provider="azure_openai",
                model_name="legacy-deployment",
                base_url="https://legacy.openai.azure.test",
            ),
        ),
        (
            {
                "AZURE_OPENAI_ENDPOINT": "https://current.openai.azure.test/v1",
                "AZURE_OPENAI_DEPLOYMENT": "current-deployment",
                "AZURE_OPENAI_API_KEY": "secret-test-value",
            },
            "ChatOpenAI",
            ModelRuntimeMetadata(
                provider="azure_openai",
                model_name="current-deployment",
                base_url="https://current.openai.azure.test/v1",
            ),
        ),
        (
            {"GOOGLE_API_KEY": "secret-test-value"},
            "ChatGoogleGenerativeAI",
            ModelRuntimeMetadata(provider="google", model_name="precedence-model"),
        ),
        (
            {"ANTHROPIC_API_KEY": "secret-test-value"},
            "ChatAnthropic",
            ModelRuntimeMetadata(provider="anthropic", model_name="precedence-model"),
        ),
        (
            {"OLLAMA_API_BASE": "http://ollama.example.test:11434"},
            "ChatOllama",
            ModelRuntimeMetadata(
                provider="ollama",
                model_name="precedence-model",
                base_url="http://ollama.example.test:11434",
            ),
        ),
    ],
)
def test_existing_provider_wins_pairwise_over_standalone_openai(
    monkeypatch: pytest.MonkeyPatch,
    provider_environment: dict[str, str],
    provider_class: str,
    metadata: ModelRuntimeMetadata,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-value")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai-fallback.example.test/v1")
    monkeypatch.setenv("MODEL_NAME", "precedence-model")
    for name, value in provider_environment.items():
        monkeypatch.setenv(name, value)

    model = model_factory.get_configured_model(bypass_cache=True)

    assert any(cls.__name__ == provider_class for cls in type(model).__mro__)
    assert model._runtime_metadata == metadata


def test_standalone_openai_is_fallback_when_no_existing_provider_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-value")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai-fallback.example.test/v1")
    monkeypatch.setenv("MODEL_NAME", "openai-fallback-model")

    model = model_factory.get_configured_model(bypass_cache=True)

    assert any(cls.__name__ == "ChatOpenAI" for cls in type(model).__mro__)
    _assert_common_model_contract(
        model,
        ModelRuntimeMetadata(
            provider="openai",
            model_name="openai-fallback-model",
            base_url="https://openai-fallback.example.test/v1",
        ),
    )


@pytest.mark.parametrize("configured_retries", [0, 3])
def test_controller_owns_configured_attempts_while_native_retries_stay_disabled(
    monkeypatch: pytest.MonkeyPatch,
    configured_retries: int,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-value")
    monkeypatch.setenv("MODEL_NAME", "retry-owner-model")
    monkeypatch.setenv("MODEL_MAX_RETRIES", str(configured_retries))

    model = model_factory.get_configured_model(bypass_cache=True)

    assert model.max_retries == 0
    assert model.root_client.max_retries == 0
    assert model.root_async_client.max_retries == 0
    assert model._retry_controller.config.max_retries == configured_retries


@pytest.mark.parametrize(
    ("provider_environment", "provider", "provider_url_attribute"),
    [
        (
            {"OPENAI_API_KEY": "secret-test-value"},
            "openai",
            "openai_api_base",
        ),
        (
            {"ANTHROPIC_API_KEY": "secret-test-value"},
            "anthropic",
            "anthropic_api_url",
        ),
    ],
)
@pytest.mark.parametrize("blank_url", ["", "   \t"])
def test_optional_provider_base_url_normalizes_blank_to_none(
    monkeypatch: pytest.MonkeyPatch,
    provider_environment: dict[str, str],
    provider: str,
    provider_url_attribute: str,
    blank_url: str,
) -> None:
    for name, value in provider_environment.items():
        monkeypatch.setenv(name, value)
    url_variable = (
        "OPENAI_BASE_URL" if provider == "openai" else "ANTHROPIC_API_URL"
    )
    monkeypatch.setenv(url_variable, blank_url)
    monkeypatch.setenv("MODEL_NAME", "blank-url-model")

    model = model_factory.get_configured_model(bypass_cache=True)

    assert model._runtime_metadata == ModelRuntimeMetadata(
        provider=provider,
        model_name="blank-url-model",
        base_url=None,
    )
    provider_url = getattr(model, provider_url_attribute, None)
    assert provider_url is None or str(provider_url).strip()


def test_cached_model_keeps_construction_policy_until_cache_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")
    monkeypatch.setenv("MODEL_NAME", "cached-model")

    first = model_factory.get_configured_model()
    monkeypatch.setenv("MODEL_CALL_TIMEOUT_SECONDS", "0.01")
    cached = model_factory.get_configured_model()
    bypassed = model_factory.get_configured_model(bypass_cache=True)

    assert cached is first
    assert cached._model_call_policy.timeout_seconds == 0.2
    assert bypassed is not first
    assert bypassed._model_call_policy.timeout_seconds == 0.01


def _configure_ollama(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
) -> Any:
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")
    monkeypatch.setenv("MODEL_NAME", model_name)
    return model_factory.get_configured_model(bypass_cache=True)


@pytest.mark.parametrize(
    "model_name",
    [
        "gemma4",
        "  GeMmA4  ",
        "team/gemma4:27b",
        " REGISTRY/TEAM/GEMMA4:LATEST ",
        "gemma4@sha256:abc",
        "team/gemma4:27b@sha256:abc",
        " registry:5000/team/GEMMA4:latest@sha256:ABC ",
    ],
)
def test_unset_reasoning_defaults_exact_gemma4_repository_to_false(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
) -> None:
    model = _configure_ollama(monkeypatch, model_name)

    assert "reasoning" in model.model_fields_set
    assert model.reasoning is False
    assert model._chat_params([])["think"] is False


@pytest.mark.parametrize(
    "model_name",
    [
        "gemma40",
        "my-gemma4",
        "gemma4x",
        "qwen3:latest",
        "gemma40@sha256:abc",
        "my-gemma4:27b@sha256:abc",
        "gemma4x@sha256:abc",
        "qwen3:latest@sha256:abc",
        "   ",
        "@sha256:abc",
        "gemma4@",
        "gemma4@@sha256:abc",
        "gemma4@sha@256:abc",
        "gemma4@sha256:",
        "gemma4@:abc",
        "gemma4@sha256:abc:def",
        "gemma4@sha256:abc@def",
        "/gemma4@sha256:abc",
        "gemma4:@sha256:abc",
    ],
)
def test_unset_reasoning_omits_keyword_for_non_gemma4_repositories(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
) -> None:
    model = _configure_ollama(monkeypatch, model_name)

    assert "reasoning" not in model.model_fields_set
    assert "reasoning" not in model.model_dump(exclude_unset=True)


def test_empty_model_name_is_not_classified_as_gemma4() -> None:
    assert model_factory._is_gemma4_model("") is False


@pytest.mark.parametrize(
    ("configured_value", "expected", "model_name"),
    [
        ("1", True, "qwen3:latest"),
        ("true", True, "gemma4"),
        ("true", True, "gemma4@@sha256:abc"),
        ("YES", True, "qwen3:latest"),
        ("  On\t", True, "gemma4:27b"),
        ("0", False, "qwen3:latest"),
        ("false", False, "gemma4"),
        ("NO", False, "qwen3:latest"),
        ("  Off\t", False, "gemma4:27b"),
    ],
)
def test_explicit_reasoning_boolean_overrides_apply_to_every_ollama_model(
    monkeypatch: pytest.MonkeyPatch,
    configured_value: str,
    expected: bool,
    model_name: str,
) -> None:
    monkeypatch.setenv("OLLAMA_REASONING", configured_value)

    model = _configure_ollama(monkeypatch, model_name)

    assert "reasoning" in model.model_fields_set
    assert model.reasoning is expected
    assert model._chat_params([])["think"] is expected


@pytest.mark.parametrize("configured_value", ["", "   \t", "invalid", "2"])
def test_invalid_explicit_reasoning_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    configured_value: str,
) -> None:
    monkeypatch.setenv("OLLAMA_REASONING", configured_value)
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")
    monkeypatch.setenv("MODEL_NAME", "gemma4")

    with pytest.raises(ValueError) as exc_info:
        model_factory.get_configured_model(bypass_cache=True)

    assert str(exc_info.value) == "OLLAMA_REASONING must be a boolean"


def test_higher_precedence_provider_does_not_validate_ollama_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_BEDROCK_ENDPOINT", "https://bedrock.example.test/v1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "secret-test-value")
    monkeypatch.setenv("MODEL_NAME", "bedrock-model")
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_REASONING", "invalid")

    model = model_factory.get_configured_model(bypass_cache=True)

    assert model._runtime_metadata.provider == "aws_bedrock"


def test_gemma_reasoning_policy_follows_model_cache_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")
    monkeypatch.setenv("MODEL_NAME", "gemma4")

    first = model_factory.get_configured_model()
    monkeypatch.setenv("OLLAMA_REASONING", "true")
    cached = model_factory.get_configured_model()
    bypassed = model_factory.get_configured_model(bypass_cache=True)
    still_cached = model_factory.get_configured_model()
    model_factory.clear_model_cache()
    rebuilt = model_factory.get_configured_model()

    assert cached is first
    assert cached.reasoning is False
    assert cached._chat_params([])["think"] is False
    assert bypassed is not first
    assert bypassed.reasoning is True
    assert bypassed._chat_params([])["think"] is True
    assert still_cached is first
    assert still_cached.reasoning is False
    assert still_cached._chat_params([])["think"] is False
    assert rebuilt is not first
    assert rebuilt.reasoning is True
    assert rebuilt._chat_params([])["think"] is True


def test_skill_factory_delegates_to_shared_uncached_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[bool] = []

    def shared_factory(*, bypass_cache: bool = False) -> object:
        calls.append(bypass_cache)
        return sentinel

    monkeypatch.setattr(model_factory, "get_configured_model", shared_factory)
    skill_path = Path(
        ".deepagents/skills/golden-dataset/scripts/skill_model_factory.py"
    )
    spec = importlib.util.spec_from_file_location("golden_skill_model_factory", skill_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.get_configured_model() is sentinel
    assert calls == [True]
