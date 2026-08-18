# Gemma Clarification and Completion Runtime Design

## Problem

Two runtime boundaries fail under `gemma4:latest`:

1. `ClarificationBatch` can normalize Gemma's exact legacy shorthand, but the
   `clarify_requirements` structured tool validates its function annotation
   (`list[ClarificationQuestion]`) before that batch validator runs. The live
   tool call is therefore rejected even though direct batch validation passes.
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

Declare `ClarificationBatch` as `clarify_requirements`' explicit LangChain
`args_schema`. LangChain will then run the batch's existing `mode="before"`
normalizer before nested `ClarificationQuestion` validation. The tool body and
interrupt contract continue to receive canonical question objects.

The existing policy remains authoritative:

- a whole batch must be canonical or exact legacy shorthand;
- shorthand items contain only `question` and string `options`;
- IDs are deterministic and assigned after standalone `Other` removal rules;
- input mappings/lists are copied rather than mutated;
- malformed, mixed, or partially canonical payloads remain rejected.

Tests must validate `clarify_requirements.get_input_schema()` rather than only
calling `ClarificationBatch.model_validate()`. A runtime-oriented test will
also prove canonical output reaches the interrupt adapter.

### 2. Gemma Ollama reasoning policy

Add one model-factory policy for `ChatOllama.reasoning`:

- `OLLAMA_REASONING` explicitly set to a valid boolean wins for every Ollama
  model.
- When unset and normalized model name is in the `gemma4` family, pass
  `reasoning=False`.
- When unset for all other Ollama models, preserve the existing value `None`
  (provider/model default).
- Invalid explicit values fail fast with a configuration error rather than
  silently selecting a mode.

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

1. RED: real tool input schema rejects exact Gemma shorthand on current code.
2. GREEN: real tool schema normalizes shorthand and preserves strict rejection
   cases, replay safety, and canonical schema output.
3. RED/GREEN: model-factory tests prove Gemma default false, explicit true and
   false overrides, non-Gemma unset behavior, and invalid-value failure.
4. Run focused clarification, model-factory, agent-contract, completion-guard,
   and write-file suites plus Ruff/compile/diff checks.
5. Restart local LangGraph from merged `main`, verify port health, then use a
   fresh thread for the live test. Historical failed threads remain unchanged.

## Rollback

Set `OLLAMA_REASONING=true` to restore Gemma reasoning without code changes.
Reverting the explicit clarification `args_schema` restores the old boundary,
but is not recommended because it reintroduces the reproduced validation bug.
