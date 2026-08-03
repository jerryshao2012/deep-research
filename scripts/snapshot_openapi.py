#!/usr/bin/env python3
"""Write or verify the custom FastAPI OpenAPI contract snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SNAPSHOT_PATH = REPO_ROOT / "contracts" / "custom-api.openapi.json"


def rendered_schema() -> str:
    """Return deterministic OpenAPI JSON for the custom application routes."""
    from webapp import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    """Write snapshot, or fail when --check detects drift."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_schema()

    if args.check:
        if not SNAPSHOT_PATH.is_file() or SNAPSHOT_PATH.read_text(encoding="utf-8") != rendered:
            sys.stdout.write(
                "OpenAPI snapshot is stale; run uv run python scripts/snapshot_openapi.py\n"
            )
            return 1
        return 0

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
