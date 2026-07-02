"""Transparent S3 storage layer for AWS App Runner.

Provides bi-directional sync between local filesystem paths and an S3 bucket,
so the application can read/write files locally while changes persist to S3.

The app always reads and writes to local paths (just like the Azure File Share
mount).  This module adds:
- **Startup sync**: download S3 prefixes to local dirs on app boot.
- **Background uploads**: push newly-written files to S3 after each write.
- **Fire-and-forget**: uploads never block the request path.

Usage in webapp.py / the LangGraph Platform app:
    from s3_storage import startup_sync, fire_and_forget_upload, upload_directory_sync

    # On startup (inside lifespan)
    startup_sync()

    # After document upload
    fire_and_forget_upload(str(destination), s3_key)

    # After agent run completes
    upload_directory_sync(local_output_dir, "output")
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from logger_utils import setup_logger

logger = setup_logger(__name__)

# ── S3 Client (lazy singleton) ────────────────────────────────────────────────

_s3_client = None
_client_lock = threading.Lock()


def is_s3_enabled() -> bool:
    """Return True if S3 sync is configured (env vars present)."""
    return bool(os.environ.get("S3_BUCKET_NAME") and os.environ.get("AWS_REGION"))


def _get_bucket() -> str:
    return os.environ["S3_BUCKET_NAME"]


def _get_region() -> str:
    return os.environ["AWS_REGION"]


def _get_client():
    """Lazy-initialize and return a boto3 S3 client."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    with _client_lock:
        if _s3_client is None:
            import boto3
            _s3_client = boto3.client("s3", region_name=_get_region())
            logger.info(f"S3 client initialized for bucket={_get_bucket()}, region={_get_region()}")
    return _s3_client


# ── Download helpers ──────────────────────────────────────────────────────────


def _download_prefix(s3_prefix: str, local_dir: Path) -> int:
    """Download all objects under s3_prefix to local_dir. Returns count of downloaded files."""
    if not is_s3_enabled():
        return 0

    client = _get_client()
    bucket = _get_bucket()

    # Ensure prefix has trailing slash for listing, but strip for key construction
    prefix = s3_prefix.rstrip("/") + "/"
    local_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Compute relative path after the prefix
            relative = key[len(prefix):]
            if not relative or relative.endswith("/"):
                continue  # skip directory markers

            dest = local_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)

            try:
                client.download_file(bucket, key, str(dest))
                downloaded += 1
            except Exception as exc:
                logger.warning(f"S3 download failed: {key} → {dest}: {exc}")

    return downloaded


def download_prefix_sync(s3_prefix: str, local_dir: str | Path) -> int:
    """Download all files from an S3 prefix to a local directory (blocking)."""
    if not is_s3_enabled():
        return 0
    try:
        count = _download_prefix(s3_prefix, Path(local_dir))
        if count:
            logger.info(f"S3 ↓ {s3_prefix}/ → {local_dir} ({count} files)")
        return count
    except Exception as exc:
        logger.warning(f"S3 download sync failed for {s3_prefix}: {exc}")
        return 0


# ── Upload helpers ────────────────────────────────────────────────────────────


def _upload_single(local_path: Path, s3_key: str) -> bool:
    """Upload a single file to S3. Returns True on success."""
    if not is_s3_enabled():
        return False

    try:
        client = _get_client()
        client.upload_file(str(local_path), _get_bucket(), s3_key)
        return True
    except Exception as exc:
        logger.warning(f"S3 upload failed: {local_path} → {s3_key}: {exc}")
        return False


def fire_and_forget_upload(local_path: str | Path, s3_key: str) -> None:
    """Upload a file to S3 in a background thread (non-blocking)."""
    if not is_s3_enabled():
        return

    local_path = Path(local_path)
    if not local_path.is_file():
        return

    thread = threading.Thread(
        target=_upload_single,
        args=(local_path, s3_key),
        daemon=True,
    )
    thread.start()


def upload_directory_sync(local_dir: str | Path, s3_prefix: str) -> int:
    """Upload all files from a local directory to an S3 prefix (blocking).

    Only uploads files; does not delete S3 objects that aren't local.
    Returns count of uploaded files.
    """
    if not is_s3_enabled():
        return 0

    local_dir = Path(local_dir)
    if not local_dir.is_dir():
        return 0

    uploaded = 0
    for file_path in local_dir.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(local_dir)
        s3_key = f"{s3_prefix.rstrip('/')}/{relative}"
        if _upload_single(file_path, s3_key):
            uploaded += 1

    if uploaded:
        logger.info(f"S3 ↑ {local_dir} → {s3_prefix}/ ({uploaded} files)")
    return uploaded


def fire_and_forget_directory_upload(local_dir: str | Path, s3_prefix: str) -> None:
    """Upload all files from a local directory to S3 in a background thread."""
    if not is_s3_enabled():
        return

    thread = threading.Thread(
        target=upload_directory_sync,
        args=(local_dir, s3_prefix),
        daemon=True,
    )
    thread.start()


# ── Startup sync ──────────────────────────────────────────────────────────────

# Folder mapping: S3 prefix → local path derivation
# These match the env vars set in deploy-aws.sh's App Runner configuration.

def _resolve_tracked_folders() -> list[tuple[str, Path]]:
    """Return list of (s3_prefix, local_path) pairs to sync on startup."""
    pairs: list[tuple[str, Path]] = []

    # docs/ → DOCS_ROOT from webapp.py
    docs_root = Path(__file__).resolve().parent / "docs"
    pairs.append(("docs", docs_root))

    # output/ → REPORTS_OUTPUT_FOLDER
    output_folder = os.environ.get("REPORTS_OUTPUT_FOLDER", "./output")
    pairs.append(("output", Path(output_folder)))

    # input/ → INPUT_FOLDER
    input_folder = os.environ.get("INPUT_FOLDER", "./input")
    pairs.append(("input", Path(input_folder)))

    # .langgraph_api/ → relative to project root
    langgraph_api = Path(__file__).resolve().parent / ".langgraph_api"
    pairs.append((".langgraph_api", langgraph_api))

    return pairs


def startup_sync() -> None:
    """Download all tracked folders from S3 to local filesystem on app startup.

    Called from webapp.py lifespan. Silent no-op if S3 is not configured.
    """
    if not is_s3_enabled():
        logger.debug("S3 sync not configured — skipping startup sync")
        return

    logger.info("S3 startup sync: downloading tracked folders...")
    total = 0
    for s3_prefix, local_path in _resolve_tracked_folders():
        count = download_prefix_sync(s3_prefix, local_path)
        total += count

    logger.info(f"S3 startup sync complete: {total} files downloaded")


# ── S3 key helpers for callers ────────────────────────────────────────────────


def local_path_to_s3_key(local_path: str | Path, base_s3_prefix: str, base_local_dir: str | Path) -> str:
    """Convert a local file path to an S3 key relative to a base prefix.

    Example:
        local_path_to_s3_key(
            "/deps/deep_research/docs/policy/report.pdf",
            "docs",
            "/deps/deep_research/docs"
        ) → "docs/policy/report.pdf"
    """
    local_path = Path(local_path)
    base_local_dir = Path(base_local_dir)
    try:
        relative = local_path.relative_to(base_local_dir)
    except ValueError:
        # Fallback: use filename only
        relative = Path(local_path.name)
    return f"{base_s3_prefix.rstrip('/')}/{relative}"
