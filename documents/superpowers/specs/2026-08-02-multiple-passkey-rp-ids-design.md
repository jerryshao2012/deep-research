# Multiple Passkey RP IDs Design

## Goal

Allow one backend deployment to serve passkey ceremonies for multiple unrelated frontend domains while preserving WebAuthn's requirement that each ceremony uses exactly one RP ID.

## Configuration

- Add `PASSKEY_RP_IDS`, a comma-separated allowlist.
- Retain `PASSKEY_RP_ID` as a backward-compatible fallback when `PASSKEY_RP_IDS` is absent.
- Treat a present-but-empty plural variable, empty CSV entries, both variables being present, duplicate normalized IDs, origins that match no RP ID, and RP IDs unused by every origin as startup errors.
- Normalize RP IDs through IDNA to lowercase ASCII without a trailing dot. Reject schemes, ports, paths, wildcards, invalid DNS labels, overlong values, and public suffixes. Preserve the explicit `localhost` development exception.
- Use the bundled Public Suffix List from `publicsuffixlist`; tenant hosts such as `bmo-deepagent-ui.vercel.app` and `bmo-deepagent-ui-0312.azurewebsites.net` are valid while `vercel.app`, `azurewebsites.net`, and `com` are not.

## Origin-to-RP Selection

Validate every `PASSKEY_ORIGINS` entry as an exact HTTPS origin, except existing localhost development support. Map each origin hostname to the most-specific configured RP ID for which the hostname is equal to the RP ID or is its subdomain. Longest matching RP ID wins, making parent-domain and host-specific entries deterministic. Every configured RP ID must map to at least one configured origin.

For the requested configuration:

- `https://bmo-deepagent-ui-0312.azurewebsites.net` selects `bmo-deepagent-ui-0312.azurewebsites.net`.
- `https://bmo-deepagent-ui.vercel.app` selects `bmo-deepagent-ui.vercel.app`.

## Ceremony Flow

Registration and authentication option creation resolve the RP ID from the trusted BFF origin, store it in the one-time challenge, and generate options with that RP ID. Registration stores the selected RP ID with the credential and includes only credentials already bound to that RP ID in `excludeCredentials`; users enroll separately on unrelated domains.

Verification atomically claims an unconsumed challenge by `ceremony_id` before validating kind, expiry, exact origin, resolved RP ID, proxy, session, response shape, or credential. A mismatch therefore burns the first attempt. Cryptographic verification uses the stored challenge RP ID. Authentication additionally requires `credential.rp_id == challenge.rp_id`, preventing cross-domain credential use and replay.

Credential persistence adds nullable `rp_id` storage to SQLite and PostgreSQL schemas and the corresponding Cosmos document field. Existing credentials without an RP binding are inferred and lazily backfilled through adapter compare-and-swap only when exactly one RP ID is configured; multi-RP operation rejects unbound legacy credentials without mutating them because their RP cannot be inferred safely. Store construction remains independent of passkey configuration, including when passkeys are disabled.

Passkey management lists all credentials for the account across configured RP IDs. Registration `excludeCredentials` is filtered to the RP ID selected for the current origin.

## Compatibility and Errors

Existing singular configuration continues unchanged, including disabled mode. `PASSKEY_RP_IDS` falls back to `PASSKEY_RP_ID` only when the plural key is absent. Invalid multi-RP configuration fails at startup. Unknown trusted-BFF origins retain the existing `403 {"code":"passkey_request_rejected"}` response; an allowed-origin ceremony, RP, or credential mismatch returns the generic `400 {"code":"invalid_passkey_response"}` response without configuration details.

## Tests

Cover parsing and IDNA normalization, malformed labels, exact and subdomain mapping, longest-match selection, public-suffix rejection, unmatched/unused/duplicate/conflicting/empty configuration, singular fallback, and disabled mode. Add separate cryptographic registration/authentication round trips for Azure and Vercel, RP-filtered exclusions, challenge and credential RP persistence across every store adapter, wrong-RP credential rejection, wrong-domain-first-attempt burn followed by failed correct-domain retry, and concurrent consumption.
