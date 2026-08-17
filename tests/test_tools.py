"""Tests for research agent tool functions."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends.utils import create_file_data
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from research_agent.agent import ResearchStateMiddleware
from research_agent.research_subagent import tools
from research_agent.research_subagent.tools import (
    fetch_webpage_content,
    read_docs_folder,
)


class _NamedTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _model_request(
        *,
        state: dict[str, object],
        tools: list[object],
        tool_choice: object | None = None,
) -> ModelRequest:
    return ModelRequest(
        model=object(),  # type: ignore[arg-type]
        messages=[HumanMessage(content="Research this topic")],
        state=state,
        system_message=SystemMessage(content="base instructions"),
        tools=tools,  # type: ignore[arg-type]
        tool_choice=tool_choice,
    )


def _tool_names(tools: list[object]) -> list[str | None]:
    return [
        getattr(tool, "name", tool.get("name") if isinstance(tool, dict) else None)
        for tool in tools
    ]


def _ollama_message_payload(messages: list[SystemMessage | HumanMessage]) -> list[dict[str, str]]:
    """Convert test request messages to the strict Ollama role payload shape."""
    payload: list[dict[str, str]] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            content = "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        payload.append(
            {
                "role": "system" if isinstance(message, SystemMessage) else "user",
                "content": str(content),
            }
        )
    return payload


# ── read_docs_folder tests ──

def test_read_docs_folder_reads_text_and_markdown_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", str(tmp_path / "output"))
    (tmp_path / "notes.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "summary.md").write_text("# heading", encoding="utf-8")

    result = read_docs_folder.func(
        folder_path=str(tmp_path),
        state={"doc_folder": str(tmp_path)}
    )

    assert "Content of notes.txt" in result
    assert "alpha" in result
    assert "Content of summary.md" in result
    assert "# heading" in result


def test_read_docs_folder_reports_unavailable_for_unsupported_only_folder(
        tmp_path: Path,
) -> None:
    (tmp_path / "image.png").write_bytes(b"png")

    result = read_docs_folder.func(
        folder_path=str(tmp_path),
        state={"doc_folder": str(tmp_path)}
    )

    assert "No uploaded documents are available" in result
    assert "research-agent" in result


def test_read_docs_folder_fails_closed_before_read_without_documents(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_folder = tmp_path / "stale-upload"
    upload_folder.mkdir()
    (upload_folder / "source.md").write_text("stale", encoding="utf-8")

    def fail_read(*_args, **_kwargs):
        raise AssertionError("document reader must not run")

    monkeypatch.setattr(tools, "read_docs_folder_impl", fail_read)

    result = read_docs_folder.func(
        folder_path=str(upload_folder),
        state={"has_documents": False, "doc_folder": str(upload_folder)},
    )

    assert "No uploaded documents are available" in result
    assert "If web access is enabled" in result
    assert 'subagent_type="research-agent"' in result


def test_read_docs_folder_fails_closed_before_read_for_missing_source(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_folder = tmp_path / "missing-upload"

    def fail_read(*_args, **_kwargs):
        raise AssertionError("document reader must not run")

    monkeypatch.setattr(tools, "read_docs_folder_impl", fail_read)

    result = read_docs_folder.func(
        folder_path=str(missing_folder),
        state={"has_documents": True, "doc_folder": str(missing_folder)},
    )

    assert "No uploaded documents are available" in result
    assert "research-agent" in result


def test_read_docs_folder_does_not_suggest_web_when_disabled(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_read(*_args, **_kwargs):
        raise AssertionError("document reader must not run")

    monkeypatch.setattr(tools, "read_docs_folder_impl", fail_read)

    result = read_docs_folder.func(
        folder_path=str(tmp_path / "stale-upload"),
        state={
            "has_documents": False,
            "doc_folder": str(tmp_path / "stale-upload"),
            "no_web": "true",
        },
    )

    assert "No uploaded documents are available" in result
    assert "Web research is disabled" in result
    assert "research-agent" not in result


# ── read_file tests ──

def test_read_file_impl_returns_structured_preview_for_large_markdown(tmp_path: Path) -> None:
    large_doc = tmp_path / "policy.md"
    repeated_section = (
        "## Liability Coverage\n"
        "This section explains liability coverage, claim handling, deductibles, and exclusions.\n\n"
    )
    large_doc.write_text(
        "# Ontario Automobile Policy\n\n"
        + repeated_section * 600,
        encoding="utf-8",
    )

    result = tools.read_file_impl(str(large_doc), state={})

    assert "returning a structured preview" in result
    assert "Heading outline" in result
    assert "## Liability Coverage" in result
    assert len(result) < 20000
    assert "Section chunks:" in result


def test_read_file_impl_can_target_specific_markdown_section(tmp_path: Path) -> None:
    policy_doc = tmp_path / "policy.md"
    policy_doc.write_text(
        "# Ontario Automobile Policy\n\n"
        "## Section 3 - Liability Coverage\n"
        "Liability coverage protects you when you are at fault.\n\n"
        "## Section 4 - Accident Benefits Coverage\n"
        "Accident benefits may be available regardless of fault.\n",
        encoding="utf-8",
    )

    result = tools.read_file_impl(
        f"{policy_doc}#Section 4 - Accident Benefits Coverage",
        state={},
    )

    assert "Section 4 - Accident Benefits Coverage" in result
    assert "Accident benefits may be available regardless of fault." in result
    assert "Liability coverage protects you when you are at fault." not in result


def test_read_file_impl_reports_unknown_markdown_section(tmp_path: Path) -> None:
    policy_doc = tmp_path / "policy.md"
    policy_doc.write_text(
        "# Ontario Automobile Policy\n\n"
        "## Section 3 - Liability Coverage\n"
        "Liability coverage protects you when you are at fault.\n",
        encoding="utf-8",
    )

    result = tools.read_file_impl(
        f"{policy_doc}#Section 9 - Missing Section",
        state={},
    )

    assert "Section 'Section 9 - Missing Section' not found" in result
    assert "Available sections:" in result


# ── middleware tests ──

def test_research_state_middleware_seeds_research_request_file() -> None:
    middleware = ResearchStateMiddleware()

    result = middleware.before_agent(
        state={"messages": [HumanMessage(content="Generate 5 Q/A pairs from ./docs/policy/")]},
        runtime=None,
    )

    assert result is not None
    assert "/research_request.md" in result["files"]
    assert "Generate 5 Q/A pairs" in "".join(result["files"]["/research_request.md"]["content"])


def test_research_state_middleware_keeps_task_config_out_of_chat_history() -> None:
    middleware = ResearchStateMiddleware()
    initial_messages = [HumanMessage(content="Research local model compatibility")]

    updates = middleware.before_agent(
        state={"messages": initial_messages},
        runtime=None,
    )

    assert updates is not None
    added_messages = updates.get("messages", [])
    assert not any(isinstance(message, SystemMessage) for message in added_messages)

    messages = [*initial_messages, *added_messages]
    state = {**updates, "messages": messages}
    request = ModelRequest(
        model=object(),  # type: ignore[arg-type]
        messages=messages,
        state=state,
        system_message=SystemMessage(content="base instructions"),
    )

    configured = middleware.configure_request(request)

    assert configured.messages == messages
    assert configured.system_message is not None
    assert "base instructions" in str(configured.system_message.content)
    assert "Task configurations:" in str(configured.system_message.content)
    assert "<PlanDirective>" in str(configured.system_message.content)


def test_research_state_middleware_removes_legacy_late_task_config() -> None:
    middleware = ResearchStateMiddleware()
    messages = [
        HumanMessage(content="Research local model compatibility"),
        SystemMessage(content="Task configurations: \nstale configuration"),
        AIMessage(content="Starting research…"),
    ]
    request = ModelRequest(
        model=object(),  # type: ignore[arg-type]
        messages=messages,
        state={"messages": messages},
        system_message=SystemMessage(content="base instructions"),
    )

    configured = middleware.configure_request(request)

    assert not any(
        isinstance(message, SystemMessage) for message in configured.messages
    )
    assert configured.system_message is not None
    system_content = str(configured.system_message.content)
    assert "Task configurations:" in system_content
    assert "stale configuration" not in system_content


@pytest.mark.parametrize(
    "doc_folder",
    (
        pytest.param("../rejected", id="rejected"),
        pytest.param("/missing/upload-folder", id="missing"),
    ),
)
def test_configure_request_omits_unvalidated_doc_folder_prompt(
        doc_folder: str,
) -> None:
    middleware = ResearchStateMiddleware()

    configured = middleware.configure_request(
        _model_request(
            state={"doc_folder": doc_folder, "has_documents": True},
            tools=[_NamedTool("task")],
        )
    )

    assert configured.system_message is not None
    system_content = str(configured.system_message.content)
    assert "Please use the 'read_docs_folder' tool to read supported documents" not in system_content
    assert doc_folder not in system_content
    assert "Do not call `llm_wiki_query` or `read_docs_folder`" in system_content


def test_configure_request_omits_empty_doc_folder_prompt(tmp_path: Path) -> None:
    empty_folder = tmp_path / "empty-upload"
    empty_folder.mkdir()
    middleware = ResearchStateMiddleware()

    configured = middleware.configure_request(
        _model_request(
            state={"doc_folder": str(empty_folder), "has_documents": True},
            tools=[_NamedTool("task")],
        )
    )

    assert configured.system_message is not None
    system_content = str(configured.system_message.content)
    assert "Please use the 'read_docs_folder' tool to read supported documents" not in system_content
    assert str(empty_folder) not in system_content
    assert "Do not call `llm_wiki_query` or `read_docs_folder`" in system_content


def test_configure_request_keeps_block_system_content_before_user_for_ollama() -> None:
    middleware = ResearchStateMiddleware()
    request = ModelRequest(
        model=object(),  # type: ignore[arg-type]
        messages=[HumanMessage(content="Research local model compatibility")],
        state={"has_documents": False, "no_web": False},
        system_message=SystemMessage(
            content=[{"type": "text", "text": "base instructions"}]
        ),
        tools=[_NamedTool("task")],  # type: ignore[arg-type]
    )

    configured = middleware.configure_request(request)

    assert configured.system_message is not None
    ollama_payload = _ollama_message_payload(
        [configured.system_message, *configured.messages]
    )
    assert [message["role"] for message in ollama_payload] == ["system", "user"]
    assert "base instructions" in ollama_payload[0]["content"]
    assert "<Source Guidance>" in ollama_payload[0]["content"]


def test_configure_request_removes_document_tools_without_document_context() -> None:
    middleware = ResearchStateMiddleware()
    request_tools = [
        _NamedTool("llm_wiki_query"),
        _NamedTool("task"),
        _NamedTool("read_docs_folder"),
    ]

    configured = middleware.configure_request(
        _model_request(state={"has_documents": False}, tools=request_tools)
    )

    assert _tool_names(configured.tools) == ["task"]


def test_configure_request_keeps_document_tools_for_validated_upload_folder(
        tmp_path: Path,
) -> None:
    upload_folder = tmp_path / "upload"
    upload_folder.mkdir()
    (upload_folder / "source.md").write_text("source", encoding="utf-8")
    middleware = ResearchStateMiddleware()
    request_tools = [_NamedTool("llm_wiki_query"), _NamedTool("read_docs_folder")]

    configured = middleware.configure_request(
        _model_request(
            state={"doc_folder": str(upload_folder), "has_documents": True},
            tools=request_tools,
        )
    )

    assert configured.tools == request_tools
    assert configured.system_message is not None
    assert (
        f"from this folder first: '{upload_folder}'"
        in str(configured.system_message.content)
    )


def test_configure_request_keeps_document_tools_for_physical_wiki_raw_source(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_folder = tmp_path / "thread-1"
    upload_folder.mkdir()
    raw_folder = tmp_path / "docs" / "threads-wiki" / "thread-1" / "raw"
    raw_folder.mkdir(parents=True)
    (raw_folder / "source.txt").write_text("source", encoding="utf-8")
    monkeypatch.setenv("WIKI_BASE_DIR", str(tmp_path))
    middleware = ResearchStateMiddleware()
    request_tools = [_NamedTool("llm_wiki_query"), _NamedTool("read_docs_folder")]

    configured = middleware.configure_request(
        _model_request(
            state={"doc_folder": str(upload_folder), "has_documents": True},
            tools=request_tools,
        )
    )

    assert configured.tools == request_tools


def test_llm_wiki_query_fails_closed_before_resolution_without_documents(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale_path = str(tmp_path / "stale-upload")

    def fail_resolution(*_args, **_kwargs):
        raise AssertionError("wiki path resolution must not run")

    def fail_query(*_args, **_kwargs):
        raise AssertionError("wiki query must not run")

    monkeypatch.setattr(tools.ThreadWikiPaths, "resolve", fail_resolution)
    monkeypatch.setattr(tools, "run_query", fail_query)

    result = tools.llm_wiki_query.func(
        question="What is in the upload?",
        state={"has_documents": False, "doc_folder": stale_path},
    )

    assert "No uploaded documents are available" in result
    assert "If web access is enabled" in result
    assert 'subagent_type="research-agent"' in result


def test_llm_wiki_query_fails_closed_before_resolution_for_missing_source(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_path = str(tmp_path / "missing-upload")

    def fail_resolution(*_args, **_kwargs):
        raise AssertionError("wiki path resolution must not run")

    def fail_query(*_args, **_kwargs):
        raise AssertionError("wiki query must not run")

    monkeypatch.setattr(tools.ThreadWikiPaths, "resolve", fail_resolution)
    monkeypatch.setattr(tools, "run_query", fail_query)

    result = tools.llm_wiki_query.func(
        question="What is in the upload?",
        state={"has_documents": True, "doc_folder": missing_path},
    )

    assert "No uploaded documents are available" in result
    assert "research-agent" in result


def test_llm_wiki_query_does_not_suggest_web_when_disabled(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_resolution(*_args, **_kwargs):
        raise AssertionError("wiki path resolution must not run")

    def fail_query(*_args, **_kwargs):
        raise AssertionError("wiki query must not run")

    monkeypatch.setattr(tools.ThreadWikiPaths, "resolve", fail_resolution)
    monkeypatch.setattr(tools, "run_query", fail_query)

    result = tools.llm_wiki_query.func(
        question="What is in the upload?",
        state={
            "has_documents": False,
            "doc_folder": str(tmp_path / "stale-upload"),
            "no_web": True,
        },
    )

    assert "No uploaded documents are available" in result
    assert "research-agent" not in result


def test_llm_wiki_query_valid_upload_reaches_wiki_resolution(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_folder = tmp_path / "upload"
    upload_folder.mkdir()
    (upload_folder / "source.md").write_text("source", encoding="utf-8")
    resolved = {"called": False}

    def record_resolution(*_args, **_kwargs):
        resolved["called"] = True
        return SimpleNamespace(wiki_content=tmp_path / "not-built.md")

    monkeypatch.setattr(tools.ThreadWikiPaths, "resolve", record_resolution)

    result = tools.llm_wiki_query.func(
        question="What is in the upload?",
        state={"has_documents": True, "doc_folder": str(upload_folder)},
    )

    assert resolved["called"] is True
    assert "wiki has not been built yet" in result


def test_configure_request_hides_document_tools_for_virtual_files_only(
        tmp_path: Path,
) -> None:
    upload_folder = tmp_path / "empty-upload"
    upload_folder.mkdir()
    middleware = ResearchStateMiddleware()
    request_tools = [_NamedTool("llm_wiki_query"), _NamedTool("read_docs_folder")]

    configured = middleware.configure_request(
        _model_request(
            state={
                "doc_folder": str(upload_folder),
                "has_documents": True,
                "files": {"/raw/source.md": create_file_data("virtual")},
            },
            tools=request_tools,
        )
    )

    assert configured.tools == []


def test_configure_request_preserves_unknown_tool_order_tool_choice_and_input_tools() -> None:
    middleware = ResearchStateMiddleware()
    unknown_tool = object()
    request_tools = [
        {"name": "llm_wiki_query"},
        _NamedTool("task"),
        unknown_tool,
        _NamedTool("other"),
        {"name": "read_docs_folder"},
    ]
    original_tools = list(request_tools)
    tool_choice = {"type": "function", "function": {"name": "task"}}
    request = _model_request(
        state={"has_documents": False},
        tools=request_tools,
        tool_choice=tool_choice,
    )

    configured = middleware.configure_request(request)

    assert configured.tools == [request_tools[1], unknown_tool, request_tools[3]]
    assert configured.tool_choice == tool_choice
    assert request.tools == original_tools


@pytest.mark.parametrize(
    ("no_web", "expected_guidance"),
    [
        pytest.param(
            False,
            (
                "No uploaded document sources are available. Do not call "
                "`llm_wiki_query` or `read_docs_folder`. Use `task` with "
                '`subagent_type="research-agent"` for web research.'
            ),
            id="web-only-bool",
        ),
        pytest.param(
            "false",
            (
                "No uploaded document sources are available. Do not call "
                "`llm_wiki_query` or `read_docs_folder`. Use `task` with "
                '`subagent_type="research-agent"` for web research.'
            ),
            id="web-only-string",
        ),
        pytest.param(
            True,
            (
                "Neither uploaded documents nor web research is available. Do not "
                "invent facts or use `llm_wiki_query`, `read_docs_folder`, "
                "`tavily_search`, or `fetch_webpage_content`. You may still use "
                "workflow and output tools such as `write_todos`, `write_file`, "
                "and applicable `read_file`. Clearly report this source constraint "
                "to the user."
            ),
            id="no-sources-bool",
        ),
        pytest.param(
            "true",
            (
                "Neither uploaded documents nor web research is available. Do not "
                "invent facts or use `llm_wiki_query`, `read_docs_folder`, "
                "`tavily_search`, or `fetch_webpage_content`. You may still use "
                "workflow and output tools such as `write_todos`, `write_file`, "
                "and applicable `read_file`. Clearly report this source constraint "
                "to the user."
            ),
            id="no-sources-string",
        ),
    ],
)
def test_configure_request_adds_no_document_source_guidance(
        no_web: bool | str, expected_guidance: str
) -> None:
    middleware = ResearchStateMiddleware()

    configured = middleware.configure_request(
        _model_request(
            state={"has_documents": False, "no_web": no_web},
            tools=[_NamedTool("task")],
        )
    )

    assert configured.system_message is not None
    system_content = str(configured.system_message.content)
    assert expected_guidance in system_content
    assert "Do not call tools" not in system_content
    assert "Please use the 'read_docs_folder' tool to read supported documents" not in system_content


def test_configure_request_adds_document_source_guidance(tmp_path: Path) -> None:
    upload_folder = tmp_path / "upload"
    upload_folder.mkdir()
    (upload_folder / "source.md").write_text("source", encoding="utf-8")
    middleware = ResearchStateMiddleware()

    configured = middleware.configure_request(
        _model_request(
            state={"doc_folder": str(upload_folder), "has_documents": True},
            tools=[_NamedTool("task"), _NamedTool("llm_wiki_query")],
        )
    )

    assert configured.system_message is not None
    assert (
        "Uploaded sources are available. Use `llm_wiki_query` or "
        "`read_docs_folder` to ground relevant claims in those sources."
        in str(configured.system_message.content)
    )


def test_before_agent_uses_same_turn_doc_folder_for_progress(tmp_path: Path) -> None:
    upload_folder = tmp_path / "upload"
    upload_folder.mkdir()
    (upload_folder / "source.md").write_text("source", encoding="utf-8")
    middleware = ResearchStateMiddleware()

    updates = middleware.before_agent(
        state={
            "messages": [
                HumanMessage(content=f"Research --doc-folder {upload_folder}")
            ]
        },
        runtime=None,
    )

    assert updates is not None
    assert updates["doc_folder"] == str(upload_folder)
    assert updates["messages"][0].content == (
        "Searching your uploaded documents for relevant information…"
    )


def test_research_state_middleware_preserves_state_during_resume_round(
        monkeypatch,
) -> None:
    middleware = ResearchStateMiddleware(
        config_getter=lambda: {
            "configurable": {
                "resume_incomplete_todos": True,
                "resume_round": 2,
                "resume_max_rounds": 3,
            }
        }
    )
    monkeypatch.setattr(
        middleware,
        "_extract_parameters_from_user_input",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("resume phrase must not be parsed as parameters")
        ),
    )
    original_request = create_file_data("Original research goal")
    state = {
        "messages": [
            HumanMessage(content="Original research goal"),
            HumanMessage(content="Please continue!"),
        ],
        "files": {"/research_request.md": original_request},
        "todos": [{"content": "Finish report", "status": "pending"}],
        "doc_folder": "./docs/original",
        "skill": "golden-dataset",
        "no_web": True,
        "_last_user_msg_hash": "original-message-hash",
        "verification_round": 2,
        "verification_feedback": "Keep prior verification feedback.",
        "research_pass": 4,
    }

    result = middleware.before_agent(state=state, runtime=None)

    assert not result
    assert state["files"]["/research_request.md"] == original_request
    assert all(
        not (
                isinstance(message, AIMessage)
                and message.content
                in {
                    "Starting research…",
                    "Searching your uploaded documents for relevant information…",
                }
        )
        for message in (result or {}).get("messages", [])
    )
    assert not {
        "verification_round",
        "verification_feedback",
        "research_pass",
        "_last_user_msg_hash",
        "doc_folder",
        "skill",
        "no_web",
    }.intersection(result or {})

    request = ModelRequest(
        model=object(),  # type: ignore[arg-type]
        messages=state["messages"],
        state=state,
        system_message=SystemMessage(content="base instructions"),
    )
    configured = middleware.configure_request(request)

    assert configured.system_message is not None
    assert "Keep prior verification feedback." in str(
        configured.system_message.content
    )


_MISSING_TODOS = object()


@pytest.mark.parametrize(
    "todos",
    (
            pytest.param(
                [{"content": "Finished", "status": "completed"}],
                id="completed",
            ),
            pytest.param(_MISSING_TODOS, id="missing"),
            pytest.param(
                ["not a todo", {"content": "Unknown", "status": "blocked"}],
                id="malformed",
            ),
    ),
)
def test_research_state_middleware_uses_normal_path_without_incomplete_todos(
        todos,
        monkeypatch,
) -> None:
    middleware = ResearchStateMiddleware(
        config_getter=lambda: {
            "configurable": {"resume_incomplete_todos": True}
        }
    )
    monkeypatch.setattr(
        middleware,
        "_extract_parameters_from_user_input",
        lambda *_args: {"skill": "interview-prep", "no_web": False},
    )
    original_request = create_file_data("Original research goal")
    state = {
        "messages": [HumanMessage(content="Please continue!")],
        "files": {"/research_request.md": original_request},
        "skill": "golden-dataset",
        "no_web": True,
        "_last_user_msg_hash": "original-message-hash",
        "verification_round": 2,
        "verification_feedback": "Old feedback.",
        "research_pass": 4,
    }
    if todos is not _MISSING_TODOS:
        state["todos"] = todos

    result = middleware.before_agent(state=state, runtime=None)

    assert result is not None
    assert "".join(result["files"]["/research_request.md"]["content"]) == (
        "Please continue!"
    )
    assert any(
        isinstance(message, AIMessage)
        and message.content == "Starting research…"
        for message in result["messages"]
    )
    assert result["verification_round"] == 0
    assert result["verification_feedback"] is None
    assert result["research_pass"] == 0
    assert result["_last_user_msg_hash"] != "original-message-hash"
    assert result["skill"] == "interview-prep"
    assert result["no_web"] is False


# ── ls / glob tests ──

def test_ls_lists_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "file1.txt").touch()
    (tmp_path / "dir1").mkdir()
    (tmp_path / "dir1" / "file2.txt").touch()

    result = tools.ls.invoke({"path": str(tmp_path), "state": {}})

    assert "file1.txt" in result
    assert "dir1/" in result
    assert "file2.txt" not in result


def test_ls_handles_nonexistent_path(tmp_path: Path) -> None:
    result = tools.ls.invoke({"path": str(tmp_path / "nonexistent"), "state": {}})
    assert "Error: Path" in result
    assert "not found" in result


def test_glob_finds_files_matching_pattern(tmp_path: Path) -> None:
    (tmp_path / "test1.md").touch()
    (tmp_path / "test2.md").touch()
    (tmp_path / "other.txt").touch()
    subdir = tmp_path / "sub"
    subdir.mkdir()
    (subdir / "test3.md").touch()

    result = tools.glob.invoke({"pattern": f"{tmp_path}/*.md", "state": {}})
    assert "test1.md" in result
    assert "test2.md" in result
    assert "other.txt" not in result
    assert "test3.md" not in result

    result = tools.glob.invoke({"pattern": f"{tmp_path}/**/*.md", "state": {}})
    assert "test1.md" in result
    assert "test2.md" in result
    assert "test3.md" in result


def test_glob_handles_nonexistent_base_path() -> None:
    result = tools.glob.invoke({"pattern": "/nonexistent/path/*.md", "state": {}})
    assert "Error: Base path" in result


# ── fetch_webpage_content tests ──

def test_fetch_webpage_content_returns_markdown_for_valid_url() -> None:
    result = fetch_webpage_content.invoke({"url": "https://example.com", "timeout": 5.0, "state": {}})

    assert not result.startswith("Error fetching content")
    assert len(result) > 0


def test_fetch_webpage_content_handles_invalid_url() -> None:
    result = fetch_webpage_content.invoke(
        {"url": "https://this-domain-does-not-exist-12345.com", "timeout": 2.0, "state": {}})

    assert result.startswith("Error fetching content")


def test_fetch_webpage_content_has_proper_tool_metadata() -> None:
    assert hasattr(fetch_webpage_content, "name")
    assert fetch_webpage_content.name == "fetch_webpage_content"
    assert hasattr(fetch_webpage_content, "description")
    assert "markdown" in fetch_webpage_content.description.lower()
