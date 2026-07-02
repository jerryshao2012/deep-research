"""Model-factory diagnostics used by the ``/storage/info`` endpoint.

Provides provider auto-detection, model creation, and a minimal connectivity
test — all wrapped in timing information to help diagnose deployment issues.
"""

from __future__ import annotations

import asyncio
import logging
import os

import time

logger = logging.getLogger(__name__)


# ── Public entry point ────────────────────────────────────────────────────────

async def run_model_diagnostics() -> dict:
    """Detect the configured provider, create a model, and send a test prompt.

    Returns a nested dictionary suitable for JSON serialisation in an API
    response.  Failures at any stage are captured rather than raised so the
    endpoint always returns a complete picture.
    """
    diagnostics: dict = {
        "detected_provider": None,
        "configuration": {},
        "model_creation": {"success": False, "error": None, "elapsed_seconds": None},
        "test_request": {
            "success": False,
            "prompt": None,
            "response": None,
            "error": None,
            "elapsed_seconds": None,
        },
    }

    provider, config = _detect_model_provider()
    diagnostics["detected_provider"] = provider
    diagnostics["configuration"] = config

    if provider == "none":
        diagnostics["model_creation"]["error"] = (
            "No supported model provider environment variables found."
        )
        return diagnostics

    # ── Create the model ──────────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        model = await asyncio.to_thread(_create_model_safe)
        elapsed = round(time.monotonic() - t0, 3)
        diagnostics["model_creation"] = {
            "success": True,
            "error": None,
            "elapsed_seconds": elapsed,
            "model_type": type(model).__name__,
            "model_repr": repr(model)[:500],
        }
    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 3)
        diagnostics["model_creation"] = {
            "success": False,
            "error": str(exc),
            "elapsed_seconds": elapsed,
        }
        return diagnostics  # cannot proceed without a model

    # ── Test prompt ───────────────────────────────────────────────────────────
    test_prompt = "Reply with exactly: OK"
    try:
        t0 = time.monotonic()
        response = await asyncio.to_thread(model.invoke, test_prompt)
        elapsed = round(time.monotonic() - t0, 3)
        diagnostics["test_request"] = {
            "success": True,
            "prompt": test_prompt,
            "response": (
                str(response.content)[:500]
                if hasattr(response, "content")
                else str(response)[:500]
            ),
            "response_metadata": _extract_response_metadata(response),
            "error": None,
            "elapsed_seconds": elapsed,
        }
    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 3)
        diagnostics["test_request"] = {
            "success": False,
            "prompt": test_prompt,
            "response": None,
            "error": str(exc),
            "elapsed_seconds": elapsed,
        }

    return diagnostics


# ── Private helpers ───────────────────────────────────────────────────────────

def _mask(value: str | None, visible: int = 4) -> str | None:
    if not value:
        return None
    if len(value) <= visible:
        return "****"
    return value[:visible] + "*" * (len(value) - visible)


def _detect_model_provider() -> tuple[str, dict]:
    """Return ``(provider_name, config_dict)`` based on environment variables.

    ``config_dict`` contains provider-specific keys with secrets masked.
    """
    # AWS Bedrock
    if (
            os.getenv("AWS_BEDROCK_ENDPOINT")
            and os.getenv("AWS_BEARER_TOKEN_BEDROCK")
            and os.getenv("MODEL_NAME")
    ):
        return "aws_bedrock", {
            "AWS_BEDROCK_ENDPOINT": os.getenv("AWS_BEDROCK_ENDPOINT"),
            "AWS_BEARER_TOKEN_BEDROCK": _mask(os.getenv("AWS_BEARER_TOKEN_BEDROCK")),
            "MODEL_NAME": os.getenv("MODEL_NAME"),
        }

    # Azure OpenAI (legacy — requires explicit API version)
    if (
            os.getenv("AZURE_OPENAI_ENDPOINT")
            and os.getenv("AZURE_OPENAI_DEPLOYMENT")
            and (
            os.getenv("AZURE_OPENAI_API_KEY")
            or (os.getenv("AZURE_CLIENT_ID") and os.getenv("AZURE_OPENAI_SCOPE"))
    )
            and os.getenv("AZURE_OPENAI_API_VERSION")
    ):
        auth_type = os.getenv("AZURE_AUTH_TYPE", "api_key")
        return "azure_openai_legacy", {
            "AZURE_OPENAI_ENDPOINT": os.getenv("AZURE_OPENAI_ENDPOINT"),
            "AZURE_OPENAI_DEPLOYMENT": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
            "AZURE_OPENAI_API_VERSION": os.getenv("AZURE_OPENAI_API_VERSION"),
            "AZURE_AUTH_TYPE": auth_type,
            "AZURE_OPENAI_API_KEY": _mask(os.getenv("AZURE_OPENAI_API_KEY")),
            "AZURE_CLIENT_ID": os.getenv("AZURE_CLIENT_ID"),
            "AZURE_OPENAI_SCOPE": os.getenv("AZURE_OPENAI_SCOPE"),
        }

    # Azure OpenAI (new — no explicit API version)
    if (
            os.getenv("AZURE_OPENAI_ENDPOINT")
            and os.getenv("AZURE_OPENAI_DEPLOYMENT")
            and os.getenv("AZURE_OPENAI_API_KEY")
    ):
        return "azure_openai", {
            "AZURE_OPENAI_ENDPOINT": os.getenv("AZURE_OPENAI_ENDPOINT"),
            "AZURE_OPENAI_DEPLOYMENT": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
            "AZURE_OPENAI_API_KEY": _mask(os.getenv("AZURE_OPENAI_API_KEY")),
        }

    # Google Gemini
    if os.getenv("GOOGLE_API_KEY") and os.getenv("MODEL_NAME"):
        return "google_gemini", {
            "GOOGLE_API_KEY": _mask(os.getenv("GOOGLE_API_KEY")),
            "MODEL_NAME": os.getenv("MODEL_NAME"),
        }

    # Anthropic
    if os.getenv("ANTHROPIC_API_KEY") and os.getenv("MODEL_NAME"):
        return "anthropic", {
            "ANTHROPIC_API_KEY": _mask(os.getenv("ANTHROPIC_API_KEY")),
            "MODEL_NAME": os.getenv("MODEL_NAME"),
        }

    # Ollama
    if os.getenv("OLLAMA_API_BASE") and os.getenv("MODEL_NAME"):
        return "ollama", {
            "OLLAMA_API_BASE": os.getenv("OLLAMA_API_BASE"),
            "MODEL_NAME": os.getenv("MODEL_NAME"),
        }

    return "none", {}


def _create_model_safe():
    """Import and call ``model_factory.get_configured_model()`` safely."""
    from model_factory import get_configured_model

    return get_configured_model()


def _extract_response_metadata(response) -> dict:
    """Pull a small set of non-sensitive metadata fields from a LangChain response."""
    meta: dict = {}
    if hasattr(response, "response_metadata") and response.response_metadata:
        rm = response.response_metadata
        for key in ("model_name", "model_provider", "finish_reason", "usage"):
            if key in rm:
                meta[key] = rm[key]
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        meta["usage_metadata"] = response.usage_metadata
    return meta
