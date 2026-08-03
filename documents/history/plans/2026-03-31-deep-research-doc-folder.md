# Deep Research Doc Folder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace PDF-only local document input with `--doc-folder` support for multiple document types and add built-in `slides` and `interview` output targets.

**Architecture:** Generalize local document ingestion into a single tool that extracts normalized text from supported files, then route final-format generation through built-in target tools selected by a target-aware CLI. Keep the implementation small and aligned with the existing plain-English instruction pattern.

**Tech Stack:** Python, argparse, LangChain tools, Marker PDF, python-docx, python-pptx, openpyxl, pytest

---

### Task 1: Add failing tests for target formatting helpers

**Files:**
- Create: `deep_research/tests/test_tools.py`
- Modify: `deep_research/research_agent/tools.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Write minimal implementation**
- [ ] **Step 4: Run test to verify it passes**

### Task 2: Add failing tests for CLI argument parsing and instruction building

**Files:**
- Create: `deep_research/tests/test_research_agent_cli.py`
- Modify: `deep_research/research_agent.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Write minimal implementation**
- [ ] **Step 4: Run test to verify it passes**

### Task 3: Add failing tests for document ingestion

**Files:**
- Modify: `deep_research/tests/test_tools.py`
- Modify: `deep_research/research_agent/tools.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Write minimal implementation**
- [ ] **Step 4: Run test to verify it passes**

### Task 4: Update docs and dependencies

**Files:**
- Modify: `deep_research/README.md`
- Modify: `deep_research/pyproject.toml`

- [ ] **Step 1: Update dependency list**
- [ ] **Step 2: Update CLI examples and tool descriptions**
- [ ] **Step 3: Run targeted tests**
