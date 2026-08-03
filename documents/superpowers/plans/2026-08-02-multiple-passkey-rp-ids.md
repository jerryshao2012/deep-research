# Multiple Passkey RP IDs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support multiple unrelated WebAuthn RP IDs in one backend by mapping each trusted frontend origin to exactly one RP ID.

**Architecture:** `PasskeyConfig` validates RP IDs with IDNA and a bundled Public Suffix List, then owns a deterministic origin-to-RP mapping. Auth-store adapters persist RP binding on credentials and expose atomic challenge claiming plus compare-and-swap legacy binding. `PasskeyService` resolves RP at option creation, validates stored RP after challenge claim, and uses stored challenge RP for cryptographic verification.

**Tech Stack:** Python 3.12, FastAPI, py_webauthn, publicsuffixlist, SQLite/PostgreSQL/Cosmos DB, pytest

**Files:**
- Modify: `../../../pyproject.toml`
- Modify: `../../../uv.lock`
- Modify: `../../../webapp/passkeys.py`
- Modify: `../../../webapp/auth_store.py`
- Modify: `../../../webapp/auth_store_postgres.py`
- Modify: `../../../webapp/auth_store_cosmos.py`
- Modify: `../../../.env.example`
- Modify: `../../../README.md`
- Modify: `../../../tests/test_passkeys.py`
- Modify: `../../../tests/test_auth_store.py`
- Modify: `../../../tests/test_auth_store_adapters.py`
- Modify: `../../../tests/test_passkey_auth_store.py`
- Modify: `../../../tests/test_azure_persistence_scripts.py`

`../../../webapp/config.py` and `../../../webapp/oauth_handler.py` do not change: store construction stays independent of RP configuration. Legacy RP binding is lazy in `PasskeyService`, so disabled mode can open stores without RP settings.

No commit or staging steps: user explicitly requested local unstaged, uncommitted changes.

---

### Task 1: RP allowlist parsing and deterministic mapping

- [ ] **Step 1: Add RED parser and mapping tests**

Add `_multi_rp_env()` in `../../../tests/test_passkeys.py` by copying `_enabled_env()`, removing `PASSKEY_RP_ID`, and then setting plural RP/origin values. Every valid plural-only test must use this helper or explicitly execute `env.pop("PASSKEY_RP_ID")` before setting `PASSKEY_RP_IDS`; only conflict tests retain both keys. Add the following concrete tests, asserting `PasskeyConfigurationError` for every reject case:

| Test | Input | Exact assertion |
|---|---|---|
| `test_plural_rp_ids_map_each_requested_origin` | requested Azure/Vercel values | `config.rp_ids` equals normalized two-item tuple and both `rp_id_for_origin` calls return matching tenant host |
| `test_rp_mapping_uses_longest_compatible_suffix` | `example.com,login.example.com` plus `https://login.example.com` | selected RP is `login.example.com` |
| `test_singular_rp_id_is_absent_only_fallback` | plural key absent, singular `example.com` | `config.rp_ids == ("example.com",)` and `config.rp_id == "example.com"` |
| `test_disabled_mode_ignores_malformed_rp_settings` | disabled plus malformed plural/singular | `PasskeyConfig.from_environ(env) == PasskeyConfig(enabled=False)` |
| `test_present_plural_rejects_empty_tokens` | each of `""`, `",host.example"`, `"host.example,"`, `"host.example,,other.example"` | raises message containing `PASSKEY_RP_IDS` |
| `test_both_singular_and_plural_rp_variables_are_rejected_even_when_one_is_empty` | both keys present for each empty/nonempty combination | raises message containing `must not both be set` |
| `test_rp_ids_reject_duplicates_after_normalization` | `EXAMPLE.com.,example.com` and Unicode/punycode equivalent | raises message containing `duplicate` |
| `test_rp_ids_reject_noncanonical_dns_values` | scheme, port, path, wildcard, empty/overlong/malformed label, >253-byte DNS name, invalid surrogate IDNA | raises message containing `PASSKEY_RP_IDS` |
| `test_rp_ids_reject_public_suffixes` | `com`, `vercel.app`, `azurewebsites.net` | raises message containing `registrable` |
| `test_rp_ids_accept_tenant_hosts_and_localhost` | both requested tenants and `localhost` in separate valid envs | normalized value is accepted |
| `test_unicode_origin_hostname_matches_idna_normalized_rp` | Unicode RP and origin | origin selects the normalized ASCII RP |
| `test_every_origin_must_match_and_every_rp_id_must_be_used` | one unmatched origin, then one unused RP | each configuration raises its distinct startup error |

Use the exact requested environment in the primary test:

```python
env.pop("PASSKEY_RP_ID")
env.update({
    "PASSKEY_RP_IDS": "bmo-deepagent-ui-0312.azurewebsites.net,bmo-deepagent-ui.vercel.app",
    "PASSKEY_ORIGINS": "https://bmo-deepagent-ui-0312.azurewebsites.net,https://bmo-deepagent-ui.vercel.app",
})
config = PasskeyConfig.from_environ(env)
assert config.rp_id_for_origin("https://bmo-deepagent-ui-0312.azurewebsites.net") == "bmo-deepagent-ui-0312.azurewebsites.net"
assert config.rp_id_for_origin("https://bmo-deepagent-ui.vercel.app") == "bmo-deepagent-ui.vercel.app"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_passkeys.py -k "rp_id or plural_rp or public_suffix or unicode_origin" -v
```

Expected: failures such as `PASSKEY_RP_ID is required`, missing `rp_ids`, or missing `rp_id_for_origin`; existing singular tests remain green.

- [ ] **Step 3: Add dependency and minimal parser**

Run `uv add publicsuffixlist idna`, making both validators direct dependencies and updating `../../../pyproject.toml` and `../../../uv.lock`.

In `../../../webapp/passkeys.py`:

```python
@dataclass(frozen=True)
class PasskeyConfig:
    enabled: bool
    rp_ids: tuple[str, ...] = ()
    origin_rp_ids: tuple[tuple[str, str], ...] = ()

    @property
    def rp_id(self) -> str:
        if len(self.rp_ids) != 1:
            raise PasskeyConfigurationError("A singular passkey RP ID is unavailable")
        return self.rp_ids[0]

    def rp_id_for_origin(self, origin: str) -> str:
        for configured_origin, rp_id in self.origin_rp_ids:
            if secrets.compare_digest(origin, configured_origin):
                return rp_id
        raise InvalidPasskeyError("Invalid passkey response")
```

Add `_normalize_rp_id(value)` using `idna.encode(value, uts46=False, std3_rules=True)`, total/label length checks, no URL syntax/wildcards, and `PublicSuffixList().privatesuffix(rp_id) is not None`; allow exact `localhost`. Parse `PASSKEY_RP_IDS` only when the key exists. If it exists, reject any empty token and reject presence of `PASSKEY_RP_ID` regardless of value. Otherwise require singular `PASSKEY_RP_ID`. Deduplicate after normalization. For each validated origin, choose `max(matches, key=len)` and require all RP IDs appear in `origin_rp_ids`.

- [ ] **Step 4: Verify GREEN**

Run the focused command from Step 2. Expected: all selected tests pass.

---

### Task 2: Credential RP binding and adapter migrations

- [ ] **Step 1: Add RED storage contract tests**

Assign tests exactly:

- `../../../tests/test_auth_store.py`: `test_credential_round_trip_persists_rp_id`, `test_list_credentials_filters_by_rp_id_but_none_lists_all`, `test_bind_legacy_credential_rp_id_is_compare_and_swap`, `test_invalid_credential_rp_id_is_rejected`, `test_sqlite_legacy_schema_adds_nullable_rp_id_idempotently`, and `test_sqlite_partial_migration_preserves_bound_and_unbound_rows`.
- `../../../tests/test_auth_store_adapters.py`: `test_postgres_existing_schema_adds_rp_column`, `test_postgres_conditional_rp_binding_is_idempotent`, `test_cosmos_legacy_binding_retries_etag_conflict`, `test_cosmos_multi_rp_rejection_does_not_replace_document`, plus adapter-specific atomic claim tests.
- `../../../tests/test_passkey_auth_store.py`: parameterized contract tests `test_credential_rp_round_trip`, `test_credential_rp_filter`, `test_claim_challenge_returns_stored_record_exactly_once`, and `test_claim_challenge_is_atomic_under_concurrency` for SQLite and configured PostgreSQL/Cosmos adapters.

Each credential round-trip asserts exact `record.rp_id`; filter tests create two credentials and assert one/all credential ID sets; CAS tests assert first/same binding true and different binding false with unchanged persisted RP. Migration tests construct the old schema manually, open the store twice, assert one nullable column, preserve bound rows, and leave legacy rows `NULL`. Claim tests create a record, assert first claim returns its original kind/origin/RP even when caller intends different expectations, and assert all later/concurrent claims return `None`.

- [ ] **Step 2: Verify RED for each adapter node**

Run:

```bash
uv run pytest tests/test_auth_store.py -k "rp_id or claim_challenge or legacy" -v
uv run pytest tests/test_auth_store_adapters.py -k "rp_id or claim_challenge or legacy or etag" -v
uv run pytest tests/test_passkey_auth_store.py -k "rp_id or claim_challenge or legacy" -v
```

Expected: exact failures are `CredentialRecord` missing `rp_id`, `create_credential()` receiving unexpected `rp_id`, and adapters missing `claim_challenge`/`bind_credential_rp_id`.

- [ ] **Step 3: Implement shared store contract**

In `../../../webapp/auth_store.py`, append `rp_id: str | None` to `CredentialRecord`. Change the protocol to exact signatures `list_credentials(self, identity: str, rp_id: str | None = None) -> list[CredentialRecord]`, `bind_credential_rp_id(self, credential_id: str, rp_id: str) -> bool`, and `claim_challenge(self, ceremony_id: str) -> ChallengeRecord | None`.

Extend credential validation with canonical non-empty RP IDs for new records. SQLite migration runs inside existing initialization transaction: inspect `PRAGMA table_info(auth_credentials)`, execute `ALTER TABLE auth_credentials ADD COLUMN rp_id TEXT` only when absent, and never bulk-fill it. Fresh tables include nullable `rp_id`. `bind_credential_rp_id` updates only `WHERE credential_id=? AND (rp_id IS NULL OR rp_id='')`; a repeat with the same RP returns true after reread, a different RP returns false. `list_credentials(..., rp_id)` adds an exact filter only when non-`None`.

- [ ] **Step 4: Implement PostgreSQL and Cosmos parity**

PostgreSQL initialization uses `ALTER TABLE auth_credentials ADD COLUMN IF NOT EXISTS rp_id TEXT`; conditional bind uses one transaction and `UPDATE ... WHERE credential_id=%s AND (rp_id IS NULL OR rp_id='') RETURNING credential_id`, followed by exact reread for idempotence. Cosmos reads missing fields as `None`; bind uses ETag replace CAS with bounded conflict retry and refuses a different existing value. Multi-RP rejection is service-side and performs no bind call.

Replace expectation-taking `consume_challenge` with `claim_challenge(ceremony_id)` in all adapters. Update existing `test_create_and_consume_challenge_exactly_once`, `test_challenge_mismatch_consumes_and_never_reopens`, `test_expired_challenge_is_consumed_and_returns_none`, `test_postgres_mismatched_challenge_is_consumed_with_row_lock`, and `test_cosmos_challenge_mismatch_consumes_once_with_etag` to the claim API. Claim atomically sets `consumed_at` and returns the stored record regardless of kind/origin/RP/expiry. Unknown, already-consumed, or CAS-lost IDs return `None`. Preserve Cosmos registration reservation release after successful claim.

- [ ] **Step 5: Verify GREEN and idempotence**

Rerun the three Step 2 commands. Then rerun each migration test twice against the same store/schema. Expected: all pass; conditional real PostgreSQL/Cosmos suites may skip when endpoints are absent.

---

### Task 3: Per-origin ceremonies and legacy binding

- [ ] **Step 1: Add RED service and crypto tests**

Add these exact `../../../tests/test_passkeys.py` cases with existing SQLite/service/ES256 helpers:

- `test_registration_options_select_rp_and_filter_exclusions`: create Azure-, Vercel-, and unbound credentials; under singular config assert unbound credential is CAS-bound before exclusion and only selected-RP IDs appear. Under multi-RP config assert unbound data causes generic rejection and remains unbound.
- `test_authentication_options_select_rp_for_each_requested_domain`: call both origins and assert options plus stored challenge RP equal the matching tenant host.
- `test_azure_registration_and_authentication_crypto_round_trip` and `test_vercel_registration_and_authentication_crypto_round_trip`: use real ES256 registration/assertion fixtures and assert persisted credential RP, returned passkey session, and user.
- `test_authentication_rejects_credential_bound_to_other_rp`: bind credential to Azure, claim a Vercel challenge, assert generic error and no session/counter update.
- `test_unbound_legacy_credential_binds_only_for_single_rp`: assert options path and authentication path each persist the sole RP.
- `test_unbound_legacy_credential_is_rejected_without_mutation_for_multiple_rps`: assert registration options and authentication both fail and reread remains unbound.
- `test_wrong_allowed_domain_burns_challenge_before_correct_domain_retry`: parameterize registration/authentication; first wrong allowed origin fails, second correct origin fails because claim is spent.
- `test_concurrent_service_verification_allows_only_one_claim`: synchronize two verification calls and assert one success, one generic failure, one session/counter mutation.
- `test_verification_claim_precedes_all_validation_failures`: parameterize wrong kind, expired challenge, wrong stored proxy, missing session, expired session, malformed response, missing credential, and wrong credential; for each, assert first failure and a correct retry both return generic invalid response because claim is spent.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_passkeys.py -k "azure or vercel or wrong_allowed_domain or credential_bound or exclusion or legacy_credential or concurrent_service" -v
```

Expected: both origins use the old singular RP, credentials lack RP binding/filtering, or service still calls `consume_challenge`.

- [ ] **Step 3: Implement minimal service behavior**

For option creation, resolve `rp_id = self.config.rp_id_for_origin(origin)`, persist it on the challenge, and generate options with it. Registration first lists all credentials. If any are unbound, bind and reread them only when `len(config.rp_ids) == 1`; otherwise raise the generic invalid response without mutation. Then build exclusions from credentials whose exact `rp_id` equals the selected RP.

For both verification methods:

```python
challenge = self.store.claim_challenge(ceremony_id)
if challenge is None:
    raise InvalidPasskeyError("Invalid passkey response")
resolved_rp_id = self.config.rp_id_for_origin(origin)
if now >= challenge.expires_at or (
    challenge.kind != expected_kind
    or challenge.origin != origin
    or challenge.rp_id != resolved_rp_id
    or challenge.proxy_id != proxy_id
):
    raise InvalidPasskeyError("Invalid passkey response")
```

Only after this block validate session, response shape, and credential. Both py_webauthn verifier calls receive `expected_rp_id=challenge.rp_id`. Registration calls `create_credential(..., rp_id=challenge.rp_id)`. Authentication requires exact `credential.rp_id == challenge.rp_id`. If credential RP is missing and `len(config.rp_ids) == 1`, call adapter CAS bind and reread; otherwise reject without mutation. Management `list_credentials` passes no RP filter and therefore returns all account credentials.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command, then:

```bash
uv run pytest tests/test_passkeys.py tests/test_passkey_auth_store.py -v
```

Expected: all pass, including separate Azure/Vercel crypto fixtures and retry rejection after mismatch.

---

### Task 4: Route errors and documented environment contract

- [ ] **Step 1: Add RED route and documentation tests**

Add route-level tests `test_unknown_origin_remains_exact_passkey_request_rejected_403`, `test_allowed_origin_wrong_rp_returns_generic_invalid_response_400_without_config`, and `test_wrong_rp_credential_returns_generic_invalid_response_400_without_config`. Each constructs the full FastAPI request using existing trusted-proxy headers and the requested two-domain config; the latter two first create a real stored challenge/credential with the mismatching RP.

Assert exact bodies:

```python
assert response.status_code == 403
assert response.json() == {"code": "passkey_request_rejected"}

assert response.status_code == 400
assert response.json() == {"code": "invalid_passkey_response"}
assert "rp" not in response.text.lower()
```

Update `../../../tests/test_azure_persistence_scripts.py` with a failing assertion for:

```text
PASSKEY_RP_IDS="bmo-deepagent-ui-0312.azurewebsites.net,bmo-deepagent-ui.vercel.app"
PASSKEY_ORIGINS="https://bmo-deepagent-ui-0312.azurewebsites.net,https://bmo-deepagent-ui.vercel.app"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_passkeys.py -k "unknown_origin or wrong_rp" -v
uv run pytest tests/test_azure_persistence_scripts.py -k passkey -v
```

Expected: environment contract assertions fail because documentation still uses singular `PASSKEY_RP_ID`; route tests may expose incorrect status/body until service mapping is complete.

- [ ] **Step 3: Update examples and preserve route behavior**

Update `../../../.env.example` and `../../../README.md` with the exact plural example, absent-only singular fallback, IDNA/PSL restrictions, most-specific origin mapping, separate enrollment for unrelated domains, all-credentials management listing, and RP-filtered registration exclusions. Keep existing route exception mapping: unknown origin at trusted-BFF boundary is 403; post-claim mismatch is generic 400.

- [ ] **Step 4: Verify GREEN**

Rerun both Step 2 commands. Expected: all pass and no response contains configured RP values.

---

### Task 5: Full regression and clean working state

- [ ] Run exact affected suites:

```bash
uv run pytest tests/test_passkeys.py tests/test_auth_store.py tests/test_auth_store_adapters.py tests/test_passkey_auth_store.py tests/test_oauth_setup.py tests/test_frontend_api_contract.py tests/test_azure_persistence_scripts.py -v
```

- [ ] Run the complete backend suite:

```bash
uv run pytest tests/ -v
```

- [ ] Run exact lint and formatting checks:

```bash
uv run ruff check webapp/passkeys.py webapp/auth_store.py webapp/auth_store_postgres.py webapp/auth_store_cosmos.py tests/test_passkeys.py tests/test_auth_store.py tests/test_auth_store_adapters.py tests/test_passkey_auth_store.py tests/test_azure_persistence_scripts.py
uv run ruff format --check webapp/passkeys.py webapp/auth_store.py webapp/auth_store_postgres.py webapp/auth_store_cosmos.py tests/test_passkeys.py tests/test_auth_store.py tests/test_auth_store_adapters.py tests/test_passkey_auth_store.py tests/test_azure_persistence_scripts.py
```

- [ ] Prove diff hygiene and requested git state:

```bash
git diff --check
git diff --cached --quiet
git status --short
```

Expected: checks exit zero, cached diff is empty, and status shows only intended unstaged/untracked passkey feature files plus the two design/plan documents.
