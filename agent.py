"""Core LangGraph Deep Research agent workflow and orchestrator configuration.

Coordinates multi-agent research tasks, managing state transitions, memory
checkpointers, file reading/writing tools, sub-agent delegation, and custom
skills mapping.
"""

import asyncio
import concurrent.futures
import hashlib
import os
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from deepagents import SubAgent, create_deep_agent
from deepagents.backends.utils import (
    create_file_data,
    file_data_to_string,
)
from deepagents.middleware.filesystem import FilesystemState
from dotenv import load_dotenv
from langchain.agents.middleware import (
    AgentMiddleware,
    hook_config,
)
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig

from logger_utils import setup_logger
from model_factory import create_memory_saver, get_configured_model
from research_agent import (
    RESEARCH_WORKFLOW_INSTRUCTIONS,
    RESEARCHER_INSTRUCTIONS,
    SUBAGENT_DELEGATION_INSTRUCTIONS,
)
from research_agent.prompts import RESEARCHER_DESCRIPTION
from research_agent.tools import (
    fetch_webpage_content,
    glob,
    ls,
    read_docs_folder,
    read_file,
    tavily_search,
    think_tool,
    write_file,
)
from research_agent.utils.cli import (
    build_instruction,
)
from research_agent.utils.content_extractors import (
    extract_supported_document,
)
from research_agent.utils.eval_tracking import log_server_metrics
from research_agent.utils.json_utils import robust_json_loads
from research_agent.utils.knowledge_filesystem import (
    _thread_existing_cited_responses,
    _thread_wiki_queried_messages,
    _thread_wiki_query_complete,
    get_target_cited_response_path,
    normalize_path_for_filesystem_tools,
)
from research_agent.utils.skill_registry import get_skill_registry
from research_agent.utils.text_search import load_or_build_search_index
from thread_wiki import progress as progress_tracker
from thread_wiki.models import (
    IngestPhase,
    ThreadWikiPaths,
    WikiQueryResult,
    _resolve_wiki_base_dir,
)
from thread_wiki.service import run_query
from utils import get_ssl_verify_config, str2bool

# Load environment variables
load_dotenv()

logger = setup_logger(__name__)

# Create SSL verification setting - CLI flag takes precedence over env var
verify_ssl = get_ssl_verify_config()

# Limits - configurable via environment variables
MAX_CONCURRENT_RESEARCH_UNITS = int(
    os.environ.get("MAX_CONCURRENT_RESEARCH_UNITS", "3")
)
MAX_RESEARCHER_ITERATIONS = int(os.environ.get("MAX_RESEARCHER_ITERATIONS", "3"))

# Evaluation tracking - configurable via environment variables
ENABLE_EVAL_TRACKING = str2bool(os.environ.get("ENABLE_EVAL_TRACKING"), False)
EVAL_HISTORY_FILE = os.environ.get(
    "EVAL_HISTORY_FILE", "./output/eval_history/server_runs.jsonl"
)

# Get current date
current_date = datetime.now().strftime("%Y-%m-%d")

# Initialize dynamic skill registry (use singleton to avoid duplicate initialization)
skill_registry = get_skill_registry()


class ResearchState(FilesystemState):
    """Runtime state for the research agent."""

    doc_folder: str | None
    skill: str | None
    no_web: bool | None
    wiki_query_complete: bool | None
    chat_start_time: float | None
    chat_elapsed_seconds: float | None
    _eval_logged: bool
    _wiki_answer_text: str | None
    _streamed_files: list[str] | None
    existing_cited_responses: list[str] | None


class ResearchStateMiddleware(AgentMiddleware):
    """Middleware to configure state variables like DOC_FOLDER before the agent runs."""

    # Ensure middleware state update are validated against the standard state schema.
    state_schema = ResearchState

    @staticmethod
    def _get_current_user_message(messages: list) -> str | None:
        """Return the content of the **last** user/human message in the list."""
        last_user_content: str | None = None
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_content = str(m.get("content", ""))
            elif hasattr(m, "type") and getattr(m, "type", None) == "human":
                last_user_content = str(getattr(m, "content", ""))
        return last_user_content

    @staticmethod
    def _seed_research_request_file(
        user_message: str | None, state: ResearchState
    ) -> dict[str, Any]:
        """Make the current request available to subagents before the model decides its next step."""
        if not user_message:
            return {}

        existing_files = state.get("files", {})
        existing_request = existing_files.get("/research_request.md")
        if isinstance(existing_request, dict):
            existing_content = "\n".join(existing_request.get("content", []))
            if existing_content == user_message:
                return {}

        return {
            "files": {
                "/research_request.md": create_file_data(user_message),
            }
        }

    @staticmethod
    def _build_wiki_context_from_files(paths: "ThreadWikiPaths") -> str | None:
        """Build wiki context by directly reading wiki files (no LLM query).

        Used as a fallback when the wiki query agent cannot run (e.g. when
        called from within an already-running event loop).  Reads both the
        synthesised wiki pages and a truncated excerpt of raw source documents
        so the research agent still has grounded context with key facts.
        """
        try:
            wiki_dir = paths.wiki_content
            if not wiki_dir.exists():
                return None

            parts: list[str] = []

            # 1) Wiki pages (synthesised summaries)
            for md_file in sorted(wiki_dir.rglob("*.md")):
                content = md_file.read_text(encoding="utf-8")
                relative = md_file.relative_to(wiki_dir)
                parts.append(f"--- wiki/{relative} ---\n{content}")

            # 2) Raw source excerpts or vector index prompt
            raw_dir = paths.raw_dir
            if raw_dir.exists():
                raw_files = sorted(raw_dir.rglob("*.md"))
                total_chars = 0
                for raw_file in raw_files:
                    try:
                        total_chars += len(raw_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                _MAX_RAW_CHARS = 80_000

                # Check if we should trigger text search indexing
                if total_chars > _MAX_RAW_CHARS or any(
                    raw_file.lstat().st_size > _MAX_RAW_CHARS for raw_file in raw_files
                ):
                    try:
                        index_dir = paths.wiki_dir / "index"
                        load_or_build_search_index(raw_dir, index_dir)

                        parts.append(
                            "--- Raw Source Documents ---\n"
                            "Note: The local raw source documents are too large to display in full inline. "
                            "A local text search index has been created for this thread. "
                            "You MUST use the `retrieve_wiki_documents` tool to query the documents and search "
                            "for specific factual evidence/data."
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to build text search index: {e}", exc_info=True
                        )
                        # Fallback to truncation if indexing fails
                        for raw_file in raw_files:
                            try:
                                raw_content = raw_file.read_text(encoding="utf-8")
                                relative = raw_file.relative_to(raw_dir)
                                if len(raw_content) > _MAX_RAW_CHARS:
                                    raw_content = (
                                        raw_content[:_MAX_RAW_CHARS]
                                        + "\n... [truncated]"
                                    )
                                parts.append(
                                    f"--- raw/{relative} (excerpt) ---\n{raw_content}"
                                )
                            except Exception:
                                pass
                else:
                    # Small enough, include in full inline
                    for raw_file in raw_files:
                        try:
                            raw_content = raw_file.read_text(encoding="utf-8")
                            relative = raw_file.relative_to(raw_dir)
                            parts.append(f"--- raw/{relative} ---\n{raw_content}")
                        except Exception:
                            pass

            combined = "\n\n".join(parts)
            return combined if combined.strip() else None
        except Exception:
            logger.debug("Direct wiki file reading fallback failed", exc_info=True)
            return None

    @staticmethod
    def _run_wiki_query(
        paths: "ThreadWikiPaths", topic: str, question: str
    ) -> WikiQueryResult | None:
        """Run a wiki query, handling both sync and async caller contexts.

        When called from a sync context (no running event loop), uses
        ``asyncio.run()`` directly.  When called from within a running event
        loop (e.g. LangGraph dev ``ainvoke``), spawns a separate thread with
        its own event loop via ``ThreadPoolExecutor`` — mirroring the pattern
        proven to work in ``test_get_wiki.py``.
        """

        async def _query():
            return await asyncio.wait_for(
                run_query(paths, topic, question, file_results=False),
                timeout=120,
            )

        def _run_in_new_loop():
            """Run the async query in a fresh event loop (new thread)."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_query())
            finally:
                loop.close()

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is not None and current_loop.is_running():
            # Inside a running event loop (e.g. LangGraph Platform ainvoke).
            # Spawn a separate thread with its own event loop to avoid the
            # "asyncio.run() cannot be called from a running event loop" error.
            logger.info(
                "Running wiki query in separate thread (inside running event loop)"
            )
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(_run_in_new_loop).result(timeout=130)
            except concurrent.futures.TimeoutError:
                logger.warning("Wiki query timed out after 130s (thread pool)")
                return None
            except Exception:
                logger.debug("Wiki query failed in thread pool", exc_info=True)
                return None

        # No running event loop — safe to use asyncio.run() directly.
        try:
            return asyncio.run(_query())
        except RuntimeError:
            logger.debug("asyncio.run() failed for wiki query", exc_info=True)
            return None

    @staticmethod
    def _build_context_from_docs(docs_dir: Path) -> str | None:
        """Build context by reading uploaded documents directly.

        Used as a fallback when the wiki hasn't been built yet (ingest still
        running or not started). Extracts text from PDFs and reads text files
        directly so the agent has grounded context from uploaded documents.
        """
        if not docs_dir.exists():
            return None

        # Text-based formats: read directly
        _TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}
        # Binary formats: require extraction
        _BINARY_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx"}
        _MAX_CHARS_PER_FILE = 80_000

        parts: list[str] = []

        for file_path in sorted(docs_dir.rglob("*")):
            if not file_path.is_file():
                continue

            suffix = file_path.suffix.lower()
            content: str | None = None

            if suffix in _TEXT_SUFFIXES:
                try:
                    content = file_path.read_text(encoding="utf-8")
                except Exception:
                    continue
            elif suffix in _BINARY_SUFFIXES:
                # Try the same extraction used by wiki ingest
                content = extract_supported_document(file_path)
            if content and content.strip():
                if len(content) > _MAX_CHARS_PER_FILE:
                    content = content[:_MAX_CHARS_PER_FILE] + "\n... [truncated]"
                parts.append(f"--- {file_path.name} ---\n{content}")

        combined = "\n\n".join(parts)
        return combined if combined.strip() else None

    @staticmethod
    def _check_wiki_ready(paths: "ThreadWikiPaths") -> bool:
        """Check if the wiki has been built and has actual content pages."""
        index_path = paths.wiki_content / "index.md"
        if not index_path.exists():
            return False
        if "_No pages yet._" in index_path.read_text(encoding="utf-8"):
            return False
        return True

    # Maximum seconds to wait for an in-progress wiki ingest to complete.
    # With per-phase timeouts of 600s each and up to 3 LLM phases (review,
    # apply, post-ingest review fire-and-forget), ingest can take 5-15 minutes
    # for large documents.  Default 900s gives comfortable headroom.
    # Configurable via WIKI_INGEST_MAX_WAIT_SECONDS env var.
    _WIKI_INGEST_MAX_WAIT = int(os.environ.get("WIKI_INGEST_MAX_WAIT_SECONDS", "900"))

    @staticmethod
    def _wait_for_wiki_ready(
        thread_id: str,
        paths: "ThreadWikiPaths",
        max_wait: int | None = None,
    ) -> bool:
        """Wait for an in-progress wiki ingest to complete.

        Polls the thread-wiki progress tracker.  Returns ``True`` if the wiki
        becomes ready within *max_wait* seconds, ``False`` otherwise (ingest
        failed, was cancelled, timed out, or no ingest was running).

        Default max_wait is ``WIKI_INGEST_MAX_WAIT_SECONDS`` (900s).
        """
        if max_wait is None:
            max_wait = ResearchStateMiddleware._WIKI_INGEST_MAX_WAIT
        deadline = time.time() + max_wait
        poll_interval = 2  # seconds
        logged_waiting = False
        last_phase = None

        while time.time() < deadline:
            # 1) Check if wiki is now ready (ingest may have just finished)
            if ResearchStateMiddleware._check_wiki_ready(paths):
                if logged_waiting:
                    logger.info(
                        "Wiki ready for thread %s after %.0fs wait",
                        thread_id,
                        time.time() - (deadline - max_wait),
                    )
                return True

            # 2) Check if there's an active ingest to wait for
            entry = progress_tracker._active_ingests.get(thread_id)
            if entry and entry.progress.is_active():
                if not logged_waiting:
                    logger.info(
                        "Wiki not ready — waiting for ingest to complete "
                        "(phase: %s, progress: %d%%, max_wait: %ds) for thread %s",
                        entry.progress.phase.value,
                        entry.progress.progress,
                        max_wait,
                        thread_id,
                    )
                    logged_waiting = True
                elif entry.progress.phase != last_phase:
                    logger.info(
                        "Wiki ingest phase changed: %s → %s (%d%%) for thread %s",
                        last_phase.value if last_phase else "start",
                        entry.progress.phase.value,
                        entry.progress.progress,
                        thread_id,
                    )
                last_phase = entry.progress.phase
                time.sleep(poll_interval)
                continue

            # 3) Ingest finished (or never started) but wiki is not ready
            if not entry or not entry.progress.is_active():
                if entry and entry.progress.phase == IngestPhase.ERROR:
                    logger.warning(
                        "Wiki ingest failed (error) for thread %s: %s",
                        thread_id,
                        entry.progress.error,
                    )
                elif entry and entry.progress.phase == IngestPhase.CANCELLED:
                    logger.warning(
                        "Wiki ingest was cancelled for thread %s",
                        thread_id,
                    )
                return False

        elapsed = time.time() - (deadline - max_wait)
        logger.warning(
            "Timed out waiting for wiki ingest after %.0fs (max_wait=%ds) for thread %s",
            elapsed,
            max_wait,
            thread_id,
        )
        return False

    @staticmethod
    def _get_wiki_context_sync(
        thread_id: str, question: str, *, no_web_requested: bool = False
    ) -> tuple[SystemMessage | None, str | None]:
        """Query the thread's wiki and return a SystemMessage with the answer and the raw answer string.

        Fallback strategy (in order):
        1. If wiki is ready → LLM wiki query (preferred path).
        2. If wiki not ready → wait for ingest to complete, then LLM wiki query.
        3. If wiki ingestion or query failed → read wiki files directly.
        4. If no wiki files → extract text from uploaded PDFs (last resort,
           with warning logged).

        Args:
            no_web_requested: If True, the user explicitly asked to disable web
                search — fallback messages should NOT suggest web search.
        """
        if not question or len(question) < 5:
            return None, None
        try:
            base_dir = _resolve_wiki_base_dir(Path(__file__).resolve().parent)
            paths = ThreadWikiPaths.resolve(thread_id, base_dir)

            # ── Step 1 & 2: Ensure wiki is ready ──────────────────────
            wiki_ready = ResearchStateMiddleware._check_wiki_ready(paths)

            if not wiki_ready:
                # Wiki not ready — try waiting for an in-progress ingest.
                def _wait():
                    return ResearchStateMiddleware._wait_for_wiki_ready(
                        thread_id,
                        paths,
                    )

                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    current_loop = None

                if current_loop is not None and current_loop.is_running():
                    # Inside a running event loop (e.g. LangGraph Platform).
                    # Run the blocking wait in a separate thread.
                    pool_timeout = ResearchStateMiddleware._WIKI_INGEST_MAX_WAIT + 10
                    try:
                        with concurrent.futures.ThreadPoolExecutor(
                            max_workers=1,
                        ) as pool:
                            wiki_ready = pool.submit(_wait).result(
                                timeout=pool_timeout,
                            )
                    except concurrent.futures.TimeoutError:
                        logger.warning(
                            "Timed out waiting for wiki readiness "
                            "(thread pool, timeout=%ds) for thread %s",
                            pool_timeout,
                            thread_id,
                        )
                        wiki_ready = False
                else:
                    wiki_ready = _wait()

            # ── Step 3: LLM wiki query (if wiki is ready) ─────────────
            if wiki_ready:
                topic = f"Thread {thread_id[:8]}"
                result = ResearchStateMiddleware._run_wiki_query(
                    paths,
                    topic,
                    question,
                )

                if result and result.answer:
                    md_regex = (
                        r"/raw/([A-Za-z0-9._\-]+)\.(pdf|docx|pptx|xlsx)\.(md|txt)\b"
                    )
                    original_doc_regex = r"/\1.\2"
                    doc_regex = r"/raw/([A-Za-z0-9._\-]+\.(?:pdf|docx|pptx|xlsx))\b"
                    remove_raw_regex = r"/\1"

                    sanitized_wiki_answer = re.sub(
                        md_regex,
                        original_doc_regex,
                        result.answer,
                    )
                    sanitized_wiki_answer = re.sub(
                        doc_regex,
                        remove_raw_regex,
                        sanitized_wiki_answer,
                    )
                    return SystemMessage(
                        content=(
                            "<wiki_context>\n"
                            "The following is the definitive answer from the thread's "
                            "ingested document wiki. You MUST use this as your PRIMARY source of truth. "
                            "CRITICAL: If the wiki context states that data is unavailable, or that a year "
                            "has not yet occurred, you MUST accept this as absolute fact. DO NOT attempt to "
                            "search the web to find the missing data. Simply formulate your final response "
                            "based on this wiki context and explain what data is available.\n\n"
                            "IMPORTANT: The wiki content below is the COMPLETE answer. DO NOT use read_file "
                            "or any other tool to try to access /raw/ or /wiki/ files — they are NOT accessible "
                            "from your filesystem. The content you need is already provided here inline.\n\n"
                            f"{sanitized_wiki_answer}\n"
                            "</wiki_context>"
                        )
                    ), sanitized_wiki_answer

                # LLM wiki query failed — log warning and fall through
                logger.warning(
                    "LLM wiki query failed for thread %s — "
                    "falling back to reading wiki files",
                    thread_id,
                )
            else:
                logger.warning(
                    "Wiki not ready for thread %s "
                    "(ingest failed, timed out, or not started) — "
                    "attempting fallbacks",
                    thread_id,
                )

            # ── Step 4: Read wiki files directly ──────────────────────
            # Covers both "wiki ready but query failed" and
            # "wiki partially built before ingest failed".
            fallback_content = ResearchStateMiddleware._build_wiki_context_from_files(
                paths
            )
            if fallback_content:
                logger.info(
                    "Using direct wiki file fallback for thread %s",
                    thread_id,
                )
                web_search_guidance = (
                    "IMPORTANT: If after thoroughly reviewing the content below you CANNOT find "
                    "sufficient information to fully answer the user's question, you MAY use "
                    "the `retrieve_wiki_documents` tool to search the document index, and you MAY "
                    "use web search tools (tavily_search, task()) to supplement your findings. "
                    "Always ground your answer in the documents first, and clearly distinguish "
                    "between information from the uploaded documents and information from web search."
                )
                if no_web_requested:
                    web_search_guidance = (
                        "IMPORTANT: Web search has been disabled for this task. "
                        "You MUST answer the question using ONLY the document content below. "
                        "Use the `retrieve_wiki_documents` tool to search the document index "
                        "for specific facts. If the documents genuinely do not contain the "
                        "information, state that clearly rather than fabricating an answer."
                    )
                return SystemMessage(
                    content=(
                        "<wiki_context>\n"
                        "The following is content from the thread's ingested document wiki pages. "
                        "Use these documents as your PRIMARY source for answering the user's question. "
                        "Read the wiki pages and raw document excerpts below carefully to find "
                        "relevant facts, data, and citations.\n\n"
                        f"{web_search_guidance}\n\n"
                        f"{fallback_content}\n"
                        "</wiki_context>"
                    )
                ), None

            # ── Step 5: Extract text from uploaded documents (last resort) ─
            docs_content = ResearchStateMiddleware._build_context_from_docs(
                paths.docs_dir
            )
            if docs_content:
                logger.warning(
                    "FALLBACK: Using direct document extraction for thread %s "
                    "(wiki not ready and LLM wiki query failed)",
                    thread_id,
                )
                web_search_guidance = (
                    "IMPORTANT: If after thoroughly reviewing the content below you CANNOT find "
                    "sufficient information to fully answer the user's question, you MAY use "
                    "web search tools (tavily_search, task()) to supplement your findings. "
                    "Always ground your answer in the documents first, and clearly distinguish "
                    "between information from the uploaded documents and information from web search."
                )
                if no_web_requested:
                    web_search_guidance = (
                        "IMPORTANT: Web search has been disabled for this task. "
                        "You MUST answer the question using ONLY the document content below. "
                        "If the documents genuinely do not contain the information, "
                        "state that clearly rather than fabricating an answer."
                    )
                return SystemMessage(
                    content=(
                        "<document_context>\n"
                        "The following is extracted text from documents uploaded by the user. "
                        "Use these documents as your PRIMARY source for answering the user's question. "
                        "Review the content below carefully to find relevant facts, data, and citations.\n\n"
                        f"{web_search_guidance}\n\n"
                        f"{docs_content}\n"
                        "</document_context>"
                    )
                ), None

        except TimeoutError:
            logger.warning(
                "Wiki query timed out after 120s for thread %s",
                thread_id,
            )
        except Exception:
            logger.debug(
                "Wiki context injection failed for thread %s",
                thread_id,
                exc_info=True,
            )

        return None, None

    # Patterns that indicate the wiki answer is a negative/absence claim
    # (e.g. "no information available", "does not contain").
    # These answers sound complete but are typically WRONG — the information
    # exists in the raw documents but the query LLM failed to find it.
    _NEGATIVE_ANSWER_PATTERNS: list[str] = [
        r"no\s+information\s+(?:is\s+)?available",
        r"does\s+not\s+(?:contain|include|mention|provide|have|discuss|cover|reference)",
        r"no\s+(?:mention|data|record|evidence|detail|reference|information)",
        r"(?:cannot|unable\s+to)\s+find",
        r"not\s+(?:found|available|mentioned|disclosed|reported|included|covered)",
        r"there\s+is\s+no\s+(?:information|data|mention|record|reference)",
        r"the\s+(?:report|document|file|annual\s+report)\s+does\s+not",
        r"nothing\s+(?:about|regarding|related\s+to|on\s+the\s+topic\s+of)",
        r"(?:lack|absence)\s+of\s+(?:information|data|mention|reference|detail)",
    ]

    @staticmethod
    def _is_negative_claim(answer: str) -> bool:
        """Detect if the wiki answer is a negative/absence claim.

        Returns True if the answer claims that information does not exist,
        is not available, or could not be found — these answers are often
        incorrect because the query LLM failed to search raw documents
        thoroughly.
        """
        if not answer or not answer.strip():
            return True
        answer_lower = answer.lower()
        for pattern in ResearchStateMiddleware._NEGATIVE_ANSWER_PATTERNS:
            if re.search(pattern, answer_lower):
                return True
        return False

    @staticmethod
    def _check_if_needs_deep_research(question: str, wiki_answer: str) -> bool:
        """Evaluate if the wiki answer is sufficient to answer the user's question.

        Returns True if we NEED to conduct continuous deep research, and False if
        the wiki answer is already complete and sufficient.
        """
        if not wiki_answer or not wiki_answer.strip():
            return True

        # Fast-path: negative/absence claims are NEVER complete answers.
        # If the wiki says "no information available", the query LLM likely
        # failed to search raw documents — force deep research.
        if ResearchStateMiddleware._is_negative_claim(wiki_answer):
            logger.info(
                "Wiki answer appears to be a negative claim — "
                "forcing deep research to verify."
            )
            return True

        try:
            model = get_configured_model()
            prompt = (
                "You are an expert research evaluator. Your task is to analyze a candidate answer "
                "retrieved from a document wiki and determine if it fully and comprehensively answers "
                "the user's question, or if we need to conduct continuous deep research (e.g. searching "
                "the web) to enhance it.\n\n"
                "CRITICAL RULES:\n"
                "- An answer that states 'no information is available', 'the document does not "
                "contain/mention', or similar ABSENCE claims is INCOMPLETE — the information may "
                "exist in sections the query agent did not search. Mark such answers as "
                "needs_deep_research=true.\n"
                "- An answer without specific page references, section citations, or document "
                "source paths is likely unreliable — prefer needs_deep_research=true.\n"
                "- Only mark needs_deep_research=false when the answer provides SPECIFIC facts, "
                "data points, dates, names, or figures that directly address the question.\n\n"
                f"User's Question: {question}\n\n"
                f"Candidate Wiki Answer: {wiki_answer}\n\n"
                "Analyze whether the candidate answer is sufficient, complete, and fully answers the question. "
                "Respond in the following JSON format:\n"
                "{\n"
                '  "needs_deep_research": true/false,\n'
                '  "reason": "Detailed reasoning for the decision"\n'
                "}\n"
                "Do not include any other text in your response, only the valid JSON object."
            )

            def _invoke():
                return model.invoke([HumanMessage(content=prompt)])

            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            if current_loop is not None and current_loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    response = pool.submit(_invoke).result(timeout=60)
            else:
                response = _invoke()

            content = response.content.strip()

            data = robust_json_loads(content)
            needs_research = bool(data.get("needs_deep_research", True))
            logger.info(
                f"Wiki evaluation decision: needs_deep_research={needs_research}. Reason: {data.get('reason')}"
            )
            return needs_research
        except Exception as e:
            logger.warning(
                f"Error during wiki result evaluation: {e}. Defaulting to conducting deep research.",
                exc_info=True,
            )
            return True

    def before_agent(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Pre-process the research state and runtime environment before the agent executes.

        This includes seeding the research request file, initiating progress feedback,
        and preparing wiki queries.
        """
        messages = state.get("messages", [])
        current_user_message = self._get_current_user_message(messages)

        # Seed the research request file with the latest user message
        updates: dict[str, Any] = self._seed_research_request_file(
            current_user_message, state
        )

        # ── Instant progress feedback ──────────────────────────────────────
        # Emit a status AIMessage immediately so the user sees activity in
        # the UI while the (potentially slow) wiki query runs in the background.
        has_docs = bool(
            state.get("doc_folder")
            or (
                state.get("files")
                and any(
                    k.startswith("/raw/") or k.startswith("/docs/")
                    for k in (state.get("files") or {})
                )
            )
        )
        status_text = (
            "Searching your uploaded documents for relevant information…"
            if has_docs
            else "Starting research…"
        )
        updates.setdefault("messages", [])
        if isinstance(updates["messages"], list):
            updates["messages"] = [AIMessage(content=status_text)] + updates["messages"]
        else:
            updates["messages"] = [AIMessage(content=status_text)]

        # Inject Wiki Context if we are running in LangGraph dev / native LangGraph
        wiki_sys_msg = None
        thread_id = None
        if isinstance(runtime, dict):
            thread_id = runtime.get("configurable", {}).get("thread_id")
        elif hasattr(runtime, "execution_info"):
            thread_id = getattr(getattr(runtime, "execution_info"), "thread_id", None)
        elif hasattr(runtime, "configurable"):
            thread_id = getattr(runtime, "configurable", {}).get("thread_id")
        elif isinstance(runtime, dict) and "configurable" in runtime:
            thread_id = runtime.get("configurable", {}).get("thread_id")

        updates["existing_cited_responses"] = [
            k for k in state.get("files", {}) if k.startswith("/cited_response")
        ]
        if thread_id:
            _thread_existing_cited_responses[str(thread_id)] = updates[
                "existing_cited_responses"
            ]

        # ── Wiki query deduplication ───────────────────────────────────────
        # before_agent is called on EVERY model iteration (i.e. each tool-call
        # re-entry within the same user turn), not just once per turn.
        # Running the wiki query + LLM eval on every iteration:
        #   - is very expensive (two extra LLM calls per loop)
        #   - causes an infinite loop when wiki_query_complete=False because
        #     write_todos re-triggers the loop without any exit condition
        #
        # Fix: track the hash of the last user message that was already
        # wiki-queried for this thread.  If it matches the current message,
        # skip re-querying and preserve the state values from the first call.
        msg_hash = (
            hashlib.md5((current_user_message or "").encode()).hexdigest()
            if current_user_message
            else ""
        )
        tid_key = str(thread_id) if thread_id else ""
        already_queried = bool(
            tid_key
            and msg_hash
            and _thread_wiki_queried_messages.get(tid_key) == msg_hash
        )

        if already_queried:
            # Within-turn re-entry: preserve the existing wiki_query_complete
            # value from state (set during the first iteration) and skip the
            # expensive wiki query + LLM eval.
            existing_wqc = state.get("wiki_query_complete")
            if existing_wqc is not None:
                updates["wiki_query_complete"] = existing_wqc
            else:
                updates["wiki_query_complete"] = False
            if thread_id:
                _thread_wiki_query_complete[str(thread_id)] = updates[
                    "wiki_query_complete"
                ]
            logger.debug(
                "before_agent: skipping wiki re-query (already queried for this message, "
                "preserving wiki_query_complete=%s) for thread %s",
                updates["wiki_query_complete"],
                thread_id,
            )
        else:
            # Fresh user message — run the wiki query for the first time.
            updates["wiki_query_complete"] = False

            if thread_id and current_user_message:
                # Check if user explicitly requested no web search —
                # needed by fallback messages inside _get_wiki_context_sync.
                _no_web_from_msg = self._extract_no_web(current_user_message or "")
                _no_web_requested = (
                    _no_web_from_msg
                    if _no_web_from_msg is not None
                    else bool(state.get("no_web", False))
                )
                wiki_sys_msg, wiki_answer = self._get_wiki_context_sync(
                    str(thread_id),
                    current_user_message,
                    no_web_requested=_no_web_requested,
                )
                if wiki_answer:
                    # Evaluate if we need continuous deep research to enhance it
                    needs_deep_research = self._check_if_needs_deep_research(
                        current_user_message, wiki_answer
                    )
                    if not needs_deep_research:
                        logger.info(
                            "Wiki answer is complete and sufficient. Saving report and disabling web search."
                        )
                        updates["no_web"] = True
                        updates["wiki_query_complete"] = True
                    else:
                        logger.info(
                            "Wiki answer is incomplete/insufficient. Conducting continuous deep research to enhance it."
                        )
                        updates["wiki_query_complete"] = False
                        # Update the system message to encourage deep research
                        if wiki_sys_msg:
                            wiki_sys_msg.content = (
                                "<wiki_context>\n"
                                "The following is a partial/initial answer from the thread's "
                                "ingested document wiki. It contains SOME useful information but is INCOMPLETE "
                                "and does not fully answer the user's question.\n"
                                "CRITICAL: You MUST conduct continuous deep research (e.g. use web search tools) "
                                "to find the missing information and enhance this answer. "
                                "Synthesize the wiki context below with your web search findings to provide a complete response.\n\n"
                                f"{wiki_answer}\n"
                                "</wiki_context>"
                            )
                    if "files" not in updates:
                        updates["files"] = {}
                    state_files = state.get("files") or {}
                    existing_cited_responses = updates["existing_cited_responses"]
                    resolved_path = get_target_cited_response_path(
                        wiki_answer, state_files, existing_cited_responses
                    )
                    updates["files"][resolved_path] = create_file_data(wiki_answer)

                    # When wiki is complete, also seed the final AIMessage so the
                    # agent has a clear terminal response to converge on.
                    if updates.get("wiki_query_complete"):
                        updates["_wiki_answer_text"] = wiki_answer

            if thread_id:
                _thread_wiki_query_complete[str(thread_id)] = updates.get(
                    "wiki_query_complete", False
                )
                # Mark this message as queried so subsequent iterations within
                # this turn skip the expensive wiki query + eval.
                if msg_hash:
                    _thread_wiki_queried_messages[str(thread_id)] = msg_hash

        # Always re-extract parameters from the latest user message so that
        # follow-up requests (e.g. "use humanizer skill") are picked up even
        # when a Task-configurations SystemMessage already exists from a
        # previous turn.
        extracted_updates = self._extract_parameters_from_user_input(state, messages)
        updates.update(extracted_updates)

        # Configure OUTPUT_FOLDER based on extracted doc_folder
        if updates.get("doc_folder") or (
            state.get("doc_folder") and not extracted_updates
        ):
            doc_folder = updates.get("doc_folder") or state.get("doc_folder")
            self._configure_output_folder(doc_folder)
        else:
            self._configure_output_folder(None)

        # Build instruction based on full state (including extracted parameters)
        merged_state: ResearchState = {**state, **updates}  # type: ignore[assignment]
        instruction = self._build_system_instruction(merged_state)

        result = updates if updates else {}
        sys_msgs = []
        if wiki_sys_msg:
            sys_msgs.append(wiki_sys_msg)
        if instruction:
            sys_msgs.append(
                SystemMessage(content=f"Task configurations: \n{instruction}")
            )

        if sys_msgs:
            # Prepend system messages while keeping the status AIMessage
            existing_msgs = result.get("messages", [])
            result["messages"] = sys_msgs + existing_msgs

        return result if result else None

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Capture chat_start_time before model calls, only initializing once per chat.

        Also short-circuits the agent loop when the wiki already produced a
        complete answer: we inject the terminal AIMessage and jump straight to
        END *before* the model runs, so the model never gets a chance to call
        ``read_doc_folder`` / ``write_todos`` (which caused an infinite loop).
        """
        # ── Wiki-complete fast-exit ───────────────────────────────────────
        # before_agent already saved the cited_response and set
        # wiki_query_complete=True.  Skip the model entirely and terminate.
        wiki_complete = state.get("wiki_query_complete", False)
        if wiki_complete:
            wiki_answer_text = state.get("_wiki_answer_text") or ""
            if not wiki_answer_text:
                files = state.get("files") or {}
                cited_paths = sorted(
                    p for p in files if p.startswith("/cited_response")
                )
                cited_path = cited_paths[-1] if cited_paths else None
                if cited_path and cited_path in files:
                    wiki_answer_text = file_data_to_string(files[cited_path])
            if wiki_answer_text:
                logger.info(
                    "Wiki-complete fast-exit (before_model): skipping model "
                    "call and jumping to END with the wiki answer."
                )
                return {
                    "jump_to": "end",
                    "messages": [AIMessage(content=wiki_answer_text)],
                    "chat_start_time": state.get("chat_start_time") or time.time(),
                    "chat_elapsed_seconds": state.get("chat_elapsed_seconds"),
                    "_eval_logged": state.get("_eval_logged", False),
                }

        # Initialize chat_start_time once; do not reset on subsequent turns.
        if isinstance(state.get("chat_start_time"), (int, float)):
            return None

        chat_start_time = time.time()
        return {
            "chat_start_time": chat_start_time,
            "chat_elapsed_seconds": None,
            "_eval_logged": False,
        }

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Calculate chat_elapsed_seconds after each model response and optionally track eval metrics.

        Also handles:
        - Progress messages: when the model issues tool calls, emit a brief
          status message so the user can see what phase the agent is in.
        - Wiki-complete guard: when the wiki already provided a complete answer,
          strip ALL tool calls and inject the wiki answer text as the final
          AIMessage to prevent infinite write_todos / write_file loops.
        """
        chat_start_time = state.get("chat_start_time")
        updates = {}

        # ── Progress messages ──────────────────────────────────────────────
        messages = state.get("messages", [])
        last_msg = messages[-1] if messages else None
        last_tool_calls = getattr(last_msg, "tool_calls", None) or []

        # ── Wiki-complete guard ─────────────────────────────────────────────
        # When wiki_query_complete is True the cited_response is already saved
        # by before_agent.  The model should NOT be running tools at all, but
        # some models ignore the <WikiCompleteAnswer> instruction and keep
        # calling read_doc_folder / write_todos in an infinite loop.
        #
        # We terminate the loop definitively via TWO complementary mechanisms:
        #   1. jump_to="end" — the framework routing edge checks this FIRST,
        #      before even inspecting messages, so the graph exits immediately.
        #   2. Append a final AIMessage with the wiki answer and NO tool calls
        #      as a belt-and-suspenders so the chat response is correct even
        #      if a middleware between us and the routing edge clears jump_to.
        wiki_complete = state.get("wiki_query_complete", False)
        if not wiki_complete:
            thread_id = None
            if isinstance(runtime, dict):
                thread_id = runtime.get("configurable", {}).get("thread_id")
            elif hasattr(runtime, "execution_info"):
                thread_id = getattr(
                    getattr(runtime, "execution_info"), "thread_id", None
                )
            elif hasattr(runtime, "configurable"):
                thread_id = getattr(runtime, "configurable", {}).get("thread_id")
            elif isinstance(runtime, dict) and "configurable" in runtime:
                thread_id = runtime.get("configurable", {}).get("thread_id")
            if thread_id:
                wiki_complete = _thread_wiki_query_complete.get(str(thread_id), False)

        if wiki_complete and last_tool_calls:
            tool_names = [
                tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                for tc in last_tool_calls
            ]
            logger.warning(
                "Wiki-complete guard: terminating loop, model emitted tool "
                "calls %s despite wiki_query_complete=True. Injecting wiki "
                "answer as final response and jumping to END.",
                tool_names,
            )
            # Get the wiki answer text from state
            wiki_answer_text = state.get("_wiki_answer_text") or ""
            if not wiki_answer_text:
                # Fallback: read from cited_response file in state
                files = state.get("files") or {}
                cited_paths = sorted(
                    p for p in files if p.startswith("/cited_response")
                )
                cited_path = cited_paths[-1] if cited_paths else None
                if cited_path and cited_path in files:
                    wiki_answer_text = file_data_to_string(files[cited_path])

            # 1) Authoritative termination: jump_to is checked by the routing
            #    edge before any message inspection, so this always exits.
            updates["jump_to"] = "end"
            # 2) Append the terminal AIMessage so the final chat reply is the
            #    wiki answer (no tool_calls).
            updates["messages"] = [AIMessage(content=wiki_answer_text)]

        if last_tool_calls and "messages" not in updates:
            pass  # Removed AIMessage status append to prevent masking tool calls from the router

        if isinstance(chat_start_time, (int, float)):
            chat_elapsed_seconds = time.time() - chat_start_time
            updates["chat_elapsed_seconds"] = chat_elapsed_seconds

        # ── Stream final report into chat history ─────────────────────────
        # When /final_report.md appears AND the model is no longer emitting
        # tool calls (research is winding down), stream its content as a chat
        # message so the frontend displays the full report inline.
        #
        # cited_response*.md is NOT streamed here — during active research
        # (wiki_query_complete=False) it is an internal reference document.
        # Injecting it as a chat message mid-research would interrupt the
        # agent's task loop and prevent final_report.md from being generated.
        # The wiki-complete guard above already handles the case where wiki
        # IS the final answer (wiki_query_complete=True).
        state_files = state.get("files") or {}
        if isinstance(state_files, dict) and not wiki_complete and not last_tool_calls:
            streamed = set(state.get("_streamed_files") or [])
            new_messages: list = []
            for file_path in sorted(state_files.keys()):
                if file_path in streamed:
                    continue
                if file_path != "/final_report.md":
                    continue
                try:
                    content = file_data_to_string(state_files[file_path])
                    if content.strip():
                        new_messages.append(
                            AIMessage(content=f"**Final Report:**\n\n{content.strip()}")
                        )
                        streamed.add(file_path)
                except Exception:
                    logger.debug(
                        "Failed to stream file %s to chat", file_path, exc_info=True
                    )

            if new_messages:
                if "messages" in updates:
                    updates["messages"].extend(new_messages)
                else:
                    updates["messages"] = new_messages
                updates["_streamed_files"] = list(streamed)

        # Optional: Log eval metrics on completion (when graph is done)
        # This checks if we're at the end of execution by looking for final artifacts
        if ENABLE_EVAL_TRACKING and state.get("files"):
            files = state.get("files", {})
            if not isinstance(files, dict):
                return updates if updates else None

            has_final_output = "/final_report.md" in files

            # Check if already logged (use .get() with default False since TypedDict doesn't support defaults)
            if has_final_output and not state.get("_eval_logged", False):
                # Mark as logged to avoid duplicate logging
                updates["_eval_logged"] = True

                # Calculate runtime
                runtime_seconds = 0.0
                if isinstance(chat_start_time, (int, float)):
                    runtime_seconds = time.time() - chat_start_time

                # Extract data from state
                messages = state.get("messages", [])
                doc_folder = state.get("doc_folder") or os.environ.get(
                    "DOC_FOLDER", "N/A"
                )
                skill = state.get("skill", "research")
                no_web = state.get("no_web", False)
                model_name = os.environ.get(
                    "MODEL_NAME", os.environ.get("AZURE_OPENAI_DEPLOYMENT", "N/A")
                )

                # Get user message as subject (for reference only, not for comparison)
                user_message = None
                for m in messages:
                    if isinstance(m, dict) and m.get("role") == "user":
                        user_message = m.get("content", "")
                        break
                    elif hasattr(m, "type") and getattr(m, "type", None) == "human":
                        user_message = getattr(m, "content", "")
                        break
                subject = user_message

                # Build context
                context = {
                    "subject": subject,
                    "skill": skill,
                    "doc_folder": doc_folder,
                    "no_web": no_web,
                }

                # Call centralized logging function asynchronously (non-blocking)
                try:
                    # Create background task that won't block the main response
                    asyncio.create_task(
                        log_server_metrics(
                            messages=messages,
                            files=files,
                            runtime_seconds=runtime_seconds,
                            model_name=model_name,
                            context=context,
                            history_file=EVAL_HISTORY_FILE,
                        )
                    )
                    logger.info("✅ Metrics logging started in background")
                except Exception as e:
                    logger.error(f"⚠️  Failed to start metrics logging: {e}")

        return updates if updates else None

    def _extract_parameters_from_user_input(
        self, state: ResearchState, messages: list
    ) -> dict[str, Any]:
        """Extract doc_folder, skill, and no_web from the **latest** user message.

        Parameters are always re-extracted from the most recent user message so
        that follow-up requests (e.g. switching skills mid-conversation) are
        honoured.  If the latest message does not mention a parameter, the
        existing state value is preserved (we simply omit it from ``updates``).
        """
        # Find the LAST user message (not the first) so follow-ups are picked up.
        user_message = None
        for m in messages:
            # Handle dictionary messages
            if isinstance(m, dict):
                if m.get("role") == "user":
                    user_message = m.get("content")
            # Handle LangChain message objects (not SystemMessage)
            elif hasattr(m, "content") and not isinstance(m, SystemMessage):
                if hasattr(m, "type") and m.type == "human":
                    user_message = m.content
                elif not hasattr(m, "type"):
                    user_message = m.content

        if not user_message:
            return {}

        user_message = str(user_message)
        updates = {}

        # Extract doc_folder — only if not already set (doc_folder rarely changes)
        if not state.get("doc_folder"):
            updates["doc_folder"] = self._extract_doc_folder(user_message)

        # Always attempt skill extraction from the latest message so users can
        # switch skills mid-conversation (e.g. "use humanizer skill").
        extracted_skill = self._extract_skill(user_message)
        if extracted_skill:
            updates["skill"] = extracted_skill

        # Extract no_web if not already set
        if state.get("no_web") is None:
            no_web_value = self._extract_no_web(user_message)
            if no_web_value is not None:
                updates["no_web"] = no_web_value

        # Remove None values from updates
        return {k: v for k, v in updates.items() if v is not None}

    @staticmethod
    def _configure_output_folder(doc_folder: str | None) -> None:
        """Configure OUTPUT_FOLDER and DOC_FOLDER environment variables.

        DOC_FOLDER is persisted as an env var so that subagent state schemas
        (which may not include ``doc_folder``) can still access it as a
        fallback inside ``read_doc_folder``.
        """
        reports_output_folder = os.environ.get("REPORTS_OUTPUT_FOLDER", "./output")
        if not doc_folder:
            output_folder = reports_output_folder
        else:
            output_folder = str(Path(reports_output_folder) / Path(doc_folder).name)

        # Normalize path for deepagents filesystem tools compatibility (cross-platform)
        normalized_path = normalize_path_for_filesystem_tools(output_folder)
        os.environ["OUTPUT_FOLDER"] = normalized_path

        # Persist doc_folder so read_doc_folder can fall back to it inside
        # subagents whose state schema doesn't carry the key.
        if doc_folder:
            os.environ["DOC_FOLDER"] = doc_folder
        else:
            os.environ.pop("DOC_FOLDER", None)

    @staticmethod
    def _extract_doc_folder(user_message: str) -> str | None:
        """Extract doc_folder from user message patterns and verify it exists."""
        potential_path: str | None = None

        # Look for --doc-folder pattern
        doc_match = re.search(r"--doc-folder\s+['\"]?([^\s'\"]+)['\"]?", user_message)
        if doc_match:
            # Normalize Windows backslashes to forward slashes
            potential_path = doc_match.group(1).replace("\\", "/")

        if not potential_path:
            # Look for path patterns like ./docs/policy/ or .\docs\policy\ or quoted paths
            path_match = re.search(r"['\"](\.[/\\][^'\"]+)['\"]", user_message)
            if path_match:
                p = path_match.group(1).replace("\\", "/")
                if "doc" in p.lower() or "policy" in p.lower() or "folder" in p.lower():
                    potential_path = p

        if not potential_path:
            # Look for unquoted paths that contain common document folder names
            # Matches ./path/to/dir, /path/to/dir, or path/to/dir
            unquoted_match = re.search(
                r"((?:\.?/)?[\\w/.-]+(?:[/\\][\\w/.-]+)+)", user_message
            )
            if unquoted_match:
                p = unquoted_match.group(1).replace("\\", "/")
                if any(
                    keyword in p.lower()
                    for keyword in ["doc", "policy", "data", "input", "file"]
                ):
                    potential_path = p

        if not potential_path:
            return None

        # Verify the path exists; if not, check if it's inside 'deep_research'
        path = Path(potential_path)
        if not path.exists():
            # Try to prefix with deep_research if not already
            if not potential_path.startswith(
                "./deep_research/"
            ) and not potential_path.startswith("deep_research/"):
                deep_path = Path("deep_research") / potential_path.lstrip("./")
                if deep_path.exists():
                    return str(deep_path)

        return potential_path

    @staticmethod
    def _extract_skill(user_message: str) -> str | None:
        """Extract skill from user message patterns using dynamic skill registry."""
        # Look for --skill pattern
        skill_match = re.search(r"--skill\s+([^\s]+)", user_message)
        if skill_match:
            return skill_match.group(1)

        message_lower = user_message.lower()

        # Combine legacy and migrated skill IDs for direct matching
        all_skill_ids = list(skill_registry.skill_ids) + list(skill_registry.SKILL_IDS)
        # Direct skill-id match: check if any skill ID appears in the user
        # message (e.g. "use humanizer skill" contains "humanizer").
        # Prefer longer IDs first to avoid partial matches.
        for sid in sorted(all_skill_ids, key=len, reverse=True):
            if sid in message_lower:
                return sid

        # Fallback: use skill registry keyword / description matching
        # (legacy skills only — migrated skills have no keyword lists)
        matches = skill_registry.find_skills_by_keyword(message_lower)
        if matches:
            # Return the first match (most relevant based on keyword priority)
            return matches[0].skill_id

        return None

    @staticmethod
    def _extract_no_web(user_message: str) -> bool | None:
        """Extract no_web flag from user message patterns."""
        message_lower = user_message.lower()

        # Patterns that indicate no_web should be True
        disable_patterns = [
            r"without\s+web",
            r"no\s+web",
            r"disable\s+web",
            r"offline",
            r"no\s+internet",
            r"no\s+search",
            r"disable\s+search",
            r"--no-web",
            r"-n(?:\s|$)",
        ]

        for pattern in disable_patterns:
            if re.search(pattern, message_lower):
                return True

        # Patterns that indicate no_web should be False (explicit enable)
        enable_patterns = [
            r"with\s+web",
            r"with\s+search",
            r"enable\s+search",
            r"search\s+the\s+web",
        ]

        for pattern in enable_patterns:
            if re.search(pattern, message_lower):
                return False

        return None

    @staticmethod
    def _build_system_instruction(state: ResearchState) -> str:
        """Build system instruction from ResearchState parameters.

        Appends a *State Context* block so the agent knows what files are
        already available.  This is the general mechanism that lets any skill
        work correctly in follow-up turns — the agent can decide the right
        workflow for any skill (post-process, extend, or start fresh).
        """
        instruction = build_instruction(
            subject="",
            doc_folder=state.get("doc_folder"),
            skill=state.get("skill"),
            no_web=str2bool(state.get("no_web"), False),
        )
        instruction = instruction.replace(
            "Research the following subject: ", ""
        ).strip()

        # --- Wiki-complete fast-path ---
        # When the wiki already produced a complete and sufficient answer (saved
        # as cited_response.md by before_agent), instruct the agent to skip the
        # full research workflow and simply output the existing content.  This
        # prevents infinite write_todos / write_file loops.
        wiki_complete = state.get("wiki_query_complete", False)
        if wiki_complete:
            files = state.get("files") or {}
            cited_response_paths = sorted(
                p for p in files if p.startswith("/cited_response")
            )
            cited_response_path = (
                cited_response_paths[-1]
                if cited_response_paths
                else "/cited_response.md"
            )
            instruction += (
                "\n\n<WikiCompleteAnswer>"
                "\nIMPORTANT: The user's question has ALREADY been fully answered by the "
                "document wiki. The complete answer is already saved as a cited_response file."
                "\nYou MUST follow these steps and NOTHING ELSE:"
                f"\n1. Use `read_file` to read `{cited_response_path}`."
                "\n2. Output the EXACT content of that file as your final conversational reply."
                "\n3. Do NOT call `write_todos`, `write_file`, `task()`, `tavily_search`, "
                "or any other tool."
                "\n4. Do NOT attempt to re-research, re-synthesize, or rewrite the answer."
                "\nThe work is DONE. Just read the file and return its content verbatim."
                "\n</WikiCompleteAnswer>"
            )
            return instruction

        # --- General state context ---
        # Tell the agent what files already exist so it can decide the right
        # workflow for any skill (post-process, extend, or start fresh).
        files = state.get("files") or {}
        if files:
            file_list = ", ".join(f"`{f}`" for f in sorted(files.keys()))
            instruction += (
                "\n\n<State Context>"
                f"\nFiles already available from prior turns: {file_list}"
                "\nIf the user's request refers to existing content (e.g. 'review', "
                "'rewrite', 'improve', 'humanize'), use `read_file` to load the "
                "relevant file first, then apply the requested skill or changes, "
                "then use `write_file` to save the result."
                "\n</State Context>"
            )

        return instruction


# Combine orchestrator instructions (RESEARCHER_INSTRUCTIONS only for sub-agents)
INSTRUCTIONS = (
    RESEARCH_WORKFLOW_INSTRUCTIONS
    + "\n\n"
    + "=" * 80
    + "\n\n"
    + SUBAGENT_DELEGATION_INSTRUCTIONS.format(
        max_concurrent_research_units=MAX_CONCURRENT_RESEARCH_UNITS,
        max_researcher_iterations=MAX_RESEARCHER_ITERATIONS,
    )
)

# Create research subagent
# The sub-agent is intentionally web-only to keep delegation focused and avoid
# filesystem/state write confusion inside isolated sub-agent contexts.
research_sub_agent: SubAgent = {
    "name": "research-agent",
    "description": RESEARCHER_DESCRIPTION,
    "system_prompt": RESEARCHER_INSTRUCTIONS.format(
        date=current_date,
    ),
    "tools": [
        tavily_search,
        fetch_webpage_content,
        think_tool,
    ],
}
try:
    model = get_configured_model()
except Exception as e:
    logger.critical(f"CRITICAL ERROR INITIALIZING MODEL: {e}", exc_info=True)
    traceback.print_exc()
    with open("/deps/deep_research/FATAL_ERROR.log", "w") as f:
        f.write("CRITICAL ERROR: get_configured_model() failed!\n")
        f.write(traceback.format_exc())
    time.sleep(
        15
    )  # Give App Runner 15 seconds to flush the logs to CloudWatch before exiting
    raise
# Recursion limit - configurable via environment variable (applied at graph compile time)
RECURSION_LIMIT = int(os.environ.get("GRAPH_RECURSION_LIMIT", "200"))

# Create the agent
# Orchestrator owns document/filesystem tools.
# Web discovery can still be delegated to `research-agent` via task().
# The `skills` parameter auto-creates SkillsMiddleware backed by the agent's
# internal FilesystemBackend — all skills live in .deepagents/skills/.
# The checkpointer provides persistent state per thread_id — configurable via
# MEMORY_TYPE env var (memory|sqlite|postgres|cosmosdb).
# When unset (default for langgraph dev / LangGraph Platform), the graph is
# created without a checkpointer and the platform injects its own persistence.
checkpointer = create_memory_saver()
_agent_kwargs: dict[str, Any] = dict(
    model=model,
    tools=[
        think_tool,
        read_file,
        write_file,
        ls,
        glob,
        read_docs_folder,
    ],
    system_prompt=INSTRUCTIONS,
    subagents=[research_sub_agent],
    middleware=[ResearchStateMiddleware()],
    skills=[
        ".deepagents/skills/",
        "./doc/.deepagents/skills/",
        "./docs/.deepagents/skills/",
    ],
)
if checkpointer is not None:
    _agent_kwargs["checkpointer"] = checkpointer

agent = create_deep_agent(**_agent_kwargs).with_config(
    RunnableConfig(recursion_limit=RECURSION_LIMIT)
)
