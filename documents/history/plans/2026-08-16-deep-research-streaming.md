# Deep Research Tool Gating and Live Subagent Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent document-only research tools from being selected without uploaded sources, show one initial research status, and render live nested research-agent tool calls.

**Architecture:** Backend owns a shared fail-closed document-context predicate used by middleware and the wiki tool. Frontend owns tri-state document availability derived only from confirmed API outcomes, raw-order presentation dedupe, and reactive reads from LangGraph SDK subagent state. Existing LangGraph subgraph transport and wiki behavior remain unchanged.

**Tech Stack:** Python 3.13, LangChain/LangGraph/deepagents, pytest, React 19, TypeScript, Next.js 16, LangGraph SDK `useStream`, Node test runner, Testing Library.

---

## Repository and worktree safety

- Backend repository: `/Users/jerryshao/Documents/projects/IBM/ai/deep-research`
- Frontend repository: `/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui`
- Approved specification: `documents/history/specs/2026-08-16-deep-research-streaming-design.md`
- Backend already contains user-owned modifications in `research_agent/agent.py` and `tests/test_tools.py`; preserve and extend them.
- Frontend already contains a user-owned `next-env.d.ts` modification; never stage or overwrite it.
- Before edits, capture `git diff --binary` for the three dirty files under `/tmp` and record their SHA-256 hashes. Re-run both comparisons after implementation and after `yarn build`.
- Do not commit any implementation change from this already-dirty workspace. Whole-file staging would absorb pre-existing hunks, and overlapping hunks make partial staging unsafe. Hand off verified working-tree diffs for user review. The already-isolated specification/plan documentation commits are the only commits created in this session.

## File structure

### Backend

- Create `thread_wiki/source_types.py`: public shared Thread Wiki source-type policy for text, structured, office, and code formats.
- Modify `thread_wiki/service.py`: consume the shared source policy instead of private duplicated suffix sets.
- Create `research_agent/document_context.py`: bounded canonical validation, physical Thread Wiki raw resolution, and model-visible tool filtering helpers.
- Modify `research_agent/agent.py`: state field, merged-state progress decision, request-scoped tool gating, and explicit source guidance.
- Modify `research_agent/research_subagent/tools.py`: execution-time guard before wiki path resolution and `run_query`.
- Create `tests/test_document_context.py`: focused predicate and tool-list policy tests.
- Modify `tests/test_tools.py`: middleware request, progress, instruction, and wiki guard regressions.

### Frontend

- Create `src/app/hooks/useThreadDocumentAvailability.ts`: tri-state transitions, thread-state synchronization, and run-update serialization behind an injected client boundary.
- Create `tests/thread-document-availability.test.tsx`: hook-level list/upload/delete and submission integration tests.
- Create `src/app/utils/submit-research-message.ts`: extracted actual submission boundary.
- Create `tests/submit-research-message.test.ts`: send payload regressions.
- Modify `src/app/hooks/useChat.ts`: add `has_documents` to graph state typing.
- Modify `src/app/components/ChatInterface.tsx`: synchronize confirmed document state, clear stale folder state, and include confirmed availability in runs.
- Modify `src/app/utils/processMessages.ts`: raw-order initial-status dedupe.
- Create `tests/process-messages.test.ts`: punctuation and barrier regressions.
- Modify `src/app/components/ChatMessage.tsx`: remove stale memoization and reread SDK subagent state on every parent render.
- Modify `tests/subagent-chat-message.test.tsx`: live mutable-snapshot rerender regression.

## Task 1: Backend document-context policy

**Files:**

- Create: `research_agent/document_context.py`
- Create: `thread_wiki/source_types.py`
- Modify: `thread_wiki/service.py`
- Create: `tests/test_document_context.py`

- [ ] **Step 1: Snapshot dirty-file baselines**

Run before any implementation edit:

```bash
git diff --binary -- research_agent/agent.py tests/test_tools.py > /tmp/deep-research-preexisting.patch
shasum -a 256 research_agent/agent.py tests/test_tools.py
```

From the frontend repository:

```bash
git diff --binary -- next-env.d.ts > /tmp/bmo-ui-preexisting.patch
shasum -a 256 next-env.d.ts
```

Keep these artifacts outside both repositories. They are diagnostics, not source edits.

- [ ] **Step 2: Write failing predicate tests**

Cover explicit false overriding stale upload/raw sources, explicit true requiring an actual supported source, and CLI fallback with no flag. Resolve raw evidence only through `ThreadWikiPaths` at `<wiki-base>/docs/threads-wiki/<thread-id>/raw`, where `<thread-id>` is derived from the existing LangGraph upload `doc_folder`. Reject whitespace, malformed/traversing paths, missing folders, empty folders, unreadable files, filesystem roots, files beyond the configured depth, generated files, virtual agent-state `/raw/` or `/docs/` keys, and generated `/wiki/` files. Add more than 20 unrelated files before a valid source and prove existence detection still finds the source. Parameterize wiki-only valid formats: `.json`, `.csv`, `.yaml`, `.yml`, and representative supported code suffixes such as `.py` and `.ts`.

```python
from deepagents.backends.utils import create_file_data

from research_agent.document_context import has_document_context


def test_explicit_false_overrides_stale_folder(tmp_path):
    (tmp_path / "source.pdf").write_bytes(b"pdf")
    assert not has_document_context(
        {"has_documents": False, "doc_folder": str(tmp_path)}
    )


def test_explicit_true_requires_supported_source(tmp_path):
    assert not has_document_context(
        {"has_documents": True, "doc_folder": str(tmp_path)}
    )
    (tmp_path / "source.pdf").write_bytes(b"pdf")
    assert has_document_context(
        {"has_documents": True, "doc_folder": str(tmp_path)}
    )


def test_physical_thread_wiki_raw_source_supports_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_BASE_DIR", str(tmp_path))
    raw_dir = tmp_path / "docs" / "threads-wiki" / "thread-1" / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "source.json").write_text('{"source": true}')
    assert has_document_context(
        {"doc_folder": "docs/threads/thread-1"}
    )


def test_virtual_and_generated_files_are_not_upload_evidence():
    assert not has_document_context(
        {
            "files": {
                "/raw/source.md": create_file_data("virtual source"),
                "/docs/source.md": create_file_data("virtual source"),
                "/research_request.md": create_file_data("question"),
                "/wiki/topic.md": create_file_data("agent output"),
            }
        }
    )
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_document_context.py -q
```

Expected: collection failure because `research_agent.document_context` does not exist.

- [ ] **Step 4: Implement minimal shared predicate**

Create a dependency-light module. Do not import `agent.py` or `tools.py`.

```python
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

from research_agent.research_subagent.utils.knowledge_filesystem import MAX_GLOB_DEPTH
from thread_wiki.models import ThreadWikiPaths, _resolve_wiki_base_dir
from thread_wiki.source_types import SUPPORTED_WIKI_SOURCE_SUFFIXES

DOCUMENT_TOOL_NAMES = {"llm_wiki_query", "read_docs_folder"}


def _has_supported_source(folder: Path) -> bool:
    if folder == Path(folder.anchor) or not folder.is_dir():
        return False
    try:
        for path in folder.rglob("*"):
            try:
                relative = path.relative_to(folder)
            except ValueError:
                return False
            if len(relative.parts) > MAX_GLOB_DEPTH or not path.is_file():
                continue
            if (
                path.suffix.lower() in SUPPORTED_WIKI_SOURCE_SUFFIXES
                and path.stat().st_mode & 0o444
            ):
                return True
        return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _resolve_upload_and_raw(value: object) -> tuple[Path, Path] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if ".." in PurePath(raw).parts:
        return None
    upload = Path(raw).expanduser().resolve(strict=False)
    thread_id = upload.name
    if not thread_id or thread_id in {".", ".."}:
        return None
    wiki_base = _resolve_wiki_base_dir(Path(__file__).resolve().parent.parent)
    raw_dir = ThreadWikiPaths.resolve(thread_id, wiki_base).raw_dir
    return upload, raw_dir


def has_document_context(state: Mapping[str, Any] | None) -> bool:
    state = state or {}
    explicit = state.get("has_documents")
    if explicit is False:
        return False
    resolved = _resolve_upload_and_raw(state.get("doc_folder"))
    folder_has_source = bool(
        resolved
        and (_has_supported_source(resolved[0]) or _has_supported_source(resolved[1]))
    )
    if explicit is True:
        return folder_has_source
    return folder_has_source


def tool_name(tool: Any) -> str | None:
    return tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)


def configure_document_tools(
    tools: Sequence[Any], *, documents_available: bool
) -> list[Any]:
    if documents_available:
        return list(tools)
    return [tool for tool in tools if tool_name(tool) not in DOCUMENT_TOOL_NAMES]
```

In `thread_wiki/source_types.py`, move the service's existing text/structured and
binary suffix sets into public frozen sets and union them with
`SUPPORTED_CODE_SUFFIXES`. Export `SUPPORTED_WIKI_SOURCE_SUFFIXES`. Update
`thread_wiki/service.py` to consume that same constant, so tool eligibility and
wiki ingestion cannot drift. Do not copy the extension list into
`document_context.py`.

- [ ] **Step 5: Run tests and verify GREEN**

```bash
uv run pytest tests/test_document_context.py -q
```

Expected: all predicate tests pass.

- [ ] **Step 6: Leave policy changes unstaged and inspect diff**

```bash
git diff -- research_agent/document_context.py thread_wiki/source_types.py thread_wiki/service.py tests/test_document_context.py
git diff --check
```

## Task 2: Backend request gating and source guidance

**Files:**

- Modify: `research_agent/agent.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Write failing model-request policy tests**

Extend `tests/test_tools.py` with helpers that create named fake tools and a `ModelRequest`. Assert no-document state removes `llm_wiki_query` and `read_docs_folder`; `/raw/`, `/docs/`, and validated folder state preserve them; generated files alone do not expose them; unknown tool objects remain visible; and the original `tool_choice` is preserved. Test all three instruction branches: documents, web-only `research-agent`, and neither source.

```python
class _NamedTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _request_for(state, tools):
    messages = state.get("messages", [HumanMessage(content="Research graph engineering")])
    return ModelRequest(
        model=object(),
        messages=messages,
        state={**state, "messages": messages},
        tools=tools,
        system_message=SystemMessage(content="base instructions"),
    )


def test_configure_request_hides_document_tools_without_documents():
    middleware = ResearchStateMiddleware()
    configured = middleware.configure_request(
        _request_for(
            {"has_documents": False, "no_web": False},
            [_NamedTool("llm_wiki_query"), _NamedTool("read_docs_folder"), _NamedTool("task")],
        )
    )
    assert [tool.name for tool in configured.tools] == ["task"]
    assert "No uploaded document context is available" in str(
        configured.system_message.content
    )
    assert "research-agent" in str(configured.system_message.content)


def test_configure_request_preserves_tool_choice_and_unknown_tools():
    unknown = object()
    request = _request_for({"has_documents": False}, [unknown, _NamedTool("task")])
    request = request.override(tool_choice="required")
    configured = ResearchStateMiddleware().configure_request(request)
    assert configured.tool_choice == "required"
    assert configured.tools == [unknown, request.tools[1]]
```

- [ ] **Step 2: Write failing merged-state progress test**

Create a real temporary folder containing `source.md`, pass a user message with `--doc-folder`, and assert `before_agent` emits the retained exact message `Searching your uploaded documents for relevant information…` on that same turn.

- [ ] **Step 3: Run focused tests and verify RED**

```bash
uv run pytest tests/test_tools.py -q -k "document_tools or document_context or progress"
```

Expected: document tools remain visible, source guidance is absent, or progress uses pre-extraction state.

- [ ] **Step 4: Implement middleware state and request policy**

In `ResearchState`, add:

```python
has_documents: bool | None
```

Import `configure_document_tools` and `has_document_context`. In `before_agent`, extract parameters before progress selection, construct `effective_state = {**state, **extracted_updates}`, then call `has_document_context(effective_state)`. Preserve resume behavior and existing strict-Ollama task-configuration injection.

In `configure_request`:

```python
state = request.state or {}
documents_available = has_document_context(state)
tools = configure_document_tools(
    request.tools,
    documents_available=documents_available,
)
instruction = self._build_system_instruction(state)
```

Pass `tools=tools` in the existing `request.override(...)`.

Append one of these request-scoped blocks in `_build_system_instruction`:

```python
if has_document_context(state):
    instruction += (
        "\n\n<DocumentContext>\nUploaded source documents are available. "
        "Use llm_wiki_query or read_docs_folder to ground relevant claims in "
        "those sources.\n</DocumentContext>"
    )
elif str2bool(state.get("no_web"), False):
    instruction += (
        "\n\n<DocumentContext>\nNo uploaded document context is available, "
        "and web research is disabled. Do not call document research tools or "
        "invent sources. Report this source constraint clearly to the user."
        "\n</DocumentContext>"
    )
else:
    instruction += (
        "\n\n<DocumentContext>\nNo uploaded document context is available. "
        "Do not call llm_wiki_query or read_docs_folder. Delegate web research "
        "with task using subagent_type=\"research-agent\".\n</DocumentContext>"
    )
```

- [ ] **Step 5: Run focused and compatibility tests**

```bash
uv run pytest tests/test_document_context.py tests/test_tools.py tests/test_prompts_validation.py -q
```

Expected: all pass, including existing Ollama system-message ordering tests.

- [ ] **Step 6: Inspect backend middleware diff without staging**

```bash
git diff -- research_agent/agent.py tests/test_tools.py
git diff --check
```

## Task 3: Wiki execution-time defense

**Files:**

- Modify: `research_agent/research_subagent/tools.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Write a failing wiki guard test**

Patch both `tools.ThreadWikiPaths.resolve` and `tools.run_query` with functions that raise assertions, then call the underlying tool function with explicit no-document state and a stale folder. Assert the return explains unavailable documents. This proves the guard executes before thread/wiki resolution, not merely before `run_query`.

```python
def test_llm_wiki_query_does_not_reach_service_without_documents(monkeypatch, tmp_path):
    def unexpected_resolve(*_args, **_kwargs):
        raise AssertionError("wiki paths must not be resolved")

    async def unexpected_query(*_args, **_kwargs):
        raise AssertionError("run_query must not be called")

    monkeypatch.setattr(tools.ThreadWikiPaths, "resolve", unexpected_resolve)
    monkeypatch.setattr(tools, "run_query", unexpected_query)
    result = tools.llm_wiki_query.func(
        question="What is graph engineering?",
        state={"has_documents": False, "doc_folder": str(tmp_path)},
    )
    assert "No uploaded documents" in result
```

- [ ] **Step 2: Run test and verify RED**

```bash
uv run pytest tests/test_tools.py::test_llm_wiki_query_does_not_reach_service_without_documents -q
```

Expected: current stale `doc_folder` path proceeds beyond the desired shared guard or returns different behavior.

- [ ] **Step 3: Add guard before thread/wiki resolution**

```python
from research_agent.document_context import has_document_context


if not has_document_context(state):
    return (
        "Error: No uploaded documents are available for this session. "
        "Use the research-agent for web research when web access is enabled."
    )
```

Place this at the beginning of `llm_wiki_query`, before deriving `thread_id`, resolving `ThreadWikiPaths`, or calling `run_query`.

- [ ] **Step 4: Run backend focused suite**

```bash
uv run pytest tests/test_document_context.py tests/test_tools.py tests/test_citations.py -q
```

Expected: all pass.

- [ ] **Step 5: Inspect wiki defense diff without staging**

```bash
git diff -- research_agent/research_subagent/tools.py tests/test_tools.py
git diff --check
```

## Task 4: Frontend tri-state document availability

**Files:**

- Create: `src/app/hooks/useThreadDocumentAvailability.ts`
- Create: `tests/thread-document-availability.test.tsx`
- Create: `src/app/utils/submit-research-message.ts`
- Create: `tests/submit-research-message.test.ts`
- Modify: `src/app/hooks/useChat.ts`
- Modify: `src/app/components/ChatInterface.tsx`

- [ ] **Step 1: Write failing hook and submit-boundary tests**

Use Testing Library `renderHook`, injected `listDocuments` and `updateThreadState` functions, and real `Response` objects. Cover confirmed `404`, empty/non-empty 200 responses, network rejection, 500, upload success, and deletion of the last document. Preserve the existing LangGraph deep-agent thread-ID creation/selection flow; do not introduce a second thread ID or a separate upload identity.

```typescript
import "./setup-dom";
import assert from "node:assert/strict";
import { act, renderHook, waitFor } from "@testing-library/react";
import test from "node:test";
import { useThreadDocumentAvailability } from "../src/app/hooks/useThreadDocumentAvailability";

test("confirmed 404 clears stale document state", async () => {
  const updates: unknown[] = [];
  const { result } = renderHook(() =>
    useThreadDocumentAvailability({
      threadId: "thread-1",
      listDocuments: async () => new Response(null, { status: 404 }),
      updateThreadState: async (_threadId, values) => updates.push(values),
    })
  );
  await act(() => result.current.refresh());
  assert.equal(result.current.availability, false);
  assert.deepEqual(updates, [{ has_documents: false, doc_folder: null }]);
});

test("network and 5xx failures stay unknown and do not clear state", async () => {
  // Run once with a rejected list call and once with Response status 500.
  // Assert availability === null and updateThreadState was not called.
});

test("upload and deletion transitions stay consistent", async () => {
  // markUploadSuccess("docs/threads/thread-1") => true + persisted folder.
  // Deleting one of multiple documents keeps availability true.
  // markDocumentDeleted(lastName) => false + cleared folder.
});

test("late response from a previous thread cannot replace current state", async () => {
  // Start thread A refresh with a deferred Response.
  // Rerender for thread B; assert immediate reset to null and [].
  // Resolve B, then resolve A. Final documents and availability must remain B's.
});
```

Assert initial `availability === null` before any response. Use deferred promises
for the cross-thread case; do not rely on timers.

In `tests/submit-research-message.test.ts`, capture the call at the actual injected
send boundary:

```typescript
test("pending folder forces confirmed document context in sent state", () => {
  const calls: unknown[] = [];
  submitResearchMessage({
    messageText: "Research graph engineering",
    noWeb: false,
    availability: null,
    pendingDocFolder: "docs/threads/thread-1",
    sendMessage: (content, state) => calls.push({ content, state }),
  });
  assert.deepEqual(calls, [
    {
      content: "Research graph engineering",
      state: {
        no_web: false,
        doc_folder: "docs/threads/thread-1",
        has_documents: true,
      },
    },
  ]);
});
```

Add separate unknown, false, and true cases. Assert one send call per submission.

- [ ] **Step 2: Run tests and verify RED**

```bash
node --import tsx --test --test-isolation=none tests/thread-document-availability.test.tsx tests/submit-research-message.test.ts
```

Expected: module-not-found failures for the new hook and submit helper.

- [ ] **Step 3: Implement the injected availability hook**

The hook owns the mapping from actual HTTP outcomes to graph state. It accepts dependencies rather than importing the LangGraph client, which keeps tests real at the boundary without mocking SDK internals.

```typescript
type DocumentItem = { name: string; size: number; type?: string };
type Options = {
  threadId: string | null;
  listDocuments(threadId: string): Promise<Response>;
  updateThreadState(
    threadId: string,
    values: { has_documents: boolean; doc_folder: string | null }
  ): Promise<void>;
};

export function useThreadDocumentAvailability(options: Options) {
  // documents: DocumentItem[]
  // availability: boolean | null; null means loading/unknown/error.
  // Reset documents and availability when threadId changes.
  // Guard each refresh with a generation ref or AbortController so a late
  // response from the previous thread cannot mutate current thread state.
  // refresh(): maps Response 404/200/5xx and parse/network errors.
  // markUploadSuccess(docFolder): persists true and folder.
  // markDocumentDeleted(filename): removes local item and clears state if last.
}
```

If upload completion occurs before React has rerendered with the newly selected
thread, pass the already-created LangGraph `activeThreadId` into the persistence
callback explicitly. This is only timing-safe plumbing for the existing thread-ID
feature; it must not create or derive another identifier. Add a regression test
showing upload, document state update, and subsequent run all use the same ID.

- [ ] **Step 4: Run hook tests and verify GREEN**

```bash
node --import tsx --test --test-isolation=none tests/thread-document-availability.test.tsx
```

Expected: every HTTP, transition, persistence, and thread-race behavior passes.

- [ ] **Step 5: Wire tri-state through chat state**

Add `has_documents?: boolean | null` to `StateType`. In `ChatInterface`, replace local `documents` plus `fetchDocuments` transition logic with the hook. Pass an authenticated `listDocuments` callback and a client-backed `updateThreadState` callback. Call:

- `refresh()` on thread changes and after upload/delete;
- `markUploadSuccess(docFolder)` only after a successful upload response;
- `markDocumentDeleted(filename)` only after a successful delete response;
- an extracted `submitResearchMessage(...)` from the actual `handleSubmit` path.

Implement `submitResearchMessage` as the actual send boundary, not a passive state
builder. It accepts the current message, web flag, tri-state availability, optional
pending folder, and injected `sendMessage`; it calls `sendMessage` exactly once.
Unknown availability omits `has_documents`; confirmed false/true serializes the
boolean; a pending folder serializes `doc_folder` and forces `has_documents: true`.
`ChatInterface.handleSubmit` must call this helper.

```typescript
type SubmitOptions = {
  messageText: string;
  noWeb: boolean;
  availability: boolean | null;
  pendingDocFolder?: string;
  sendMessage(content: string, state: Record<string, unknown>): void;
};

export function submitResearchMessage(options: SubmitOptions): void {
  const state: Record<string, unknown> = { no_web: options.noWeb };
  if (options.availability !== null) {
    state.has_documents = options.availability;
  }
  if (options.pendingDocFolder) {
    state.doc_folder = options.pendingDocFolder;
    state.has_documents = true;
  }
  options.sendMessage(options.messageText, state);
}
```

Add boundary tests that capture injected `sendMessage` calls and prove unknown,
false, true, and pending-folder payloads. Hook tests cover HTTP transitions and
cross-thread races; submit tests prove the values actually passed to the existing
run sender. No source-text assertions are permitted.

- [ ] **Step 6: Run focused tests and type/lint checks**

```bash
node --import tsx --test --test-isolation=none tests/thread-document-availability.test.tsx tests/submit-research-message.test.ts
yarn lint
```

Expected: tests pass and lint exits 0.

- [ ] **Step 7: Inspect frontend availability diff without staging**

```bash
git diff -- src/app/hooks/useThreadDocumentAvailability.ts tests/thread-document-availability.test.tsx src/app/utils/submit-research-message.ts tests/submit-research-message.test.ts src/app/hooks/useChat.ts src/app/components/ChatInterface.tsx
git diff --check
```

## Task 5: Frontend raw-order start-status dedupe

**Files:**

- Modify: `src/app/utils/processMessages.ts`
- Create: `tests/process-messages.test.ts`

- [ ] **Step 1: Write failing dedupe tests**

Test mixed punctuation, keeping the first status, human barriers, tool barriers, non-status AI barriers, and unrelated repeated prose.

```typescript
test("collapses consecutive initial research statuses", () => {
  const result = processMessages(
    [
      { id: "start-1", type: "ai", content: "Starting research…" },
      { id: "start-2", type: "ai", content: "Starting research..." },
    ],
    false
  );
  assert.deepEqual(result.map((item) => item.message.id), ["start-1"]);
});

test("tool messages form a raw-order barrier", () => {
  const result = processMessages(
    [
      { id: "start-1", type: "ai", content: "Starting research…" },
      { id: "tool-1", type: "tool", content: "done", tool_call_id: "call-1" },
      { id: "start-2", type: "ai", content: "Starting research..." },
    ],
    false
  );
  assert.deepEqual(result.map((item) => item.message.id), ["start-1", "start-2"]);
});
```

- [ ] **Step 2: Run tests and verify RED**

```bash
node --import tsx --test tests/process-messages.test.ts
```

Expected: both adjacent status messages remain.

- [ ] **Step 3: Implement raw-order canonicalization**

Add a private helper:

```typescript
function isInitialResearchStatus(message: Message, toolCalls: ToolCall[]): boolean {
  if (toolCalls.length > 0) return false;
  const text = extractMessageText(message).trim().replace(/\.\.\.$/, "…");
  return text === "Starting research…";
}
```

Track `previousRawWasInitialStatus` while iterating raw messages. Reset it on every human message, tool message, and non-status assistant message. Skip only a consecutive recognized assistant status; keep the first. Perform this before adding the AI message to `messageMap` while preserving existing tool-result association.

- [ ] **Step 4: Run tests and verify GREEN**

```bash
node --import tsx --test tests/process-messages.test.ts
```

Expected: all status and barrier tests pass.

- [ ] **Step 5: Inspect frontend presentation diff without staging**

```bash
git diff -- src/app/utils/processMessages.ts tests/process-messages.test.ts
git diff --check
```

## Task 6: Live nested subagent tool rendering

**Files:**

- Modify: `src/app/components/ChatMessage.tsx`
- Modify: `tests/subagent-chat-message.test.tsx`

- [ ] **Step 1: Write failing stable-reference rerender test**

Use stable `message`, `toolCalls`, and `stream` objects. Mutate only the value returned by `stream.getSubagent`, then call Testing Library `rerender` with identical props. Assert `tavily_search` appears.

```typescript
test("refreshes nested tools from a mutable SDK snapshot", () => {
  const message = { id: "ai-live", type: "ai", content: "" } as const;
  const rootTask: ToolCall = {
    id: "task-live",
    name: "task",
    args: {
      description: "Research graph engineering",
      subagent_type: "research-agent",
    },
    status: "pending",
  };
  let nested: Array<Record<string, unknown>> = [];
  const stream = {
    getSubagent: () => ({ toolCalls: nested }),
  };
  const props = { message, toolCalls: [rootTask], stream };
  const view = render(<ChatMessage {...props} />);

  assert.equal(screen.queryByRole("button", { name: /tavily_search/i }), null);
  nested = [
    {
      id: "search-live",
      call: { name: "tavily_search", args: { query: "graph engineering" } },
      state: "pending",
    },
  ];
  view.rerender(<ChatMessage {...props} />);
  assert.ok(screen.getByRole("button", { name: /tavily_search/i }));
});
```

- [ ] **Step 2: Run test and verify RED**

```bash
node --import tsx --test --test-isolation=none tests/subagent-chat-message.test.tsx
```

Expected: nested tool remains absent because `React.memo` and `useMemo` reuse stable props.

- [ ] **Step 3: Remove stale memoization**

Change `ChatMessage` from `React.memo<ChatMessageProps>(...)` to a normal exported function or const. Remove `useMemo` around the `subAgents` projection and compute it during each render. Keep `useState`/`useCallback`, existing adapter use, task-ID association, expansion behavior, and `ChatMessage.displayName` if still applicable.

- [ ] **Step 4: Run all subagent tests and verify GREEN**

```bash
node --import tsx --test --test-isolation=none tests/subagent-stream-adapter.test.ts tests/subagent-chat-message.test.tsx tests/langgraph-run-executor.test.ts
```

Expected: all pass, including parallel task association and `streamSubgraphs: true`.

- [ ] **Step 5: Inspect frontend live trace diff without staging**

```bash
git diff -- src/app/components/ChatMessage.tsx tests/subagent-chat-message.test.tsx
git diff --check
```

## Task 7: Cross-repository verification and live acceptance

**Files:**

- No production edits unless verification exposes a failure.

- [ ] **Step 1: Run backend focused verification**

```bash
uv run pytest tests/test_document_context.py tests/test_tools.py tests/test_citations.py tests/test_prompts_validation.py -q
uv run ruff check research_agent/document_context.py research_agent/agent.py research_agent/research_subagent/tools.py thread_wiki/source_types.py thread_wiki/service.py tests/test_document_context.py tests/test_tools.py
git diff --check
```

Expected: zero failures and zero lint/diff errors.

- [ ] **Step 2: Run frontend focused verification**

```bash
node --import tsx --test --test-isolation=none tests/thread-document-availability.test.tsx tests/submit-research-message.test.ts tests/process-messages.test.ts tests/subagent-stream-adapter.test.ts tests/subagent-chat-message.test.tsx tests/langgraph-run-executor.test.ts
yarn lint
yarn build
git diff --check
```

Expected: zero test failures; lint and build exit 0.

- [ ] **Step 3: Run configured model diagnostics**

From backend repository:

```bash
uv run python -c "import asyncio, json; from webapp.model_diagnostics import run_model_diagnostics; print(json.dumps(asyncio.run(run_model_diagnostics()), indent=2))"
```

Expected: detected Ollama provider, successful model construction, successful connectivity prompt.

- [ ] **Step 4: Run fresh no-document acceptance**

Start backend and frontend in separate terminals:

```bash
uv run langgraph dev --no-reload --no-browser
```

```bash
yarn dev
```

Create a fresh thread with no upload and ask: `Research graph engineering and produce a cited report.` Verify:

- one rendered `Starting research…` status;
- no `llm_wiki_query` or `read_docs_folder` root tool call;
- at least one `research-agent` task;
- expanded task shows `tavily_search`, `fetch_webpage_content`, or `think_tool` live;
- `/final_report.md` is created and displayed.

If delegation does not occur with the configured local model, stop and return to tool-policy/prompt diagnosis. Do not mark complete based only on unit tests.

- [ ] **Step 5: Inspect repository state and Threadroot score**

```bash
git status --short
threadroot score latest --json
```

Run in each repository where Threadroot data exists. Confirm only intended files and known pre-existing modifications remain.

Compare dirty-file baselines before reporting completion:

```bash
git diff --binary -- research_agent/agent.py tests/test_tools.py
shasum -a 256 research_agent/agent.py tests/test_tools.py
```

```bash
git diff --binary -- next-env.d.ts
shasum -a 256 next-env.d.ts
```

For backend files, review the new combined diff against `/tmp/deep-research-preexisting.patch` and confirm every original hunk is still present. For `next-env.d.ts`, require byte-identical hash and diff compared with `/tmp/bmo-ui-preexisting.patch`. If `yarn build` changes it, stop and restore only that file's captured pre-build content with `apply_patch`, then rerun the comparison.

- [ ] **Step 6: Record durable decisions and code areas**

Use CCE `record_decision` for the fail-closed tri-state policy and `record_code_area` for each meaningful backend/frontend file group.
