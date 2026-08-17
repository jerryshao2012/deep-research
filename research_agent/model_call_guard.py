"""Configuration and safe metadata for bounded model calls."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Literal, Mapping
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
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/-]+$")


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
        """Create an error without retaining arbitrary request content."""
        self.provider = _safe_provider(provider)
        self.model_name = (
            model_name.strip()
            if model_name and _SAFE_IDENTIFIER.fullmatch(model_name.strip())
            else None
        )
        detail = f" for model {self.model_name!r}" if self.model_name else ""
        super().__init__(f"Unsupported model override for provider {self.provider!r}{detail}")
