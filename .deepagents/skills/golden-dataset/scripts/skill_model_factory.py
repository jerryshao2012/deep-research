"""Model configuration for the deep research project of skill's LLM as a judge."""

from __future__ import annotations

from research_agent.cli_utils import get_ssl_verify_config as get_ssl_verify_config
from research_agent.model_factory import get_configured_model as _shared_model_factory
from research_agent.retry_utils import ModelRetryController as ModelRetryController


def get_configured_model():
    """Build an uncached judge model through the shared guarded factory."""
    return _shared_model_factory(bypass_cache=True)
