"""All HTTP route handlers for the webapp.

Routes are registered on module-level functions decorated directly with
``@app.get/post/delete``.  The FastAPI ``app`` instance is imported lazily
(via ``_get_app()``) to avoid circular-import issues with ``webapp/__init__``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import sys
import zipfile
from typing import Any

import yaml
from fastapi import File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from pathlib import Path, PurePosixPath

import webapp.config as _cfg
from research_agent.utils.content_extractors import extract_supported_document
from research_agent.utils.skill_registry import get_skill_registry
from webapp.auth_helpers import is_authenticated
from webapp.model_diagnostics import run_model_diagnostics
from webapp.utils import (
    detect_media_type,
    extract_thread_id_from_folder,
    format_bytes,
    get_free_space,
    safe_filename,
    safe_relative_folder,
)
from webapp.wiki_hooks import trigger_wiki_auto_ingest, trigger_wiki_delete_hooks

logger = logging.getLogger(__name__)


def _webapp_module():
    """Return the top-level ``webapp`` package module (for monkeypatched attrs)."""
    return sys.modules["webapp"]


# ── Health ────────────────────────────────────────────────────────────────────


def register_health_routes(app) -> None:
    """Register the ``/health`` endpoint on the FastAPI app."""

    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        m = _webapp_module()
        free_space = await asyncio.to_thread(get_free_space, m.DOCS_ROOT.parent)
        return {
            "status": "healthy",
            "version": m.API_VERSION,
            "docs_root": str(m.DOCS_ROOT),
            "free_space_bytes": free_space,
            "free_space_human": format_bytes(free_space),
        }


# ── Storage ───────────────────────────────────────────────────────────────────


def register_storage_routes(app) -> None:
    """Register the ``/storage/info`` endpoint on the FastAPI app."""

    @app.get("/storage/info")
    async def storage_info(request: Request, x_api_key: str | None = Header(None)):
        """Get server storage details and model factory diagnostics."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )

        m = _webapp_module()
        total, used, free = await asyncio.to_thread(
            shutil.disk_usage, str(m.DOCS_ROOT.parent)
        )

        model_diagnostics = await run_model_diagnostics()

        return {
            "storage": {
                "total_space_bytes": total,
                "used_space_bytes": used,
                "free_space_bytes": free,
                "total_space_human": format_bytes(total),
                "used_space_human": format_bytes(used),
                "free_space_human": format_bytes(free),
                "usage_percentage": round((used / total) * 100, 2) if total > 0 else 0,
            },
            "model_factory": model_diagnostics,
            "environment_variables": dict(os.environ),
        }


# ── Document CRUD ─────────────────────────────────────────────────────────────


def register_document_routes(app) -> None:
    """Register document CRUD endpoints (view, extract, upload, list, download, delete)."""

    @app.get("/documents/view/{filename}")
    async def view_document(
            request: Request,
            filename: str,
            folder: str = "policy",
            x_api_key: str | None = Header(None),
    ):
        """Serve a document for inline viewing (browser renders instead of downloading)."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )

        safe_name = safe_filename(filename)
        m = _webapp_module()
        relative_folder = safe_relative_folder(folder)
        file_path = m.DOCS_ROOT.joinpath(*relative_folder.parts, safe_name)

        if not (await asyncio.to_thread(file_path.exists)):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{filename}' not found in folder '{folder}'",
            )

        if not (await asyncio.to_thread(file_path.is_file)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{filename}' is not a file",
            )

        return FileResponse(
            path=file_path,
            filename=safe_name,
            media_type=detect_media_type(file_path),
            headers={"Content-Disposition": "inline"},
        )

    @app.get("/documents/extract/{filename}")
    async def extract_document(
            request: Request,
            filename: str,
            folder: str = "policy",
            x_api_key: str | None = Header(None),
    ) -> dict:
        """Extract text/markdown content from a document for preview."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )

        safe_name = safe_filename(filename)
        m = _webapp_module()
        relative_folder = safe_relative_folder(folder)
        file_path = m.DOCS_ROOT.joinpath(*relative_folder.parts, safe_name)

        if not (await asyncio.to_thread(file_path.exists)):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{filename}' not found in folder '{folder}'",
            )

        if not (await asyncio.to_thread(file_path.is_file)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{filename}' is not a file",
            )

        supported_extensions = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md"}
        if file_path.suffix.lower() not in supported_extensions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Extraction not supported for '{file_path.suffix}' files",
            )

        try:
            content = await asyncio.to_thread(extract_supported_document, file_path)
            return {
                "filename": safe_name,
                "content": content,
            }
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except Exception as e:
            logger.error(f"Document extraction failed for '{safe_name}': {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to extract document content: {e}",
            )

    @app.post("/documents/upload", status_code=status.HTTP_201_CREATED)
    async def upload_documents(
            request: Request,
            folder: str = Form("policy"),
            files: list[UploadFile] = File(...),
            x_api_key: str | None = Header(None),
    ) -> dict:
        """Upload documents to a specified folder within docs directory."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )

        m = _webapp_module()
        relative_folder = safe_relative_folder(folder)
        destination_dir = m.DOCS_ROOT.joinpath(*relative_folder.parts)
        await asyncio.to_thread(destination_dir.mkdir, parents=True, exist_ok=True)

        MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "500"))
        max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024

        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Upload size exceeds limit of {MAX_UPLOAD_SIZE_MB}MB.",
            )

        saved: list[dict[str, Any]] = []
        total_uploaded_size = 0
        import shutil

        for upload in files:
            # Check size on the file object
            upload.file.seek(0, 2)  # seek to end
            file_size = upload.file.tell()
            upload.file.seek(0)  # reset to beginning

            if file_size > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File {upload.filename} exceeds limit of {MAX_UPLOAD_SIZE_MB}MB.",
                )

            filename = safe_filename(upload.filename)
            destination = destination_dir / filename

            def _write_stream(up_file, dest):
                with open(dest, "wb") as f:
                    shutil.copyfileobj(up_file, f)

            await asyncio.to_thread(_write_stream, upload.file, destination)
            total_uploaded_size += file_size
            saved.append(
                {
                    "filename": filename,
                    "path": str(
                        PurePosixPath("docs", *relative_folder.parts, filename)
                    ),
                    "size": file_size,
                }
            )

        free_space = await asyncio.to_thread(get_free_space, m.DOCS_ROOT.parent)

        # Auto-trigger wiki ingest for thread folders
        thread_id = extract_thread_id_from_folder(str(relative_folder))
        wiki_ingest_started = False
        if thread_id:
            asyncio.create_task(
                trigger_wiki_auto_ingest(thread_id),
                name=f"wiki-auto-ingest-trigger-{thread_id}",
            )
            wiki_ingest_started = True

        return {
            "folder": str(relative_folder),
            "count": len(saved),
            "saved": saved,
            "total_uploaded_bytes": total_uploaded_size,
            "free_space_bytes": free_space,
            "free_space_human": format_bytes(free_space),
            "wiki_ingest_started": wiki_ingest_started,
            "wiki_ingest_thread_id": thread_id if wiki_ingest_started else None,
        }

    @app.get("/documents/list")
    async def list_documents(
            request: Request,
            folder: str = "policy",
            x_api_key: str | None = Header(None),
    ) -> dict:
        """List all files in a specified folder within docs directory."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )

        m = _webapp_module()
        relative_folder = safe_relative_folder(folder)
        target_dir = m.DOCS_ROOT.joinpath(*relative_folder.parts)

        if not target_dir.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Folder '{folder}' does not exist",
            )

        if not target_dir.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{folder}' is not a directory",
            )

        def _list_items():
            res = []
            for item in target_dir.iterdir():
                if item.is_file():
                    res.append(
                        {"name": item.name, "type": "file", "size": item.stat().st_size}
                    )
                elif item.is_dir():
                    res.append({"name": item.name, "type": "folder", "size": None})
            return res

        items = await asyncio.to_thread(_list_items)
        items.sort(key=lambda x: x["name"])

        return {
            "folder": str(relative_folder),
            "count": len(items),
            "items": items,
        }

    @app.get("/documents/download/{filename}")
    async def download_document(
            request: Request,
            filename: str,
            folder: str = "policy",
            x_api_key: str | None = Header(None),
    ):
        """Download a specific file from a folder."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )

        safe_name = safe_filename(filename)
        m = _webapp_module()
        relative_folder = safe_relative_folder(folder)
        file_path = m.DOCS_ROOT.joinpath(*relative_folder.parts, safe_name)

        if not (await asyncio.to_thread(file_path.exists)):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{filename}' not found in folder '{folder}'",
            )

        if not (await asyncio.to_thread(file_path.is_file)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{filename}' is not a file",
            )

        return FileResponse(
            path=file_path,
            filename=safe_name,
            media_type=detect_media_type(file_path),
        )

    @app.delete("/documents/{filename}", status_code=status.HTTP_200_OK)
    async def delete_document(
            request: Request,
            filename: str,
            folder: str = "policy",
            x_api_key: str | None = Header(None),
    ) -> dict:
        """Delete a specific file from a folder."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )

        safe_name = safe_filename(filename)
        m = _webapp_module()
        relative_folder = safe_relative_folder(folder)
        file_path = m.DOCS_ROOT.joinpath(*relative_folder.parts, safe_name)

        if not (await asyncio.to_thread(file_path.exists)):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{filename}' not found in folder '{folder}'",
            )

        if not (await asyncio.to_thread(file_path.is_file)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{filename}' is not a file",
            )

        await asyncio.to_thread(file_path.unlink)

        # Trigger wiki delete hooks for thread folders
        thread_id = extract_thread_id_from_folder(str(relative_folder))
        if thread_id:
            asyncio.create_task(
                trigger_wiki_delete_hooks(thread_id, deleted_filename=safe_name),
                name=f"wiki-delete-hook-{thread_id}",
            )

        return {
            "message": f"File '{filename}' deleted successfully",
            "folder": str(relative_folder),
            "filename": safe_name,
        }

    @app.delete("/documents/folder/{folder}", status_code=status.HTTP_200_OK)
    async def delete_folder_contents(
            request: Request,
            folder: str,
            x_api_key: str | None = Header(None),
    ) -> dict:
        """Delete all files in a specified folder."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )

        m = _webapp_module()
        relative_folder = safe_relative_folder(folder)
        target_dir = m.DOCS_ROOT.joinpath(*relative_folder.parts)

        if not target_dir.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Folder '{folder}' does not exist",
            )

        if not target_dir.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{folder}' is not a directory",
            )

        def _delete_files():
            deleted_count = 0
            for item in target_dir.iterdir():
                if item.is_file():
                    item.unlink()
                    deleted_count += 1
            return deleted_count

        deleted_count = await asyncio.to_thread(_delete_files)

        # Trigger wiki delete hooks for thread folders
        thread_id = extract_thread_id_from_folder(str(relative_folder))
        if thread_id:
            asyncio.create_task(
                trigger_wiki_delete_hooks(thread_id),
                name=f"wiki-folder-delete-hook-{thread_id}",
            )

        return {
            "message": f"All files deleted from folder '{folder}'",
            "folder": str(relative_folder),
            "deleted_count": deleted_count,
        }


# ── OAuth Authentication ──────────────────────────────────────────────────────


def register_oauth_routes(app) -> None:
    """Register OAuth authentication endpoints (login, callback, validate, refresh, logout)."""

    @app.get("/auth/login/{provider}")
    async def oauth_login(
            provider: str, request: Request, redirect_url: str | None = None
    ):
        """Initiate OAuth login with Google or GitHub."""
        if not _cfg.OAUTH_ENABLED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth authentication is not enabled. Install required dependencies.",
            )

        if provider not in ("google", "github"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported OAuth provider: {provider}. Use 'google' or 'github'.",
            )

        # Dynamically detect target frontend URL
        target_frontend = None
        if redirect_url:
            cleaned_url = redirect_url.rstrip("/")
            if cleaned_url in _cfg.FRONTEND_ORIGINS:
                target_frontend = cleaned_url

        if not target_frontend:
            referer = request.headers.get("referer")
            if referer:
                from urllib.parse import urlparse

                try:
                    parsed = urlparse(referer)
                    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
                    if origin in _cfg.FRONTEND_ORIGINS:
                        target_frontend = origin
                except Exception:
                    pass

        if not target_frontend:
            target_frontend = (
                _cfg.FRONTEND_ORIGINS[0]
                if _cfg.FRONTEND_ORIGINS
                else "http://localhost:3000"
            )

        request.session["oauth_frontend_url"] = target_frontend

        forwarded_proto = request.headers.get("x-forwarded-proto", "http")
        forwarded_host = request.headers.get(
            "x-forwarded-host", request.headers.get("host", "")
        )
        base_url = (
            f"{forwarded_proto}://{forwarded_host}"
            if forwarded_host
            else str(request.base_url).rstrip("/")
        )
        redirect_uri = f"{base_url}/auth/callback/{provider}"

        try:
            login_url = await _cfg.get_oauth_login_url(request, provider, redirect_uri)
            return RedirectResponse(url=login_url)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to generate login URL: {e!s}",
            )

    @app.get("/auth/callback/{provider}")
    async def oauth_callback(provider: str, request: Request):
        """Handle OAuth callback from Google or GitHub."""
        if not _cfg.OAUTH_ENABLED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth authentication is not enabled.",
            )

        if provider not in ("google", "github"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported OAuth provider: {provider}.",
            )

        try:
            if provider == "google":
                user_data = await _cfg.handle_google_callback(request)
            else:
                user_data = await _cfg.handle_github_callback(request)

            frontend_url = request.session.pop("oauth_frontend_url", None)
            if not frontend_url:
                frontend_url = (
                    _cfg.FRONTEND_ORIGINS[0]
                    if _cfg.FRONTEND_ORIGINS
                    else "http://localhost:3000"
                )
            frontend_url = frontend_url.rstrip("/")

            session_token = user_data["session_token"]
            return RedirectResponse(
                url=f"{frontend_url}/login/success?token={session_token}"
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"OAuth authentication failed: {e!s}",
            )

    @app.get("/auth/session/validate")
    async def validate_session(request: Request, x_api_key: str | None = Header(None)):
        """Validate an OAuth session token."""
        if not _cfg.OAUTH_ENABLED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth authentication is not enabled.",
            )

        token = x_api_key
        if not token:
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header[7:]

        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing session token.",
            )

        user_data = _cfg.user_manager.validate_session(token)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session token.",
            )

        return {
            "valid": True,
            "user": {
                "identity": user_data["identity"],
                "email": user_data.get("email"),
                "name": user_data.get("name"),
                "provider": user_data.get("provider"),
                "avatar_url": user_data.get("picture") or user_data.get("avatar_url"),
            },
            "metadata": {
                k: v
                for k, v in user_data.items()
                if k
                   not in {
                       "identity",
                       "email",
                       "name",
                       "provider",
                       "picture",
                       "avatar_url",
                       "raw_token",
                       "session_token",
                   }
            },
        }

    @app.post("/auth/session/refresh")
    async def refresh_session(request: Request, x_api_key: str | None = Header(None)):
        """Refresh (extend) an OAuth session token by 24 hours."""
        if not _cfg.OAUTH_ENABLED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth authentication is not enabled.",
            )

        token = x_api_key
        if not token:
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header[7:]

        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing session token.",
            )

        user_data = _cfg.user_manager.refresh_session(token)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session token.",
            )

        return {
            "valid": True,
            "user": {
                "identity": user_data["identity"],
                "email": user_data.get("email"),
                "name": user_data.get("name"),
                "provider": user_data.get("provider"),
                "avatar_url": user_data.get("picture") or user_data.get("avatar_url"),
            },
        }

    @app.post("/auth/logout")
    async def logout(request: Request, x_api_key: str | None = Header(None)):
        """Logout user by invalidating their OAuth session token."""
        if not _cfg.OAUTH_ENABLED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth authentication is not enabled.",
            )

        token = x_api_key
        if not token:
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header[7:]

        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing session token.",
            )

        identity = _cfg.handle_logout(token)
        if not identity:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found or already expired.",
            )

        # Clean up the logged users tracking in auth module
        from auth import _logged_oauth_users

        if identity in _logged_oauth_users:
            _logged_oauth_users.discard(identity)
            logger.info(f"✅ Cleaned up logged user tracking for: {identity}")

        return {
            "success": True,
            "message": "Successfully logged out",
            "identity": identity,
        }


# ── Skills ────────────────────────────────────────────────────────────────────


def register_skills_routes(app) -> None:
    """Register skill management endpoints (list, upload, delete)."""

    @app.get("/skills")
    async def list_skills(request: Request, x_api_key: str | None = Header(None)):
        """List all available skills from deep_research."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )
        try:

            def _load_skills():
                registry = get_skill_registry()
                skills_list = []
                seen_ids = set()
                frontmatter_re = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)

                # 1. Standard loaded skills from skill_registry
                if registry:
                    for s_id in registry.list_skill_ids():
                        info = registry.get_skill_info(s_id)
                        if info:
                            skills_list.append(
                                {
                                    "id": info.skill_id,
                                    "name": info.name,
                                    "description": info.description,
                                    "source": "system",
                                    "is_removable": False,
                                    "keywords": info.keywords,
                                }
                            )
                            seen_ids.add(info.skill_id)
                            seen_ids.add(info.name)

                # 2. System / Migrated skills from .deepagents/skills/
                deepagents_skills_dir = (
                        Path(__file__).resolve().parent.parent / ".deepagents" / "skills"
                )
                if deepagents_skills_dir.is_dir():
                    for skill_dir in deepagents_skills_dir.iterdir():
                        if not skill_dir.is_dir():
                            continue
                        skill_file = skill_dir / "SKILL.md"
                        if not skill_file.is_file():
                            continue
                        try:
                            content = skill_file.read_text(encoding="utf-8")
                            match = frontmatter_re.match(content)
                            if match:
                                fm = yaml.safe_load(match.group(1)) or {}
                                name = fm.get("name", skill_dir.name)
                                if (
                                        name not in seen_ids
                                        and skill_dir.name not in seen_ids
                                ):
                                    skills_list.append(
                                        {
                                            "id": skill_dir.name,
                                            "name": name,
                                            "description": (
                                                    fm.get("description") or ""
                                            ).strip(),
                                            "source": "system",
                                            "is_removable": False,
                                            "keywords": fm.get("keywords", []),
                                        }
                                    )
                                    seen_ids.add(name)
                                    seen_ids.add(skill_dir.name)
                        except Exception as err:
                            logger.warning(f"Error parsing skill in {skill_dir}: {err}")

                # 3. Uploaded custom skills from ./doc/.deepagents/skills/ and ./docs/.deepagents/skills/
                docs_skills_dir = (
                        Path(__file__).resolve().parent.parent
                        / "docs"
                        / ".deepagents"
                        / "skills"
                )
                if docs_skills_dir.is_dir():
                    for skill_dir in docs_skills_dir.iterdir():
                        if not skill_dir.is_dir():
                            continue
                        skill_file = skill_dir / "SKILL.md"
                        if not skill_file.is_file():
                            continue
                        try:
                            content = skill_file.read_text(encoding="utf-8")
                            match = frontmatter_re.match(content)
                            if match:
                                fm = yaml.safe_load(match.group(1)) or {}
                                name = fm.get("name", skill_dir.name)
                                if (
                                        name not in seen_ids
                                        and skill_dir.name not in seen_ids
                                ):
                                    skills_list.append(
                                        {
                                            "id": skill_dir.name,
                                            "name": name,
                                            "description": (
                                                    fm.get("description") or ""
                                            ).strip(),
                                            "source": "uploaded",
                                            "is_removable": True,
                                            "keywords": fm.get("keywords", []),
                                        }
                                    )
                                    seen_ids.add(name)
                                    seen_ids.add(skill_dir.name)
                        except Exception as err:
                            logger.warning(f"Error parsing skill in {skill_dir}: {err}")

                # Server-side sorting by skill name (case-insensitive)
                skills_list.sort(key=lambda s: s["name"].lower())
                return skills_list

            skills_list = await asyncio.to_thread(_load_skills)
            return {"skills": skills_list, "total": len(skills_list)}
        except Exception as e:
            logger.error(f"Failed to list skills: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to load skills: {str(e)}",
            )

    @app.post("/skills/upload", status_code=status.HTTP_201_CREATED)
    async def upload_skill(
            request: Request,
            file: UploadFile | None = File(None),
            files: list[UploadFile] | None = File(None),
            paths: list[str] | None = Form(None),
            x_api_key: str | None = Header(None),
    ):
        """Upload and install a new agent skill archive (.zip), SKILL.md file, or full skill directory into ./doc/.deepagents/skills/."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Provide X-API-Key header or Authorization header.",
            )
        try:
            doc_skills_dir = (
                    Path(__file__).resolve().parent.parent
                    / "docs"
                    / ".deepagents"
                    / "skills"
            )
            doc_skills_dir.mkdir(parents=True, exist_ok=True)

            installed_name = "custom_skill"

            # Multi-file folder upload handling
            if files and len(files) > 0:
                for idx, upload in enumerate(files):
                    rel_path_str = (
                        paths[idx] if (paths and idx < len(paths)) else upload.filename
                    )
                    if not rel_path_str:
                        continue
                    clean_p = PurePosixPath(rel_path_str)
                    parts = [
                        pt
                        for pt in clean_p.parts
                        if pt not in ("..", ".", "__MACOSX") and not pt.startswith("._")
                    ]
                    if not parts:
                        continue
                    installed_name = parts[0]
                    dest_file = doc_skills_dir.joinpath(*parts)
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    content = await upload.read()
                    dest_file.write_bytes(content)

            elif file is not None:
                content = await file.read()
                filename = file.filename or "uploaded_skill"
                skill_stem = safe_filename(Path(filename).stem)

                if filename.lower().endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(content)) as zf:
                        members = zf.infolist()
                        valid_members = [
                            m
                            for m in members
                            if not m.filename.startswith("__MACOSX")
                               and not m.filename.startswith("._")
                        ]
                        first_parts = {
                            m.filename.split("/")[0]
                            for m in valid_members
                            if "/" in m.filename
                        }
                        has_root_skill_md = any(
                            m.filename == "SKILL.md" for m in valid_members
                        )

                        if len(first_parts) == 1 and not has_root_skill_md:
                            zf.extractall(doc_skills_dir, members=valid_members)
                            installed_name = list(first_parts)[0]
                        else:
                            installed_name = skill_stem
                            out_dir = doc_skills_dir / installed_name
                            out_dir.mkdir(parents=True, exist_ok=True)
                            zf.extractall(out_dir, members=valid_members)
                elif filename == "SKILL.md" or filename.lower().endswith(".md"):
                    installed_name = (
                        skill_stem if skill_stem != "SKILL" else "custom_skill"
                    )
                    out_dir = doc_skills_dir / installed_name
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "SKILL.md").write_bytes(content)
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Unsupported file format. Please upload a .zip skill archive, SKILL.md file, or skill directory.",
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No file or folder data provided for upload.",
                )

            # Instantly refresh registry
            registry = get_skill_registry()
            if registry:
                registry._skills_ids = None
                registry.reload_all()

            return {
                "success": True,
                "message": f"Skill '{installed_name}' uploaded to ./doc/.deepagents/skills/ and active immediately.",
                "skill_name": installed_name,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to process skill upload: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to upload skill: {str(e)}",
            )

    @app.delete("/skills/{skill_id}")
    async def delete_skill(
            skill_id: str, request: Request, x_api_key: str | None = Header(None)
    ):
        """Remove an uploaded skill from ./doc/.deepagents/skills/."""
        if not is_authenticated(x_api_key, request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key.",
            )
        try:
            doc_skills_dir = (
                    Path(__file__).resolve().parent.parent
                    / "docs"
                    / ".deepagents"
                    / "skills"
            )
            skill_dir = doc_skills_dir / skill_id
            if not skill_dir.exists() or not skill_dir.is_dir():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Uploaded skill '{skill_id}' not found in ./docs/.deepagents/skills/ or cannot be removed.",
                )
            await asyncio.to_thread(
                shutil.rmtree, str(skill_dir), ignore_errors=True, onerror=None
            )

            registry = get_skill_registry()
            if registry:
                registry._skills_ids = None
                registry.reload_all()

            return {
                "success": True,
                "message": f"Skill '{skill_id}' removed successfully.",
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete skill '{skill_id}': {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to remove skill: {str(e)}",
            )


# ── Chat Thread State (simplified LangGraph protocol, in-memory) ────────────────

# In-memory store for thread state.  Mirrors the LangGraph /threads/{id}/state
# protocol so the frontend cross-deployment sync can use the same API shape.
#
# Each entry: {"values": dict, "last_access": float, "created": float}
_chat_thread_state: dict[str, dict[str, Any]] = {}

_MAX_THREADS = 1000
_MAX_VALUE_SIZE = 2_000_000  # 2 MB per value
_TTL_SECONDS = 3 * 24 * 3600  # 3 days


def _touch_thread(thread_id: str) -> None:
    """Record that *thread_id* was just accessed (creates entry if missing)."""
    now = __import__("time").time()
    entry = _chat_thread_state.get(thread_id)
    if entry is None:
        _chat_thread_state[thread_id] = {
            "values": {},
            "last_access": now,
            "created": now,
        }
    else:
        entry["last_access"] = now


def _cleanup_expired() -> None:
    """Remove threads that haven't been accessed in _TTL_SECONDS.

    Called lazily on every GET / POST so the store never grows unbounded.
    """
    cutoff = __import__("time").time() - _TTL_SECONDS
    expired = [
        tid
        for tid, entry in _chat_thread_state.items()
        if entry.get("last_access", 0) < cutoff
    ]
    for tid in expired:
        del _chat_thread_state[tid]
    if expired:
        logger.info("chat_thread_state: expired %d thread(s)", len(expired))


def register_chat_thread_routes(app) -> None:
    """Register chat thread state and management endpoints."""

    @app.get("/chat_threads/{thread_id}/state")
    async def get_chat_thread_state(thread_id: str):
        """Return thread state values.  No auth — thread ID is the access key."""
        _cleanup_expired()
        _touch_thread(thread_id)
        entry = _chat_thread_state.get(thread_id, {})
        return {"values": entry.get("values", {})}

    @app.post("/chat_threads/{thread_id}/state")
    async def update_chat_thread_state(thread_id: str, body: dict[str, Any]):
        """Update thread state values.  No auth — thread ID is the access key.

        Expects JSON body: ``{"values": {key: value, ...}}``.
        Merges with existing values so partial updates are safe.
        """
        _cleanup_expired()

        incoming = body.get("values") if isinstance(body, dict) else None
        if not isinstance(incoming, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Body must contain a 'values' object.",
            )

        # Size check on incoming values
        for key, val in incoming.items():
            val_str = str(val) if not isinstance(val, str) else val
            if len(val_str.encode("utf-8")) > _MAX_VALUE_SIZE:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Value for '{key}' exceeds {_MAX_VALUE_SIZE} bytes.",
                )

        _touch_thread(thread_id)
        entry = _chat_thread_state[thread_id]
        current = entry.get("values", {})
        current.update(incoming)
        entry["values"] = current

        # Evict oldest entries if over the max-threads limit
        if len(_chat_thread_state) > _MAX_THREADS:
            sorted_entries = sorted(
                _chat_thread_state.items(),
                key=lambda kv: kv[1].get("last_access", 0),
            )
            to_evict = sorted_entries[: len(_chat_thread_state) - _MAX_THREADS]
            for tid, _ in to_evict:
                del _chat_thread_state[tid]
            logger.info(
                "chat_thread_state: evicted %d oldest thread(s) (max=%d)",
                len(to_evict),
                _MAX_THREADS,
            )

        logger.debug(
            "chat_thread_state updated: thread=%s keys=%s",
            thread_id,
            list(incoming.keys()),
        )

        return {"success": True}


# ── Convenience: register everything at once ────────────────────────────────────


def register_all_routes(app) -> None:
    """Register health, storage, document, OAuth, skills, and chat-thread routes on *app*."""
    register_health_routes(app)
    register_storage_routes(app)
    register_document_routes(app)
    register_oauth_routes(app)
    register_skills_routes(app)
    register_chat_thread_routes(app)
