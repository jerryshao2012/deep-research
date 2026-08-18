"""Command-line interface (CLI) launcher for the Deep Research agent.

Sets up argument parsers, tracks thread state memory, displays a console spinner
during processing, and prints final response messages and outputs.
"""

import asyncio
import itertools
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from deepagents.backends.utils import file_data_to_string
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from research_agent.agent import agent, model
from research_agent.citation_failure import (
    ReportCitationAggregateError,
    ReportCitationError,
    citation_failure_is_current,
    raise_current_citation_failure,
)
from research_agent.cli_utils import format_messages, show_prompt, str2bool
from research_agent.completion_guard import artifact_fingerprint
from research_agent.model_call_guard import (
    ModelCallTimeoutError,
    cancel_model_call_scope,
)
from research_agent.research_subagent.resume import inspect_todos
from research_agent.research_subagent.utils.cli import build_parser, list_skills
from research_agent.research_subagent.utils.knowledge_filesystem import (
    normalize_path_for_filesystem_tools,
)

# Load environment variables
load_dotenv()


class Spinner:
    """A console spinner that displays an animated progress indicator.

    Runs a brailler-pattern animation in a background daemon thread,
    over-writable with a custom status message.

    Attributes:
        message: The status text displayed next to the spinner.
    """

    def __init__(self, message="Working..."):
        """Initialize the spinner with an optional status message.

        Args:
            message: Status text to display. Defaults to ``"Working..."``.
        """
        self.spinner = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])
        self.stop_running = threading.Event()
        self.thread = None
        self.message = message

    def spin(self):
        """Run the spinner animation loop. Called from a background thread."""
        while not self.stop_running.is_set():
            sys.stdout.write(f"\r\033[K\033[36m{next(self.spinner)}\033[0m {self.message}")
            sys.stdout.flush()
            time.sleep(0.1)

    def start(self, message=None):
        """Start the spinner in a daemon thread.

        Args:
            message: Optional new status message to display.
        """
        if message:
            self.message = message
        self.stop_running.clear()
        self.thread = threading.Thread(target=self.spin)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        """Stop the spinner and clear the animation line from the terminal."""
        self.stop_running.set()
        if self.thread and self.thread.is_alive():
            self.thread.join()
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def wrap_as_message(m):
    """Wrap a dict-based message into a LangChain BaseMessage for format_messages compatibility."""
    if isinstance(m, BaseMessage):
        return m
    if not isinstance(m, dict):
        return HumanMessage(content=str(m))

    role = m.get("role", "")
    content = m.get("content", "")
    name = m.get("name")
    tool_calls = m.get("tool_calls")

    if role == "user" or role == "human":
        return HumanMessage(content=content, name=name)
    if role == "assistant" or role == "ai":
        return AIMessage(content=content, name=name, tool_calls=tool_calls)
    if role == "tool":
        return ToolMessage(content=content, tool_call_id=m.get("tool_call_id", "N/A"), name=name)

    return HumanMessage(content=content, name=name)


CSV_EXPORT_PATH_RE = re.compile(r"\*\*CSV exported to:\*\*\s*`([^`]+)`")
MAX_STREAM_DIAGNOSTIC_CHARS = 600


def _normalize_model_call_config(config):
    """Return one safe config carrying a cancellable model-call scope."""
    if not isinstance(config, Mapping):
        return {"configurable": {"model_call_scope_id": str(uuid.uuid4())}}

    configurable = config.get("configurable")
    if isinstance(configurable, Mapping) and configurable.get("model_call_scope_id"):
        return config

    resolved = dict(config)
    resolved_configurable = dict(configurable) if isinstance(configurable, Mapping) else {}
    resolved_configurable["model_call_scope_id"] = str(uuid.uuid4())
    resolved["configurable"] = resolved_configurable
    return resolved


def _model_call_scope_id(config) -> str | None:
    configurable = config.get("configurable", {})
    if not isinstance(configurable, Mapping):
        return None
    scope_id = configurable.get("model_call_scope_id")
    return str(scope_id) if scope_id is not None else None


def _contains_model_control_error(error: BaseException) -> bool:
    """Detect timeout/cancellation controls, including nested exception groups."""
    if isinstance(
        error,
        (
            ModelCallTimeoutError,
            ReportCitationError,
            asyncio.CancelledError,
            KeyboardInterrupt,
        ),
    ):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_model_control_error(item) for item in error.exceptions)
    return False


def _find_report_citation_error(
    error: BaseException,
) -> ReportCitationError | None:
    """Extract a safe citation control from nested exception groups."""
    if isinstance(error, ReportCitationError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for item in error.exceptions:
            found = _find_report_citation_error(item)
            if found is not None:
                return found
    return None


def _citation_control_categories(error: BaseException) -> set[str]:
    """Return fixed safe categories without inspecting exception messages."""
    if isinstance(error, ReportCitationError):
        return {"citation"}
    if isinstance(error, ModelCallTimeoutError):
        return {"timeout"}
    if isinstance(error, asyncio.CancelledError):
        return {"cancellation"}
    if isinstance(error, KeyboardInterrupt):
        return {"interrupt"}
    if isinstance(error, BaseExceptionGroup):
        categories: set[str] = set()
        for item in error.exceptions:
            categories.update(_citation_control_categories(item))
        return categories
    return {"persistence"}


def _raise_safe_citation_control(error: BaseException) -> None:
    """Raise citation control without formatting group or sibling messages."""
    if isinstance(error, ReportCitationError):
        raise error from None
    raise ReportCitationAggregateError(
        _citation_control_categories(error)
    ) from None


def _cancel_configured_model_scope(config) -> None:
    scope_id = _model_call_scope_id(config)
    if scope_id is not None:
        cancel_model_call_scope(scope_id)


def _citation_result_identity(
    result: Mapping[str, object],
    *,
    run_id: object = None,
) -> tuple[str | None, str | None]:
    resolved_run_id = run_id
    if not isinstance(resolved_run_id, (str, uuid.UUID)):
        resolved_run_id = result.get("completion_current_run_id")
    if isinstance(resolved_run_id, uuid.UUID):
        resolved_run_id = str(resolved_run_id)
    if not isinstance(resolved_run_id, str) or not resolved_run_id:
        resolved_run_id = None

    files = result.get("files")
    report = files.get("/final_report.md") if isinstance(files, Mapping) else None
    return resolved_run_id, artifact_fingerprint(report)


def _citation_failure_pending(
    result: Mapping[str, object],
    *,
    run_id: object = None,
) -> bool:
    resolved_run_id, report_fingerprint = _citation_result_identity(
        result,
        run_id=run_id,
    )
    return citation_failure_is_current(
        result,
        run_id=resolved_run_id,
        report_fingerprint=report_fingerprint,
    )


def _raise_result_citation_failure(
    result: Mapping[str, object],
    *,
    run_id: object = None,
) -> None:
    resolved_run_id, report_fingerprint = _citation_result_identity(
        result,
        run_id=run_id,
    )
    raise_current_citation_failure(
        result,
        run_id=resolved_run_id,
        report_fingerprint=report_fingerprint,
    )


def _raise_unaccepted_report(
    result: Mapping[str, object],
    *,
    strict_required: bool,
) -> None:
    """Fail closed for malformed or unaccepted reports in strict CLI mode."""
    if not strict_required:
        return
    files = result.get("files")
    if not isinstance(files, Mapping) or "/final_report.md" not in files:
        return
    fingerprint = artifact_fingerprint(files.get("/final_report.md"))
    if (
        not isinstance(fingerprint, str)
        or not fingerprint
        or result.get("citation_accepted_report_fingerprint") != fingerprint
    ):
        raise ReportCitationError()


def _render_live_stream_message(message: object, *, no_web: bool) -> None:
    """Render live model/tool content only when web generation is disabled."""
    if no_web:
        format_messages([wrap_as_message(message)])


def _render_final_result(
    result: Mapping[str, object],
    content: str,
    *,
    no_web: bool,
) -> None:
    """Render one accepted web result, without replaying rejected generations."""
    if no_web:
        messages = result.get("messages")
        if isinstance(messages, list):
            format_messages([wrap_as_message(message) for message in messages])
        return

    _raise_unaccepted_report(result, strict_required=True)
    files = result.get("files")
    report = files.get("/final_report.md") if isinstance(files, Mapping) else None
    if report is None:
        return
    fingerprint = artifact_fingerprint(report)
    if result.get("citation_accepted_report_fingerprint") != fingerprint:
        return
    format_messages([AIMessage(content=content)])


def generate_research_title(research_content, *, config):
    """Generate a concise title for the research content using the configured LLM."""
    config = _normalize_model_call_config(config)
    try:
        content_snippet = extract_message_content(research_content)[:2000]

        prompt = (
            "Based on the following research content, generate a short, concise, and descriptive "
            "file name (maximum 5 words, without extension). Return ONLY the file name, using "
            "kebab-case or snake_case for spacing. No quotes, no extra text:\\n\\n"
            f"{content_snippet}"
        )

        response = model.invoke([HumanMessage(content=prompt)], config=config)
        title = response.content.strip()

        # Format title with underscores and proper capitalization
        title = title.replace(" ", "_").title()  # Replace spaces with underscores first
        title = ''.join(
            [c if c.isalnum() or c == '_' else '_' for c in title])  # Replace special characters with underscores
        title = re.sub(r'_+', '_', title)  # Replace multiple underscores with single
        title = title.strip('_')  # Remove leading/trailing underscores
        return title if title else "research-report"
    except BaseException as error:
        citation_error = _find_report_citation_error(error)
        if citation_error is not None:
            _cancel_configured_model_scope(config)
            _raise_safe_citation_control(error)
        if _contains_model_control_error(error):
            _cancel_configured_model_scope(config)
            raise
        if isinstance(error, Exception):
            print(f"Warning: Could not generate title ({error}). Using default.")
            return "research-report"
        raise


def extract_message_content(message):
    """Normalize agent output into plain text for saving and display."""
    if isinstance(message, dict):
        content = message.get("content", "")
    elif isinstance(message, BaseMessage):
        content = message.content
    else:
        content = message

    if isinstance(content, list):
        normalized_parts = []
        for item in content:
            if isinstance(item, str):
                normalized_parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or str(item)
                normalized_parts.append(text)
            else:
                normalized_parts.append(str(item))
        return "\n".join(part for part in normalized_parts if part)

    return str(content)


def _message_role_name_content(message) -> tuple[str, str, str]:
    """Extract role, tool name, and normalized content from any message shape."""
    if isinstance(message, dict):
        role = message.get("role", "")
        name = message.get("name", "") or ""
    else:
        role = getattr(message, "type", "")
        name = getattr(message, "name", "") or ""
    return role, name, extract_message_content(message)


def _is_unsuccessful_tool_output(content: str) -> bool:
    """Check whether a tool output string indicates a failure.

    Returns ``True`` for empty output or output starting with known
    error prefixes (invalid JSON, schema validation failure, etc.).
    """
    text = content.strip()
    failure_prefixes = (
        "Invalid JSON payload:",
        "Schema validation failed",
        "Unknown skill",
        "Error invoking tool",
        "ERROR:",
    )
    return not text or text.startswith(failure_prefixes)


def _looks_like_incomplete_delegation(content: str) -> bool:
    """Check whether the agent output looks like an unfinished delegation.

    Returns ``True`` if the content contains phrases indicating the agent
    is still waiting for sub-agent results rather than delivering a final
    answer.
    """
    text = content.strip().lower()
    if not text:
        return True
    markers = (
        "i have delegated",
        "i've delegated",
        "has been delegated",
        "awaiting the results",
        "awaiting results",
        "once the agent returns",
        "once the findings are returned",
        "i will synthesize",
        "will synthesize the information",
        "use the render_skill_output tool to deliver the final result",
        "use the render_skill_output tool to generate the final deliverable",
    )
    return any(marker in text for marker in markers)


def _truncate_for_log(content: str, max_chars: int = MAX_STREAM_DIAGNOSTIC_CHARS) -> str:
    """Truncate and flatten content for safe inclusion in log/diagnostic messages.

    Args:
        content: The text to truncate.
        max_chars: Maximum allowed length. Defaults to
            ``MAX_STREAM_DIAGNOSTIC_CHARS``.

    Returns:
        The content collapsed to a single line and truncated with ``"..."``
        if it exceeds ``max_chars``.
    """
    text = content.strip().replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _is_azure_content_filter_error(error: Exception) -> bool:
    """Detect Azure Content Safety filter errors from exception messages.

    Returns ``True`` if the error text contains keywords associated with
    Azure's content filtering or Responsible AI safety system.
    """
    text = str(error).lower()
    markers = (
        "content filter",
        "content_filter"
        "responsibleai"
        "safety system",
    )
    return any(marker in text for marker in markers)


def _last_stream_message_diagnostics(state: dict | None) -> tuple[str, str, str]:
    """Extract diagnostic info from the last message in a streamed state.

    Used after a streaming failure to help debug what the agent was doing.

    Args:
        state: The last known agent state from streaming, or ``None``.

    Returns:
        A tuple of ``(role, name, truncated_content_preview)``.
    """
    if not state:
        return "", "", ""

    messages = state.get("messages", [])
    if not messages:
        return "", "", ""
    last = messages[-1]
    role, name, content = _message_role_name_content(last)
    return role, name, _truncate_for_log(content)


def select_output_content(
    result: dict,
    skill: str | None = None,
    *,
    run_id: object = None,
    strict_citations_required: bool = True,
) -> str:
    """Choose the best final content from files/messages for saving to disk."""
    _raise_result_citation_failure(result, run_id=run_id)
    _raise_unaccepted_report(
        result,
        strict_required=strict_citations_required,
    )
    files = result.get("files", {})
    if "/final_report.md" in files:
        return file_data_to_string(files["/final_report.md"])

    messages = result.get("messages", [])
    if not messages:
        return ""

    if skill:
        for message in reversed(messages):
            role, name, content = _message_role_name_content(message)
            if role == "tool" and name == "render_skill_output" and not _is_unsuccessful_tool_output(content):
                return content

        # Structured skills may be rendered successfully inside a subagent and then
        # returned through the parent `task` tool as plain tool output.
        for message in reversed(messages):
            role, name, content = _message_role_name_content(message)
            if role == "tool" and name == "task" and not _is_unsuccessful_tool_output(content):
                return content

    last_message = messages[-1]
    return extract_message_content(last_message)


def should_retry_with_invoke(
    result: dict,
    skill: str | None = None,
    *,
    run_id: object = None,
    strict_citations_required: bool = True,
) -> bool:
    """Detect partial streamed states that should be retried via synchronous invoke."""
    if _citation_failure_pending(result, run_id=run_id):
        return False
    _raise_unaccepted_report(
        result,
        strict_required=strict_citations_required,
    )
    if inspect_todos(result.get("todos")).has_incomplete:
        return True
    content = select_output_content(
        result,
        skill,
        run_id=run_id,
        strict_citations_required=strict_citations_required,
    )
    return _looks_like_incomplete_delegation(content)


def save_research_to_file(
    research_content,
    filename=None,
    output_folder=None,
    *,
    config=None,
):
    """Save research output to a timestamped Markdown file.

    Generates a concise title using the LLM and writes the content to
    ``<title>-<date>.md`` inside the output folder.

    Args:
        research_content: The research text (string or message object).
        filename: Optional explicit filename. Auto-generated if not given.
        output_folder: Target directory path. Defaults to the current
            working directory.

    Returns:
        The filesystem path to the saved file as a string.
    """
    # Get current date and time
    current_date = datetime.now().strftime("%Y-%m-%d_%I_%M_%S_%p")

    # Generate a title for the research
    title = generate_research_title(research_content, config=config)

    # If filename is not provided, use the generated title as the filename
    if not filename:
        filename = f"{title}-{current_date}.md"

    # Extract string content if a dictionary message was passed
    research_content = extract_message_content(research_content)

    # Determine the full path for the file
    if output_folder:
        output_dir = Path(output_folder)
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / filename
    else:
        file_path = Path(filename)

    # Write the research content to the file
    with open(file_path, "w", encoding="utf-8") as file:
        file.write(research_content)

    return str(file_path)


def derive_output_folder(doc_folder: str | None) -> Path:
    """Resolve the output directory for research results.

    If a documents folder is configured, a subdirectory named after it is
    created inside ``REPORTS_OUTPUT_FOLDER``.

    Args:
        doc_folder: Path to the documents folder, or ``None``.

    Returns:
        The resolved output directory as a ``Path``.
    """
    if not doc_folder:
        return Path(os.environ.get("REPORTS_OUTPUT_FOLDER", "./output"))
    return Path(os.environ.get("REPORTS_OUTPUT_FOLDER", "./output")) / Path(doc_folder).name


def configure_output_folder(doc_folder: str | None) -> Path:
    """Set up the output folder and persist relevant environment variables.

    Sets ``OUTPUT_FOLDER`` and ``DOC_FOLDER`` in the process environment so
    that sub-agents and filesystem tools can discover the correct paths.

    Args:
        doc_folder: Path to the documents folder, or ``None``.

    Returns:
        The normalized output directory path.
    """
    output_folder = derive_output_folder(doc_folder)
    # Normalize path for deepagents filesystem tools compatibility (cross-platform)
    normalized_path = normalize_path_for_filesystem_tools(str(output_folder))
    os.environ["OUTPUT_FOLDER"] = normalized_path
    # Persist doc_folder so read_doc_folder can access it inside subagents
    # whose state schema doesn't carry the key.
    if doc_folder:
        os.environ["DOC_FOLDER"] = doc_folder
    else:
        os.environ.pop("DOC_FOLDER", None)
    return Path(normalized_path)


def main():
    """Entry point for the Deep Research CLI.

    Parses command-line arguments, sets up the output folder and thread
    configuration, streams the agent's progress with a spinner, handles
    streaming fallback on error, and saves the final research output to
    a Markdown file.
    """
    parser = build_parser()
    args = parser.parse_args()
    args.verify_ssl = str2bool(args.verify_ssl)
    args.verbose = str2bool(args.verbose)

    if args.help:
        parser.print_help()
        sys.exit(0)

    if args.skill == "list":
        list_skills()
        sys.exit(0)

    subject = args.subject
    if not subject and args.subject_file and os.path.exists(args.subject_file):
        with open(args.subject_file, "r", encoding="utf-8") as handle:
            subject = handle.read().strip()

    instruction = subject
    title = None

    if args.title:
        title = args.title

    # Generate or use provided thread_id for state tracking (enables wiki context, etc.)
    thread_id = args.thread_id or str(uuid.uuid4())
    invocation_run_id = uuid.uuid4()
    model_call_scope_id = str(uuid.uuid4())
    config = {
        "run_id": invocation_run_id,
        "configurable": {
            "thread_id": thread_id,
            "model_call_scope_id": model_call_scope_id,
        }
    }
    print(f"Thread ID: {thread_id}")

    print(f"Starting research on: {args.subject}")
    show_prompt(instruction, title="Research Instruction")
    print("This may take a few minutes as the agent searches and analyzes...")

    # Run the agent with progress printouts
    result = {}
    start_time = time.time()
    last_time = start_time

    # Determine output folder for output
    output_folder = configure_output_folder(args.doc_folder)
    print(f"Output subfolder is set to: {output_folder}")

    messages = {
        "messages": [
            {
                "role": "user",
                "content": instruction,
            }
        ],
        "doc_folder": args.doc_folder,
        "no_web": args.no_web,
        "skill": args.skill,
    }
    if args.verbose:
        # Show progress with spinner
        spinner = Spinner("Initializing research inputs...")
        spinner.start()
        last_stream_state = None
        last_tool_name = ""

        try:
            # We attempt to stream updates from LangGraph to provide visibility
            for state in agent.stream(
                    messages,
                    config=config,
                    stream_mode="values",
            ):
                _raise_result_citation_failure(
                    state,
                    run_id=invocation_run_id,
                )
                current_time = time.time()
                step_time = current_time - last_time
                last_time = current_time

                spinner.stop()

                # Inspect the latest state change
                msgs = state.get("messages", [])
                next_spinner_msg = "Agent is working..."

                if msgs:
                    last = msgs[-1]
                    last_stream_state = state
                    # Web-capable runs suppress raw model/tool content until citation
                    # acceptance. Safe status text below remains live.
                    _render_live_stream_message(last, no_web=args.no_web)

                    # Handle both dict-based and object-based messages
                    if isinstance(last, dict):
                        role = last.get("role", "")
                        content = str(last.get("content", ""))
                        name = last.get("name", "")
                        tool_calls = last.get("tool_calls", [])
                    else:
                        role = getattr(last, "type", "")
                        content = str(getattr(last, "content", ""))
                        name = getattr(last, "name", "")
                        tool_calls = getattr(last, "tool_calls", [])

                    # Web-capable progress contains fixed trusted text only.
                    if not args.no_web:
                        if role == "ai" and tool_calls:
                            print(f"⚙️  Research tool running (⏱️  {step_time:.1f}s)")
                            next_spinner_msg = "Research tool running..."
                        elif role in {"ai", "tool"}:
                            print(f"✅ Research step completed (⏱️  {step_time:.1f}s)")
                            next_spinner_msg = "Research step completed..."
                        elif role in {"human", "user"}:
                            print(f"🚀 Research started (⏱️  {step_time:.1f}s)")
                            next_spinner_msg = "Research in progress..."
                    elif role == "ai" and tool_calls:
                        for tc in tool_calls:
                            tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                            last_tool_name = tc_name
                            print(f"⚙️  Agent decided to act: Calling `{tc_name}`... (⏱️  {step_time:.1f}s)")
                            next_spinner_msg = f"Executing `{tc_name}`..."
                    elif role == "tool":
                        if name:
                            last_tool_name = name
                        print(
                            f"✅ Executed tool `{name}` successfully (output size: {len(content)} chars) (⏱️  {step_time:.1f}s)")
                        next_spinner_msg = "Analyzing tool output..."
                    elif role == "ai" and content:
                        print(f"💬 Agent updated its response based on findings... (⏱️  {step_time:.1f}s)")
                        next_spinner_msg = "Structuring final thoughts..."
                    elif role == "human" or role == "user":
                        print(f"🚀 Started research task... (⏱️  {step_time:.1f}s)")
                        next_spinner_msg = "Agent is formulating a plan..."

                result = state  # The last emitted state is our final result
                spinner.start(next_spinner_msg)

            spinner.stop()
            total_time = time.time() - start_time
            print(f"\n✨ Research completed in {total_time:.1f}s!\n")
        except BaseException as error:
            spinner.stop()
            citation_error = _find_report_citation_error(error)
            if citation_error is not None:
                cancel_model_call_scope(model_call_scope_id)
                _raise_safe_citation_control(error)
            if _contains_model_control_error(error):
                cancel_model_call_scope(model_call_scope_id)
                raise
            if not isinstance(error, Exception):
                raise
            e = error
            spinner.stop()
            total_time = time.time() - start_time
            if _is_azure_content_filter_error(e):
                print("⚠️  Streaming interrupted by Azure Content Filtering."
                      f"Switching to fallback invoke... (failed after {total_time:.1f}s)"
                      )
            elif not args.no_web:
                print(
                    "⚠️  Web-capable stream interrupted; running normally... "
                    f"(failed after {total_time:.1f}s)"
                )
            else:
                print(f"⚠️  Streaming not fully supported ({e}), running normally... (failed after {total_time:.1f}s)")

            if args.no_web:
                role, name, preview = _last_stream_message_diagnostics(
                    last_stream_state
                )
                diagnostic_tool_name = name or last_tool_name or "unknown"
                print(f"🔎  Stream diagnostics: "
                      f"last_tool=`{diagnostic_tool_name}`, last_role=`{role or 'unknown'}`"
                      )
                if preview:
                    print(f"🔎  Preview of last message (truncated): {preview}")

            spinner.start("Running fallback synchronous invoke...")
            start_invoke = time.time()
            try:
                result = agent.invoke(
                    messages,
                    config=config,
                )
                _raise_result_citation_failure(
                    result,
                    run_id=invocation_run_id,
                )
            except BaseException as error:
                spinner.stop()
                citation_error = _find_report_citation_error(error)
                if citation_error is not None:
                    cancel_model_call_scope(model_call_scope_id)
                    _raise_safe_citation_control(error)
                if _contains_model_control_error(error):
                    cancel_model_call_scope(model_call_scope_id)
                raise
            else:
                spinner.stop()

            invoke_time = time.time() - start_invoke
            print(f"\n✨ Fallback research completed in {invoke_time:.1f}s!\n")
    else:
        # Run the agent directly without showing progress
        try:
            result = agent.invoke(
                messages,
                config=config,
            )
            _raise_result_citation_failure(
                result,
                run_id=invocation_run_id,
            )
        except BaseException as error:
            citation_error = _find_report_citation_error(error)
            if citation_error is not None:
                cancel_model_call_scope(model_call_scope_id)
                _raise_safe_citation_control(error)
            if _contains_model_control_error(error):
                cancel_model_call_scope(model_call_scope_id)
            raise
        total_time = time.time() - start_time
        print(f"\n✨ Research completed in {total_time:.1f}s!\n")

    _raise_result_citation_failure(result, run_id=invocation_run_id)
    try:
        retry_with_invoke = should_retry_with_invoke(
            result,
            args.skill,
            run_id=invocation_run_id,
            strict_citations_required=not args.no_web,
        )
    except ReportCitationError:
        cancel_model_call_scope(model_call_scope_id)
        raise
    if retry_with_invoke:
        spinner = Spinner("Stream ended with incomplete output; running final synchronous pass...")
        spinner.start()
        start_invoke = time.time()
        try:
            result = agent.invoke(
                messages,
                config=config,
            )
            _raise_result_citation_failure(
                result,
                run_id=invocation_run_id,
            )
        except BaseException as error:
            spinner.stop()
            citation_error = _find_report_citation_error(error)
            if citation_error is not None:
                cancel_model_call_scope(model_call_scope_id)
                _raise_safe_citation_control(error)
            if _contains_model_control_error(error):
                cancel_model_call_scope(model_call_scope_id)
            raise
        else:
            spinner.stop()
        invoke_time = time.time() - start_invoke
        print(f"\n🔁 Finalization pass completed in {invoke_time:.1f}s!\n")

    _raise_result_citation_failure(result, run_id=invocation_run_id)
    try:
        file_content = select_output_content(
            result,
            args.skill,
            run_id=invocation_run_id,
            strict_citations_required=not args.no_web,
        )
    except ReportCitationError:
        cancel_model_call_scope(model_call_scope_id)
        raise
    if args.verbose:
        _render_final_result(
            result,
            file_content,
            no_web=args.no_web,
        )
    filename = save_research_to_file(
        file_content,
        title,
        output_folder=output_folder,
        config=config,
    )

    print("\n" + "=" * 80)
    if "/final_report.md" in result.get("files", {}):
        print(f"Final Report ({filename}):")
    else:
        print(f"Final Response ({filename}):")
    print("=" * 80)


if __name__ == "__main__":
    main()
