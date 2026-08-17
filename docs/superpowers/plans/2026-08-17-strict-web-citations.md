# Strict Web Citation Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent web-enabled research reports with missing, placeholder, or unresolved citations from being streamed or marked verified, while preserving no-web and document-only runs.

**Architecture:** Add a pure deterministic Markdown citation policy before optional LLM judging. Carry web-mode through an ephemeral input channel into hidden per-generation state, then use a two-phase citation-failure transition so defect metadata checkpoints before a safe `ReportCitationError` is raised. Structural citation acceptance remains active even when LLM verification is disabled or configured for zero rounds.

**Tech Stack:** Python 3.12+, LangGraph state channels/middleware, LangChain messages, `urllib.parse`, dataclasses, pytest/pytest-asyncio.

---

## File map

- Create `research_agent/research_subagent/utils/citation_policy.py` — pure Markdown audit, URL normalization, placeholder-host rules, source/reference resolution, and bounded defects.
- Modify `research_agent/research_subagent/utils/verification.py:68-363` — citation-blocking verdict fields and early deterministic preflight.
- Create `research_agent/citation_failure.py` — checkpoint-safe failure state, fingerprint/run correlation, safe error, and transition helpers.
- Modify `research_agent/agent.py:89-95,125-168,311-379,459-478,532-610,693-1066,1068-1115,1255-1274` — ephemeral raw `no_web`, effective per-generation mode, strict verification flow, two-phase failure hooks, and finalization gating.
- Modify `research_agent/completion_guard.py:344-390` — refuse finalization while a matching citation failure is pending.
- Create `tests/test_citation_policy.py` — deterministic grammar matrix.
- Modify `tests/test_verification.py` — early gate, zero-round behavior, sync/async revision/failure transitions, and non-streaming.
- Modify `tests/test_completion_guard.py` — pending citation failure blocks finalization.
- Modify `tests/test_agent_contracts.py` — ephemeral channel and hook registration.
- Modify `tests/test_citations.py` — compatibility with existing citation validation.
- Modify `documents/guides/reliability.md` and `documents/guides/evaluation.md` — strict acceptance behavior and exemptions.

### Task 1: Pure citation grammar and defect model

**Files:**
- Create: `research_agent/research_subagent/utils/citation_policy.py`
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
git add research_agent/research_subagent/utils/citation_policy.py tests/test_citation_policy.py
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

Also prove a valid strict report proceeds to existing grounding/sufficiency/adversarial checks, while `strict_web_citations=False` preserves current behavior for no-web/document-only content.

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
    ...
```

Update `format_feedback()` to render bounded exact messages: missing concrete HTTP(S) URL, placeholder entry label, unresolved reference numbers, and malformed group/range. Do not include report prose.

- [ ] **Step 4: Preserve control exceptions**

Explicitly re-raise `ModelCallTimeoutError`, `ReportCitationError`, and `asyncio.CancelledError` in direct judge and composite verification exception paths. Ordinary judge/parser errors retain existing fallback behavior.

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
4. completion/verification internal `jump_to: model` retains the current effective value.

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

In each visible `before_agent`, read only ephemeral `state.get("no_web")`, default absence to false, and overwrite all three effective fields. Resume/internal model jumps keep effective fields because `before_agent` is not rerun for internal jumps. Replace prompt, tool eligibility, verification, and metrics reads of raw `no_web` with `effective_no_web`.

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
- Create: `research_agent/citation_failure.py`
- Modify: `research_agent/agent.py:89-95,311-379,459-478,693-1066`
- Modify: `research_agent/completion_guard.py:344-390`
- Modify: `tests/test_verification.py`
- Modify: `tests/test_completion_guard.py`

- [ ] **Step 1: Write RED pure transition tests**

Required state contract:

```python
class CitationFailureState(TypedDict, total=False):
    citation_failure_run_id: Annotated[NotRequired[str | None], OmitFromInput]
    citation_failure_report_fingerprint: Annotated[NotRequired[str | None], OmitFromInput]
    citation_failure_defects: Annotated[NotRequired[tuple[dict[str, str], ...]], OmitFromInput]
```

Test current run/fingerprint match raises `ReportCitationError`; stale run, changed report, malformed defects, or explicit new run clears/ignores failure. Error text contains only bounded defect codes/reference numbers.

- [ ] **Step 2: Write RED compiled two-phase tests**

For sync and async compiled graphs with a checkpointer:

- final correction attempt returns `jump_to: end` and checkpoints failure metadata plus intermediate terminal tag;
- report is not streamed, `_streamed_files` remains unchanged, verified/accepted-at-limit fingerprints remain unset;
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
```

Use runtime `execution_info.run_id` as authoritative, then top-level configured run ID, matching completion-guard resolution. Store only serialized bounded defects and fingerprint, never report text.

- [ ] **Step 5: Wire two-phase hooks and finalization block**

In `_apply_verification_verdict`, citation-blocking final attempt must not populate accepted-at-limit fields. It writes failure update, tags terminal intermediate, and jumps to end. Add `after_agent`/`aafter_agent` hooks that call the pure raise helper. `finalize_accepted_report` returns `None` whenever a matching citation failure is pending.

- [ ] **Step 6: Run failure/finalization tests and confirm GREEN**

Run: `uv run pytest tests/test_verification.py tests/test_completion_guard.py -q -k 'citation or finalization or stream'`

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add research_agent/citation_failure.py research_agent/agent.py research_agent/completion_guard.py tests/test_verification.py tests/test_completion_guard.py
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
- invalid report gets exactly one structural correction opportunity when off/zero;
- unchanged invalid report then checkpoints and raises `ReportCitationError`;
- with positive rounds, configured count bounds correction attempts;
- non-structural judge `needs_revision` may retain accepted-at-limit behavior;
- no-web/document-only runs retain current zero-round finalization.

- [ ] **Step 2: Run matrix and confirm RED**

Run: `uv run pytest tests/test_verification.py tests/test_verification_progress.py -q -k 'disabled or zero_round or structural'`

- [ ] **Step 3: Separate structural and optional verification policy**

Create helpers:

```python
def _structural_citation_required(state) -> bool: ...
def _optional_llm_verification_enabled() -> bool: ...
def _effective_citation_attempt_limit() -> int:
    return max(MAX_VERIFICATION_ROUNDS, 1)
```

Run strict preflight whenever owned report is ready and not already structurally accepted for its fingerprint. Invoke judges only when optional verification is enabled. Progress labels must distinguish `Citation check 1/1` from `Verification n/N` and never claim verified on blocking failure.

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
  tests/test_agent_contracts.py -q
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
