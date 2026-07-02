"""Shared pytest fixtures and configuration for the test suite."""

from pathlib import Path

import pytest
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# A fixed test API key used by all tests.  An autouse fixture below patches
# the webapp config so authentication always succeeds with this value.
TEST_API_KEY = "test-api-key-for-unit-tests"


@pytest.fixture(autouse=True)
def _patch_webapp_api_key(monkeypatch):
    """Ensure the webapp accepts TEST_API_KEY during tests.

    This avoids coupling unit tests to the developer's local .env file.
    """
    import webapp.config as _cfg

    monkeypatch.setattr(_cfg, "API_KEY", TEST_API_KEY)
