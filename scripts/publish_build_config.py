#!/usr/bin/env python3
"""Atomically publish a prepared build configuration file."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path


def publish(source: Path, target: Path) -> None:
    """Replace target atomically with source bytes and mode, then sync directory."""
    source_bytes = source.read_bytes()
    source_mode = stat.S_IMODE(source.stat().st_mode)
    target_parent = target.parent.resolve(strict=True)
    if target.parent.resolve(strict=True) != target_parent or not target.is_file():
        raise ValueError("target must be an existing regular file")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.publish.", dir=target_parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), source_mode)
            stream.write(source_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(target_parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    """Parse command-line paths and publish prepared configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    arguments = parser.parse_args()
    publish(arguments.source, arguments.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
