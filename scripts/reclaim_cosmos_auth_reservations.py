"""One-off, quiesced repair for orphan Cosmos passkey reservations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or reclaim old orphan Cosmos auth reservations.",
        epilog=(
            "SAFETY: stop every application replica and writer before running. "
            "Cosmos cannot fence a paused cross-container writer. Dry-run is default."
        ),
    )
    parser.add_argument("--identity", required=True)
    parser.add_argument("--cutoff", required=True, type=float)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--confirm-quiesced",
        action="store_true",
        help="Confirm every app replica and auth writer is stopped.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply repair. Omit for a read-only dry run.",
    )
    parser.add_argument(
        "--include-committed-missing",
        action="store_true",
        help=(
            "Also reclaim missing-documents committed markers with no lease age; "
            "requires quiescence and an absence point-read."
        ),
    )
    return parser


def main(
        argv: Sequence[str] | None = None,
        *,
        store_factory: Callable[[], Any] | None = None,
) -> int:
    """Run maintenance only after explicit quiescence confirmation."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.confirm_quiesced:
        parser.error(
            "--confirm-quiesced is required; stop every app replica and writer first"
        )
    if store_factory is None:
        from webapp.config import user_manager

        store = user_manager.store
    else:
        store = store_factory()
    from webapp.auth_store_cosmos import CosmosAuthStore

    if not isinstance(store, CosmosAuthStore):
        store.close()
        parser.error("configured auth store is not Cosmos DB")
    try:
        report = store.reclaim_orphan_reservations(
            args.identity,
            cutoff=args.cutoff,
            limit=args.limit,
            confirmed_quiesced=True,
            include_committed_missing=args.include_committed_missing,
            apply=args.apply,
        )
        sys.stdout.write(f"{json.dumps(report, sort_keys=True)}\n")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
