"""Tests for CLI helper functions."""

import os
from pathlib import Path

from research_agent_cli import (
    configure_output_folder,
    derive_output_folder,
)


def test_derive_output_folder_uses_final_doc_folder_segment() -> None:
    assert derive_output_folder("./doc/policy") == Path("output") / "policy"
    assert derive_output_folder("./docs/policy") == Path("output") / "policy"


def test_configure_output_folder_overwrites_stale_env_value(monkeypatch) -> None:
    monkeypatch.setenv("OUTPUT_FOLDER", "output/docs/old")

    output_folder = configure_output_folder("./doc/policy")

    assert output_folder == Path("output") / "policy"
    assert os.environ["OUTPUT_FOLDER"] == "./output/policy"
