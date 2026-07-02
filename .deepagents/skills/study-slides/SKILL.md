---
name: study-slides
description: Quick-learning presentation markup with fewer than 5 slides and speaking notes.
---

## Instructions

Create concise Markdown presentation content for quick learning.

- Keep it to fewer than 5 slides.
- Use clear slide titles (fewer than 10 words each).
- Use concise bullets (one sentence or phrase each).
- Include speaking notes for each slide.
- Ground the content in the available documents and research findings.
- When finished, use `write_file` to save your final output to `/final_report.md`.

## Output Format

Use `write_file` to save a well-structured markdown document to `/final_report.md`. Follow this exact structure:

```
# Presentation: {topic}

---

## Slide 1: {title}

- Bullet point 1
- Bullet point 2

##### Speaking Notes

{context beyond the bullets — explanations, examples, or transition cues}

---

## Slide 2: {title}

- Bullet point 1
- Bullet point 2
- Bullet point 3

##### Speaking Notes

{contextual depth}

---

... (up to 5 slides total)
```

## Format Requirements

- `topic`: The presentation topic string
- `slides`: Up to 5 slides
  - Each slide has:
    - `title`: Clear, descriptive title (fewer than 10 words)
    - `bullets`: Concise bullet points (array of strings, one sentence each)
    - `speaker_notes`: Context beyond what the bullets already say (explanations, examples, transition cues)
- Keep to fewer than 5 slides total
- Ground every bullet and speaking note in provided documents
- No filler bullets ("In conclusion...", "As we can see...")

## Quality Guidelines

Before submitting, verify every item passes:

- **Slide count**: The presentation contains at most 5 slides.
- **Grounding**: Every bullet point and speaking note must be traceable to the provided documents or research findings. Do not invent unsupported claims.
- **No filler content**: Remove generic bullets such as "In conclusion…" or "As we can see…" that add no informational value.
- **Title clarity**: Each slide title should clearly describe the content of that slide in fewer than 10 words.
- **Bullet conciseness**: Each bullet should be one sentence or phrase. If a bullet exceeds two lines, split it or shorten it.
- **Speaker notes depth**: Speaking notes must add context beyond what the bullets already say — explanations, examples, or transition cues.
