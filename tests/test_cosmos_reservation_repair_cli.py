"""Safety contract for one-off Cosmos reservation repair CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from webapp.auth_store_cosmos import CosmosAuthStore


def test_cli_help_does_not_bootstrap_configured_auth_storage(tmp_path):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("block mkdir", encoding="utf-8")
    environment = dict(
        os.environ,
        DB_TYPE="sqlite",
        SQLITE_DB_PATH=str(blocked_parent / "auth.db"),
        PASSKEY_ENABLED="true",
        PASSKEY_RP_ID="example.com",
        PASSKEY_ORIGINS="https://app.example.com",
        PASSKEY_PROXY_ID="web-bff",
        PASSKEY_PROXY_SECRET="p" * 32,
        OAUTH_SECRET_KEY="0123456789abcdef0123456789abcdef",
        GOOGLE_CLIENT_ID="google-client",
        GOOGLE_CLIENT_SECRET="google-secret",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.reclaim_cosmos_auth_reservations",
            "--help",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--confirm-quiesced" in result.stdout


def test_cli_refuses_without_quiesced_confirmation_before_store_creation():
    from scripts.reclaim_cosmos_auth_reservations import main

    called = False

    def store_factory():
        nonlocal called
        called = True
        pytest.fail("store must not be created before confirmation")

    with pytest.raises(SystemExit):
        main(
            ["--identity", "google:123", "--cutoff", "1300"],
            store_factory=store_factory,
        )

    assert called is False


def test_cli_forwards_committed_missing_opt_in(capsys):
    class RecordingStore(CosmosAuthStore):
        def __init__(self):
            super().__init__(containers={})
            self.kwargs = None

        def reclaim_orphan_reservations(self, _identity, **kwargs):
            self.kwargs = kwargs
            return {
                "identity": "google:123",
                "cutoff": kwargs["cutoff"],
                "apply": kwargs["apply"],
                "credential_orphans": 0,
                "challenge_orphans": 0,
                "reclaimed": 0,
            }

    from scripts.reclaim_cosmos_auth_reservations import main

    store = RecordingStore()
    assert (
            main(
                [
                    "--identity",
                    "google:123",
                    "--cutoff",
                    "1300",
                    "--confirm-quiesced",
                    "--include-committed-missing",
                ],
                store_factory=lambda: store,
            )
            == 0
    )

    assert store.kwargs["include_committed_missing"] is True
    assert store.kwargs["apply"] is False
    assert "credential_A" not in capsys.readouterr().out
