"""Knowledge filesystem configuration and document management tools.

Defines safety limits for file operations (max sizes, read limits) and provides
routines for listing, globbing, reading, and querying document workspace sections.
"""

from __future__ import annotations

import os
import random
import re
from pathlib import Path
from typing import Annotated

from deepagents.backends.utils import file_data_to_string, create_file_data
from dotenv import load_dotenv
from langgraph._internal._constants import CONFIG_KEY_SEND
from langgraph.config import get_config
from langgraph.prebuilt import InjectedState

from logger_utils import setup_logger
from research_agent.utils.content_extractors import extract_supported_document

# Load environment variables
load_dotenv()
# These can be configured via environment variables with sensible defaults
MAX_GLOB_DEPTH = int(os.environ.get("MAX_GLOB_DEPTH", "3"))
MAX_FILES_TO_READ = int(os.environ.get("MAX_FILES_TO_READ", "20"))
MAX_TOTAL_SIZE_MB = int(os.environ.get("MAX_TOTAL_SIZE_MB", "50"))
MAX_INLINE_FILE_CHARS = int(os.environ.get("MAX_INLINE_FILE_CHARS", "40000"))
LARGE_FILE_PREVIEW_CHARS = int(os.environ.get("LARGE_FILE_PREVIEW_CHARS", "12000"))
LARGE_FILE_HEADING_LIMIT = int(os.environ.get("LARGE_FILE_HEADING_LIMIT", "24"))
SECTION_CHUNK_LIMIT = int(os.environ.get("SECTION_CHUNK_LIMIT", "3"))

SUPPORTED_DOC_SUFFIXES = {".pdf", ".txt", ".md", ".docx", ".pptx", ".xlsx"}

# Project root for resolving SkillsMiddleware backend paths to filesystem paths.
# SkillsMiddleware uses the agent's internal FilesystemBackend, which produces
# paths like /skills/.deepagents/skills/<name>/SKILL.md relative to cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

logger = setup_logger(__name__)

# Global mapping of thread_id to existing_cited_responses list to bypass injected state limitations in tools
_thread_existing_cited_responses: dict[str, list[str]] = {}

# Global mapping of thread_id to wiki_query_complete status to bypass injected state limitations
_thread_wiki_query_complete: dict[str, bool] = {}

# Global mapping of thread_id to the hash of the last user message that was
# already wiki-queried during the current turn. Used by before_agent to skip
# re-running the expensive wiki query + LLM eval on every within-turn
# model iteration (which caused the write_todos infinite loop).
_thread_wiki_queried_messages: dict[str, str] = {}


def clear_thread_cache(thread_id: str) -> None:
    """Clear thread-specific cached global states to prevent memory leaks."""
    _thread_existing_cited_responses.pop(str(thread_id), None)
    _thread_wiki_query_complete.pop(str(thread_id), None)
    _thread_wiki_queried_messages.pop(str(thread_id), None)


def send_files_to_state(updates: dict) -> None:
    """Persist file updates to LangGraph state via the Pregel channel API.

    This mirrors how deepagents' built-in write_file tool persists files.
    Direct mutation of state["files"] via InjectedState does NOT persist
    because LangGraph only tracks changes queued through CONFIG_KEY_SEND.

    Args:
        updates: dict mapping file paths to FileData dicts (from create_file_data).
    """
    try:
        config = get_config()
        send = config["configurable"][CONFIG_KEY_SEND]
        send([("files", updates)])
    except Exception as e:
        logger.warning(f"Could not persist files to state: {e}")


def normalize_path_for_filesystem_tools(
        path_str: str
) -> str:
    """Normalize paths for cross-platform compatibility with deepagents filesystem tools.
    
    Deepagents filesystem tools (glob, ls, etc.) expect paths relative to the working directory.
    This function ensures paths start with './' instead of '/' for proper resolution on all platforms.
    
    Args:
        path_str: The path string to normalize
        
    Returns:
        Normalized path string with proper relative prefix
    """
    if not path_str:
        return path_str

    # Convert Windows backslashes to forward slashes for consistency
    normalized = path_str.replace('\\', '/')

    # If it's a real absolute path that exists, return it as-is
    # This is important for tests and cases where the user provides a real absolute path
    if Path(normalized.split('*')[0].split('?')[0]).is_absolute() and Path(
            normalized.split('*')[0].split('?')[0]).exists():
        return normalized

    # If path starts with '/', it's being treated as absolute from root but likely intended as relative to project
    # Convert to relative path by adding './' prefix
    if normalized.startswith('/') and not normalized.startswith('./'):
        normalized = './' + normalized.lstrip('/')
    # Ensure relative paths also start with './' for explicit relative reference
    elif not normalized.startswith('./') and not normalized.startswith('/'):
        normalized = './' + normalized

    return normalized


def write_content_to_output_folder(
        filename: str,
        content: str
) -> str:
    """Write content to a file in the output folder."""
    reports_output_folder = os.environ.get("OUTPUT_FOLDER", "./output")
    output_subfolder = Path(reports_output_folder)
    output_subfolder.mkdir(parents=True, exist_ok=True)
    file_path = output_subfolder / filename
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return normalize_path_for_filesystem_tools(str(file_path))


def _build_large_file_preview(file_path: Path, file_content: str) -> str:
    """Return a compact preview for oversized files to avoid flooding model context."""
    normalized_name = normalize_path_for_filesystem_tools(str(file_path))
    sections = _extract_markdown_sections(file_content)
    heading_lines = [section["heading"] for section in sections[:LARGE_FILE_HEADING_LIMIT]]

    preview_parts = [
        (
            f"File '{normalized_name}' is {len(file_content)} chars long; "
            "returning a structured preview instead of the full content to keep context usable."
        ),
        "",
    ]

    if heading_lines:
        preview_parts.extend([
            "Heading outline:",
            *heading_lines,
            "",
        ])

    if sections:
        preview_parts.append("Section chunks:")
        for idx, section in enumerate(sections[:SECTION_CHUNK_LIMIT], start=1):
            preview_parts.extend([
                f"{idx}. {section['heading']}",
                section["content"][:1500].rstrip(),
                "",
            ])

    preview_parts.extend([
        "Leading excerpt:",
        file_content[:LARGE_FILE_PREVIEW_CHARS].rstrip(),
    ])

    if len(file_content) > LARGE_FILE_PREVIEW_CHARS:
        preview_parts.extend([
            "",
            "Trailing excerpt:",
            file_content[-2000:].lstrip(),
        ])

    return "\n".join(preview_parts)


def _split_markdown_selector(file_path: str) -> tuple[str, str | None]:
    """Split a file path into base path and optional section selector.

    Supports the ``file.md#Section Title`` convention for targeting a
    specific heading within a Markdown file.

    Args:
        file_path: A file path possibly containing a ``#`` section selector.

    Returns:
        A tuple of ``(base_path, selector_or_none)``.
    """
    if "#" not in file_path:
        return file_path, None
    base_path, selector = file_path.split("#", 1)
    selector = selector.strip()
    return base_path, selector or None


def _normalize_heading_text(value: str) -> str:
    """Normalize heading text for case-insensitive comparison.

    Strips leading ``#`` markers, collapses whitespace, and lowercases.

    Args:
        value: Raw heading text (e.g., ``"##  Introduction  "``).

    Returns:
        A normalized, lowercased string suitable for comparison.
    """
    value = value.strip()
    value = re.sub(r"^#+\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.casefold()


def _extract_markdown_sections(file_content: str) -> list[dict[str, str]]:
    """Parse Markdown content into a list of heading + body sections.

    Args:
        file_content: Raw Markdown text.

    Returns:
        A list of dicts, each with ``"heading"`` and ``"content"`` keys.
    """
    sections: list[dict[str, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in file_content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            if current_heading is not None:
                sections.append(
                    {
                        "heading": current_heading,
                        "content": "\n".join(current_lines).strip(),
                    }
                )
            current_heading = stripped.strip()
            current_lines = [line]
            continue

        if current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        sections.append(
            {
                "heading": current_heading,
                "content": "\n".join(current_lines).strip(),
            }
        )

    return sections


def _read_markdown_section(file_path: Path, file_content: str, selector: str) -> str:
    """Return the body text of a specific Markdown section by heading.

    Args:
        file_path: Path to the file (used for error messages).
        file_content: The full Markdown content.
        selector: The heading text to match (case-insensitive).

    Returns:
        The section body if found, or an error message listing available
        headings.
    """
    sections = _extract_markdown_sections(file_content)
    normalized_selector = _normalize_heading_text(selector)

    for section in sections:
        if _normalize_heading_text(section["heading"]) == normalized_selector:
            return section["content"]

    available_sections = "\n".join(
        f"- {section['heading']}" for section in sections[:LARGE_FILE_HEADING_LIMIT]
    )
    return (
        f"Section '{selector}' not found in '{normalize_path_for_filesystem_tools(str(file_path))}'.\n"
        f"Available sections:\n{available_sections}"
    )


def _get_extracted_path(
        file_path: Path,
        output_folder: Path
) -> Path:
    """Get the target path for an extracted file."""
    suffix = file_path.suffix.lower()
    if suffix in {".pdf", ".md", ".docx", ".pptx"}:
        new_extension = ".md"
    else:
        new_extension = ".txt"

    new_filename = f"{file_path.name}_extracted{new_extension}"
    return output_folder / "extracted" / new_filename


def _resolve_doc_output_subfolder(
        folder: Path
) -> Path:
    """Resolve the output subfolder for extracted document content.

    Ensures extracted files land inside the configured ``OUTPUT_FOLDER``.

    Args:
        folder: The source document folder.

    Returns:
        The resolved output directory as a ``Path``.
    """
    reports_output_folder = os.environ.get("OUTPUT_FOLDER", "./output")
    configured_output = Path(os.environ.get("OUTPUT_FOLDER", reports_output_folder))
    if configured_output.name == folder.name:
        return configured_output
    if configured_output == Path(reports_output_folder):
        return configured_output / folder.name
    return configured_output


def _save_extracted_content(
        original_file_path: Path,
        content: str,
        output_folder: Path | None = None
) -> str:
    """Save extracted document text to a cached file for reuse.

    Args:
        original_file_path: Path to the source document.
        content: Extracted text content to save.
        output_folder: Target directory. Defaults to ``OUTPUT_FOLDER``.

    Returns:
        The normalized path to the saved extracted file.
    """
    if output_folder:
        output_dir = output_folder
    else:
        reports_output_folder = os.environ.get("OUTPUT_FOLDER", "./output")
        output_dir = Path(reports_output_folder)
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = _get_extracted_path(original_file_path, output_dir)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return normalize_path_for_filesystem_tools(str(file_path))


def ls_impl(
        path: str,
        state: Annotated[dict, InjectedState] = None
) -> str:
    """List files in a directory with fallback support.
    
    Tries to list from the virtual filesystem in state first (DeepAgents backend),
    then falls back to the local filesystem if not available.

    Args:
        path: The path to the directory to list.
        state: LangGraph state containing virtual filesystem (injected automatically).

    Returns:
        A list of files in the directory or an error message.
    """
    normalized_path = normalize_path_for_filesystem_tools(path)

    # Try 1: Check virtual filesystem in state (DeepAgents backend)
    if state and "files" in state:
        try:
            dir_files = []

            # Normalize the target directory path for comparison
            norm_dir = normalized_path.rstrip('/').lstrip('./')
            if norm_dir == '':
                norm_dir = '.'

            for file_path in state["files"]:
                # Normalize the file path
                norm_file = file_path.lstrip('/')

                # Get the parent directory of the file
                if '/' in norm_file:
                    parent_dir = '/'.join(norm_file.split('/')[:-1])
                else:
                    parent_dir = '.'

                # Check if file is directly in the target directory
                if parent_dir == norm_dir or parent_dir == normalized_path.rstrip('/'):
                    # Extract just the filename
                    filename = norm_file.split('/')[-1]
                    # Mark directories with trailing slash (we can't determine this from flat file list)
                    dir_files.append(filename)

            if dir_files:
                return "\n".join(sorted(dir_files))
        except Exception as e:
            pass  # Fall through to local filesystem

    # Try 2: Use local filesystem
    normalized_path = normalize_path_for_filesystem_tools(path)
    p = Path(normalized_path)
    # Resolve SkillsMiddleware backend paths to filesystem paths
    if p.is_absolute() and str(p).startswith("/skills/"):
        p = _PROJECT_ROOT / str(p)[len("/skills/"):]
    if not p.exists():
        return f"Error: Path '{path}' not found"
    if not p.is_dir():
        return f"Error: Path '{path}' is not a directory"

    try:
        files = [f.name + ("/" if f.is_dir() else "") for f in p.iterdir()]
        return "\n".join(sorted(files))
    except Exception as e:
        return f"Error listing directory '{path}': {e}"


def glob_impl(
        pattern: str,
        state: Annotated[dict, InjectedState] = None
) -> str:
    """Implementation of glob pattern matching with fallback support.
    
    Tries to match against the virtual filesystem in state first, then falls back
    to the local filesystem if not available.

    Args:
        pattern: The glob pattern to match (e.g., "**/*.md").
        state: LangGraph state containing virtual filesystem (injected automatically).

    Returns:
        A list of matching file paths or an error message.
    """
    normalized_pattern = normalize_path_for_filesystem_tools(pattern)

    # Try 1: Check virtual filesystem in state (DeepAgents backend)
    if state and "files" in state:
        try:
            import fnmatch
            matched_files = []

            for file_path in state["files"]:
                # Normalize the file path for comparison
                norm_file = file_path.lstrip('/')

                # Check if the file matches the pattern
                # Handle different pattern formats
                if fnmatch.fnmatch(norm_file, normalized_pattern.lstrip('./')):
                    matched_files.append(file_path)
                elif fnmatch.fnmatch(norm_file, normalized_pattern):
                    matched_files.append(file_path)
                elif fnmatch.fnmatch(file_path, normalized_pattern):
                    matched_files.append(file_path)

            if matched_files:
                return "\n".join(sorted(matched_files))
        except Exception as e:
            pass  # Fall through to local filesystem

    # Try 2: Use local filesystem
    normalized_pattern = normalize_path_for_filesystem_tools(pattern)

    # If it's a real absolute path or starts with ./, use it
    if normalized_pattern.startswith('./') or (len(normalized_pattern) > 0 and normalized_pattern[0] == '/') or (
            len(normalized_pattern) > 1 and normalized_pattern[1] == ':'):
        # Determine if it's an absolute path from the start
        is_absolute = (len(normalized_pattern) > 0 and normalized_pattern[0] == '/') or (
                len(normalized_pattern) > 1 and normalized_pattern[1] == ':')

        # For glob, we need to split the fixed part from the pattern part
        # We can't just use Path(normalized_pattern) because it might not like wildcards in some OS calls
        if is_absolute:
            # On Unix, parts[0] is '/'
            parts = Path(normalized_pattern.split('*')[0].split('?')[0]).parts
            # Reconstruct the base path from fixed parts
            base_path = Path(*parts)
            # The rest is the pattern
            glob_pattern = normalized_pattern[len(str(base_path)):]
            if glob_pattern.startswith('/'):
                glob_pattern = glob_pattern[1:]
            if not glob_pattern:
                glob_pattern = "*"
        else:
            path_obj = Path(normalized_pattern)
            parts = path_obj.parts
            fixed_parts = []
            pattern_parts = []
            found_wildcard = False
            for part in parts:
                if '*' in part or '?' in part:
                    found_wildcard = True
                if found_wildcard:
                    pattern_parts.append(part)
                else:
                    fixed_parts.append(part)

            if not fixed_parts:
                base_path = Path(".")
            else:
                base_path = Path(*fixed_parts)

            glob_pattern = "/".join(pattern_parts) if pattern_parts else "*"
    else:
        # Fallback for simple patterns or relative patterns without ./
        if "/" in normalized_pattern:
            base_path_str, glob_pattern = normalized_pattern.rsplit("/", 1)
            if not glob_pattern:  # case like "path/to/dir/"
                glob_pattern = "*"
            base_path = Path(base_path_str)
        else:
            base_path = Path(".")
            glob_pattern = normalized_pattern

    if not base_path.exists():
        return f"Error: Base path for pattern '{pattern}' not found"

    try:
        # If it's a recursive glob, handle it
        matches = list(base_path.glob(glob_pattern))
        return "\n".join(sorted(str(m.relative_to(base_path)) for m in matches))
    except Exception as e:
        return f"Error running glob for pattern '{pattern}': {e}"


def read_file_impl(
        file_path: str,
        state: Annotated[dict, InjectedState] = None
) -> str:
    """Implementation of file reading with fallback support.
    
    Tries to read from the virtual filesystem in state first (DeepAgents backend),
    then falls back to the local filesystem if not available.

    For Markdown files, you can read specific sections by appending `#` followed by
    the heading text. Example: `report.md#Introduction` or `docs/guide.md## Installation Steps`.
    The section selector is case-insensitive and matches the exact heading text (including # symbols).

    Args:
        file_path: The path to the file to read. Can include a section selector for markdown files (e.g., 'file.md#Section Title').
        state: LangGraph state containing virtual filesystem (injected automatically).

    Returns:
        The content of the file (or specific section if selector provided), or an error message if the file not found.
    """
    # Normalize path first for consistent comparison across both filesystems
    raw_file_path, section_selector = _split_markdown_selector(file_path)
    normalized_path = normalize_path_for_filesystem_tools(raw_file_path)

    # Try 1: Check state["files"] for virtual filesystem (DeepAgents backend)
    if state and "files" in state:
        # Try normalized variants with original path and normalized path for maximum compatibility
        normalized_variants = [
            file_path,
            normalized_path,
            '/' + normalized_path.lstrip('./'),
            normalized_path.lstrip('/'),
        ]

        for variant in normalized_variants:
            if variant in state["files"]:
                try:
                    file_content = file_data_to_string(state["files"][variant])
                    logger.info(f"Read from virtual filesystem: {variant}")
                    if section_selector:
                        return _read_markdown_section(Path(raw_file_path), file_content, section_selector)
                    return file_content
                except Exception:
                    continue

    # Try 2: Use local filesystem
    # Resolve relative paths in this order:
    # 1) relative to current working directory
    # 2) relative to OUTPUT_FOLDER (for short paths like "extracted/foo.md")
    input_path = Path(normalized_path)
    if input_path.is_absolute():
        # Resolve SkillsMiddleware backend paths (e.g., /skills/.deepagents/skills/...)
        # to actual filesystem paths under the project root.
        path_str = str(input_path)
        if path_str.startswith("/skills/"):
            full_path = _PROJECT_ROOT / path_str[len("/skills/"):]
        else:
            full_path = input_path
    else:
        cwd_candidate = Path(normalized_path)
        if cwd_candidate.exists():
            full_path = cwd_candidate
        else:
            reports_output_folder = os.environ.get("OUTPUT_FOLDER", "./output")
            output_dir = Path(reports_output_folder)

            relative_normalized = normalized_path.lstrip("./")
            output_root_normalized = output_dir.as_posix().lstrip("./")
            # If caller already passed a path rooted at OUTPUT_FOLDER (e.g. output/policy/...),
            # do not prepend OUTPUT_FOLDER again.
            if output_root_normalized and relative_normalized.startswith(output_root_normalized + "/"):
                full_path = Path(relative_normalized)
            else:
                full_path = output_dir / relative_normalized

    if not full_path.exists():
        return f"Error: File '{full_path}' not found"

    try:
        file_content = full_path.read_text(encoding="utf-8")
        logger.info(f"Read from local filesystem: {full_path}")
        if section_selector:
            return _read_markdown_section(full_path, file_content, section_selector)
        if len(file_content) > MAX_INLINE_FILE_CHARS:
            logger.info(
                f"Returning preview for oversized file {full_path} ({len(file_content)} chars)"
            )
            return _build_large_file_preview(full_path, file_content)
        return file_content
    except Exception as e:
        return f"Error reading file '{file_path}': {e}"


def write_file_impl(
        file_path: str,
        content: str,
        state: dict | None = None,
) -> str:
    """Write content to a file with virtual filesystem support.

    Writes content to the specified file path. If using a DeepAgents backend,
    the file is stored in the virtual filesystem. Otherwise, it writes to the
    local filesystem.

    Args:
        file_path: The path where the file should be written.
        content: The content to write to the file.
        state: Optional state dictionary to modify directly (mostly for testing).

    Returns:
        Confirmation message with the normalized file path, or an error message.
    """
    try:
        # If state is provided, update it (mostly for backward compatibility / tests)
        if state is not None:
            if "files" not in state:
                state["files"] = {}
            state["files"][file_path] = create_file_data(content)

        # Normalize the file path
        normalized_path = normalize_path_for_filesystem_tools(file_path)

        # Check if we are in a runnable context
        runnable_context = False
        try:
            get_config()
            runnable_context = True
        except Exception:
            pass

        if runnable_context:
            # Persist to LangGraph state via the channel API (same mechanism as
            # deepagents' built-in write_file tool).  Direct mutation of
            # state["files"] does NOT survive the node boundary.
            sandbox_file_path = file_path.lstrip('.') if file_path.startswith('./') else file_path
            try:
                send_files_to_state({sandbox_file_path: create_file_data(content)})
                logger.info(f"Persisted to state: {sandbox_file_path}")
                return f"Successfully wrote {len(content)} bytes to `{sandbox_file_path}`"
            except Exception as e:
                logger.warning(f"Could not persist {sandbox_file_path} to state: {e}")

        # Fallback to local filesystem
        reports_output_folder = os.environ.get("OUTPUT_FOLDER", "./output")
        output_dir = Path(reports_output_folder)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Resolve the full path
        full_path = Path(normalized_path)
        if not full_path.is_absolute():
            full_path = output_dir / normalized_path

        # Create parent directories if needed
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Write the file
        full_path.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} bytes to `{normalize_path_for_filesystem_tools(str(full_path))}`"
    except Exception as e:
        return f"Error writing file `{file_path}`: {str(e)}"


def _check_thread_wiki_ready(folder_path: str) -> tuple[bool, str | None]:
    """Check if the folder being read is a docs/threads/<thread_id> folder with a built wiki.

    Returns a tuple of (is_thread_docs_folder, wiki_content_path).
    If is_thread_docs_folder is True and wiki is ready, wiki_content_path points to the wiki dir.
    """
    try:
        # Resolve to absolute path for reliable pattern matching
        resolved = Path(folder_path).resolve()
        parts = resolved.parts

        # Look for the docs/threads/<thread_id> pattern anywhere in the path
        for i, part in enumerate(parts):
            if part == "threads" and i > 0 and parts[i - 1] == "docs":
                # Found docs/threads — the thread_id is either this component or next
                if i + 1 < len(parts):
                    thread_id = parts[i + 1]
                else:
                    # The folder itself is the threads dir, not a specific thread
                    return False, None

                # Build the expected wiki path:
                # docs/threads/<thread_id>  →  docs/threads-wiki/<thread_id>/wiki/
                base = Path(*parts[:i - 1])  # everything before "docs"
                wiki_content = base / "docs" / "threads-wiki" / thread_id / "wiki"
                index_path = wiki_content / "index.md"

                if index_path.exists():
                    content = index_path.read_text(encoding="utf-8")
                    if "_No pages yet._" not in content:
                        logger.info(
                            "[read_doc_folder] docs/threads/%s folder detected — wiki is ready at %s",
                            thread_id,
                            wiki_content,
                        )
                        return True, str(wiki_content)

                return True, None  # It IS a thread docs folder, but wiki not ready yet
    except Exception:
        pass
    return False, None


def read_docs_folder_impl(
        folder_path: str,
        specific_files: list[str] | None = None,
        state: Annotated[dict, InjectedState] = None,
) -> str:
    """Read and extract text from supported documents in a given folder.

    Use this tool when you need to research from local documents instead of or in addition
    to web search. Supported file types are PDF, text, Markdown, Word, PowerPoint, and Excel.

    If the folder contains a large number of files or the total size is very large,
    this tool will return a summary of the contents instead of all text.
    You can then use the `specific_files` argument to read particular documents of interest.

    Args:
        folder_path: The absolute or relative path to the folder containing document files.
        specific_files: Optional list of filenames within the folder to read specifically.
            If provided, only these files will be processed, bypassing general limits.
        state: LangGraph state (injected automatically, do not supply).

    Returns:
        Extracted text from supported documents, a summary for large folders, or an error message.
    """
    # ── Early exit: thread docs folder with a ready wiki ──────────────────
    # When documents have already been ingested into the wiki workspace, do NOT
    # re-process the raw PDFs from docs/threads/<thread_id>.  Skip straight to
    # the wiki so the agent uses the synthesised pages instead.
    is_thread_docs, wiki_content_path = _check_thread_wiki_ready(folder_path)
    if is_thread_docs and wiki_content_path:
        logger.info(
            "[read_doc_folder] Skipping raw document extraction for thread docs folder '%s' "
            "— wiki is already built. Redirecting agent to wiki at '%s'.",
            folder_path,
            wiki_content_path,
        )
        wiki_index_path = str(Path(wiki_content_path) / "index.md")
        return (
            "The documents in this folder have already been ingested into the wiki workspace. "
            "Do NOT re-process the raw files. "
            "Instead, use the `read_file` tool to read the synthesised wiki pages. "
            f"Start with the wiki index at `{wiki_index_path}` for an overview and list of all pages, "
            "then read individual wiki pages for detailed information.\n\n"
            "Do NOT call `read_doc_folder` again on this folder."
        )

    configured_doc_folder: str | None = None
    if state and isinstance(state, dict):
        configured_doc_folder = state.get("doc_folder")

    # Fallback: subagent state schemas may not include doc_folder, so the
    # orchestrator also persists it as an environment variable.
    if not configured_doc_folder:
        configured_doc_folder = os.environ.get("DOC_FOLDER")

    if not configured_doc_folder:
        return (
            "Error: No document folder has been configured for this research task. "
            "Pass --doc-folder <path> when invoking the CLI, or include the folder path "
            "(e.g. '--doc-folder ./docs/policy/') in your message when using the API. "
            "Do NOT attempt to read from any other filesystem path."
        )

    allowed_root = Path(configured_doc_folder).resolve()
    folder = Path(folder_path).resolve()
    try:
        folder.relative_to(allowed_root)
    except ValueError:
        logger.error(
            f"[read_doc_folder] Redirecting '{folder_path}' → '{allowed_root}' (only the configured doc_folder is permitted).")
        folder = allowed_root

    if not folder.exists(): return f"Error: Folder '{folder}' does not exist."
    if not folder.is_dir(): return f"Error: '{folder}' is not a directory."

    specific_set = set(specific_files) if specific_files else None

    # Build folder listing
    all_candidates: list[Path] = []
    for file_path in folder.rglob("*"):
        if len(file_path.relative_to(folder).parts) > MAX_GLOB_DEPTH:
            continue
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_DOC_SUFFIXES:
            all_candidates.append(file_path)
    supported_files = sorted(all_candidates)

    if not supported_files:
        return f"No supported document files found in {folder_path}. Supported types: .pdf, .txt, .md, .docx, .pptx, .xlsx."

    if specific_set:
        files_to_process = [f for f in supported_files if f.name in specific_set]
        if not files_to_process:
            return f"None of the requested files were found in {folder_path}. Available: {', '.join(f.name for f in supported_files[:10])}..."
    else:
        total_files = len(supported_files)
        total_size_mb = sum(f.lstat().st_size for f in supported_files) / (1024 * 1024)

        if total_files > MAX_FILES_TO_READ or total_size_mb > MAX_TOTAL_SIZE_MB:
            avg_size_mb = total_size_mb / total_files if total_files > 0 else 0
            max_files_by_size = max(1, int(MAX_TOTAL_SIZE_MB / avg_size_mb)) if avg_size_mb > 0 else MAX_FILES_TO_READ
            sample_size = min(MAX_FILES_TO_READ, total_files, max_files_by_size)
            auto_sample = [f.name for f in random.sample(supported_files, sample_size)]
            preview_list = "\n".join(f"- {f.name} ({f.lstat().st_size / 1024:.1f} KB)" for f in supported_files[:60])
            if total_files > 60: preview_list += f"\n... and {total_files - 60} more files (not shown)."
            auto_sample_str = ", ".join(f'"{n}"' for n in auto_sample)
            return (
                f"TOOL RESULT — folder too large to read all at once: {total_files} files, {total_size_mb:.1f} MB (limits: {MAX_FILES_TO_READ} files / {MAX_TOTAL_SIZE_MB} MB).\n\n"
                "ACTION REQUIRED — do NOT ask the user for confirmation. You MUST immediately:\n"
                f"1. Call read_doc_folder again on '{folder_path}' with specific_files set to the auto-sample below.\n"
                "2. Continue research using those documents.\n\n"
                f"Pre-built diverse auto-sample ({len(auto_sample)} files, evenly spread across the directory):\n"
                f"[{auto_sample_str}]\n\n"
                f"Full file listing (first 60 of {total_files}):\n{preview_list}"
            )
        files_to_process = supported_files

    extracted_text: list[str] = []
    processed_files: list[str] = []
    failed_files: list[str] = []
    output_subfolder = _resolve_doc_output_subfolder(folder)

    for file_path in files_to_process:
        target_path = _get_extracted_path(file_path, output_subfolder)
        if target_path.exists():
            logger.info(f"Skipping {file_path.name}, already extracted to {target_path}")
            try:
                content = target_path.read_text(encoding="utf-8")
                processed_files.append(f"{file_path.name} (skipped, loaded from {target_path})")
                extracted_text.append(f"--- Content of {file_path.name} (from cache) ---\n{content}\n")
                continue
            except Exception as exc:
                logger.error(f"Failed to read existing extract {target_path}: {exc}. Re-extracting...")

        logger.info(f"Processing document: {file_path.name}...")
        try:
            content = extract_supported_document(file_path)
            saved_path = _save_extracted_content(file_path, content, output_folder=output_subfolder)
            processed_files.append(f"{file_path.name} (saved to {saved_path})")
            extracted_text.append(f"--- Content of {file_path.name} ---\n{content}\n")
        except Exception as exc:
            failed_files.append(file_path.name)
            extracted_text.append(f"--- Error reading {file_path.name}: {exc} ---\n")

    summary_lines = [f"Processed {len(processed_files)}/{len(files_to_process)} supported file(s) from {folder}."]
    if processed_files: summary_lines.append(f"Files processed: {', '.join(processed_files)}")
    if failed_files: summary_lines.append(f"Files failed: {', '.join(failed_files)}")
    summary_lines.append(
        "\nIMPORTANT: Use ONLY the file paths listed above. Do NOT reference "
        "filenames from the user's prompt if they differ from the actual files "
        "discovered here. If you need to read individual files, use the exact "
        "paths shown in 'Files processed' above with the `read_file` tool."
    )

    total_text = "\n".join(extracted_text)
    if len(total_text) > 40000:
        logger.info("\n".join(summary_lines))
        try:
            from research_agent.utils.text_search import load_or_build_search_index

            extracted_dir = output_subfolder / "extracted"
            index_dir = output_subfolder / "index"
            load_or_build_search_index(extracted_dir, index_dir)

            return "\n".join(summary_lines + [
                "",
                f"Text omitted because total size is {len(total_text)} chars (too large to display inline).",
                "The documents have been ingested into the wiki workspace.",
                "Please read the synthesized wiki pages under `/wiki/` (e.g. `/wiki/index.md`) using the `read_file` tool to find the information."
            ])
        except Exception as e:
            logger.error(f"Failed to process: {e}", exc_info=True)
            return "\n".join(summary_lines + ["",
                                              f"Text omitted because total size is {len(total_text)} chars (too large to display inline). Please use the `read_file` tool on the specific file paths listed above to read them or check `/wiki/` if ingested."])
    else:
        logger.info("\n".join(summary_lines))
        return "\n".join(summary_lines + ["", "--- EXTRACTED DOCUMENTS ---", ""] + extracted_text)


def normalize_citations_for_comparison(text: str) -> str:
    """Normalize text content for comparison by removing citation formatting differences."""
    if not text:
        return ""
    # Normalize paths like /raw/bmo_ar2025.pdf.md to bmo_ar2025.pdf, /bmo_ar2025.pdf to bmo_ar2025.pdf
    text = re.sub(r'/raw/([A-Za-z0-9._\-]+)\.(pdf|docx|pptx|xlsx)\.(md|txt)\b', r'\1.\2', text)
    text = re.sub(r'/raw/([A-Za-z0-9._\-]+\.(?:pdf|docx|pptx|xlsx))\b', r'\1', text)
    text = re.sub(r'/([A-Za-z0-9._\-]+\.(?:pdf|docx|pptx|xlsx))\b', r'\1', text)
    # Remove all whitespace to ignore formatting differences
    return "".join(text.split())


def get_target_cited_response_path(content: str, state_files: dict | None,
                                   existing_cited_responses: list[str] | None) -> str:
    """Resolve the file path to write the cited response to, avoiding overwriting cited responses from previous turns.
    
    If the content (ignoring sanitization/citation diffs) matches any cited_response in state_files,
    that matching path is returned (in-place sanitization/update).
    
    Otherwise, if a new cited_response file (not in existing_cited_responses) was already created during this turn,
    that path is returned (so multiple writes in the same turn reuse the same new path).
    
    Otherwise, a new cited_response path is allocated (e.g. /cited_response.md if it doesn't exist in existing_cited_responses,
    or /cited_response_N.md where N is determined by incrementing the highest existing suffix).
    """
    state_files = state_files or {}

    # Fallback to global thread mapping if existing_cited_responses is None/empty
    if not existing_cited_responses:
        try:
            from langgraph.config import get_config
            config = get_config()
            thread_id = config.get("configurable", {}).get("thread_id")
            if thread_id:
                if str(thread_id) in _thread_existing_cited_responses:
                    existing_cited_responses = _thread_existing_cited_responses[str(thread_id)]
        except Exception:
            pass

    existing_cited_responses = existing_cited_responses or []

    norm_content = normalize_citations_for_comparison(content)

    # 1. Check if the content is just a sanitized/variant version of a cited_response currently in the state
    for r_path in state_files:
        if r_path.startswith("/cited_response"):
            try:
                existing_content = file_data_to_string(state_files[r_path])
                if normalize_citations_for_comparison(existing_content) == norm_content:
                    return r_path
            except Exception:
                pass

    # 2. Check if we already created a new cited_response file during this turn (not in existing_cited_responses)
    new_cited_responses_in_turn = [
        p for p in state_files
        if p.startswith("/cited_response") and p not in existing_cited_responses
    ]
    if new_cited_responses_in_turn:
        # Return the first/active one created in this turn
        return sorted(new_cited_responses_in_turn)[0]

    # 3. Allocate a new path
    if "/cited_response.md" not in existing_cited_responses:
        return "/cited_response.md"

    max_n = 0
    for r_path in existing_cited_responses:
        match = re.search(r'_(\d+)\.md$', r_path)
        if match:
            max_n = max(max_n, int(match.group(1)))

    next_n = max_n + 1
    return f"/cited_response_{next_n}.md"


def get_active_cited_response_path(state_files: dict | None, existing_cited_responses: list[str] | None) -> str:
    """Find the active cited response path for the current turn.
    
    This is either:
    1. A cited response file in state_files that is NOT in existing_cited_responses (meaning it was created this turn).
    2. Or if none, the highest index /cited_response*.md in state_files.
    3. Or default to "/cited_response.md".
    """
    state_files = state_files or {}

    # Fallback to global thread mapping if existing_cited_responses is None/empty
    if not existing_cited_responses:
        try:
            from langgraph.config import get_config
            config = get_config()
            thread_id = config.get("configurable", {}).get("thread_id")
            if thread_id:
                if str(thread_id) in _thread_existing_cited_responses:
                    existing_cited_responses = _thread_existing_cited_responses[str(thread_id)]
        except Exception:
            pass

    existing_cited_responses = existing_cited_responses or []

    # 1. Look for a brand new cited_response file created in this turn
    new_cited_responses = [p for p in state_files if
                           p.startswith("/cited_response") and p not in existing_cited_responses]
    if new_cited_responses:
        return sorted(new_cited_responses)[-1]

    # 2. Fall back to the highest numbered cited_response that exists in state_files
    existing_in_state = [p for p in state_files if p.startswith("/cited_response")]
    if existing_in_state:
        def get_suffix_num(path: str) -> int:
            """Extract the numeric suffix from a cited_response path."""
            match = re.search(r'_(\d+)\.md$', path)
            if match:
                return int(match.group(1))
            return 0

        return sorted(existing_in_state, key=get_suffix_num)[-1]

    return "/cited_response.md"
