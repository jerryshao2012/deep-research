"""Shared model configuration for the deep research project."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
import numpy as np
from azure.identity import ManagedIdentityCredential, get_bearer_token_provider
from dotenv import dotenv_values, load_dotenv
from langchain.embeddings import init_embeddings
from langchain_core.embeddings import Embeddings
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_checkpoint_cosmosdb import CosmosDBSaver
from pydantic import SecretStr

from research_agent.cli_utils import get_ssl_verify_config
from research_agent.logger_utils import setup_logger
from research_agent.model_call_guard import (
    ModelCallPolicy,
    ModelRuntimeMetadata,
    build_guarded_provider_model,
)
from research_agent.retry_utils import wrap_model_with_rate_limiting

logger = setup_logger(__name__)


def create_config():
    """Load environment variables from the local ``.env`` file into a config dict.

    Reads ``.env`` from the same directory as this module, logs the loaded
    variables, and returns them as a plain dictionary.  Primarily used for
    development and debugging.

    Returns:
        A dictionary of all key-value pairs found in the ``.env`` file.
    """
    # Load environment variables from .env file
    env_path = Path(__file__) / '.env'
    if env_path.exists():
        load_dotenv(env_path, override=True)
        # Get all environment variables from .env file
        env_vars = dotenv_values(env_path)
        logger.info(f"Loaded {len(env_vars)} environment variables from {env_path}")
    else:
        logger.warning(f".env file not found at {env_path}")
        env_vars = {}
    config = {}

    # Add all environment variables to config
    for key, value in env_vars.items():
        config[key] = value
        logger.info(f"Added to config: {key}")
    logger.info(f"Config created with {len(config)} total items")
    return config


def get_openai_auth_kwargs() -> dict:
    """Return the authentication kwargs for Azure OpenAI clients.

    Reads ``AZURE_AUTH_TYPE`` from the environment:
      * ``"managed_identity"`` → returns ``azure_ad_token_provider``
      * anything else (default ``"api_key"``) → returns ``api_key``
    """
    if os.getenv("AZURE_AUTH_TYPE") == "managed_identity":
        logger.info("Using Managed Identity for Azure OpenAI authentication.")
        credential = ManagedIdentityCredential(
            client_id=os.environ.get("AZURE_CLIENT_ID")
        )
        token_provider = get_bearer_token_provider(
            credential, os.environ["AZURE_OPENAI_SCOPE"]
        )
        return {"azure_ad_token_provider": token_provider}
    else:
        # default: os.getenv("AZURE_AUTH_TYPE") == "api_key"
        logger.info("Using API Key for Azure OpenAI authentication.")
        return {"api_key": SecretStr(os.getenv("AZURE_OPENAI_API_KEY", ""))}


class SimpleLocalEmbeddings(Embeddings):
    """A deterministic, completely local bag-of-words projection embedding model.

    Used as an offline fallback that works with FAISS.

    Attributes:
        size: Dimensionality of the embedding vectors. Defaults to 1536.
    """

    def __init__(self, size=1536):
        """Initialize the embedding model.

        Args:
            size: Dimensionality of the embedding vectors. Defaults to 1536.
        """
        self.size = size

    def _embed(self, text: str) -> list[float]:
        """Embed a single text into a deterministic bag-of-words projection.

        Each word is hashed (MD5) to a fixed index in the embedding vector
        with a sign based on the hash bits.  The resulting vector is L2-
        normalized so cosine similarity = dot product.

        Args:
            text: The input text to embed.

        Returns:
            A list of ``self.size`` floats representing the normalized
            embedding vector.
        """
        words = [w.strip(".,!?\"'()[]{}<>").lower() for w in text.split()]
        vec = np.zeros(self.size, dtype=np.float32)
        if not words:
            return vec.tolist()

        for word in words:
            if not word:
                continue
            # MD5 hash to deterministic feature index
            h = int(hashlib.md5(word.encode('utf-8')).hexdigest(), 16)
            idx = h % self.size
            sign = 1 if ((h >> 4) % 2 == 0) else -1
            vec[idx] += sign

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents.

        Args:
            texts: A list of documents strings to embed.

        Returns:
            A list of embedding vectors, one per input documents.
        """
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string.

        Delegates to :meth:`_embed` — queries and documents use the same
        embedding space.

        Args:
            text: The query string to embed.

        Returns:
            A normalized embedding vector as a list of floats.
        """
        return self._embed(text)


def create_embedding_model():
    """Create an embedding model instance with graceful fallbacks."""
    # 1. Try Azure OpenAI if configured
    if (
            os.getenv("AZURE_EMBEDDING_NAME")
            and os.getenv("AZURE_OPENAI_ENDPOINT")
            and os.getenv("AZURE_EMBEDDING_DEPLOYMENT_NAME")
            and os.getenv("AZURE_OPENAI_API_VERSION")
    ):
        try:
            logger.info("Using Azure OpenAI embedding model.")
            return init_embeddings(
                model=f"azure_openai:{os.environ['AZURE_EMBEDDING_NAME']}",
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                azure_deployment=os.environ["AZURE_EMBEDDING_DEPLOYMENT_NAME"],
                api_version=os.environ["AZURE_OPENAI_API_VERSION"],
                **get_openai_auth_kwargs(),
            )
        except Exception as e:
            logger.warning(f"Failed to initialize Azure OpenAI embeddings: {e}. Trying other providers...")

    # 2. Try OpenAI if configured
    if os.getenv("OPENAI_API_KEY"):
        try:
            logger.info("Using OpenAI embedding model.")
            from langchain_openai import OpenAIEmbeddings
            return OpenAIEmbeddings(
                model="text-embedding-3-small",
                api_key=SecretStr(os.environ["OPENAI_API_KEY"])
            )
        except Exception as e:
            logger.warning(f"Failed to initialize OpenAI embeddings: {e}. Trying other providers...")

    # 3. Try Google if configured
    if os.getenv("GOOGLE_API_KEY"):
        try:
            logger.info("Using Google embedding model (models/gemini-embedding-001).")
            from langchain_google_genai import GoogleGenerativeAIEmbeddings
            return GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
        except Exception as e:
            logger.warning(f"Failed to initialize Google embeddings: {e}. Trying other providers...")

    # 4. Try Ollama if configured
    if os.getenv("OLLAMA_API_BASE"):
        try:
            logger.info("Using Ollama embedding model.")
            from langchain_ollama import OllamaEmbeddings
            emb_model = os.getenv("EMBEDDING_MODEL_NAME") or os.getenv("MODEL_NAME") or "nomic-embed-text"
            return OllamaEmbeddings(
                model=emb_model,
                base_url=os.environ["OLLAMA_API_BASE"]
            )
        except Exception as e:
            logger.warning(f"Failed to initialize Ollama embeddings: {e}. Trying fallback...")

    # 5. Ultimate Fallback: SimpleLocalEmbeddings
    logger.warning("No embedding provider configured or initialization failed. Falling back to SimpleLocalEmbeddings.")
    return SimpleLocalEmbeddings(size=1536)


def create_memory_saver():
    """Create a LangGraph checkpointer based on the MEMORY_TYPE env var.

    Supported types:
      - ``None`` (default / unset): no checkpointer — the platform handles persistence
      - ``"memory"``: ephemeral InMemorySaver — no persistence across restarts
      - ``"sqlite"``: AsyncSqliteSaver — persistent, local file-based checkpoints
      - ``"postgres"``: AsyncPostgresSaver — persistent, production-grade
      - ``"cosmosdb"``: CosmosDBSaver — Azure CosmosDB-backed (sync, use with care in async)

    When MEMORY_TYPE is not set, returns ``None`` so the graph is created
    checkpoint-free.  This is the expected default for ``langgraph dev`` /
    LangGraph Platform deployments (the platform injects its own persistence).
    Set ``MEMORY_TYPE=memory`` (or sqlite / postgres) when running via the
    ``langgraph dev`` / LangGraph Platform.

    Note: sqlite and postgres AsyncSavers require an active event loop and are
    typically created via ``setup_checkpointer()`` from the server lifespan.
    """
    memory_type = os.environ.get("MEMORY_TYPE", "").strip().lower()

    if not memory_type:
        return None

    if memory_type == "memory":
        return InMemorySaver()

    if memory_type == "cosmosdb":
        endpoint = os.environ.get("COSMOSDB_ENDPOINT")
        if not endpoint:
            logger.warning(
                "MEMORY_TYPE=cosmosdb but COSMOSDB_ENDPOINT is not set; "
                "falling back to InMemorySaver."
            )
            return InMemorySaver()
        return CosmosDBSaver(
            database_name=os.environ.get("COSMOSDB_DB_NAME", "deep-research-checkpoints"),
            container_name=os.environ.get("COSMOSDB_CONTAINER_NAME", "checkpoints"),
        )

    # For sqlite / postgres: return InMemorySaver at module-load time.
    # The real saver is set up later via setup_checkpointer() once the
    # event loop is running (in the FastAPI lifespan).
    if memory_type in ("sqlite", "postgres", "postgresql"):
        logger.info(
            "MEMORY_TYPE=%s — using InMemorySaver at import time; "
            "persistent checkpointer will be set up during server startup.",
            memory_type,
        )
        return InMemorySaver()

    logger.error("Unsupported MEMORY_TYPE: %s", memory_type)
    raise ValueError(f"Unsupported MEMORY_TYPE: {memory_type}")


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _is_gemma4_model(model_name: str) -> bool:
    normalized = model_name.strip().casefold()
    if normalized.count("@") > 1:
        return False
    model, digest_separator, digest = normalized.partition("@")
    if not model or any(not segment for segment in model.split("/")):
        return False
    if digest_separator:
        algorithm, algorithm_separator, digest_value = digest.partition(":")
        if (
            not algorithm_separator
            or not algorithm
            or not digest_value
            or "@" in algorithm
            or ":" in digest_value
            or "@" in digest_value
        ):
            return False

    repository_with_tag = model.rsplit("/", 1)[-1]
    repository, tag_separator, tag = repository_with_tag.partition(":")
    if tag_separator and (not tag or ":" in tag):
        return False
    return repository == "gemma4"


def _ollama_reasoning_kwargs(model_name: str) -> dict[str, bool]:
    raw = os.getenv("OLLAMA_REASONING")
    if raw is None:
        return {"reasoning": False} if _is_gemma4_model(model_name) else {}

    normalized = raw.strip().casefold()
    if normalized in _TRUE_VALUES:
        return {"reasoning": True}
    if normalized in _FALSE_VALUES:
        return {"reasoning": False}
    raise ValueError("OLLAMA_REASONING must be a boolean")


_cached_model = None


def clear_model_cache():
    """Clear the globally cached model instance."""
    global _cached_model
    _cached_model = None


def get_configured_model(bypass_cache: bool = False):
    """Get the globally cached model instance or build it if missing."""
    global _cached_model
    if not bypass_cache and _cached_model is not None:
        return _cached_model
    model = _build_configured_model()
    if not bypass_cache:
        _cached_model = model
    return model


def _build_configured_model():
    """Build the first matching chat model from the environment configuration with rate limit retry."""
    verify_ssl = get_ssl_verify_config()
    policy = ModelCallPolicy.from_env()

    def openai_clients() -> tuple[httpx.Client, httpx.AsyncClient]:
        return (
            httpx.Client(verify=verify_ssl, timeout=policy.timeout_seconds),
            httpx.AsyncClient(verify=verify_ssl, timeout=policy.timeout_seconds),
        )

    def finalize(provider_class, kwargs, metadata):
        guarded = build_guarded_provider_model(
            provider_class,
            kwargs,
            metadata,
            policy,
        )
        return wrap_model_with_rate_limiting(guarded)

    def optional_url(*names: str) -> str | None:
        for name in names:
            value = os.getenv(name)
            if value is not None and value.strip():
                return value.strip()
        return None

    if (
            os.getenv("AWS_BEDROCK_ENDPOINT")
            and os.getenv("AWS_BEARER_TOKEN_BEDROCK")
            and os.getenv("MODEL_NAME")
    ):
        from langchain_openai import ChatOpenAI

        endpoint = os.environ["AWS_BEDROCK_ENDPOINT"]
        model_name = os.environ["MODEL_NAME"]
        http_client, http_async_client = openai_clients()
        return finalize(
            ChatOpenAI,
            {
                "base_url": endpoint,
                "api_key": SecretStr(os.environ["AWS_BEARER_TOKEN_BEDROCK"]),
                "model": model_name,
                "max_retries": 0,
                "request_timeout": policy.timeout_seconds,
                "http_client": http_client,
                "http_async_client": http_async_client,
                "stream_usage": True,
            },
            ModelRuntimeMetadata(
                provider="aws_bedrock",
                model_name=model_name,
                base_url=endpoint,
            ),
        )

    # Legacy Azure OpenAI configuration (with explicit API version)
    if (
            os.getenv("AZURE_OPENAI_ENDPOINT")
            and os.getenv("AZURE_OPENAI_DEPLOYMENT")
            and (os.getenv("AZURE_OPENAI_API_KEY")
                 or (os.getenv("AZURE_CLIENT_ID") and os.getenv("AZURE_OPENAI_SCOPE")))
            and os.getenv("AZURE_OPENAI_API_VERSION")
    ):
        from langchain_openai import AzureChatOpenAI

        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
        deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
        http_client, http_async_client = openai_clients()
        return finalize(
            AzureChatOpenAI,
            {
                "azure_endpoint": endpoint,
                "azure_deployment": deployment,
                "api_version": os.environ["AZURE_OPENAI_API_VERSION"],
                "max_retries": 0,
                "request_timeout": policy.timeout_seconds,
                "http_client": http_client,
                "http_async_client": http_async_client,
                "stream_usage": True,
                **get_openai_auth_kwargs(),
            },
            ModelRuntimeMetadata(
                provider="azure_openai",
                model_name=deployment,
                base_url=endpoint,
            ),
        )

    # New Azure OpenAI configuration (without explicit API version)
    if (
            os.getenv("AZURE_OPENAI_ENDPOINT")
            and os.getenv("AZURE_OPENAI_DEPLOYMENT")
            and os.getenv("AZURE_OPENAI_API_KEY")
    ):
        from langchain_openai import ChatOpenAI

        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
        deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
        http_client, http_async_client = openai_clients()
        return finalize(
            ChatOpenAI,
            {
                "base_url": endpoint,
                "api_key": SecretStr(os.environ["AZURE_OPENAI_API_KEY"]),
                "model": deployment,
                "max_retries": 0,
                "request_timeout": policy.timeout_seconds,
                "http_client": http_client,
                "http_async_client": http_async_client,
                "stream_usage": True,
            },
            ModelRuntimeMetadata(
                provider="azure_openai",
                model_name=deployment,
                base_url=endpoint,
            ),
        )

    if os.getenv("GOOGLE_API_KEY") and os.getenv("MODEL_NAME"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        model_name = os.environ["MODEL_NAME"]
        native_client_args = {
            "timeout": policy.timeout_seconds,
            "verify": verify_ssl,
        }
        return finalize(
            ChatGoogleGenerativeAI,
            {
                "model": model_name,
                "api_key": SecretStr(os.environ["GOOGLE_API_KEY"]),
                "temperature": 0.0,
                "streaming": True,
                "max_retries": 0,
                "request_timeout": policy.timeout_seconds,
                "client_args": native_client_args,
            },
            ModelRuntimeMetadata(provider="google", model_name=model_name),
        )

    if os.getenv("ANTHROPIC_API_KEY") and os.getenv("MODEL_NAME"):
        from langchain_anthropic import ChatAnthropic

        model_name = os.environ["MODEL_NAME"]
        endpoint = optional_url("ANTHROPIC_API_URL", "ANTHROPIC_BASE_URL")
        return finalize(
            ChatAnthropic,
            {
                "model_name": model_name,
                "api_key": SecretStr(os.environ["ANTHROPIC_API_KEY"]),
                "temperature": 0.0,
                "max_retries": 0,
                "timeout": policy.timeout_seconds,
                "base_url": endpoint or "https://api.anthropic.com",
            },
            ModelRuntimeMetadata(
                provider="anthropic",
                model_name=model_name,
                base_url=endpoint,
            ),
        )

    if os.getenv("OLLAMA_API_BASE") and os.getenv("MODEL_NAME"):
        from langchain_ollama import ChatOllama

        model_name = os.environ["MODEL_NAME"]
        endpoint = os.environ["OLLAMA_API_BASE"]
        native_client_kwargs = {
            "timeout": policy.timeout_seconds,
            "verify": verify_ssl,
        }
        return finalize(
            ChatOllama,
            {
                "model": model_name,
                "base_url": endpoint,
                "temperature": 0.0,
                "client_kwargs": dict(native_client_kwargs),
                "sync_client_kwargs": dict(native_client_kwargs),
                "async_client_kwargs": dict(native_client_kwargs),
                **_ollama_reasoning_kwargs(model_name),
            },
            ModelRuntimeMetadata(
                provider="ollama",
                model_name=model_name,
                base_url=endpoint,
            ),
        )

    if os.getenv("OPENAI_API_KEY") and os.getenv("MODEL_NAME"):
        from langchain_openai import ChatOpenAI

        model_name = os.environ["MODEL_NAME"]
        endpoint = optional_url("OPENAI_BASE_URL")
        http_client, http_async_client = openai_clients()
        return finalize(
            ChatOpenAI,
            {
                "api_key": SecretStr(os.environ["OPENAI_API_KEY"]),
                "model": model_name,
                "max_retries": 0,
                "request_timeout": policy.timeout_seconds,
                "http_client": http_client,
                "http_async_client": http_async_client,
                "stream_usage": True,
                "base_url": endpoint or "https://api.openai.com/v1",
            },
            ModelRuntimeMetadata(
                provider="openai",
                model_name=model_name,
                base_url=endpoint,
            ),
        )

    raise ValueError("No model found. Please set up a model")
