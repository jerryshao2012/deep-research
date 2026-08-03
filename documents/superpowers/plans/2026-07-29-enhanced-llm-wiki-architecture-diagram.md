# Enhanced LLM Wiki Architecture Diagram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an editable, validated draw.io architecture diagram and 2000 px PNG showing the Enhanced LLM Wiki ingestion and query system.

**Architecture:** Construct the approved layered pipeline directly in the visible draw.io canvas. Use stable semantic cell IDs, three color-coded processing lanes, orthogonal connectors, and separate safety/operations rails; save only after live geometry exists.

**Tech Stack:** Draw.io desktop, `drawio-live` MCP, `drawio-file-utils` MCP.

**Constraint:** Do not modify application code and do not create a Git commit.

**Live-workflow boundary:** Create every editable vertex, label, and connector
through paced `drawio-live` graph operations. Do not generate/import XML first,
and do not use operating-system mouse, keyboard, or screen control.

**Node style:** Use rounded rectangles for every component node
(`rounded=1;arcSize=14`) with orthogonal connectors.

---

### Task 1: Launch canvas and establish visual frame

**Files:**
- Create: `../../diagrams/enhanced-llm-wiki-architecture.drawio`
- Create: `../../diagrams/enhanced-llm-wiki-architecture.png`

- [ ] **Step 1: Launch live draw.io**

Call `drawio_live_launch` with a visible 500 ms step delay.

- [ ] **Step 2: Verify graph readiness**

Call `drawio_live_status`.

Expected: `graph_ready=true`.

- [ ] **Step 3: Add title, subtitle, and main section labels**

Create editable text with stable IDs:

- `diagram_title` — `Enhanced LLM Wiki — Multi-Source, Code-Aware Ingestion`
- `diagram_subtitle` — `Existing Thread Wiki enhanced with deterministic local code parsing`
- `section_sources` — `Sources`
- `section_staging` — `Validation & Staging`
- `section_processing` — `Code-Aware Processing`
- `section_knowledge` — `Knowledge & Query`

- [ ] **Step 4: Inspect first frame**

Call `drawio_live_screenshot`.

Expected: clear 16:9 landscape composition with no clipping.

### Task 2: Draw primary ingestion pipeline

**Files:**
- Modify: live draw.io graph

- [ ] **Step 1: Add source nodes**

Add blue nodes with stable IDs:

- `source_upload` — `File Upload\nDocuments + source files`
- `source_git` — `Public Git URL\nHTTPS + optional ref`

- [ ] **Step 2: Add separate upload and Git validation**

Add amber nodes:

- `validate_upload` — `Validate Uploaded Files\nPreserve names and paths`
- `validate_git` — `Validate Git Import\nAllowlisted hosts • optional ref\nBounded shallow clone`

- [ ] **Step 3: Add canonical staging merge**

Add amber node `stage_originals`:

`Stage Canonical Originals\nPreserve repository paths • write /raw/`

- [ ] **Step 4: Connect sources through their correct validation paths**

Add stable-ID orthogonal edges:

- `edge_upload_validate`: `source_upload` → `validate_upload`
- `edge_git_validate`: `source_git` → `validate_git`
- `edge_upload_stage`: `validate_upload` → `stage_originals`
- `edge_git_stage`: `validate_git` → `stage_originals`

- [ ] **Step 5: Inspect source section**

Call `drawio_live_screenshot`; fix spacing or clipping with
`drawio_live_update_cell`.

### Task 3: Draw three processing lanes

**Files:**
- Modify: live draw.io graph

- [ ] **Step 1: Add ordinary-document lane**

Create green nodes:

- `route_document` — `Ordinary Document`
- `process_document` — `Existing extraction\n+ text chunking`

- [ ] **Step 2: Add whole-code lane**

Create violet nodes:

- `route_code` — `Whole Code File`
- `process_code` — `Tree-sitter adapters\nNormalized semantic units\nRepository symbol/import index`

Use the exact final label `Deterministic repository symbol/import index`.

- [ ] **Step 3: Add embedded-code lane**

Create pink nodes:

- `route_embedded` — `Embedded Code Fence`
- `process_embedded` — `Conservative fence extraction\nDocument-attached AST units\nNo repository linking or speculative call/type resolution`

- [ ] **Step 4: Connect staging to all lanes**

Use labeled orthogonal edges with stable IDs:

- `edge_stage_document` — `ordinary`
- `edge_stage_code` — `recognized code`
- `edge_stage_embedded` — `supported fence`

- [ ] **Step 5: Add supervised build**

Create cyan node `supervised_ingest`:

`Unified Supervised Wiki Build\nAnalyze → Apply → Review`

Connect every processing lane to this node.

Use stable edge IDs `edge_document_ingest`, `edge_code_ingest`, and
`edge_embedded_ingest`.

- [ ] **Step 6: Inspect processing lanes**

Call `drawio_live_fit`, then `drawio_live_screenshot`.

Expected: three lanes align vertically and remain visually distinct.

### Task 4: Draw persistence, query, and cross-cutting rails

**Files:**
- Modify: live draw.io graph

- [ ] **Step 1: Add citable original-source store**

Create purple node `original_sources`:

`Citable Original Sources\nCanonical /raw/ documents and code\nDocument pages • source-code line ranges`

- [ ] **Step 2: Add non-citable derived knowledge**

Create light-purple node `derived_knowledge`:

`Derived Navigation — Never Cite Directly\nWiki pages • /raw/_code/ semantic chunks\nRepository index • versioned manifest`

- [ ] **Step 3: Add grounded query**

Create red node `grounded_query`:

`Grounded Query in Existing Thread Wiki\nWiki API + llm_wiki_query\nResolve citations to original files`

- [ ] **Step 4: Connect build, navigation, and citation flow**

Add stable-ID connectors:

- `edge_stage_originals`: `stage_originals` → `original_sources`
- `edge_ingest_knowledge`: `supervised_ingest` → `derived_knowledge`
- `edge_knowledge_query`: `derived_knowledge` → `grounded_query`
- `edge_sources_query`: `original_sources` → `grounded_query`, labeled
  `ground facts + citation targets`

- [ ] **Step 5: Add safety rail**

Create orange dashed container `safety_rail`:

`Safety & Fallback\nParse only—never execute • recover partial trees\nDisabled / oversized / unsupported encoding / parser failure /\nno usable units → existing text path`

- [ ] **Step 6: Add operations rail**

Create gray dashed container `operations_rail`:

`Status & Operations\nProgress snapshot • code-analysis summary • deterministic warnings\nRun-scoped regeneration prevents stale artifacts`

- [ ] **Step 7: Add legend**

Add editable legend cells with stable IDs:

- `legend_input` — blue inputs
- `legend_validation` — amber validation/staging
- `legend_document` — green ordinary documents
- `legend_ast` — violet whole-file AST
- `legend_embedded` — pink embedded code
- `legend_supervised` — cyan supervised processing
- `legend_knowledge` — purple/red knowledge/query
- `legend_safety` — orange dashed safety rail
- `legend_operations` — gray dashed operations rail

- [ ] **Step 8: Inspect complete diagram**

Call `drawio_live_fit` and `drawio_live_screenshot`.

Check hierarchy, labels, edge directions, overlaps, and whitespace.

- [ ] **Step 9: Inspect editable graph model**

Call `drawio_live_inspect`.

Expected: every title, section label, node, legend item, and connector has its
planned stable ID; labels remain editable text; vertices and edges are distinct
graph cells.

### Task 5: Save, validate, export, and review

**Files:**
- Create: `../../diagrams/enhanced-llm-wiki-architecture.drawio`
- Create: `../../diagrams/enhanced-llm-wiki-architecture.png`

- [ ] **Step 1: Save live graph**

Call `drawio_live_save_snapshot` only after all visible geometry exists, with:

`output_path`:

`/Users/jerryshao/Documents/projects/IBM/ai/deep-research/document/diagrams/enhanced-llm-wiki-architecture.drawio`

- [ ] **Step 2: Validate source**

Call `drawio_validate` with:

`input_path`:

`/Users/jerryshao/Documents/projects/IBM/ai/deep-research/document/diagrams/enhanced-llm-wiki-architecture.drawio`

Expected: valid draw.io document with no structural errors.

Call `drawio_inspect` on:

`/Users/jerryshao/Documents/projects/IBM/ai/deep-research/document/diagrams/enhanced-llm-wiki-architecture.drawio`

Expected: planned vertex and edge IDs exist, labels are editable text, and
geometry is stable after serialization.

- [ ] **Step 3: Export review PNG**

Call `drawio_export` with:

- `input_path`:
  `/Users/jerryshao/Documents/projects/IBM/ai/deep-research/document/diagrams/enhanced-llm-wiki-architecture.drawio`
- `output_path`:
  `/Users/jerryshao/Documents/projects/IBM/ai/deep-research/document/diagrams/enhanced-llm-wiki-architecture.png`
- `format="png"`
- `embed=false`
- `width=2000`


- [ ] **Step 4: Inspect exported PNG**

Open the absolute PNG path with the image-view tool.

Verify:

- all labels are readable;
- no connectors cross unrelated nodes;
- code is described as parsed, never executed;
- embedded code explicitly excludes repository resolution;
- citations point to original sources;
- no clipping or excessive whitespace.

- [ ] **Step 5: Deliver artifacts**

Return clickable paths to `.drawio`, PNG, design spec, and this plan. State
validation result. Do not stage or commit the files.
