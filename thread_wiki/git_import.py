"""Safe public Git repository import for AST-aware thread wiki ingestion."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

_DEFAULT_ALLOWED_HOSTS = frozenset(
    {"github.com", "gitlab.com", "bitbucket.org"}
)
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
        "vendor",
    }
)
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class GitImportError(ValueError):
    """Safe validation or import failure suitable for an API response."""


@dataclass(frozen=True, slots=True)
class ParsedGitRepository:
    """Validated public repository location."""

    url: str
    host: str
    slug: str


@dataclass(frozen=True, slots=True)
class GitImportResult:
    """Installed repository metadata."""

    repository_url: str
    ref: str | None
    destination: Path
    file_count: int
    total_bytes: int


GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def _env_positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def _allowed_hosts() -> frozenset[str]:
    configured = os.getenv("WIKI_GIT_ALLOWED_HOSTS")
    if not configured:
        return _DEFAULT_ALLOWED_HOSTS
    return frozenset(
        host.strip().lower()
        for host in configured.split(",")
        if host.strip()
    )


def validate_git_repo_url(url: str) -> ParsedGitRepository:
    """Validate an anonymous HTTPS URL on the configured public host allowlist."""
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https":
        raise GitImportError("Repository URL must use HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise GitImportError("Repository URL must not contain credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise GitImportError("Repository URL contains an invalid port.") from exc
    if port is not None:
        raise GitImportError("Repository URL must not specify a custom port.")
    host = (parsed.hostname or "").lower()
    if host not in _allowed_hosts():
        raise GitImportError("Repository host is not allowed for public import.")
    if parsed.query or parsed.fragment or parsed.params:
        raise GitImportError("Repository URL must not contain query or fragment data.")

    decoded_path = unquote(parsed.path)
    parts = [part for part in decoded_path.split("/") if part]
    if len(parts) < 2 or any(part in {".", ".."} for part in parts):
        raise GitImportError("Repository URL path is invalid.")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
        raise GitImportError("Repository URL path contains unsupported characters.")
    parts[-1] = parts[-1].removesuffix(".git")
    if not parts[-1]:
        raise GitImportError("Repository name is missing.")
    slug = "-".join([host, *parts])
    return ParsedGitRepository(url=candidate, host=host, slug=slug)


def validate_git_ref(ref: str | None) -> str | None:
    """Validate an optional branch or tag without invoking a shell."""
    if ref is None:
        return None
    candidate = ref.strip()
    if (
            not _REF_PATTERN.fullmatch(candidate)
            or candidate.startswith("-")
            or candidate.startswith("/")
            or ".." in candidate
            or "@{" in candidate
            or "//" in candidate
            or candidate.endswith(("/", "."))
    ):
        raise GitImportError("Branch or tag name is invalid.")
    return candidate


def _prune_checkout(checkout: Path) -> None:
    """Remove metadata, ignored build trees, and all symbolic links."""
    for current, directories, files in os.walk(checkout, topdown=True):
        current_path = Path(current)
        retained: list[str] = []
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                path.unlink()
            elif name in _IGNORED_DIRECTORIES:
                shutil.rmtree(path)
            else:
                retained.append(name)
        directories[:] = retained
        for name in files:
            path = current_path / name
            if path.is_symlink():
                path.unlink()


def _measure_checkout(checkout: Path) -> tuple[int, int]:
    max_files = _env_positive_int("WIKI_GIT_IMPORT_MAX_FILES", 5_000)
    max_bytes = _env_positive_int(
        "WIKI_GIT_IMPORT_MAX_BYTES",
        104_857_600,
    )
    file_count = 0
    total_bytes = 0
    for path in sorted(checkout.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        file_count += 1
        if file_count > max_files:
            raise GitImportError(
                f"Repository exceeds import file limit of {max_files}."
            )
        try:
            total_bytes += path.stat().st_size
        except OSError as exc:
            raise GitImportError("Repository file metadata could not be read.") from exc
        if total_bytes > max_bytes:
            raise GitImportError(
                f"Repository exceeds import size limit of {max_bytes} bytes."
            )
    if file_count == 0:
        raise GitImportError("Repository contains no importable files.")
    return file_count, total_bytes


def import_public_git_repository(
        docs_dir: Path,
        url: str,
        *,
        ref: str | None = None,
        runner: GitRunner = subprocess.run,
) -> GitImportResult:
    """Shallow-clone, validate, and install a public repository under docs."""
    repository = validate_git_repo_url(url)
    checked_ref = validate_git_ref(ref)
    docs_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=".git-import-", dir=docs_dir))
    checkout = temp_root / "checkout"
    destination = docs_dir / "repositories" / repository.slug
    backup = temp_root / "previous"
    command = [
        "git",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "core.hooksPath=/dev/null",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
    ]
    if checked_ref is not None:
        command.extend(["--branch", checked_ref])
    command.extend(["--", repository.url, str(checkout)])
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    timeout = _env_positive_int("WIKI_GIT_IMPORT_TIMEOUT_SECONDS", 120)

    try:
        try:
            completed = runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitImportError(
                f"Repository clone exceeded timeout of {timeout} seconds."
            ) from exc
        except OSError as exc:
            raise GitImportError("Git client is unavailable.") from exc
        if completed.returncode != 0 or not checkout.is_dir():
            raise GitImportError(
                "Repository clone failed. Confirm the URL and branch/tag are public."
            )

        _prune_checkout(checkout)
        file_count, total_bytes = _measure_checkout(checkout)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.replace(backup)
        try:
            checkout.replace(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return GitImportResult(
            repository_url=repository.url,
            ref=checked_ref,
            destination=destination,
            file_count=file_count,
            total_bytes=total_bytes,
        )
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)
