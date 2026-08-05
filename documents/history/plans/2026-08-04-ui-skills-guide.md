# UI Skills Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a screenshot-led user guide for finding, configuring, applying, and reviewing skills through the Deep Agent UI.

**Architecture:** Create one task-oriented Markdown guide under `documents/guides/` and reuse four existing images from `documents/assets/screenshots/`. Keep CLI and skill-authoring details in existing guides, then add navigation links from the handbook, usage guide, contributor guide, and root README.

**Tech Stack:** Markdown, PNG screenshots, pytest documentation contracts

**Constraint:** Keep all changes local and uncommitted.

---

### Task 1: Create the UI skills guide

**Files:**
- Create: `documents/guides/skills.md`
- Modify: `documents/README.md`
- Reference: `documents/assets/screenshots/Skills - Available Skills-20260723-pztn.png`
- Reference: `documents/assets/screenshots/Skills - Configuration_Skills-20260723-pznq.png`
- Reference: `documents/assets/screenshots/Skills - Apply a Skill-20260723-qaar.png`
- Reference: `documents/assets/screenshots/Skills - Result of Applying a Skill-20260723-qaee.png`

- [ ] **Step 1: Establish the guide contract**

Use this heading sequence:

```markdown
# Use skills in Deep Agent
## How skills work
## Find an available skill
## Configure skills
## Apply a skill
## Review the result
## Troubleshooting
## Related documentation
```

State that screenshots are illustrative and installed skills vary by deployment. Define a skill as a named capability or set of output instructions, not as a guaranteed file type.

- [ ] **Step 2: Document discovery with the available-skills screenshot**

Explain how to open `Skills`, search by capability, category, or keyword, read descriptions, and choose a suitable installed skill. Add the image using:

- Alt text: `Available Skills panel with skill search and installed capabilities`
- Relative target from the guide: `../assets/screenshots/Skills%20-%20Available%20Skills-20260723-pztn.png`

- [ ] **Step 3: Document administrator configuration**

Describe `Settings` -> `Configuration` -> `Skills`, the live-backend indicator, search, refresh, `Upload Skill`, and `Save`. Keep role boundaries explicit: ordinary users select installed skills; administrators manage availability. Avoid backend endpoint and package-format claims. Add the image using:

- Alt text: `Skills configuration with backend status, search, refresh, and upload controls`
- Relative target from the guide: `../assets/screenshots/Skills%20-%20Configuration_Skills-20260723-pznq.png`

Do not assert that upload or refresh always requires `Save`; tell readers to use `Save` when the UI shows pending configuration changes.

- [ ] **Step 4: Document application and thread context**

Explain that a selected skill request identifies the skill and can reuse recent messages, state files, and uploaded documents from the current thread. Tell readers to review the generated request if the deployed UI presents a confirmation step; do not claim selection always sends immediately. Add the image using:

- Alt text: `Study Slides skill request using recent messages, state files, and an uploaded document`
- Relative target from the guide: `../assets/screenshots/Skills%20-%20Apply%20a%20Skill-20260723-qaar.png`

- [ ] **Step 5: Document results and troubleshooting**

Explain that results appear in the conversation and may add state files depending on the skill. Include the image using:

- Alt text: `Structured presentation result created by the selected skill`
- Relative target from the guide: `../assets/screenshots/Skills%20-%20Result%20of%20Applying%20a%20Skill-20260723-qaee.png`

Cover: empty skill list or missing live-backend status, missing newly uploaded skill, irrelevant output, missing source context, and retrying with a more explicit request. Link CLI usage and custom skill authoring rather than duplicating them.

- [ ] **Step 6: Index the canonical guide**

Add `Use skills in the UI` under the guides section in `documents/README.md`, pointing to `guides/skills.md` and describing discovery, configuration, application, and results. This index entry is required for the documentation contract to accept the new canonical guide.

- [ ] **Step 7: Run the focused documentation contract**

Run:

```bash
uv run python -m pytest tests/test_documentation.py -q
```

Expected: all documentation tests pass, including local image-link resolution.

### Task 2: Add guide navigation

**Files:**
- Modify: `documents/getting-started/usage.md`
- Modify: `documents/development/extending-the-agent.md`
- Modify: `README.md`

- [ ] **Step 1: Link from usage guidance**

Add a short pointer near the output-skill examples in `documents/getting-started/usage.md`, then add `Use skills in the UI` to its related-documentation list using `../guides/skills.md`.

- [ ] **Step 2: Link the user and contributor guides**

Add `Use skills in the UI` to `documents/development/extending-the-agent.md` related documentation. This keeps skill authoring separate while giving contributors the user workflow for validation.

- [ ] **Step 3: Link from the root quick start**

After the root README's `--skill list` example, add one concise sentence linking to `documents/guides/skills.md` for the UI workflow. Do not expand the root README.

- [ ] **Step 4: Re-run the focused documentation contract**

Run:

```bash
uv run python -m pytest tests/test_documentation.py -q
```

Expected: all documentation tests pass with the new navigation links.

### Task 3: Verify the complete documentation change

**Files:**
- Verify: `documents/guides/skills.md`
- Verify: `documents/README.md`
- Verify: `documents/getting-started/usage.md`
- Verify: `documents/development/extending-the-agent.md`
- Verify: `README.md`

- [ ] **Step 1: Confirm screenshot references**

Run:

```bash
rg -n 'Skills%20-%20(Available|Configuration|Apply|Result)' documents/guides/skills.md
```

Expected: four image references in workflow order. Compare the final guide against each screenshot to confirm visible labels, captions, and sequence are accurate. Confirm `Deep_Research_Agent_with_Golden_Dataset_Generation_Skill.png` is not added to this UI guide because it is an implementation diagram.

- [ ] **Step 2: Check Markdown whitespace and patch integrity**

Run:

```bash
git diff --check
```

Expected: exit code 0 with no whitespace errors.

- [ ] **Step 3: Run final documentation tests**

Run:

```bash
uv run python -m pytest tests/test_documentation.py -q
```

Expected: all documentation tests pass.

- [ ] **Step 4: Inspect scope**

Run:

```bash
git status --short --untracked-files=all
```

Expected: guide, navigation, design/plan notes, and previously requested screenshot relocation changes only. Do not stage, commit, or push.
