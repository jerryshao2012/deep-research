# Default Final Report Path for `write_file`

## Problem

Local models can invoke `write_file` with a complete report in `content` but omit
`file_path`. The tool's generated schema currently requires `file_path`, so
LangChain rejects the call before application code runs. The report never enters
LangGraph file state, while the model may still claim that `/final_report.md` was
saved.

## Scope

Change only `write_file` argument handling. Preserve explicit writes to paths such
as `/research_request.md` and `/research_reflection.md`. Do not change report
generation, verification, frontend file rendering, or document-folder behavior.

## Design

Make `file_path` an optional tool argument whose default is
`/final_report.md`. Keep `content` required. A call containing only `content`
therefore reaches the existing write implementation and stores the artifact at
the workflow's canonical final-report path. A supplied `file_path` continues to
override the default without normalization or persistence changes beyond current
behavior.

The tool description will state that omission means final-report output and that
non-final artifacts must provide an explicit path. This gives local models a
smaller valid argument shape for the required terminal write while retaining the
general-purpose tool.

## Data Flow

1. Model emits `write_file` with `content` and optionally `file_path`.
2. Tool schema accepts the call because `file_path` has a default.
3. Missing `file_path` resolves to `/final_report.md`; explicit paths are retained.
4. Existing `write_file_impl` persists content into LangGraph file state.
5. Existing UI reads `/final_report.md` from file state and displays it in
   **Files (State)**.

## Error Handling

All existing write errors remain unchanged. This design handles only an omitted
path; it does not guess alternate paths from report content, retry failed storage,
or turn other validation failures into successful calls.

## Tests

Use test-driven development:

- Prove generated tool schema does not require `file_path`.
- Prove a content-only invocation writes to `/final_report.md`.
- Prove an explicit path is still passed through unchanged.
- Run focused tool tests and lint for changed files.

The existing unrelated `read_docs_folder` baseline failure is outside this scope
and remains untouched.
