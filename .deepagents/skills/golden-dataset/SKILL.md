---
name: golden-dataset
description: Produce a Golden Dataset starter pack with realistic customer questions and draft LLM answers. Includes automated quality metrics scoring (Similarity, Relevance, Coherence, Groundedness) and report generation via bundled scripts.
---

## Instructions

Create a Golden Dataset starter pack grounded in the available documents and research findings.

This skill covers only:

- Step 1: create realistic, self-contained customer questions
- Step 2: generate draft LLM responses for those questions

Do not do Step 3 or Step 4 (defined in
the [Producing Golden Datasets](https://github.com/microsoft/promptflow-resource-hub/blob/main/sample_gallery/golden_dataset/copilot-golden-dataset-creation-guidance.md)):

- Do not write expert answers
- Do not present draft answers as validated or final
- Do not invent citations or source references that are not supported by the provided materials

Requirements:

- **ROLE SPLIT — who does what:**
    - The main agent **orchestrator** performs the DOCUMENT ACCESS WORKFLOW (below) using local filesystem/document
      tools, prepares grounded context, and coordinates the final output.
    - The **research sub-agent** is web-only (`tavily_search`, `fetch_webpage_content`), drafts the 12 Q/A items using
      orchestrator-provided grounding plus optional web evidence, and returns the data to the orchestrator.
- **COMPLETION SEQUENCE (orchestrator only) — follow these steps in order after the sub-agent returns the drafted items:**

    1. Use `write_file` to save the dataset as a CSV file:
       - Columns: ID, Coverage Area, Question, Answer, Context
       - Save to the output folder (e.g., `./output/dataset_name.csv`)
       - The CSV must have exactly 12 items unless the user explicitly asks for a different count

    2. Run the full evaluation pipeline in one step:
       ```bash
       cd .deepagents/skills/golden-dataset/scripts && python score_dataset.py ./output/<dataset_name>.csv --eval-mode baseline
       ```
       This bundles scoring (Similarity, Relevance, Coherence, Groundedness via LLM judge),
       markdown conversion, comprehensive report generation, humanization, and regression
       tracking into a single command. Use `--eval-mode baseline` for the first run
       and `--eval-mode candidate` for follow-up runs to automatically compare against
       the baseline. Output files live on disk under the output directory.

    3. Persist the final report and metrics to the agent's state so they are
       visible in follow-up turns and the web UI:
       - Use `read_file` to load the generated report (`<stem>_report.md`) and
         metrics markdown (`<stem>_metrics.md`) from the output directory.
       - Use `write_file` to save the report to `/final_report.md`.
       - Use `write_file` to save the metrics table to `/golden_dataset_metrics.md`.
       This writes to both the sandbox filesystem AND LangGraph state in one call.

    4. Only after steps 1-3 succeed, write a brief summary to the user.
       **Do NOT skip any steps. A verbal description of the dataset is NOT a substitute for actual file generation.**

- **DOCUMENT ACCESS WORKFLOW (orchestrator only)**:

    - Step 1: Call `read_doc_folder` exactly once on the configured doc folder. This extracts
      documents and returns saved paths.
    - Step 2: Use the EXACT extracted paths with `read_file` (do NOT add leading `/`).
    - Step 3: Build grounded notes/snippets from extracted markdown files and pass them into the sub-agent task prompt.
    - Step 4: Sub-agent returns the full payload to orchestrator; orchestrator then generates CSV and runs metrics scripts.

- Produce a reviewable starter batch with exactly 12 items unless the user explicitly asks for a different count.
- Questions and answers are based on extracted knowledge documents in markdown format.
- Questions must sound like realistic non-expert customer questions.
- Every question must be self-contained and unambiguous.
- Cover the major domain areas visible in the provided materials.
- Prefer common customer-style openings such as `How do I...`, `What is...`, `Can you give me...`, `Why should I...`,
  and `What are the recommended best practices for ...`.
- Answers should be helpful and plausible, but clearly framed as starting points for later expert review.
- Keep each answer concise but complete enough (3+ sentences) for a domain expert to refine.
- If grounding is weak, narrow the question or add a short caveat inside the answer rather than overstating certainty.
- **Complete the full dataset in one pass. Do NOT stop mid-generation to ask the user which topics to prioritize. Make all topic and coverage choices autonomously based on the available documents.**
- Include `context` for every item — the supporting RAG context that best matches the question and answer, used for Groundedness evaluation.
- **Sequential IDs**: Ensure each item has a sequential string ID starting from "1".

## Output Format

Use `write_file` to save the dataset as a CSV file with the following columns:

```
ID, Coverage Area, Question, Answer, Context
```

Each row represents one Q/A pair. The CSV must:
- Include a header row
- Have exactly 12 rows (unless user specifies otherwise)
- Include the `dataset_name` as part of the filename (sanitized: lowercase, underscores)
- Include the `domain` field in the first data row or as metadata
- Include `coverage_areas` as a comment or metadata line

**Top-level metadata fields:**
- `dataset_name` — A descriptive name for the dataset (derived from domain)
- `domain` — The subject domain (e.g., "Employee handbook and HR policy")
- `recommended_total_dataset_size` — Suggested full dataset size (start with max(50, num_items * 4))
- `coverage_areas` — List of topic areas covered by the items

**Per-item fields:**
- `id` — Sequential string (1, 2, 3, ...)
- `coverage_area` — Which coverage area this item belongs to
- `question` — Realistic customer question
- `answer` — Draft LLM response (3+ sentences, grounded in source materials)
- `context` — RAG passage or summary that grounds this Q/A pair

## Quality Guidelines

Before submitting, verify every item passes:

- **Item count**: Exactly 12 items are present unless the user explicitly requested a different count.
- **Question realism**: Every question sounds like a real customer inquiry — avoid academic or overly technical phrasing. Prefer openings such as "How do I…", "What is…", "Can you give me…".
- **Self-contained questions**: Each question must be understandable on its own without referencing other items in the dataset.
- **No duplicate questions**: No two items should ask the same question with different wording.
- **Coverage balance**: Items should spread across the listed coverage areas; no single area should dominate more than 40% of the dataset.
- **Grounding**: Every draft response must be traceable to the provided documents or research findings. Do not invent facts, statistics, or source references that are not supported by the materials.
- **Context capture**: Every item must include `context` containing the grounding RAG passage or summary that best supports the draft answer. Groundedness scoring depends on it.
- **Caveat over certainty**: If grounding is weak for a particular answer, narrow the question scope or add a short caveat rather than overstating confidence.
- **Draft framing**: Draft responses must be clearly framed as starting points — do not present them as validated expert answers.
- **Response completeness**: Each `answer` must be substantive enough (3+ sentences) for a domain expert to meaningfully review and refine.
- **Metric readiness**: The output must be a directly importable CSV with ID, Coverage Area, Question, Answer, and Context columns for scoring.
- **Sequential IDs**: Ensure `id` for each item is a sequential string starting from "1".

### Handling Large Scale Datasets

If the `doc-folder` contains thousands of files or very large files (hundreds of GBs):

1. **Initial Extraction**: First, call `read_doc_folder` on the configured doc folder to trigger extraction of documents.
2. **Access Extracted Content**: Use the EXACT paths from the extraction output. If paths start with `/`, strip the leading `/`.
3. **Selective Sampling**: If there are many extracted files, identify a representative subset. If the research subject is broad, **automatically sample a diverse set** to cover a range of topics without asking for confirmation.
4. **Iterative Coverage**: If needed, repeat for different coverage areas to ensure items are well-distributed.
5. **Summarization**: For very large individual extracted documents, focus on their executive summaries or introductions.
