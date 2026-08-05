# UI skills guide design

## Purpose

Create a user-facing guide that explains how to find, configure, apply, and review agent skills through the Deep Agent UI. Existing documentation covers CLI selection and developer authoring, but not the visual workflow shown by the repository screenshots.

## Audience

- End users selecting an installed skill for a research thread.
- Administrators viewing, refreshing, or uploading skills through Settings.

The guide assumes the UI and its backend are already deployed and authenticated. Deployment, CLI-only use, and skill authoring remain in their existing guides.

## Deliverable

Add `documents/guides/skills.md` as one end-to-end guide. Keep it task-oriented and use these sections:

1. What a skill changes.
2. Browse available skills.
3. Configure and upload skills as an administrator.
4. Apply a skill to the current thread.
5. Review generated results and state files.
6. Troubleshoot discovery, backend, context, and output problems.
7. Follow links for CLI use and custom skill authoring.

## Screenshot sequence

Use four existing UI screenshots in workflow order:

1. `Skills - Available Skills-20260723-pztn.png` for search and discovery.
2. `Skills - Configuration_Skills-20260723-pznq.png` for Settings, backend status, refresh, and upload controls.
3. `Skills - Apply a Skill-20260723-qaar.png` for the skill request using current thread context and uploaded documents.
4. `Skills - Result of Applying a Skill-20260723-qaee.png` for the generated structured result.

Reference each image from `documents/assets/screenshots/` with descriptive alt text and a short explanatory paragraph. Do not use `Deep_Research_Agent_with_Golden_Dataset_Generation_Skill.png`: it is an implementation diagram, not part of the UI workflow.

## Content rules

- Describe skills as named capabilities or output instructions that shape how the agent handles a request; do not imply every skill has the same output format.
- Explain that applying a skill can reuse the active thread's recent messages, state files, and uploaded documents when available.
- Separate ordinary user actions from administrator-only configuration and upload actions.
- Use visible UI labels from the screenshots: `Skills`, `Available Skills`, `Settings`, `Configuration`, `Upload Skill`, refresh, and `Save`.
- Avoid documenting backend endpoints or upload package internals in this user guide.
- Note that screenshots are illustrative and installed skills vary by deployment.

## Navigation changes

Link the new guide from:

- `documents/README.md` in the guides section.
- `documents/getting-started/usage.md` near output-skill examples and related documentation.
- `documents/development/extending-the-agent.md` as the user-side counterpart to custom skill authoring.
- Root `README.md` near its skill quick-start commands.

## Verification

- Run `uv run python -m pytest tests/test_documentation.py -q`.
- Run `git diff --check`.
- Confirm every screenshot link resolves and no screenshot remains unreferenced by mistake.
- Review the final Markdown flow against the four screenshots for label and sequence accuracy.

## Non-goals

- Duplicating CLI option reference material.
- Teaching users to write `SKILL.md` files.
- Documenting frontend or backend implementation details.
- Reorganizing unrelated screenshots or guides.
