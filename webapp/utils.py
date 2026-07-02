"""Shared filesystem utility helpers for the FastAPI webapp.

Provides functions for resolving relative folders safely under document roots,
performing thread-level folder sanitization, and handling directory cleanups.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, status


# ── Path / filename safety ────────────────────────────────────────────────────

def safe_relative_folder(folder: str) -> PurePosixPath:
    """Return a validated relative folder path that is safe to resolve under docs."""
    normalized = folder.replace("\\", "/").strip().strip("/")
    path = PurePosixPath(normalized)
    if (
            not normalized
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="folder must be a relative path inside docs",
        )
    return path


def safe_filename(filename: str | None) -> str:
    """Extract and validate the basename from an uploaded filename."""
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="uploaded files must include filenames",
        )

    name = PurePosixPath(filename.replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="uploaded files must include valid filenames",
        )
    return name


# ── Storage helpers ───────────────────────────────────────────────────────────

def format_bytes(bytes_value: int) -> str:
    """Convert a raw byte count into a human-readable string (e.g. '1.50 GB')."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if bytes_value < 1024.0:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.2f} PB"


def get_free_space(path: Path) -> int:
    """Return free disk space in bytes for the filesystem containing *path*.

    Returns ``-1`` when the free space cannot be determined.
    """
    try:
        stat = shutil.disk_usage(str(path))
        return stat.free
    except Exception:
        return -1


# ── Thread-folder helpers ─────────────────────────────────────────────────────

def extract_thread_id_from_folder(folder_str: str) -> str | None:
    """Return the thread-id if *folder_str* matches the ``threads/<thread-id>`` pattern."""
    parts = PurePosixPath(folder_str.replace("\\", "/").strip("/")).parts
    if len(parts) == 2 and parts[0] == "threads":
        return parts[1]
    return None


def detect_media_type(file_path: Path) -> str:
    """Return a MIME type string based on the file suffix."""
    ext = file_path.suffix.lower()
    _MAP: dict[str, str] = {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/msword",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.ms-excel",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.ms-powerpoint",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".json": "application/json",
        ".csv": "text/csv",
    }
    return _MAP.get(ext, "application/octet-stream")
