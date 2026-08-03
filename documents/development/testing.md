# Test and check changes

Use this guide to choose the narrowest test layer that proves a change, then expand verification in proportion to risk. It is for contributors running the current pytest suite and optional development checks from the repository root.

## Prepare the environment

Install the runtime and test dependencies declared in `pyproject.toml`:

```bash
uv sync
```

Ruff and mypy are optional development dependencies. Install the `dev` extra before using those checks:

```bash
uv sync --extra dev
```

Commands below assume the repository root as the working directory.

## Choose the test layer

The suite does not use pytest markers to separate layers; select files directly.

| Layer | Use it for | Current examples |
| --- | --- | --- |
| Fast unit and contract tests | Pure helpers, prompts, and registry contracts that do not initialize a model or contact external services | `tests/test_retry_utils.py`, `tests/test_prompts_validation.py`, `tests/test_skill_registry.py` |
| Integration and mixed tests | Boundaries between components, file formats, persistence, or external adapters | `tests/test_code_ingestion_integration.py`, `tests/test_document_upload_api.py`, `tests/test_tools.py` |
| End-to-end tests | Complete CLI or agent workflows and their output contracts | `tests/test_research_agent_cli_e2e.py` |

`tests/test_tools.py` is not an offline-fast suite. It imports `agent.py`, so collection initializes the configured model, and it contains unskipped tests that make real HTTP requests to valid and invalid public URLs; configure a supported model provider and allow outbound DNS/HTTPS before running the whole file.

Start with one test or file, then run the related layer. This offline-safe focused example requires neither model credentials nor network access:

```bash
uv run pytest tests/test_retry_utils.py -v
```

Run broader layers after their prerequisites are available, and run the full suite before merging broad orchestration, shared-tool, dependency, or configuration changes:

```bash
uv run pytest tests/test_code_ingestion_integration.py -v
uv run pytest tests/test_research_agent_cli_e2e.py -v
uv run pytest tests/ -v
```

## Run focused research-quality tests

Use these files when changing prompts, report verification, or evaluation-history analysis:

```bash
uv run pytest tests/test_prompts_validation.py -v
uv run pytest tests/test_verification.py -v
uv run pytest tests/test_learning.py -v
```

Prompt assertion groups and representative node commands are documented in [Validate prompt changes](prompt-validation.md); its collection command is the authoritative node inventory. Evaluation models, artifacts, thresholds, and privacy controls are documented in [Evaluate research quality and regressions](../guides/evaluation.md).

## Measure coverage

Coverage requires the optional `pytest-cov` plugin, which is not declared in `pyproject.toml`. Run it in a temporary uv environment or install it in your own development environment:

```bash
uv run --with pytest-cov pytest tests/ \
  --cov=research_agent \
  --cov-report=term-missing \
  --cov-report=html
```

Open `htmlcov/index.html` locally for the annotated report. Treat coverage as a gap-finding aid; retain behavior-focused assertions rather than tests written only to increase a percentage.

## Lint Python

After installing the optional `dev` extra, run the Ruff configuration from `pyproject.toml`:

```bash
uv run ruff check .
```

Limit an edit-time check to changed packages or files when useful, then run the repository-wide command before merging.

## Type-check Python

Mypy is also in the optional `dev` extra. The repository has no project-specific mypy configuration, so this command uses mypy defaults and may expose existing findings outside the current change:

```bash
uv run mypy research_agent
```

## Compare golden-dataset runs

Golden-dataset regression tracking belongs to the scoring script, not `research_agent_cli.py`. Inspect its current interface first:

```bash
uv run python .deepagents/skills/golden-dataset/scripts/score_dataset.py --help
```

With model credentials configured, record a baseline and then score the same CSV and output directory as a candidate:

```bash
uv run python .deepagents/skills/golden-dataset/scripts/score_dataset.py \
  /path/to/golden-dataset.csv \
  --output-dir ./output/golden-eval \
  --eval-mode baseline

uv run python .deepagents/skills/golden-dataset/scripts/score_dataset.py \
  /path/to/golden-dataset.csv \
  --output-dir ./output/golden-eval \
  --eval-mode candidate
```

Add `--no-humanize` to exclude the optional humanizer pass from either run. See [Evaluate research quality and regressions](../guides/evaluation.md) for comparison semantics, generated artifacts, operational tracking, and verification limits.

## Related documentation

- [Extend the agent](extending-the-agent.md)
- [Validate prompt changes](prompt-validation.md)
- [Configuration](../guides/configuration.md)
- [Evaluate research quality and regressions](../guides/evaluation.md)
- [Architecture overview](../architecture/overview.md)
