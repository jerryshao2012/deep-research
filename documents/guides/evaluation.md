# Evaluate research quality and regressions

Use this guide to score golden datasets, compare like-for-like runs, inspect operational trends, and understand report verification. It distinguishes the executable scoring pipeline from programmatic agent-run comparisons and best-effort server metrics so their artifacts and verdicts are not mixed.

## Check prerequisites

Install development dependencies with `uv sync --extra dev`. Golden-dataset scoring requires an input CSV accepted by the golden-dataset metrics scripts plus a configured model for LLM judging and optional humanization; operational metrics require a run that writes `/final_report.md`.

Hold non-experimental factors such as input, model, and relevant configuration stable while changing the factor under evaluation. Record both baseline and candidate code revisions so a code-change evaluation remains attributable; a comparison is meaningful only when its manifest represents the same test case.

## Choose the evaluation layer

| Layer | Current entry point | Purpose |
| --- | --- | --- |
| Golden-dataset scoring | `.deepagents/skills/golden-dataset/scripts/score_dataset.py` | Score an existing CSV, render artifacts, and record a simple baseline or candidate. |
| Agent-run regression utilities | `research_agent.utils.eval_tracking` | Collect orchestration metrics and compare records with thresholds. |
| Operational tracking | Agent middleware plus `ENABLE_EVAL_TRACKING` | Append facts from diverse server runs without baseline comparison. |
| Report verification | Agent middleware plus `ENABLE_VERIFICATION` | Ground citations, judge sufficiency, find gaps, and request revision. |

The main `research_agent_cli.py` no longer accepts `--eval-golden-dataset`, `--eval-mode`, or `--eval-history-file`. Generate a golden dataset with that CLI if needed, then use the scoring script for its supported baseline/candidate workflow.

## Compare a golden-dataset baseline and candidate

### Inspect the scoring command

```bash
uv run python .deepagents/skills/golden-dataset/scripts/score_dataset.py --help
```

The pipeline performs LLM scoring, converts metrics to Markdown, generates a report, optionally humanizes it, and writes artifacts. Use `--no-humanize` when you need to exclude that extra model transformation.

### Record a baseline

```bash
uv run python .deepagents/skills/golden-dataset/scripts/score_dataset.py \
  /path/to/golden-dataset.csv \
  --output-dir ./output/golden-eval \
  --eval-mode baseline
```

### Evaluate a candidate

Run the same input path and output directory after the candidate change:

```bash
uv run python .deepagents/skills/golden-dataset/scripts/score_dataset.py \
  /path/to/golden-dataset.csv \
  --output-dir ./output/golden-eval \
  --eval-mode candidate
```

The candidate searches backward for the latest baseline with the same manifest hash. If none exists, it is recorded without a comparison.

### Inspect scoring artifacts

For input `<stem>.csv`, the output directory receives:

- `<stem>-with-metrics.csv`, containing scored rows;
- `<stem>_metrics.md`, containing the rendered metrics table;
- `<stem>_report.md`, containing the generated report;
- `eval_history/golden_dataset_runs.jsonl`, when `--eval-mode` is present.

The scoring manifest contains absolute input path, input byte size, mode, timestamp, and a hash. Hashing excludes mode and timestamp, so a baseline and candidate compare only when path and size match.

This script's run metrics are deliberately small: runtime, metrics-CSV existence and size, and report existence and size. Candidate comparison ignores runtime; any difference in the remaining values yields `regression_detected`, otherwise `unchanged`. This verdict is not the richer threshold comparison described below.

## Measure agent-run behavior programmatically

`research_agent.utils.eval_tracking` provides a separate record model for complete agent results. Use it when an evaluation harness can retain the LangGraph result, runtime, stream-fallback status, output path, model, and Git SHA.

The supported sequence is:

1. call `build_manifest(...)` and keep the subject, skill, document folder, web mode, model, and TLS setting identical;
2. call `collect_run_metrics(result, runtime_seconds, stream_fallback_used)`;
3. create a `baseline` or `candidate` with `make_run_record(...)`;
4. persist through `append_jsonl(...)` and reload with `load_jsonl(...)`;
5. find `latest_baseline(records, record["manifest_hash"])`;
6. call `compare_records(baseline=..., candidate=...)`.

The canonical SHA-256 manifest hash prevents comparisons such as “generate 5 pairs” against “generate 10 pairs.” No matching baseline or a hash mismatch returns `non-comparable` rather than a pass.

### Understand collected metrics

| Category | Key measures |
| --- | --- |
| Completeness | The collector expects both `/golden_dataset_metrics.md` and `/final_report.md`; this gate is designed for golden-dataset results. |
| Tool execution | Total, successful, and failed calls; success rate; retries; distinct errored and corrected tools. |
| Parameter validation | Required-parameter rate, average heuristic quality score, analyzed calls, missing-parameter count. |
| Failure | Whether intervention was required and the resulting binary failure rate. |
| Self-correction | Retry/alternative-tool events, corrected-tool rate, tools, and correction types. |
| Token efficiency | Prompt, completion, and total tokens when message usage metadata is available. |
| Latency | End-to-end runtime in seconds. |

Tool output is treated as failed when empty or beginning with a known error prefix such as invalid JSON, schema validation failure, unknown skill, invocation error, or `ERROR:`. Parameter quality is a lightweight structural heuristic, not semantic proof that a tool argument is correct.

### Apply regression thresholds

`compare_records` uses these defaults:

| Metric | Candidate is worse when |
| --- | --- |
| Completeness | Baseline passed and candidate failed. |
| Tool execution | Total calls grow by more than 30%, unless completeness improved from fail to pass. |
| Failure | Failure rate increases. |
| Token efficiency | Total tokens increase by more than 20%, when usage exists in both runs. |
| Latency | Runtime increases by more than 15%. |
| Parameter validation | Average quality falls below 90% of baseline; above 110% is better. |
| Self-correction | Correction rate decreases; any increase is better. |

Any `worse` metric makes the overall verdict worse. Otherwise, any `better` metric makes it better; all equal metrics produce same. Token efficiency is `unavailable` unless both records captured usage metadata.

Run focused contract tests with:

```bash
uv run pytest tests/test_eval_tracking.py -v
```

## Track server operations

Enable automatic fact collection for `langgraph dev` or a long-running server:

```dotenv
ENABLE_EVAL_TRACKING=true
EVAL_HISTORY_FILE=./output/eval_history/server_runs.jsonl
EVAL_LOG_QUESTIONS=false
```

On the first middleware observation of `/final_report.md`, `_eval_logged` is set and the current state snapshot supplies tool execution, parameter quality, self-correction, token usage, latency, model, selected skill, document folder, web mode, and output file names. This snapshot can occur in the same middleware pass that requests a verification revision, so later revision messages, tokens, and latency may be omitted; no second record is written after `_eval_logged` becomes true.

Persistence differs by middleware path. Synchronous `after_model` schedules `log_server_metrics()` as a background task and does not await its file write, so the process must remain alive to flush it; asynchronous `aafter_model` awaits `log_server_metrics()` and its offloaded file write before returning. The logging function catches its own failures in either path, making this best-effort telemetry rather than a research-response gate; monitor logging errors.

### Analyze trends

Analyze recent JSONL files and render improvement suggestions:

```bash
uv run python -c "from pathlib import Path; from research_agent.utils.learning import analyze_eval_trends, generate_improvement_suggestions; a = analyze_eval_trends(Path('./output/eval_history')); print(generate_improvement_suggestions(a))"
```

`analyze_eval_trends` summarizes record count, average tool success, tokens, latency, failure patterns, topic buckets, and experiments over a time window. `compute_baseline_from_history` can calculate a median success-rate baseline across a bounded recent window; it is separate from `score_dataset.py`'s simple artifact comparison.

## Run experiments

Set experiment labels independently on each deployment:

```dotenv
EXPERIMENT_ID=prompt-v2
EXPERIMENT_VARIANT=control
```

Use the same `EXPERIMENT_ID` and a distinct variant such as `treatment` on the candidate deployment. Operational records include these fields, and trend analysis groups variants within the experiment; the variables do not randomize traffic, balance samples, or establish statistical significance.

For programmatic records, `make_run_record` also accepts `experiment_id`, `variant`, and `prompt_version` explicitly.

## Operate the verification loop

Verification runs after the model writes a non-empty `/final_report.md` and stops emitting tool calls:

1. citation grounding parses numbered web sources and validates reachability and claim support;
2. an LLM judge scores completeness, factual consistency, citation coverage, and depth;
3. an adversarial LLM review finds substantial missing perspectives or unsupported reasoning;
4. a failing verdict injects structured feedback and asks the model to overwrite `/final_report.md`.

```dotenv
ENABLE_VERIFICATION=true
MAX_VERIFICATION_ROUNDS=2
```

A checked report version receives a `complete` verdict when no checked citation fails, sufficiency is at least `0.7`, and adversarial review returns at most one gap. To bound cost, at most five citations are randomly spot-checked and evaluator prompts inspect at most the first 8,000 report characters.

Verification is a bounded best-effort guard, not certification. Each threshold verdict applies only to the version checked: when the final permitted check requests another revision, the model can produce that last revision after `MAX_VERIFICATION_ROUNDS` is reached and deliver it without another verification pass. If the composite check raises or times out, middleware logs a warning and allows the report through; individual evaluator fallbacks can also be conservative or incomplete. Lower the round cap to reduce latency, or disable the loop for controlled measurements that must exclude revision cost.

Run its focused tests with:

```bash
uv run pytest tests/test_verification.py -v
```

## Protect evaluation privacy

`EVAL_LOG_QUESTIONS=false` replaces the user subject with `[REDACTED]`; this is the runtime default. Operational records still contain model name, skill, document-folder value, output file names, timing, and aggregate tool behavior, so place history files in access-controlled storage and avoid sensitive path names.

Do not place API keys, OAuth/session tokens, source document contents, or raw model messages into custom manifests. Before sharing JSONL, review optional experiment labels and Git/output metadata as well as the subject field.

Golden-dataset scoring stores the absolute input CSV path and file size in its manifest. Use a non-sensitive evaluation path if history will be retained or exported.

## Future work

The following backlog remains actionable but is not implemented as a reliable current gate:

- add trace contracts, first-violation localization, deterministic replay, bounded stress tests, service/retrieval/memory fault injection, and runtime capability/action mediation;
- measure plan quality, plan adherence, decomposition accuracy, handoff success, redundant calls, cross-agent consistency, and loop detection;
- add golden-dataset grounding, schema/negative-constraint adherence, finalization latency, cost-per-success, compression, and time-to-first-action metrics;
- harden `.github/workflows/eval-regression.yml` with an explicit input CSV, persisted baseline/candidate data, and a candidate-producing step before treating it as an enforcing CI regression gate.

## Related documentation

- [Configuration](configuration.md)
- [Reliability](reliability.md)
- [Usage](../getting-started/usage.md)
- [Prompt validation](../development/prompt-validation.md)
- [Handbook index](../README.md)
