"""Model configuration for the deep research project of skill's LLM as a judge."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import sys
from pydantic import SecretStr

# Add parent directories to path to import retry_utils
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))
from retry_utils import wrap_model_with_rate_limiting

from utils import get_ssl_verify_config


def get_configured_model():
    """
    Build the first matching chat model from environment configuration with rate limit retry.
    Keep this separate model factory for LLM as Judge using a different model
    """
    verify_ssl = get_ssl_verify_config()

    if (
            os.getenv("AWS_BEDROCK_ENDPOINT")
            and os.getenv("AWS_BEARER_TOKEN_BEDROCK")
            and os.getenv("MODEL_NAME")
    ):
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            base_url=os.getenv("AWS_BEDROCK_ENDPOINT"),
            api_key=SecretStr(os.getenv("AWS_BEARER_TOKEN_BEDROCK", "")),
            model=os.getenv("MODEL_NAME", ""),
            http_client=httpx.Client(verify=verify_ssl),
            stream_usage=True,
        )
        return wrap_model_with_rate_limiting(model)

    # Legacy Azure OpenAI configuration (with explicit API version)
    if (
            os.getenv("AZURE_OPENAI_ENDPOINT")
            and os.getenv("AZURE_OPENAI_DEPLOYMENT")
            and (os.getenv("AZURE_OPENAI_API_KEY")
                 or (os.getenv("AZURE_CLIENT_ID") and os.getenv("AZURE_OPENAI_SCOPE")))
            and os.getenv("AZURE_OPENAI_API_VERSION")
    ):
        from langchain_openai import AzureChatOpenAI

        model = AzureChatOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
            http_client=httpx.Client(verify=verify_ssl),
            stream_usage=True,
        )
        return wrap_model_with_rate_limiting(model)

    # New Azure OpenAI configuration (without explicit API version)
    if (
            os.getenv("AZURE_OPENAI_ENDPOINT")
            and os.getenv("AZURE_OPENAI_DEPLOYMENT")
            and os.getenv("AZURE_OPENAI_API_KEY")
    ):
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            base_url=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=SecretStr(os.getenv("AZURE_OPENAI_API_KEY", "")),
            model=os.getenv("AZURE_OPENAI_DEPLOYMENT", ""),
            http_client=httpx.Client(verify=verify_ssl),
            stream_usage=True,
        )
        return wrap_model_with_rate_limiting(model)

    if os.getenv("GOOGLE_API_KEY") and os.getenv("MODEL_NAME"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs = {
            "model": os.getenv("MODEL_NAME", "gemini-2.5-pro"),
            "temperature": 0.0,
            "streaming": True,
        }

        if verify_ssl is not True:
            from google import genai
            kwargs["client"] = genai.Client(
                api_key=os.getenv("GOOGLE_API_KEY"),
                http_options={"httpx_client": httpx.Client(verify=verify_ssl)},
            )

        model = ChatGoogleGenerativeAI(**kwargs)
        return wrap_model_with_rate_limiting(model)

    if os.getenv("ANTHROPIC_API_KEY") and os.getenv("MODEL_NAME"):
        from langchain.chat_models import init_chat_model

        model = init_chat_model(
            model=os.getenv("MODEL_NAME", "anthropic:claude-sonnet-4-5-20250929"),
            temperature=0.0,
            http_client=httpx.Client(verify=verify_ssl),
        )
        return wrap_model_with_rate_limiting(model)

    if os.getenv("OLLAMA_API_BASE") and os.getenv("MODEL_NAME"):
        from langchain.chat_models import init_chat_model

        model = init_chat_model(
            model=f"ollama:{os.getenv('MODEL_NAME')}",
            base_url=os.getenv("OLLAMA_API_BASE"),
        )
        return wrap_model_with_rate_limiting(model)

    raise ValueError("No model found. Please set up a model")
