#!/usr/bin/env python3
"""DEPRECATED: This launcher for the custom ``server.py`` is no longer supported.

The ``server.py`` custom LangGraph Platform implementation has been deprecated
in favor of the official LangGraph Platform server (``langgraph dev`` /
LangSmith Deployment).

To start the development server, use::

    langgraph dev

For production, deploy via LangSmith Deployment or LangGraph Platform.

This script is kept for reference only and will be removed in a future release.
"""

import sys

_DEPRECATION_MSG = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                         ⚠️  DEPRECATED  �️                                    ║
║                                                                              ║
║  run.py (and server.py) are no longer supported.                             ║
║                                                                              ║
║  Use the official LangGraph Platform server instead:                         ║
║                                                                              ║
║    langgraph dev                                                             ║
║                                                                              ║
║  This starts the full LangGraph Platform with all endpoints, streaming,      ║
║  checkpointing, and the Studio UI at http://127.0.0.1:2024.                  ║
║                                                                              ║
║  For document uploads (port 8000), use:                                      ║
║                                                                              ║
║    uv run python -m webapp                                                   ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""


def main() -> None:
    """DEPRECATED. Print a deprecation notice and exit.

    This launcher for the custom ``server.py`` is no longer supported.
    Use ``langgraph dev`` for the LangGraph Platform server and
    ``uv run python -m webapp`` for the document upload API.
    """
    print(_DEPRECATION_MSG, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
