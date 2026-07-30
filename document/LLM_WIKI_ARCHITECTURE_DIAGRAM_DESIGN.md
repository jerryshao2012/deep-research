# Enhanced LLM Wiki Architecture Diagram Design

## Purpose and audience

Create a mixed-technical-audience architecture diagram showing how Enhanced
LLM Wiki ingests ordinary documents, whole source-code files, embedded code
fences, and public Git repositories. The diagram should explain system behavior
without requiring readers to know individual Python modules.

## Deliverables

- Editable draw.io source:
  `document/diagrams/enhanced-llm-wiki-architecture.drawio`
- Review image:
  `document/diagrams/enhanced-llm-wiki-architecture.png`

## Visual structure

Use a left-to-right layered pipeline:

1. **Sources** — file uploads and public Git URLs.
2. **Validation and staging** — allowlisted Git import, bounded shallow clone,
   preserved repository paths, and canonical original files under `/raw/`.
3. **Processing lanes**:
   - Ordinary documents use existing extraction and text chunking.
   - Whole code files use Tree-sitter adapters, normalized semantic units, and
     deterministic repository symbol/import indexing.
   - Supported code fences use conservative embedded-code extraction and
     document-attached semantic units without repository resolution.
4. **Supervised wiki build** — analyze, apply, and review.
5. **Knowledge and query** — wiki pages, semantic artifacts, manifest, explicit
   `llm_wiki_query`, and citations mapped to original documents or code lines.

Place two horizontal rails below the main flow:

- **Safety and fallback** — parse only, never execute; recover partial trees;
  unsupported encoding, disabled parsing, oversized input, parser failure, or
  no usable units fall back to existing text processing.
- **Status and operations** — progress snapshots, code-analysis summary,
  deterministic warnings, and run-scoped artifact regeneration.

## Visual language

- Blue: inputs.
- Amber: validation and staging.
- Green: ordinary-document processing.
- Violet: whole-file AST processing.
- Pink: embedded-code extraction.
- Cyan: supervised wiki build.
- Purple/red: persisted knowledge and grounded query.
- Orange/gray dashed rails: safety and operational concerns.

Use rounded rectangles, orthogonal connectors, concise labels, and a small
legend. Preserve a clear primary reading path at normal presentation scale.

## Accuracy constraints

- Show AST processing as an enhancement to existing Thread Wiki, not a separate
  wiki or endpoint.
- Distinguish derived navigation artifacts from original citable sources.
- Show original-file page or line-range citations.
- Do not imply uploaded code is imported or executed.
- Do not imply speculative call/type resolution or repository linking for
  embedded snippets.

## Validation

- All labels remain editable.
- Connector directions match ingestion and query flow.
- No overlapping labels or crossing through unrelated components.
- Export a non-embedded 2000 px PNG for review.
- Validate the `.drawio` source before final export.
