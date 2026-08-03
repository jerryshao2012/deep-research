# Deep Research Doc Folder Design

## Goal

Expand `deep_research` from a PDF-only local document input to a broader `--doc-folder` input and support two built-in output targets:

- `slides`: concise Markdown presentation markup for quick learning, capped at fewer than three slides with speaking notes.
- `interview`: a 45-minute interview question kit grounded in the provided local documents.

## Scope

This first version supports these file types from a local folder:

- `.pdf`
- `.txt`
- `.md`
- `.docx`
- `.pptx`
- `.xlsx`

Legacy Microsoft Office binary formats (`.doc`, `.ppt`, `.xls`) are out of scope.

## CLI Changes

- Replace `--pdf-folder` with `--doc-folder`.
- Replace `--slides` with `--target`.
- Support `--target slides` and `--target interview`.
- Keep the instruction-building approach in `research_agent.py`, but make it target-aware and document-aware.

## Tool Changes

- Replace `read_pdf_folder` with `read_doc_folder`.
- Keep PDF extraction via Marker.
- Add best-effort extraction for text and Office formats.
- Continue processing if a file fails; include per-file errors in tool output.
- Keep built-in target tools as reusable output specifications.

## Output Targets

### Slides

`generate_slide_markup` should output structured Markdown presentation markup with:

- fewer than three slides
- clear slide titles
- concise learning-oriented bullet content
- speaking notes per slide

### Interview

Add `generate_interview_questions` to output:

- a brief interview objective
- a 45-minute agenda
- grounded interview questions
- follow-up prompts

## Testing

Add targeted tests for:

- CLI parsing and instruction construction
- document extraction behavior for supported file types
- partial failure handling in mixed folders
- target tool output structure
