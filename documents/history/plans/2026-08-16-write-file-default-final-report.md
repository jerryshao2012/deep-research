# Default Final Report Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept content-only `write_file` tool calls and persist them at `/final_report.md` without changing explicit-path writes.

**Architecture:** Keep the existing general-purpose `write_file` tool and persistence pipeline. Change only its public argument contract so `content` remains required while `file_path` defaults to the canonical final-report path; retain explicit path passthrough and all current normalization/error behavior.

**Tech Stack:** Python 3.13, LangChain `@tool`, Pydantic-generated tool schemas, pytest, Ruff

---

## File Structure

- Modify `../../../research_agent/research_subagent/tools.py`: define the optional `file_path` default and document when omission is valid.
- Modify `../../../tests/test_write_file.py`: cover generated schema, content-only execution, and explicit-path compatibility at the decorated tool boundary.

### Task 1: Lock the `write_file` Tool Contract with Failing Tests

**Files:**
- Modify: `../../../tests/test_write_file.py`
- Test: `../../../tests/test_write_file.py`

- [ ] **Step 1: Import the decorated tool module**

Add this import with the existing imports:

```python
from research_agent.research_subagent import tools
```

- [ ] **Step 2: Add the missing-path schema and invocation regression test**

```python
def test_write_file_tool_defaults_missing_path_to_final_report(monkeypatch) -> None:
    writes: list[tuple[str, str]] = []

    def capture_write(file_path: str, content: str) -> str:
        writes.append((file_path, content))
        return f"Successfully wrote {len(content)} bytes to {file_path}"

    monkeypatch.setattr(tools, "write_file_impl", capture_write)

    schema = tools.write_file.tool_call_schema.model_json_schema()
    assert "file_path" not in schema.get("required", [])

    result = tools.write_file.func(content="# Final report", state={})

    assert writes == [("/final_report.md", "# Final report")]
    assert "/final_report.md" in result
```

- [ ] **Step 3: Add explicit-path compatibility coverage**

```python
def test_write_file_tool_preserves_explicit_path(monkeypatch) -> None:
    writes: list[tuple[str, str]] = []

    def capture_write(file_path: str, content: str) -> str:
        writes.append((file_path, content))
        return f"Successfully wrote {len(content)} bytes to {file_path}"

    monkeypatch.setattr(tools, "write_file_impl", capture_write)

    tools.write_file.func(
        content="Original question",
        state={},
        file_path="/research_request.md",
    )

    assert writes == [("/research_request.md", "Original question")]
```

- [ ] **Step 4: Run focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_write_file.py \
  -k "write_file_tool_defaults_missing_path or write_file_tool_preserves_explicit_path" \
  -q
```

Expected: missing-path test fails because `file_path` is still required by both the generated schema and Python function. Explicit-path compatibility test passes.

- [ ] **Step 5: Commit the regression tests**

```bash
git add tests/test_write_file.py
git commit -m "test: reproduce missing final report path"
```

### Task 2: Default Omitted Paths to `/final_report.md`

**Files:**
- Modify: `research_agent/research_subagent/tools.py:223-247`
- Test: `../../../tests/test_write_file.py`

- [ ] **Step 1: Change the function contract**

Reorder required injected arguments before the defaulted argument and define the canonical default:

```python
@tool(parse_docstring=True)
def write_file(
        content: str,
        state: Annotated[dict, InjectedState],
        file_path: str = "/final_report.md",
) -> str:
```

- [ ] **Step 2: Clarify the tool description**

Replace the `file_path` argument description with:

```python
        file_path: The path where the file should be written. Omit only for the
            final research report, which defaults to `/final_report.md`. Provide
            an explicit path for every non-final artifact.
```

Do not change content normalization, `write_file_impl`, logging, state injection, or error handling.

- [ ] **Step 3: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_write_file.py \
  -k "write_file_tool_defaults_missing_path or write_file_tool_preserves_explicit_path" \
  -q
```

Expected: `2 passed`.

- [ ] **Step 4: Run the complete write-file test module**

Run:

```bash
uv run pytest tests/test_write_file.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run lint and whitespace checks**

Run:

```bash
uv run ruff check research_agent/research_subagent/tools.py tests/test_write_file.py
git diff --check
```

Expected: both commands exit successfully with no findings.

- [ ] **Step 6: Commit the implementation**

```bash
git add research_agent/research_subagent/tools.py
git commit -m "fix: default missing report path"
```

### Task 3: Final Regression Verification

**Files:**
- Verify: `../../../research_agent/research_subagent/tools.py`
- Verify: `../../../tests/test_write_file.py`

- [ ] **Step 1: Run focused adjacent tool coverage**

Run:

```bash
uv run pytest tests/test_write_file.py tests/test_tools.py -q
```

Expected: all new and write-file tests pass. The previously recorded unrelated
`test_read_docs_folder_reports_unsupported_and_empty_cases` baseline failure may
remain; verify no additional failures appear.

- [ ] **Step 2: Inspect branch scope**

Run:

```bash
git status --short
git diff main...HEAD --stat
git diff main...HEAD --check
```

Expected: only the approved spec, this plan, `tools.py`, and
`../../../tests/test_write_file.py` appear; diff check is clean.

- [ ] **Step 3: Record verification evidence**

Capture exact pass/failure counts, the known baseline failure if still present,
and commit hashes for local merge handoff. Do not claim the unrelated baseline
failure was fixed.
