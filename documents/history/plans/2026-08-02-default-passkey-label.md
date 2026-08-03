# Default Passkey Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure omitted or blank passkey labels produce useful, stable backend-generated labels and never break passkey management UI validation.

**Architecture:** Add a small label policy inside `../../../webapp/passkeys.py`, where verified authenticator metadata and injected time are available. New registrations persist a generated non-empty label; credential serialization supplies stable fallbacks for legacy null/blank rows without mutating them. UI continues omitting blank input and accepts the backend-generated string.

**Tech Stack:** Python 3.12, FastAPI, py_webauthn, SQLite/auth-store adapters, pytest, Next.js/React, TypeScript, Node test runner, Testing Library.

**Constraint:** Keep all changes unstaged and uncommitted.

---

### Task 1: Define and test backend label policy

**Files:**
- Modify: `webapp/passkeys.py:395-465`
- Test: `../../../tests/test_passkeys.py`

- [ ] **Step 1: Write failing policy tests**

Add focused tests for internal helpers that assert:

```python
assert _default_passkey_label("multi_device", ("internal",), timestamp) == (
    "Synced passkey · Aug 3, 2026"
)
assert _default_passkey_label("single_device", ("internal",), timestamp) == (
    "Device passkey · Aug 3, 2026"
)
assert _default_passkey_label("single_device", ("usb",), timestamp) == (
    "Security key · Aug 3, 2026"
)
assert _default_passkey_label("single_device", ("hybrid",), timestamp) == (
    "Passkey · Aug 3, 2026"
)
```

Cover UTC date boundaries, missing/invalid timestamps (`"Passkey"`), multi-device precedence, empty/unknown transports, omitted/null/blank label normalization, trim-before-validation, non-string rejection, padded 100-code-point acceptance, and 101-code-point rejection.

- [ ] **Step 2: Run policy tests and verify RED**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_passkeys.py -k 'default_passkey_label or registration_label_normalization' -v
```

Expected: FAIL because label-policy helpers do not exist.

- [ ] **Step 3: Implement minimal policy helpers**

In `../../../webapp/passkeys.py`:

- Normalize registration labels: `None` and trimmed-empty become default requests; non-string and over-100-code-point values raise `InvalidPasskeyError`; valid labels are trimmed before counting.
- Format UTC dates with fixed English month abbreviations and numeric day without platform-specific `strftime` flags.
- Classify multi-device first, then internal, then removable transports (`usb`, `nfc`, `ble`, `smart-card`), otherwise generic passkey.
- Return stable `Passkey` when legacy timestamp cannot be converted.

- [ ] **Step 4: Run policy tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

### Task 2: Integrate defaults into registration, serialization, and rename

**Files:**
- Modify: `webapp/passkeys.py:457-610`
- Modify: `webapp/passkeys.py:740-755`
- Test: `../../../tests/test_passkeys.py`

- [ ] **Step 1: Write failing service tests**

Add tests proving:

- Omitted, explicit-null, blank, and whitespace registration labels persist and return a generated string.
- Explicit labels are trimmed and preserved.
- Invalid/101-code-point labels and non-string values are rejected after challenge claim but before cryptographic verification; padded 100-code-point labels are accepted.
- Injected clock is sampled once after successful verification and the same value supplies label date and `created_at`.
- Failed cryptographic verification never samples the post-verification label clock and persists no credential.
- Successfully read legacy null/empty credentials with empty/unknown metadata serialize with a stable generated label based on persisted `created_at`, across later requests, without changing storage. Invalid timestamps use bare `Passkey`; corrupt adapter rows remain outside scope.
- Rename rejects blank, non-string, and overlength input and keeps existing valid rename behavior.
- HTTP registration, list, and rename responses always contain string labels.

- [ ] **Step 2: Run service tests and verify RED**

Run:

```bash
cd /Users/jerryshao/Documents/projects/IBM/ai/deep-research
env PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_passkeys.py -k 'label and (registration or legacy or rename)' -v
```

Expected: FAIL because registration still stores null/blank labels and serialization returns null.

- [ ] **Step 3: Implement registration and serialization integration**

- Normalize label immediately after challenge/session checks and before `verify_registration_response`.
- After successful verification, sample `self._clock()` once.
- Generate default from verified `credential_device_type`, filtered transports, and sampled timestamp when normalized label is absent.
- Persist generated/explicit label and sampled `created_at` together.
- Make `_credential_json` always return a non-empty label, deriving legacy fallback from record metadata and creation time without writing it back.
- Make rename use trim-first explicit-label validation and reject blank defaults.
- Add HTTP route assertions for registration/list/rename string labels and non-string rename rejection.

- [ ] **Step 4: Run service tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

### Task 3: Align BFF/UI label validation and verify unlabeled enrollment

**Files:**
- Modify: `/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui/src/lib/server/passkey-bff.ts`
- Modify: `/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui/src/app/components/PasskeyManagementDialog.tsx`
- Test: `/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui/tests/passkey-bff.test.ts`
- Test: `/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui/tests/passkey-management.test.tsx`

- [ ] **Step 1: Write failing UI regression**

Add RED tests that:

- Leave enrollment label empty, verify request omits `label`, return `Device passkey · Aug 3, 2026`, and render it without generic error.
- Accept padded labels containing exactly 100 Unicode code points, including astral characters.
- Reject 101 code points without truncation for enrollment and rename.
- Accept returned passkeys only when labels trim to a non-empty value of at most 100 code points, including 100 astral characters.
- Make BFF registration/rename schemas use the same code-point count after trimming; rename rejects non-string and trimmed-empty labels, while registration continues treating trimmed-empty as a default-label request.
- Extend cancellation coverage to prove registration verification is never requested after browser cancellation.

- [ ] **Step 2: Run test**

Run:

```bash
cd /Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui
yarn test:passkeys
```

Expected: unlabeled enrollment contract may pass immediately, while astral/code-point boundary tests fail under JavaScript UTF-16 `.length` and HTML `maxLength` behavior.

- [ ] **Step 3: Implement shared client-side code-point validation**

- Count labels with `Array.from(value).length` after trimming in BFF schemas.
- Remove UTF-16-based `maxLength={100}` behavior from enrollment/rename inputs and validate trimmed code-point length before requests.
- Update `validPasskey` to require a trimmed non-empty label with at most 100 Unicode code points.
- Preserve explicit text without truncation and expose a specific accessible error for invalid label length.

- [ ] **Step 4: Rerun UI tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

### Task 4: Final verification

**Files:**
- Verify all files changed above.

- [ ] **Step 1: Run backend regression suites**

```bash
cd /Users/jerryshao/Documents/projects/IBM/ai/deep-research
env PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider \
  tests/test_passkeys.py tests/test_auth_store.py tests/test_passkey_auth_store.py \
  tests/test_oauth_setup.py tests/test_frontend_api_contract.py -q
```

- [ ] **Step 2: Run frontend passkey tests**

```bash
cd /Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui
yarn test:passkeys
yarn lint
yarn build
```

- [ ] **Step 3: Run lint and repository checks**

```bash
cd /Users/jerryshao/Documents/projects/IBM/ai/deep-research
uv run ruff check webapp/passkeys.py tests/test_passkeys.py
git diff --check
git status --short

cd /Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui
git diff --check
git status --short
```

Expected: tests and lint pass; changes remain unstaged and uncommitted.
