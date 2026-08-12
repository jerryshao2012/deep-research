#!/usr/bin/env python3
"""Strictly read only Docker Hub credentials from a dotenv file."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sanitize_passkey_dotenv import (
    SanitizeError,
    _parse,
    _read_all,
    _safe_regular_file,
    _write_all,
)


def main() -> int:
    """Emit requested username and securely capture requested PAT."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--username", action="store_true")
    parser.add_argument("--pat-file", type=Path)
    arguments = parser.parse_args()
    if not arguments.username and arguments.pat_file is None:
        parser.error("request --username and/or --pat-file")

    descriptor = -1
    pat_descriptor = -1
    pat_created = False
    success = False
    try:
        descriptor, _ = _safe_regular_file(arguments.input)
        parsed = _parse(_read_all(descriptor))
        os.close(descriptor)
        descriptor = -1
        values = {
            line.key: line.value
            for line in parsed
            if line.key in {"DOCKER_HUB_USERNAME", "DOCKER_HUB_PAT"}
        }
        username = values.get("DOCKER_HUB_USERNAME")
        pat = values.get("DOCKER_HUB_PAT")
        if arguments.pat_file is not None and pat is not None:
            pat_descriptor = os.open(
                arguments.pat_file,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            pat_created = True
            _write_all(pat_descriptor, pat)
            os.fsync(pat_descriptor)
            os.close(pat_descriptor)
            pat_descriptor = -1
        if arguments.username and username is not None:
            sys.stdout.buffer.write(username + b"\n")
        success = True
        return 0
    except (OSError, SanitizeError) as exc:
        sys.stderr.write(f"Error: Docker credential file is invalid: {exc}\n")
        return 2
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if pat_descriptor >= 0:
            os.close(pat_descriptor)
        if pat_created and not success and arguments.pat_file is not None:
            try:
                os.unlink(arguments.pat_file)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
