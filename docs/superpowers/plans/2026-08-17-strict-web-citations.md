# Strict Web Citation Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent web-enabled research reports with missing, placeholder, or unresolved citations from being streamed or marked verified, while preserving no-web and document-only runs.

**Architecture:** Add a pure deterministic Markdown citation policy before optional LLM judging. Carry web-mode through an ephemeral input channel into hidden per-generation state, then use a two-phase citation-failure transition so defect metadata checkpoints before a safe `ReportCitationError` is raised. Structural citation acceptance remains active even when LLM verification is disabled or configured for zero rounds.

**Tech Stack:** Python 3.12+, LangGraph state channels/middleware, LangChain messages, `urllib.parse`, dataclasses, pytest/pytest-asyncio.

**Dependency:** Execute `2026-08-17-model-call-timeout.md` first. This plan extends the verifier/CLI control-error branches introduced there and must not reintroduce blocking executor or fail-open timeout behavior.

---

## File map

- Create `research_agent/research_subagent/utils/citation_policy.py` — pure Markdown audit, URL normalization, placeholder-host rules, source/reference resolution, and bounded defects.
- Modify `research_agent/research_subagent/utils/verification.py:68-363` — citation-blocking verdict fields and early deterministic preflight.
- Create `research_agent/citation_failure.py` — checkpoint-safe failure state, fingerprint/run correlation, safe error, and transition helpers.
- Modify `research_agent/agent.py:89-95,125-168,311-379,459-478,532-610,693-1066,1068-1115,1255-1274` — ephemeral raw `no_web`, effective per-generation mode, strict verification flow, two-phase failure hooks, and finalization gating.
- Modify `research_agent/completion_guard.py:344-390` — refuse finalization while a matching citation failure is pending.
- Modify `research_agent/cli.py:457-568` — propagate hard citation failures without fallback invoke/finalization retry.
- Create `tests/test_citation_policy.py` — deterministic grammar matrix.
- Modify `tests/test_verification.py` — early gate, zero-round behavior, sync/async revision/failure transitions, and non-streaming.
- Modify `tests/test_completion_guard.py` — pending citation failure blocks finalization.
- Modify `tests/test_agent_contracts.py` — ephemeral channel and hook registration.
- Modify `tests/test_citations.py` — compatibility with existing citation validation.
- Modify `tests/test_research_agent_cli_e2e.py` — hard citation failure exits once without fallback.
- Modify `documents/guides/reliability.md` and `documents/guides/evaluation.md` — strict acceptance behavior and exemptions.

### Task 1: Pure citation grammar and defect model

**Files:**
- Create: `research_agent/research_subagent/utils/citation_policy.py`
- Create: `research_agent/citation_failure.py`
- Create: `tests/test_citation_policy.py`

- [ ] **Step 1: Write RED tests for accepted URL forms**

Cover source headings `Sources`, `References`, `Bibliography`, and `Works Cited`; heading boundary termination; `[1]`, `1.`, and `[1]: URL` entries; Markdown links; bare URLs; inline `[1](URL)`; duplicate URLs; and trailing punctuation.

```python
def test_numbered_reference_resolves_to_url_source():
    audit = audit_web_citations("Claim [1].\n\n## Sources\n[1] NIST: https://nist.gov/ai")
    assert audit.urls == ("https://nist.gov/ai",)
    assert audit.defects == ()
```

- [ ] **Step 2: Write RED tests for deterministic failures**

Required cases:

- no concrete URL;
- `Conceptual Source`, `placeholder`, `example source`, `source needed`, `citation needed`, and `TBD` in recognized entries/links;
- exact and subdomain forms of `example.com`, `example.org`, `example.net`, `localhost`;
- `.example`, `.invalid`, `.test`, and `.localhost` suffixes;
- missing authority or non-HTTP(S) scheme;
- unresolved singles, groups `[1, 3; 5]`, and expanded ranges `[2-4]`;
- descending/malformed ranges produce bounded structural defect rather than expansion blow-up;
- reference-like text in code fences, source entries, escaped text, and Markdown link labels is ignored;
- non-URL document/book/file entries are allowed but do not satisfy minimum web URL.

- [ ] **Step 3: Run parser tests and confirm RED**

Run: `uv run pytest tests/test_citation_policy.py -q`

Expected: collection failure because citation policy module is absent.

- [ ] **Step 4: Implement pure policy types and parser**

```python
@dataclass(frozen=True, order=True)
class CitationDefect:
    code: Literal["missing_url", "placeholder_source", "unresolved_reference", "malformed_reference"]
    detail: str

@dataclass(frozen=True)
class CitationAudit:
    urls: tuple[str, ...]
    defects: tuple[CitationDefect, ...]

def audit_web_citations(report: str) -> CitationAudit: ...

class ReportCitationError(RuntimeError):
    """Safe terminal error for structurally invalid web citations."""
```

Implementation order:

1. replace fenced code spans with equal-length whitespace;
2. identify recognized source-section line ranges by Markdown heading level;
3. parse valid link destinations and bare URLs with `urlsplit`;
4. reject the exact reserved host set/subdomains;
5. map numbered source entries to concrete normalized URLs;
6. remove source/link/escaped spans before scanning prose numeric groups/ranges;
7. expand only bounded 1–999 ascending ranges;
8. sort/deduplicate URLs and defects deterministically; and
9. cap user-facing defect details/reference numbers to a small fixed count while retaining defect code.

Use no network calls and no LLM.

- [ ] **Step 5: Run parser tests and confirm GREEN**

Run: `uv run pytest tests/test_citation_policy.py -q`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add research_agent/research_subagent/utils/citation_policy.py research_agent/citation_failure.py tests/test_citation_policy.py
git commit -m "feat: audit report citation structure"
```

### Task 2: Verification preflight independent of LLM judges

**Files:**
- Modify: `research_agent/research_subagent/utils/verification.py:68-363`
- Modify: `tests/test_verification.py`
- Modify: `tests/test_citations.py`

- [ ] **Step 1: Write RED preflight tests**

```python
verdict = await verify_report(question="q", report=bad_report, strict_web_citations=True)
assert verdict.status == "needs_revision"
assert verdict.citation_blocking is True
assert verdict.citation_defects == expected
assert sufficiency_model_calls == 0
assert adversarial_model_calls == 0
assert grounding_network_calls == 0
```

Also prove a valid strict report proceeds to existing grounding/sufficiency/adversarial checks, while `strict_web_citations=False` preserves current behavior for no-web/document-only content. Every URL form accepted by `CitationAudit` must be converted to `SourceCitation(kind="web", url=url)` and passed to `validate_web_citations`; the old narrow numbered-entry extractor must not be the source of truth.

- [ ] **Step 2: Run preflight slice and confirm RED**

Run: `uv run pytest tests/test_verification.py tests/test_citations.py -q -k 'strict or placeholder or unresolved or preflight'`

- [ ] **Step 3: Extend verdict and add early gate**

```python
@dataclass(frozen=True)
class VerificationVerdict:
    ...
    citation_blocking: bool = False
    citation_defects: tuple[CitationDefect, ...] = ()

async def verify_report(..., strict_web_citations: bool = False):
    if strict_web_citations:
        audit = audit_web_citations(report)
        if audit.defects:
            return VerificationVerdict(
                status="needs_revision",
                sufficiency_score=0.0,
                sufficiency_reason="Citation structure must be corrected.",
                citation_blocking=True,
                citation_defects=audit.defects,
            )
        citations = [SourceCitation(kind="web", url=url) for url in audit.urls]
    else:
        citations = _extract_citations_from_report(report)
    ...
```

Update `format_feedback()` to render bounded exact messages: missing concrete HTTP(S) URL, placeholder entry label, unresolved reference numbers, and malformed group/range. Do not include report prose.

- [ ] **Step 4: Preserve control exceptions**

Retain the timeout plan's async judge calls and explicit `ModelCallTimeoutError`/`asyncio.CancelledError` pass-through. Import the now-existing `ReportCitationError` and explicitly re-raise it in composite verification paths. Ordinary judge/parser errors retain existing fallback behavior.

- [ ] **Step 5: Run verification/citation suites and confirm GREEN**

Run: `uv run pytest tests/test_citation_policy.py tests/test_verification.py tests/test_citations.py -q`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add research_agent/research_subagent/utils/verification.py research_agent/research_subagent/utils/citation_policy.py tests/test_verification.py tests/test_citations.py
git commit -m "feat: enforce citation preflight before judges"
```

### Task 3: Ephemeral request web mode

**Files:**
- Modify: `research_agent/agent.py:459-478,532-610,1068-1115,1255-1274`
- Modify: `tests/test_agent_contracts.py`
- Modify: `tests/test_verification.py`

- [ ] **Step 1: Write RED checkpointed transition tests**

Compile a minimal graph with `InMemorySaver` and the production state/middleware. On one thread prove:

1. input `no_web=true` → effective no-web true → strict citations false;
2. next input omits `no_web` → raw channel absent → effective no-web false → strict citations true;
3. false→true and true→false overwrite correctly; and
4. prior `no_web=true` followed by a visible explicit-resume run with omission resets to false;
5. natural-language `no web` and `with web` directives still work when raw input is omitted; and
6. completion/verification internal `jump_to: model` retains the current effective value.

Also prove document context + effective no-web true is exempt, while document context + effective no-web false remains strict.

- [ ] **Step 2: Run transition tests and confirm RED**

Run: `uv run pytest tests/test_agent_contracts.py tests/test_verification.py -q -k 'no_web or web_mode or document_only'`

- [ ] **Step 3: Add ephemeral raw channel and hidden effective fields**

```python
from typing import Annotated, NotRequired
from langgraph.channels import EphemeralValue
from langchain.agents.middleware.types import OmitFromInput

class ResearchState(CompletionState):
    no_web: Annotated[NotRequired[bool | None], EphemeralValue(bool | None)]
    effective_no_web: Annotated[NotRequired[bool], OmitFromInput]
    strict_web_citations: Annotated[NotRequired[bool], OmitFromInput]
    web_mode_run_id: Annotated[NotRequired[str | None], OmitFromInput]
```

In every visible `before_agent`, including explicit incomplete-todo resume, resolve web mode with this precedence: supplied ephemeral `no_web` value; current user message's `_extract_no_web()` directive; public default false. Overwrite all three effective fields before the existing resume branch. `_extract_parameters_from_user_input()` must stop writing raw `no_web`; it may return the textual value to the resolver only. Internal model jumps do not rerun `before_agent`, so they retain current effective fields. Replace prompt, tool eligibility, verification, and metrics reads of raw `no_web` with `effective_no_web`.

- [ ] **Step 4: Run checkpointed transition tests and confirm GREEN**

Run: `uv run pytest tests/test_agent_contracts.py tests/test_verification.py -q -k 'no_web or web_mode or document_only'`

Expected: pass without frontend/client changes.

- [ ] **Step 5: Commit**

```bash
git add research_agent/agent.py tests/test_agent_contracts.py tests/test_verification.py
git commit -m "fix: scope web mode to request generation"
```

### Task 4: Checkpoint-safe final citation failure

**Files:**
- Modify: `research_agent/citation_failure.py`
- Modify: `research_agent/agent.py:89-95,311-379,459-478,693-1066`
- Modify: `research_agent/completion_guard.py:344-390`
- Modify: `research_agent/cli.py:457-568`
- Modify: `tests/test_verification.py`
- Modify: `tests/test_completion_guard.py`
- Modify: `tests/test_research_agent_cli_e2e.py`

- [ ] **Step 1: Write RED pure transition tests**

Required state contract:

```python
class CitationFailureState(TypedDict, total=False):
    citation_failure_run_id: Annotated[NotRequired[str | None], OmitFromInput]
    citation_failure_report_fingerprint: Annotated[NotRequired[str | None], OmitFromInput]
    citation_failure_defects: Annotated[NotRequired[tuple[dict[str, str], ...]], OmitFromInput]
    citation_accepted_report_fingerprint: Annotated[NotRequired[str | None], OmitFromInput]
```

Test current run/fingerprint match raises `ReportCitationError`; stale run, changed report, malformed defects, or explicit new run clears/ignores failure. Error text contains only bounded defect codes/reference numbers.

- [ ] **Step 2: Write RED compiled two-phase tests**

For sync and async compiled graphs with a checkpointer:

- final correction attempt returns `jump_to: end` and checkpoints failure metadata plus intermediate terminal tag;
- report is not streamed, `_streamed_files` remains unchanged, verified/accepted-at-limit fingerprints remain unset;
- first invalid report cannot stream or log metrics even when optional verification is disabled;
- valid structural audit stores current report fingerprint before finalization;
- `after_agent`/`aafter_agent` raises `ReportCitationError` only after the checkpoint exists;
- history restore exposes safe failure state; and
- next explicit run clears stale state and can succeed with a corrected report.

- [ ] **Step 3: Run failure tests and confirm RED**

Run: `uv run pytest tests/test_verification.py tests/test_completion_guard.py -q -k 'citation_failure or citation_blocking'`

- [ ] **Step 4: Implement focused failure helpers**

```python
class ReportCitationError(RuntimeError): ...

def build_citation_failure_update(*, run_id, fingerprint, defects, terminal): ...
def clear_stale_citation_failure(state, current_run_id): ...
def raise_if_current_citation_failure(state, current_run_id): ...
def citation_failure_blocks_finalization(state) -> bool: ...
def citation_acceptance_ready(state, *, required: bool) -> bool: ...
```

Use runtime `execution_info.run_id` as authoritative, then top-level configured run ID, matching completion-guard resolution. Store only serialized bounded defects and fingerprint, never report text.

- [ ] **Step 5: Wire two-phase hooks and finalization block**

On a clean strict audit, store `citation_accepted_report_fingerprint` for the current owned report. On any invalid or changed report, the marker is absent/mismatched. In `_apply_verification_verdict`, citation-blocking final attempt must not populate accepted-at-limit fields; it writes failure update, tags terminal intermediate, and jumps to end. Add `after_agent`/`aafter_agent` hooks that call the pure raise helper. Both `completion_ready_for_finalization` and `finalize_accepted_report` require a matching structural-acceptance fingerprint whenever strict citations apply, and return false/`None` for pending citation failure. Use this same gate for metrics logging.

In CLI stream handling, catch `ReportCitationError` before generic `Exception`, stop the spinner, and re-raise/exit with its safe message. Do not call fallback `agent.invoke`, `should_retry_with_invoke`, title generation, or file save after this error. Add verbose and non-verbose tests asserting one graph attempt.

- [ ] **Step 6: Run failure/finalization tests and confirm GREEN**

Run: `uv run pytest tests/test_verification.py tests/test_completion_guard.py tests/test_research_agent_cli_e2e.py -q -k 'citation or finalization or stream'`

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add research_agent/citation_failure.py research_agent/agent.py research_agent/completion_guard.py research_agent/cli.py tests/test_verification.py tests/test_completion_guard.py tests/test_research_agent_cli_e2e.py
git commit -m "fix: fail closed on unresolved web citations"
```

### Task 5: Enforce structural gate when LLM verification is disabled

**Files:**
- Modify: `research_agent/agent.py:125-168,693-1066`
- Modify: `tests/test_verification.py`
- Modify: `tests/test_verification_progress.py`

- [ ] **Step 1: Write RED configuration matrix tests**

Parameterize `ENABLE_VERIFICATION` true/false and `MAX_VERIFICATION_ROUNDS` 0/1/2. For strict web runs:

- valid report finalizes immediately when optional LLM verification is off;
- invalid report gets initial check → exactly one structural correction request when off/zero → second check/failure if still invalid;
- with positive rounds, `MAX_VERIFICATION_ROUNDS` counts allowed structural corrections, followed by one final check/failure;
- non-structural judge `needs_revision` may retain accepted-at-limit behavior;
- no-web/document-only runs retain current zero-round finalization.

- [ ] **Step 2: Run matrix and confirm RED**

Run: `uv run pytest tests/test_verification.py tests/test_verification_progress.py -q -k 'disabled or zero_round or structural'`

- [ ] **Step 3: Separate structural and optional verification policy**

Create helpers:

```python
def _structural_citation_required(state) -> bool: ...
def _optional_llm_verification_enabled() -> bool: ...
def _citation_correction_limit() -> int:
    return max(MAX_VERIFICATION_ROUNDS, 1)
```

Track `citation_corrections_used` separately from `verification_round`. On invalid audit, if `used < limit`, increment `used`, inject feedback, tag terminal intermediate, and jump to model. If `used == limit`, checkpoint terminal citation failure. Thus zero/disabled mode performs two checks around one correction, while configured value `2` permits two corrections and fails only on the third invalid check. Reset the counter for each visible request generation and after structural acceptance of a changed report.

Run strict preflight whenever owned report is ready and its structural-acceptance fingerprint does not match. Invoke judges only when optional verification is enabled. Progress labels must distinguish `Citation correction 1/1 requested` from `Verification n/N` and never claim verified on blocking failure.

- [ ] **Step 4: Run matrix and adjacent regressions**

Run: `uv run pytest tests/test_verification.py tests/test_verification_progress.py tests/test_completion_guard.py tests/test_citations.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add research_agent/agent.py tests/test_verification.py tests/test_verification_progress.py
git commit -m "fix: keep citation acceptance non-optional"
```

### Task 6: Documentation

**Files:**
- Modify: `documents/guides/reliability.md`
- Modify: `documents/guides/evaluation.md`

- [ ] **Step 1: Document strict acceptance contract**

State that web-enabled reports require at least one concrete public HTTP(S) URL, no placeholder source entries, and no unresolved numeric references. Document exact reserved hosts, no-web/document-only exemption, non-waivable behavior, one correction when optional verification is disabled, and safe final failure.

- [ ] **Step 2: Run documentation checks**

Run: `uv run pytest tests/test_prompts_validation.py tests/test_citations.py -q`

Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add documents/guides/reliability.md documents/guides/evaluation.md
git commit -m "docs: explain strict web citation acceptance"
```

### Task 7: Final verification

**Files:**
- Verify only; no planned production changes.

- [ ] **Step 1: Run focused feature suite**

```bash
uv run pytest \
  tests/test_citation_policy.py \
  tests/test_citations.py \
  tests/test_verification.py \
  tests/test_verification_progress.py \
  tests/test_completion_guard.py \
  tests/test_agent_contracts.py \
  tests/test_research_agent_cli_e2e.py -q
```

Expected: all pass.

- [ ] **Step 2: Run static checks**

```bash
uv run ruff check research_agent/research_subagent/utils/citation_policy.py research_agent/research_subagent/utils/verification.py research_agent/citation_failure.py research_agent/agent.py research_agent/completion_guard.py tests/test_citation_policy.py tests/test_verification.py
uv run python -m compileall -q research_agent
git diff --check main...HEAD
```

Expected: no output/errors.

- [ ] **Step 3: Run full suite**

Run: `uv run pytest tests/ -q`

Expected: all project tests pass; compare any environment-only cloud fixture failures against clean `main` before changing feature code.

- [ ] **Step 4: Confirm clean intentional history**

Run: `git status --short && git log --oneline main..HEAD`

Expected: clean worktree; only approved design, plan, feature, test, and documentation commits.
