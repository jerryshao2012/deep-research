# Enhanced LLM Wiki diagram design specification

This is a design and export specification for maintainers of the Enhanced LLM Wiki architecture diagram. It tells diagram editors what the visual should communicate to a mixed technical audience; visual requirements below are not claims that every pictured boundary or label is independently enforced at runtime.

## Maintain the deliverables

- Editable draw.io source: [enhanced-llm-wiki-architecture.drawio](diagrams/enhanced-llm-wiki-architecture.drawio)
- Review image: [enhanced-llm-wiki-architecture.png](diagrams/enhanced-llm-wiki-architecture.png)

Keep labels editable. Validate draw.io source before exporting a non-embedded PNG at 2000 px width.

## Compose the visual

Use a left-to-right layered pipeline:

1. **Sources** — file uploads and public Git URLs.
2. **Validation and staging** — allowlisted Git import, bounded shallow clone, preserved repository paths, and canonical original files under `/raw/`.
3. **Processing lanes**:
   - Ordinary documents use existing extraction and text chunking.
   - Whole code files use Tree-sitter adapters, normalized semantic units, and deterministic repository symbol/import indexing.
   - Supported code fences use conservative embedded-code extraction and document-attached semantic units without repository resolution.
4. **Supervised wiki build** — analyze, apply, and review.
5. **Knowledge and query** — wiki pages, semantic artifacts, manifest, explicit `llm_wiki_query`, and citations mapped to original documents or code lines.

Place two horizontal rails below the main flow:

- **Safety and fallback** — parse only, never execute; recover partial trees; unsupported encoding, disabled parsing, oversized input, parser failure, or no usable units fall back to existing text processing.
- **Status and operations** — progress snapshots, code-analysis summary, deterministic warnings, and run-scoped artifact regeneration.

## Apply visual language

- Blue: inputs.
- Amber: validation and staging.
- Green: ordinary-document processing.
- Violet: whole-file AST processing.
- Pink: embedded-code extraction.
- Cyan: supervised wiki build.
- Purple/red: persisted knowledge and grounded query.
- Orange/gray dashed rails: safety and operational concerns.

Use rounded rectangles, orthogonal connectors, concise labels, and a small legend. Preserve a clear primary reading path at normal presentation scale.

## Check accuracy

- Show AST processing as an enhancement to existing Thread Wiki, not a separate wiki or endpoint.
- Distinguish derived navigation artifacts from original citable sources; derived `/raw/_code/` files are never citation targets.
- Show citations to original document pages or original code line ranges.
- State uploaded code is parsed but never imported, compiled, or executed.
- Keep embedded snippets attached to containing documents and outside repository symbol/import resolution.
- Do not imply speculative call graphs, type resolution, or cross-language linking.
- Treat lane and boundary layout as explanatory design; verify runtime claims against [code ingestion](code-ingestion.md) and route tests before changing labels.

## Validate the export

- All labels remain editable in draw.io source.
- Connector directions match ingestion and query flow.
- Labels do not overlap or cross unrelated components.
- Review PNG remains legible at normal presentation scale.
- Export contains no embedded draw.io payload.

## Related documentation

- [Architecture overview](overview.md)
- [AST-aware code ingestion](code-ingestion.md)
- [Thread Wiki API](../api/wiki.md)
