---
name: interview
description: A grounded 45-minute interview question kit with time-boxed questions and follow-up prompts.
---

## Instructions

Create a grounded 45-minute interview kit from the provided documents and research findings.

Requirements:
- Produce exactly 8 agenda items so the total planned time is exactly 45 minutes.
- Agenda structure must be:
  1. 5 minutes: self-introduction for interviewer and interviewee.
  2. 35 minutes: six interview questions, ordered by difficulty:
    - Q1-Q2: easy
    - Q3-Q4: harder
    - Q5-Q6: hardest
  3. 5 minutes: questions the interviewee asks the interviewer.
- Include a short topic string that describes the focus of the interview.
- Include a short objective (1-2 sentences) describing what the interview is meant to assess.
- Every agenda item must include: question, timebox_minutes (integer), potential_answer, follow_up.
- For the six interview questions, follow_up must probe depth (reasoning, tradeoffs, evidence, and practical application).
- Keep every question grounded in the provided materials; do not invent unsupported claims.
- If grounding is weak for a question, re-scope the question to match available evidence.
- When finished, use `write_file` to save your final output to `/final_report.md`.

## Output Format

Use `write_file` to save a well-structured markdown document to `/final_report.md`. Follow this exact structure:

```
# Interview Kit: {topic}

## 45-minute interview objective

{objective text}

## Agenda

1. **Timebox: 5 minutes**
   **Question:** {self-introduction question}
   **Potential Answer:** {brief outline}
   **Follow-up:** {small talk or logistics}

2. **Timebox: 5 minutes**
   **Question:** {Q1 — easy}
   **Potential Answer:** {outline grounded in materials}
   **Follow-up:** {probe depth — reasoning, tradeoffs, evidence}

... (all 8 items in order)

**Total planned time: 45 minutes**

## Grounding Reminder

Tie every question back to the documents and research findings.
```

## Format Requirements

- `topic`: A short string describing the interview focus
- `objective`: 1-2 sentences describing what the interview assesses
- Exactly 8 agenda items:
  1. 5-minute self-introduction (interviewer + interviewee)
  2-7. Six interview questions ordered by difficulty (Q1-Q2 easy, Q3-Q4 harder, Q5-Q6 hardest)
  8. 5-minute closing (interviewee asks interviewer questions)
- Each item must include: **question**, **timebox_minutes** (integer), **potential_answer** (outline of what a strong answer includes, grounded in materials), **follow_up**
- Total timebox_minutes must sum to exactly 45

## Quality Guidelines

Before submitting, verify every item passes:

- **Total time**: Sum of all timeboxes equals exactly 45.
- **Agenda count**: Exactly 8 agenda items are present.
- **Agenda structure**: Items follow the required order — 5-min intro, six interview questions, 5-min closing.
- **Difficulty progression**: The six interview questions escalate in difficulty (Q1-Q2 easy, Q3-Q4 harder, Q5-Q6 hardest).
- **Grounding**: Every question and potential answer must be traceable to the provided documents or research findings. Do not invent unsupported claims.
- **Potential answers**: Each potential_answer outlines what a strong response would include, grounded in the source materials — not a single-sentence restatement of the question.
- **Follow-up depth**: Each follow_up for interview questions probes reasoning, tradeoffs, evidence, or practical application — not a generic "Can you elaborate?".
- **Self-contained questions**: Each question must be understandable on its own without needing to read the previous question.
- **No filler**: Remove generic questions such as "Tell me about yourself" from the six interview slots; those belong only in the intro/closing items.
