"""Transparent Azure Blob Storage layer.

Provides bi-directional sync between local filesystem paths and an Azure Blob Storage container,
so the application can read/write files locally while changes persist to Blob Storage.

The app always reads and writes to local paths. This module adds:
- **Startup sync**: download blobs under prefixes to local dirs on app boot.
- **Background uploads**: push newly-written files to Blob Storage after each write.
- **Fire-and-forget**: uploads never block the request path.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

from logger_utils import setup_logger

logger = setup_logger(__name__)

# ── Blob Storage Client (lazy singleton) ──────────────────────────────────────

_blob_service_client = None
_client_lock = threading.Lock()


class AzureStorageConfigurationError(RuntimeError):
    """Raised when required Azure Storage configuration is incomplete."""


def _validate_azure_storage_configuration(*, required: bool) -> bool:
    missing = []
    if not os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        if not os.environ.get("STORAGE_ACCOUNT_NAME") or not os.environ.get("STORAGE_ACCOUNT_KEY"):
            missing.append("STORAGE_ACCOUNT_NAME and STORAGE_ACCOUNT_KEY (or AZURE_STORAGE_CONNECTION_STRING)")
    if not os.environ.get("AZURE_STORAGE_CONTAINER_NAME"):
        missing.append("AZURE_STORAGE_CONTAINER_NAME")

    if not missing:
        return True
    if required:
        raise AzureStorageConfigurationError(
            "missing required Azure Blob Storage configuration: " + ", ".join(missing)
        )
    return False


def is_azure_storage_enabled() -> bool:
    """Return True if Azure Blob Storage sync is configured (env vars present)."""
    return bool(
        os.environ.get("AZURE_STORAGE_CONTAINER_NAME")
        and (
                os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
                or (os.environ.get("STORAGE_ACCOUNT_NAME") and os.environ.get("STORAGE_ACCOUNT_KEY"))
        )
    )


def _get_client():
    """Lazy-initialize and return a BlobServiceClient."""
    global _blob_service_client
    if _blob_service_client is not None:
        return _blob_service_client
    with _client_lock:
        if _blob_service_client is None:
            conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
            if conn_str:
                _blob_service_client = BlobServiceClient.from_connection_string(conn_str)
            else:
                account_name = os.environ["STORAGE_ACCOUNT_NAME"]
                account_key = os.environ["STORAGE_ACCOUNT_KEY"]
                _blob_service_client = BlobServiceClient(
                    account_url=f"https://{account_name}.blob.core.windows.net",
                    credential=account_key
                )
            logger.info("Azure Blob Storage client initialized successfully.")
    return _blob_service_client


def _get_container_client():
    client = _get_client()
    container_name = os.environ["AZURE_STORAGE_CONTAINER_NAME"]
    return client.get_container_client(container_name)


# ── Download helpers ──────────────────────────────────────────────────────────


def _download_prefix(blob_prefix: str, local_dir: Path) -> int:
    """Download all blobs under blob_prefix to local_dir. Returns count of downloaded files."""
    if not is_azure_storage_enabled():
        return 0

    container_client = _get_container_client()
    prefix = blob_prefix.rstrip("/") + "/"
    local_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = local_dir.resolve()

    downloaded = 0
    try:
        blobs = container_client.list_blobs(name_starts_with=prefix)
        for blob in blobs:
            blob_name = blob.name
            if not isinstance(blob_name, str) or not blob_name.startswith(prefix):
                continue
            relative = blob_name[len(prefix):]
            if not relative or relative.endswith("/"):
                continue  # skip directory markers or empty parts

            components = relative.split("/")
            if any(component in {"", ".", ".."} for component in components):
                raise ValueError(f"unsafe blob name: {blob_name}")
            dest = resolved_root.joinpath(*components).resolve()
            try:
                dest.relative_to(resolved_root)
            except ValueError as exc:
                raise ValueError(f"unsafe blob name: {blob_name}") from exc
            dest.parent.mkdir(parents=True, exist_ok=True)

            blob_client = container_client.get_blob_client(blob_name)
            with open(dest, "wb") as f:
                download_stream = blob_client.download_blob()
                f.write(download_stream.readall())
            downloaded += 1
    except ResourceNotFoundError:
        logger.warning(f"Azure Container not found or empty: {os.environ.get('AZURE_STORAGE_CONTAINER_NAME')}")
    return downloaded


def download_prefix_sync(blob_prefix: str, local_dir: str | Path) -> int:
    """Download all files from a Blob prefix to a local directory (blocking)."""
    if not is_azure_storage_enabled():
        return 0
    try:
        count = _download_prefix(blob_prefix, Path(local_dir))
        if count:
            logger.info(f"Azure ↓ {blob_prefix}/ → {local_dir} ({count} files)")
        return count
    except Exception as exc:
        logger.warning(f"Azure Storage download sync failed for {blob_prefix}: {exc}")
        return 0


# ── Upload helpers ────────────────────────────────────────────────────────────


def _upload_single(local_path: Path, blob_name: str) -> bool:
    """Upload a single file to Azure Blob Storage. Returns True on success."""
    if not is_azure_storage_enabled():
        return False

    try:
        container_client = _get_container_client()
        blob_client = container_client.get_blob_client(blob_name)
        with open(local_path, "rb") as data:
            blob_client.upload_blob(data, overwrite=True)
        return True
    except Exception as exc:
        logger.warning(f"Azure Storage upload failed: {local_path} → {blob_name}: {exc}")
        return False


def fire_and_forget_upload(local_path: str | Path, blob_name: str) -> None:
    """Upload a file to Blob Storage in a background thread (non-blocking)."""
    if not is_azure_storage_enabled():
        return

    local_path = Path(local_path)
    if not local_path.is_file():
        return

    thread = threading.Thread(
        target=_upload_single,
        args=(local_path, blob_name),
        daemon=True,
    )
    thread.start()


def upload_directory_sync(local_dir: str | Path, blob_prefix: str) -> int:
    """Upload all files from a local directory to a Blob prefix (blocking).

    Returns count of uploaded files.
    """
    if not is_azure_storage_enabled():
        return 0

    local_dir = Path(local_dir)
    if not local_dir.is_dir():
        return 0

    uploaded = 0
    for file_path in local_dir.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(local_dir)
        blob_name = f"{blob_prefix.rstrip('/')}/{relative}"
        if _upload_single(file_path, blob_name):
            uploaded += 1

    if uploaded:
        logger.info(f"Azure ↑ {local_dir} → {blob_prefix}/ ({uploaded} files)")
    return uploaded


def fire_and_forget_directory_upload(local_dir: str | Path, blob_prefix: str) -> None:
    """Upload all files from a local directory to Blob Storage in a background thread."""
    if not is_azure_storage_enabled():
        return

    thread = threading.Thread(
        target=upload_directory_sync,
        args=(local_dir, blob_prefix),
        daemon=True,
    )
    thread.start()


# ── Startup sync ──────────────────────────────────────────────────────────────


def _resolve_tracked_folders() -> list[tuple[str, Path]]:
    """Return list of (blob_prefix, local_path) pairs to sync on startup."""
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

    # .langgraph_api/ → .langgraph_api (for checkpoints)
    langgraph_api_dir = Path(__file__).resolve().parent / ".langgraph_api"
    pairs.append((".langgraph_api", langgraph_api_dir))

    return pairs


def startup_sync() -> int:
    """Download all tracked folders from Blob Storage to local filesystem on app startup.

    Called from webapp.py lifespan or entrypoint.sh.
    """
    if not is_azure_storage_enabled():
        logger.debug("Azure Blob Storage sync not configured — skipping startup sync")
        return 0

    logger.info("Azure Blob Storage startup sync: downloading tracked folders...")
    total = 0
    for blob_prefix, local_path in _resolve_tracked_folders():
        count = _download_prefix(blob_prefix, local_path)
        total += count

    logger.info(f"Azure Blob Storage startup sync complete: {total} files downloaded")
    return total


# ── CLI support ───────────────────────────────────────────────────────────────


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m azure_storage",
        description="Synchronize generic application folders with Azure Blob Storage.",
    )
    parser.add_subparsers(dest="command", required=True).add_parser("startup")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a required generic Azure Blob Storage startup sync."""
    args = _cli_parser().parse_args(argv)
    if args.command != "startup":
        return 2
    try:
        _validate_azure_storage_configuration(required=True)
    except AzureStorageConfigurationError as exc:
        sys.stderr.write(f"Azure Storage startup configuration failed: {exc}\n")
        return 2
    try:
        startup_sync()
    except Exception as exc:
        logger.error("Azure Storage startup sync failed: %s", exc)
        sys.stderr.write(f"Azure Storage startup sync failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
