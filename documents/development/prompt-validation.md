# Validate prompt changes

Use this guide to understand and run the executable contracts in `tests/test_prompts_validation.py`. It is for contributors changing orchestration, researcher, delegation, or report-writing instructions in `research_agent/prompts.py`.

## Run the prompt contract

Run all 37 tests:

```bash
uv run pytest tests/test_prompts_validation.py -v
```

Run one class or node while editing:

```bash
uv run pytest tests/test_prompts_validation.py::TestDelegationStrategy -v
uv run pytest tests/test_prompts_validation.py::TestHardLimits::test_hard_limits_document_search_tool_budgets -v
```

Use pytest collection when checking that this guide still matches the executable inventory:

```bash
uv run pytest tests/test_prompts_validation.py --collect-only -q
```

## Review the current inventory

The module currently collects 37 tests in seven classes.

| Class | Tests | Assertions protected |
| --- | ---: | --- |
| `TestResearcherInstructionsToolDescriptions` | 5 | Exact tool names plus `web search`, `webpage`, and `Available Research Tools`; `think_tool` also needs `CRITICAL` or reflection language, and descriptions need search, `retriev`, and reflection or strategic terms. |
| `TestDelegationStrategy` | 7 | `DEFAULT` and `1 sub-agent`; quantum-computing and overview examples; comparison/parallel terms; `Compare` plus OpenAI or Python; `Key Principles`, single-agent, token-efficiency, avoidance, and parallel-limit terms. |
| `TestHardLimits` | 7 | `Hard Limits`; simple `2-3` and complex `5` budget alternatives; search wording; stop, answer, and source terms; source-count alternatives; similar-search warning; `Research Limits` plus an iteration term. |
| `TestThinkToolGuidance` | 6 | `think_tool` with `CRITICAL`; when-to-use, reflection, gap or missing, quality or evidence, and strategic or continue-decision alternatives. |
| `TestInstructionsCohesion` | 5 | `tavily_search` in at least one workflow/researcher instruction; all three strings over 100 characters and containing `#`; exact delegation placeholders; `{skill_catalog}` or researcher instructions over 500 characters. |
| `TestReportWritingGuidelines` | 4 | `Report Writing Guidelines`; numbered-citation and `Source` terms; comparison, list, and summary or overview terms; a self-reference warning or prohibited example. |
| `TestExecutionRules` | 3 | `NEVER ask` or `Do NOT ask`; pause or immediate-action language; `write_todos` or completion language. |

## Update the contract safely

Add or change the focused assertion before editing a prompt, confirm the node fails for the intended missing behavior, then make the smallest instruction change that passes it. Keep placeholders such as `{max_concurrent_research_units}`, `{max_researcher_iterations}`, and `{skill_catalog}` intact when the corresponding runtime formatting still depends on them.

Prompt tests assert required phrases and structure, not end-to-end research quality. Use [Test and check changes](testing.md) for broader checks and [Extend the research agent](extending-the-agent.md) for instruction ownership and assembly paths.

## Related documentation

- [Test and check changes](testing.md)
- [Extend the research agent](extending-the-agent.md)
- [Evaluate research quality and regressions](../guides/evaluation.md)
- [Architecture overview](../architecture/overview.md)
- [Configuration](../guides/configuration.md)
