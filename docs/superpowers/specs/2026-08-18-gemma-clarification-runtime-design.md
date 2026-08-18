# Gemma Clarification and Completion Runtime Design

## Problem

Two runtime boundaries fail under `gemma4:latest`:

1. `ClarificationBatch` can normalize Gemma's exact legacy shorthand, and is
   already the tool's explicit `args_schema`. However, LangChain derives a
   subset `tool_call_schema` and drops model-level validators while doing so.
   Live ToolNode validation therefore rejects shorthand even though
   `get_input_schema()` and direct batch validation pass.
2. After successful delegated research, Gemma can spend its response budget in
   Ollama's reasoning channel and return no visible content or tool calls. The
   completion guard correctly retries three times, but every retry makes no
   progress; todos remain open and `/final_report.md` remains missing.

Live evidence from thread `01a0161d-f0dc-7192-8c26-988db7e7edbd` showed the
real shorthand validation error, three successful `task` calls, and four final
Gemma responses with generated tokens but zero visible content/tool calls.

## Goals

- Make the real LangChain tool boundary use the existing immutable, strict
  `ClarificationBatch` normalization policy.
- Keep canonical clarification payloads strict and unchanged.
- Make Gemma's default Ollama configuration produce actionable visible output
  for tool-driven orchestration.
- Preserve the completion guard's three-attempt fail-closed behavior.
- Provide an explicit environment override for users who want Gemma reasoning.

## Non-goals

- Do not accept arbitrary dictionaries or mixed canonical/shorthand batches.
- Do not hide historical failed tool calls or mark incomplete work complete.
- Do not increase the continuation limit.
- Do not change non-Gemma Ollama defaults when no override is configured.

## Design

### 1. Real clarification tool schema

Move shorthand normalization from a batch-level
`model_validator(mode="before")` to a field-level `BeforeValidator` embedded
in the `questions` annotation. LangChain's subset-model construction preserves
annotated field metadata, so both `get_input_schema()` and the actual
`tool_call_schema` normalize before nested `ClarificationQuestion` validation.
Keep `ClarificationBatch` as the explicit `args_schema`; the tool body and
interrupt contract continue to receive canonical question objects.

The existing policy remains authoritative:

- a whole batch must be canonical or exact legacy shorthand;
- shorthand items contain only `question` and string `options`;
- IDs are deterministic and assigned after standalone `Other` removal rules;
- input mappings/lists are copied rather than mutated;
- malformed, mixed, or partially canonical payloads remain rejected.

Tests must validate `clarify_requirements.tool_call_schema`, not only
`ClarificationBatch.model_validate()` or `get_input_schema()`. A real ToolNode
invoke/interrupt/resume/replay test must pass the exact observed shorthand,
prove the interrupt contains canonical objects, and prove replay is stable.

### 2. Gemma Ollama reasoning policy

Add one model-factory policy for `ChatOllama.reasoning`, evaluated only inside
the Ollama branch after Ollama wins existing provider precedence:

- Explicit input is stripped and case-folded. `1`, `true`, `yes`, and `on`
  mean true; `0`, `false`, `no`, and `off` mean false. Empty or any other value
  raises `ValueError("OLLAMA_REASONING must be a boolean")`; raw input is not
  echoed.
- Family matching strips/case-folds the model name, takes the final
  slash-delimited repository component, removes its final colon-delimited tag,
  and requires repository equality with `gemma4`. Thus `gemma4:latest`,
  `team/gemma4:27b`, and `REGISTRY/TEAM/GEMMA4:LATEST` match; `gemma40`,
  `my-gemma4`, and `gemma4x` do not.
- With unset override and a match, pass `reasoning=False`.
- With unset override and a non-match, omit the `reasoning` keyword entirely
  to preserve current constructor behavior exactly.

This is intentionally model-scoped: it fixes observed Gemma all-reasoning,
zero-action responses without changing Qwen, DeepSeek, or other Ollama models.
Users can restore Gemma reasoning with `OLLAMA_REASONING=true`.

### 3. Completion behavior

No completion-guard relaxation. A run is accepted only when all todos are
completed and a generation-owned, non-empty `/final_report.md` exists. Empty
model output still counts as no progress and exhausts at the existing limit.
The model configuration fix prevents the observed Gemma response mode from
feeding that path during normal tool orchestration.

## Error handling

- Clarification schema failures remain ordinary structured-tool validation
  errors with canonical field paths.
- Invalid `OLLAMA_REASONING` fails during model construction with a fixed,
  non-secret-bearing message.
- The completion guard remains the final fail-closed boundary if Gemma or any
  other model still produces no actionable output.

## Verification

1. RED: `tool_call_schema` and actual ToolNode execution reject the exact
   observed Gemma shorthand on current code.
2. GREEN: `tool_call_schema` and ToolNode interrupt/resume/replay normalize
   shorthand and preserve strict rejection cases and canonical output.
3. RED/GREEN: model-factory tests cover exact/tagged/namespaced/case-folded
   Gemma matches; false-positive names; every override literal; whitespace;
   fixed empty/invalid failures; unset non-Gemma keyword omission; provider
   precedence with stale invalid Ollama configuration; and cache environment
   isolation by adding `OLLAMA_REASONING` to `_PROVIDER_ENV`.
4. Run focused clarification, model-factory, agent-contract, completion-guard,
   and write-file suites plus Ruff/compile/diff checks.
5. Document `OLLAMA_REASONING` in environment example/configuration docs.
6. Restart local LangGraph from merged `main`, verify port health, then use a
   fresh thread with unset override and exact `gemma4:latest`. Acceptance
   requires shorthand crossing the actual tool boundary, a canonical
   clarification interrupt, visible follow-up tool calls after resume, all
   todos completed, and a generation-owned non-empty `/final_report.md`.
   Historical failed threads remain unchanged.

## Rollback

Set `OLLAMA_REASONING=true`, then restart LangGraph or call
`clear_model_cache()` before the next model construction, to restore Gemma
reasoning without code changes. Reverting the field-level validator restores
the old boundary but reintroduces the reproduced validation bug.
