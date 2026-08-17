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
from collections.abc import Awaitable, Callable
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
    ModelRequest,
    TodoListMiddleware,
    hook_config,
)
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_config

from research_agent.cli_utils import get_ssl_verify_config, str2bool
from research_agent.document_context import (
    configure_document_tools,
    has_document_context,
)
from research_agent.logger_utils import setup_logger
from research_agent.model_factory import create_memory_saver, get_configured_model
from research_agent.research_subagent import (
    RESEARCH_WORKFLOW_INSTRUCTIONS,
    RESEARCHER_INSTRUCTIONS,
    SUBAGENT_DELEGATION_INSTRUCTIONS,
)
from research_agent.research_subagent.clarification.middleware import (
    ClarificationMiddleware,
)
from research_agent.research_subagent.clarification.tool import clarify_requirements
from research_agent.research_subagent.prompts import RESEARCHER_DESCRIPTION
from research_agent.research_subagent.resume.middleware import ResumeMiddleware
from research_agent.research_subagent.resume.policy import inspect_todos
from research_agent.research_subagent.tools import (
    fetch_webpage_content,
    glob,
    llm_wiki_query,
    ls,
    read_docs_folder,
    read_file,
    tavily_search,
    think_tool,
    write_file,
)
from research_agent.research_subagent.utils.cli import (
    build_instruction,
)
from research_agent.research_subagent.utils.eval_tracking import log_server_metrics
from research_agent.research_subagent.utils.knowledge_filesystem import (
    normalize_path_for_filesystem_tools,
)
from research_agent.research_subagent.utils.skill_registry import get_skill_registry
from research_agent.research_subagent.utils.verification import (
    ENABLE_VERIFICATION,
    MAX_VERIFICATION_ROUNDS,
    VerificationVerdict,
    format_feedback,
    verify_report,
)

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
ENABLE_EVAL_TRACKING = str2bool(os.environ.get("ENABLE_EVAL_TRACKING"), True)
EVAL_HISTORY_FILE = os.environ.get(
    "EVAL_HISTORY_FILE", "./output/eval_history/server_runs.jsonl"
)
EVAL_LOG_QUESTIONS = str2bool(os.environ.get("EVAL_LOG_QUESTIONS"), False)

# Verification loop — post-generation quality review with iterative revision.
# MAX_VERIFICATION_ROUNDS / ENABLE_VERIFICATION are defined in
# research_agent.research_subagent.utils.verification — re-exported here for convenience.


def _build_verification_todo(
        *, verdict_status: str, verification_round: int, max_rounds: int
) -> dict[str, str]:
    """Build the verification todo shown after one quality-review attempt."""
    attempt = verification_round + 1
    if verdict_status == "needs_revision":
        if attempt < max_rounds:
            content = (
                f"Verification {attempt}/{max_rounds} complete — revision required"
            )
            status = "in_progress"
        else:
            content = (
                f"Verification {attempt}/{max_rounds} complete — "
                "revision limit reached"
            )
            status = "completed"
    else:
        content = f"Verified report quality (round {attempt}/{max_rounds})"
        status = "completed"

    return {
        "id": "verification_pass",
        "content": content,
        "status": status,
    }


# Get current date
current_date = datetime.now().strftime("%Y-%m-%d")

# Initialize dynamic skill registry (use singleton to avoid duplicate initialization)
skill_registry = get_skill_registry()


class ResearchState(FilesystemState):
    """Runtime state for the research agent."""

    doc_folder: str | None
    has_documents: bool | None
    skill: str | None
    no_web: bool | None
    chat_start_time: float | None
    chat_elapsed_seconds: float | None
    _eval_logged: bool
    _streamed_files: list[str] | None
    _last_user_msg_hash: str | None
    # Post-generation verification loop (Wave 1: Reflect)
    verification_round: int
    verification_feedback: str | None
    # Multi-pass research (Wave 2: Plan + Execute)
    research_pass: int


class ResearchStateMiddleware(AgentMiddleware):
    """Middleware to configure state variables like DOC_FOLDER before the agent runs."""

    # Ensure middleware state update are validated against the standard state schema.
    state_schema = ResearchState

    def __init__(
            self,
            *,
            config_getter: Callable[[], dict[str, Any]] = get_config,
    ) -> None:
        """Create middleware with an injectable per-run config source."""
        super().__init__()
        self._config_getter = config_getter

    def _is_resume_round(self, state: ResearchState) -> bool:
        try:
            config = self._config_getter()
        except RuntimeError:
            return False
        configurable = config.get("configurable", {})
        return (
                isinstance(configurable, dict)
                and configurable.get("resume_incomplete_todos") is True
                and inspect_todos(state.get("todos")).has_incomplete
        )

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

    def before_agent(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Pre-process the research state and runtime environment before the agent executes.

        Seeds the research request file, emits progress feedback, initialises
        verification state, extracts parameters, and builds the system instruction.
        """
        messages = state.get("messages", [])
        current_user_message = self._get_current_user_message(messages)
        is_resume_round = self._is_resume_round(state)
        if is_resume_round:
            extracted_updates: dict[str, Any] = {}
            effective_state = state
        else:
            extracted_updates = self._extract_parameters_from_user_input(
                state,
                messages,
            )
            effective_state = {**state, **extracted_updates}

        updates: dict[str, Any] = {}
        if not is_resume_round:
            # Seed the research request file with the latest user message.
            updates = self._seed_research_request_file(
                current_user_message,
                state,
            )
            updates.update(extracted_updates)

        # ── Instant progress feedback ──────────────────────────────────────
        if not is_resume_round:
            status_text = (
                "Searching your uploaded documents for relevant information…"
                if has_document_context(effective_state)
                else "Starting research…"
            )
            updates.setdefault("messages", [])
            if isinstance(updates["messages"], list):
                updates["messages"] = [
                                          AIMessage(content=status_text)
                                      ] + updates["messages"]
            else:
                updates["messages"] = [AIMessage(content=status_text)]

        if not is_resume_round:
            # ── Verification loop state ────────────────────────────────────
            # Track the last user message to detect fresh questions and reset
            # verification state for follow-up turns.
            msg_hash = (
                hashlib.md5((current_user_message or "").encode()).hexdigest()
                if current_user_message
                else ""
            )
            last_hash = state.get("_last_user_msg_hash") or ""
            is_fresh_message = msg_hash and msg_hash != last_hash

            if (
                    "verification_round" not in state
                    or state.get("verification_round") is None
            ):
                updates["verification_round"] = 0
                updates["verification_feedback"] = None
                updates["research_pass"] = 0

            if is_fresh_message:
                updates["verification_round"] = 0
                updates["verification_feedback"] = None
                updates["research_pass"] = 0
                updates["_last_user_msg_hash"] = msg_hash

        # Configure OUTPUT_FOLDER based on extracted doc_folder
        if updates.get("doc_folder") or (
                state.get("doc_folder") and not extracted_updates
        ):
            doc_folder = updates.get("doc_folder") or state.get("doc_folder")
            self._configure_output_folder(doc_folder)
        else:
            self._configure_output_folder(None)

        return updates if updates else None

    def configure_request(self, request: ModelRequest) -> ModelRequest:
        """Inject task configuration into the leading system prompt.

        Task configuration used to be persisted as a ``SystemMessage`` after
        the user's message. Strict Ollama chat templates reject that ordering,
        so configuration is now ephemeral and model-request scoped. Generated
        messages from older checkpoints are removed while their current value
        is rebuilt from state.
        """
        request_state = request.state or {}
        documents_available = has_document_context(request_state)
        instruction = self._build_system_instruction(
            request_state,
            documents_available=documents_available,
        )
        task_configuration = f"Task configurations: \n{instruction}"
        configured_tools = (
            configure_document_tools(request.tools, documents_available)
            if request.tools is not None
            else None
        )

        filtered_messages = [
            message
            for message in request.messages
            if not (
                isinstance(message, SystemMessage)
                and isinstance(message.content, str)
                and message.content.startswith("Task configurations:")
            )
        ]
        messages = (
            request.messages
            if len(filtered_messages) == len(request.messages)
            else filtered_messages
        )

        system_message = request.system_message
        if system_message is None:
            content: str | list[str | dict[str, Any]] = task_configuration
        elif isinstance(system_message.content, str):
            content = (
                f"{system_message.content}\n\n{task_configuration}"
            ).strip()
        else:
            content = [
                *system_message.content,
                {"type": "text", "text": task_configuration},
            ]

        return request.override(
            messages=messages,
            system_message=SystemMessage(content=content),
            tools=configured_tools,
        )

    def wrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], Any],
    ) -> Any:
        """Apply task configuration to a synchronous model request."""
        return handler(self.configure_request(request))

    async def awrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        """Apply task configuration to an asynchronous model request."""
        return await handler(self.configure_request(request))

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Capture chat_start_time before model calls, initializing once per chat."""
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

        if isinstance(chat_start_time, (int, float)):
            chat_elapsed_seconds = time.time() - chat_start_time
            updates["chat_elapsed_seconds"] = chat_elapsed_seconds

        # ── Stream reports into chat history ──────────────────────────────
        # When /final_report.md is ready and the model is no longer emitting
        # tool calls, stream cited_response*.md files first (once each), then
        # a separator, then the final report.  Gating on /final_report.md
        # ensures everything streams together at the end instead of trickling
        # in mid-research.
        state_files = state.get("files") or {}
        if (
                isinstance(state_files, dict)
                and not last_tool_calls
                and "/final_report.md" in state_files
        ):
            streamed = set(state.get("_streamed_files") or [])
            new_messages: list = []

            # ── Phase 1: unstreamed cited_response files, sorted ──────────
            cited_files = sorted(
                [
                    f for f in state_files
                    if f.lstrip("/").startswith("cited_response")
                       and f.endswith(".md")
                       and f not in streamed
                ],
                key=lambda f: (
                    0 if f.rstrip(".md").rstrip("/") in ("/cited_response", "cited_response")
                    else int(f.rstrip(".md").rsplit("_", 1)[-1])
                    if f.rstrip(".md").rsplit("_", 1)[-1].isdigit()
                    else 99
                ),
            )
            for file_path in cited_files:
                try:
                    content = file_data_to_string(state_files[file_path])
                    if content.strip():
                        new_messages.append(
                            AIMessage(
                                content=f"**LLM Wiki Query Findings:**\n\n{content.strip()}"
                            )
                        )
                        streamed.add(file_path)
                except Exception:
                    logger.debug(
                        "Failed to stream %s to chat", file_path, exc_info=True
                    )

            # ── Phase 2: separator ────────────────────────────────────────
            if new_messages and "/final_report.md" not in streamed:
                new_messages.append(AIMessage(content="---"))

            # ── Phase 3: final report ─────────────────────────────────────
            if "/final_report.md" not in streamed:
                try:
                    content = file_data_to_string(state_files["/final_report.md"])
                    if content.strip():
                        new_messages.append(
                            AIMessage(
                                content=f"**Final Report:**\n\n{content.strip()}"
                            )
                        )
                        streamed.add("/final_report.md")
                except Exception:
                    logger.debug(
                        "Failed to stream /final_report.md to chat", exc_info=True
                    )

            if new_messages:
                if "messages" in updates:
                    updates["messages"].extend(new_messages)
                else:
                    updates["messages"] = new_messages
                updates["_streamed_files"] = list(streamed)

        # ── Post-generation verification hook ────────────────────────────
        # When the model has written /final_report.md and is no longer
        # emitting tool calls, run adversarial verification.  If the report
        # needs revision, inject structured feedback as a SystemMessage so
        # the model gets another chance to improve it (up to
        # MAX_VERIFICATION_ROUNDS iterations).
        #
        # This reuses the same "jump_to / SystemMessage injection" pattern
        # proven in the wiki-complete guard above.
        if (
                ENABLE_VERIFICATION
                and not last_tool_calls
                and isinstance(state_files, dict)
                and "/final_report.md" in state_files
        ):
            verification_round = state.get("verification_round", 0)
            if verification_round < MAX_VERIFICATION_ROUNDS:
                try:
                    report_text = file_data_to_string(
                        state_files["/final_report.md"]
                    )
                except Exception:
                    report_text = ""

                if report_text.strip():
                    # Extract user question from state messages.
                    user_question = ""
                    msgs = state.get("messages", [])
                    for m in reversed(msgs):
                        if isinstance(m, HumanMessage):
                            user_question = str(m.content)
                            break
                        elif isinstance(m, dict) and m.get("role") == "user":
                            user_question = str(m.get("content", ""))
                            break
                    if not user_question:
                        # Fallback: read from research_request file.
                        if "/research_request.md" in state_files:
                            try:
                                user_question = file_data_to_string(
                                    state_files["/research_request.md"]
                                )
                            except Exception:
                                pass

                    logger.info(
                        "Verification round %d/%d — reviewing /final_report.md",
                        verification_round + 1,
                        MAX_VERIFICATION_ROUNDS,
                    )

                    # ── Emit verification progress to do ─────────────────
                    existing_todos = list(state.get("todos") or [])
                    verification_todo = {
                        "id": "verification_pass",
                        "content": (
                            f"Verifying report quality "
                            f"(round {verification_round + 1}/{MAX_VERIFICATION_ROUNDS})..."
                        ),
                        "status": "in_progress",
                    }
                    filtered_todos = [
                        t for t in existing_todos
                        if not (
                                isinstance(t, dict)
                                and "verif" in str(t.get("id", "")).lower()
                        )
                    ]
                    updates["todos"] = filtered_todos + [verification_todo]

                    try:
                        # Run verification synchronously (the event-loop
                        # / thread-pool pattern from _check_if_needs_deep_research).
                        async def _verify():
                            return await verify_report(
                                question=user_question,
                                report=report_text,
                            )

                        def _run_verify():
                            return asyncio.run(_verify())

                        try:
                            current_loop = asyncio.get_running_loop()
                        except RuntimeError:
                            current_loop = None

                        if (
                                current_loop is not None
                                and current_loop.is_running()
                        ):
                            with concurrent.futures.ThreadPoolExecutor(
                                    max_workers=1
                            ) as pool:
                                verdict: VerificationVerdict = pool.submit(
                                    _run_verify
                                ).result(timeout=120)
                        else:
                            verdict = asyncio.run(_verify())

                        logger.info(
                            "Verification verdict: %s (score=%.2f, "
                            "grounding_failures=%d, gaps=%d)",
                            verdict.status,
                            verdict.sufficiency_score,
                            sum(
                                1
                                for r in verdict.grounding_results
                                if not r.grounded or not r.reachable
                            ),
                            len(verdict.adversarial_gaps),
                        )
                    except Exception as exc:
                        logger.warning(
                            "Verification check failed: %s. "
                            "Allowing report through without revision.",
                            exc,
                        )
                        verdict = VerificationVerdict(
                            status="complete",
                            sufficiency_score=1.0,
                            sufficiency_reason="",
                            error_message=str(exc),
                        )

                    updates["todos"] = filtered_todos + [
                        _build_verification_todo(
                            verdict_status=verdict.status,
                            verification_round=verification_round,
                            max_rounds=MAX_VERIFICATION_ROUNDS,
                        )
                    ]

                    if verdict.status == "needs_revision":
                        feedback_text = format_feedback(verdict)
                        logger.info(
                            "Report needs revision — injecting feedback "
                            "for round %d",
                            verification_round + 1,
                        )
                        updates["verification_round"] = verification_round + 1
                        updates["verification_feedback"] = feedback_text
                        # Inject feedback as SystemMessage so the model
                        # sees it on the next iteration and revises.
                        updates.setdefault("messages", [])
                        if isinstance(updates["messages"], list):
                            updates["messages"] = [
                                                      SystemMessage(content=feedback_text)
                                                  ] + updates["messages"]
                        # Do NOT jump to end — let the model revise.
                    else:
                        # Report is complete — allow normal termination.
                        updates["verification_round"] = verification_round
                        updates["verification_feedback"] = None

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

                # ── Privacy redaction ───────────────────────────────────
                # When EVAL_LOG_QUESTIONS is False, redact the subject to
                # protect user privacy while preserving metric aggregation.
                if not EVAL_LOG_QUESTIONS:
                    subject = "[REDACTED]"

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

    async def aafter_model(self, state: ResearchState, runtime: Any) -> dict[str, Any] | None:
        """Asynchronous version of after_model that runs verification without blocking the main event loop."""
        chat_start_time = state.get("chat_start_time")
        updates = {}

        # ── Progress messages ──────────────────────────────────────────────
        messages = state.get("messages", [])
        last_msg = messages[-1] if messages else None
        last_tool_calls = getattr(last_msg, "tool_calls", None) or []

        if isinstance(chat_start_time, (int, float)):
            chat_elapsed_seconds = time.time() - chat_start_time
            updates["chat_elapsed_seconds"] = chat_elapsed_seconds

        # ── Stream reports into chat history ──────────────────────────────
        state_files = state.get("files") or {}
        if (
                isinstance(state_files, dict)
                and not last_tool_calls
                and "/final_report.md" in state_files
        ):
            streamed = set(state.get("_streamed_files") or [])
            new_messages: list = []

            # ── Phase 1: unstreamed cited_response files, sorted ──────────
            cited_files = sorted(
                [
                    f for f in state_files
                    if f.lstrip("/").startswith("cited_response")
                       and f.endswith(".md")
                       and f not in streamed
                ],
                key=lambda f: (
                    0 if f.rstrip(".md").rstrip("/") in ("/cited_response", "cited_response")
                    else int(f.rstrip(".md").rsplit("_", 1)[-1])
                    if f.rstrip(".md").rsplit("_", 1)[-1].isdigit()
                    else 99
                ),
            )
            for file_path in cited_files:
                try:
                    content = file_data_to_string(state_files[file_path])
                    if content.strip():
                        new_messages.append(
                            AIMessage(
                                content=f"**LLM Wiki Query Findings:**\n\n{content.strip()}"
                            )
                        )
                        streamed.add(file_path)
                except Exception:
                    logger.debug(
                        "Failed to stream %s to chat", file_path, exc_info=True
                    )

            # ── Phase 2: separator ────────────────────────────────────────
            if new_messages and "/final_report.md" not in streamed:
                new_messages.append(AIMessage(content="---"))

            # ── Phase 3: final report ─────────────────────────────────────
            if "/final_report.md" not in streamed:
                try:
                    content = file_data_to_string(state_files["/final_report.md"])
                    if content.strip():
                        new_messages.append(
                            AIMessage(
                                content=f"**Final Report:**\n\n{content.strip()}"
                            )
                        )
                        streamed.add("/final_report.md")
                except Exception:
                    logger.debug(
                        "Failed to stream /final_report.md to chat", exc_info=True
                    )

            if new_messages:
                if "messages" in updates:
                    updates["messages"].extend(new_messages)
                else:
                    updates["messages"] = new_messages
                updates["_streamed_files"] = list(streamed)

        # ── Post-generation verification hook ────────────────────────────
        if (
                ENABLE_VERIFICATION
                and not last_tool_calls
                and isinstance(state_files, dict)
                and "/final_report.md" in state_files
        ):
            verification_round = state.get("verification_round", 0)
            if verification_round < MAX_VERIFICATION_ROUNDS:
                try:
                    report_text = file_data_to_string(
                        state_files["/final_report.md"]
                    )
                except Exception:
                    report_text = ""

                if report_text.strip():
                    user_question = ""
                    msgs = state.get("messages", [])
                    for m in reversed(msgs):
                        if isinstance(m, HumanMessage):
                            user_question = str(m.content)
                            break
                        elif isinstance(m, dict) and m.get("role") == "user":
                            user_question = str(m.get("content", ""))
                            break
                    if not user_question:
                        if "/research_request.md" in state_files:
                            try:
                                user_question = file_data_to_string(
                                    state_files["/research_request.md"]
                                )
                            except Exception:
                                pass

                    logger.info(
                        "Verification round %d/%d — reviewing /final_report.md (async)",
                        verification_round + 1,
                        MAX_VERIFICATION_ROUNDS,
                    )

                    # ── Emit verification progress todo ─────────────────
                    # Add an in_progress verification task so the frontend
                    # shows a clock icon during the hook's execution window.
                    existing_todos = list(state.get("todos") or [])
                    verification_todo = {
                        "id": "verification_pass",
                        "content": (
                            f"Verifying report quality "
                            f"(round {verification_round + 1}/{MAX_VERIFICATION_ROUNDS})..."
                        ),
                        "status": "in_progress",
                    }
                    filtered_todos = [
                        t for t in existing_todos
                        if not (
                                isinstance(t, dict)
                                and "verif" in str(t.get("id", "")).lower()
                        )
                    ]
                    updates["todos"] = filtered_todos + [verification_todo]

                    try:
                        # Direct await — completely non-blocking!
                        verdict = await verify_report(
                            question=user_question,
                            report=report_text,
                        )
                        logger.info(
                            "Verification verdict: %s (score=%.2f, "
                            "grounding_failures=%d, gaps=%d)",
                            verdict.status,
                            verdict.sufficiency_score,
                            sum(
                                1
                                for r in verdict.grounding_results
                                if not r.grounded or not r.reachable
                            ),
                            len(verdict.adversarial_gaps),
                        )
                    except Exception as exc:
                        logger.warning(
                            "Verification check failed: %s. "
                            "Allowing report through without revision.",
                            exc,
                        )
                        verdict = VerificationVerdict(
                            status="complete",
                            sufficiency_score=1.0,
                            sufficiency_reason="",
                            error_message=str(exc),
                        )

                    updates["todos"] = filtered_todos + [
                        _build_verification_todo(
                            verdict_status=verdict.status,
                            verification_round=verification_round,
                            max_rounds=MAX_VERIFICATION_ROUNDS,
                        )
                    ]

                    if verdict.status == "needs_revision":
                        feedback_text = format_feedback(verdict)
                        logger.info(
                            "Report needs revision — injecting feedback "
                            "for round %d",
                            verification_round + 1,
                        )
                        updates["verification_round"] = verification_round + 1
                        updates["verification_feedback"] = feedback_text
                        updates.setdefault("messages", [])
                        if isinstance(updates["messages"], list):
                            updates["messages"] = [
                                                      SystemMessage(content=feedback_text)
                                                  ] + updates["messages"]
                    else:
                        updates["verification_round"] = verification_round
                        updates["verification_feedback"] = None

        # Optional: Log eval metrics on completion (when graph is done)
        if ENABLE_EVAL_TRACKING and state.get("files"):
            files = state.get("files", {})
            if not isinstance(files, dict):
                return updates if updates else None

            has_final_output = "/final_report.md" in files

            if has_final_output and not state.get("_eval_logged", False):
                updates["_eval_logged"] = True
                runtime_seconds = 0.0
                if isinstance(chat_start_time, (int, float)):
                    runtime_seconds = time.time() - chat_start_time

                messages = state.get("messages", [])
                doc_folder = state.get("doc_folder") or os.environ.get(
                    "DOC_FOLDER", "N/A"
                )
                skill = state.get("skill", "research")
                no_web = state.get("no_web", False)
                model_name = os.environ.get(
                    "MODEL_NAME", os.environ.get("AZURE_OPENAI_DEPLOYMENT", "N/A")
                )

                user_message = None
                for m in messages:
                    if isinstance(m, dict) and m.get("role") == "user":
                        user_message = m.get("content", "")
                        break
                    elif hasattr(m, "type") and getattr(m, "type", None) == "human":
                        user_message = getattr(m, "content", "")
                        break
                subject = user_message

                if not EVAL_LOG_QUESTIONS:
                    subject = "[REDACTED]"

                context = {
                    "subject": subject,
                    "skill": skill,
                    "doc_folder": doc_folder,
                    "no_web": no_web,
                }

                try:
                    await log_server_metrics(
                        messages=messages,
                        files=files,
                        runtime_seconds=runtime_seconds,
                        model_name=model_name,
                        context=context,
                        history_file=EVAL_HISTORY_FILE,
                    )
                    logger.info("✅ Metrics logging completed (async)")
                except Exception as e:
                    logger.error(f"⚠️  Failed metrics logging: {e}")

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
            # Look for unquoted paths that contain common documents folder names
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
    def _build_system_instruction(
            state: ResearchState, *, documents_available: bool | None = None
    ) -> str:
        """Build system instruction from ResearchState parameters.

        Appends a *State Context* block so the agent knows what files are
        already available.  This is the general mechanism that lets any skill
        work correctly in follow-up turns — the agent can decide the right
        workflow for any skill (post-process, extend, or start fresh).
        """
        if documents_available is None:
            documents_available = has_document_context(state)
        no_web = str2bool(state.get("no_web"), False)
        instruction = build_instruction(
            subject="",
            doc_folder=state.get("doc_folder") if documents_available else None,
            skill=state.get("skill"),
            no_web=no_web and documents_available,
        )
        instruction = instruction.replace(
            "Research the following subject: ", ""
        ).strip()

        # --- Structured research plan directive ---
        instruction += (
            "\n\n<PlanDirective>"
            "\nBefore delegating to sub-agents, create a structured research plan "
            "using `write_todos`. For each research question include:"
            "\n1. What specific information is needed to answer it."
            "\n2. Success criteria: 3+ credible sources per major claim, coverage of "
            "all sub-questions, specific data points where applicable."
            "\n3. Which sub-agent(s) will address each information need."
            "\n\nAfter receiving sub-agent results, compare findings against the "
            "success criteria. If criteria are not met, identify remaining gaps "
            "and launch targeted follow-up sub-agent tasks to fill them before "
            "synthesizing the final report."
            "\n</PlanDirective>"
        )

        # --- Verification feedback injection ---
        # When a prior verification pass found issues, surface the structured
        # feedback in the system instruction so the model sees it on every
        # iteration of the revision loop.
        verification_feedback = state.get("verification_feedback")
        if verification_feedback:
            instruction += "\n\n" + verification_feedback

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

        if documents_available:
            instruction += (
                "\n\n<Source Guidance>"
                "\nUploaded sources are available. Use `llm_wiki_query` or "
                "`read_docs_folder` to ground relevant claims in those sources."
                "\n</Source Guidance>"
            )
        elif no_web:
            instruction += (
                "\n\n<Source Guidance>"
                "\nNeither uploaded documents nor web research is available. Do not "
                "invent facts or use `llm_wiki_query`, `read_docs_folder`, "
                "`tavily_search`, or `fetch_webpage_content`. You may still use "
                "workflow and output tools such as `write_todos`, `write_file`, "
                "and applicable `read_file`. Clearly report this source constraint "
                "to the user."
                "\n</Source Guidance>"
            )
        else:
            instruction += (
                "\n\n<Source Guidance>"
                "\nNo uploaded document sources are available. Do not call "
                "`llm_wiki_query` or `read_docs_folder`. Use `task` with "
                '`subagent_type="research-agent"` for web research.'
                "\n</Source Guidance>"
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
# Orchestrator owns documents/filesystem tools.
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
        clarify_requirements,
        think_tool,
        read_file,
        write_file,
        ls,
        glob,
        read_docs_folder,
        llm_wiki_query,
    ],
    system_prompt=INSTRUCTIONS,
    subagents=[research_sub_agent],
    middleware=[
        TodoListMiddleware(system_prompt=""),
        ClarificationMiddleware(),
        ResumeMiddleware(),
        ResearchStateMiddleware(),
    ],
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
