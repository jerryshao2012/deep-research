# Default Passkey Label Design

## Problem

Passkey enrollment permits an omitted label. The backend then persists and returns
`label: null`, while the management UI accepts only string labels. Enrollment can
succeed server-side but the UI reports a generic failure and cannot render the
credential list.

## Design

The backend owns default-label generation because it has verified authenticator
metadata and must provide one consistent API contract to every client. An omitted
label, explicit JSON `null`, or a string that trims to empty requests a default.
Any other non-string value is rejected. A non-empty explicit label is trimmed,
must contain 1–100 Unicode code points across backend, BFF, and UI, and is rejected
rather than truncated when over length. This validation happens after the
one-time challenge is claimed but before WebAuthn verification or persistence.

After successful WebAuthn verification, sample the injected clock exactly once,
convert it to UTC, and format a locale-independent English date (`Mon D, YYYY`).
Use verified authenticator metadata with this precedence:

- Multi-device credential: `Synced passkey · <Mon D, YYYY>`
- Single-device credential with `internal` transport: `Device passkey · <Mon D, YYYY>`
- Credential with removable transport evidence (`usb`, `nfc`, `ble`, or
  `smart-card`): `Security key · <Mon D, YYYY>`
- Missing, `hybrid`, or otherwise unknown transport: `Passkey · <Mon D, YYYY>`

Example: `Device passkey · Aug 3, 2026`.

Multi-device classification always wins over transport classification. New
registrations and renames store non-empty labels of at most 100 characters, and
every passkey API response returns a non-empty label string. Rename rejects blank,
non-string, and overlength input; it never generates a default. No browser
user-agent, hardware model, account identity, or other fingerprinting data is
included.

## Compatibility and Recovery

Existing legacy credentials with a null or empty label remain readable without a
storage migration. For records successfully read by an adapter, API serialization
derives a stable fallback from persisted device type, transports, and credential
creation timestamp, converted to UTC with the same rules above. Empty or unknown
metadata uses generic classification; an unavailable or invalid timestamp uses
the stable fallback `Passkey`. Serialization does not mutate storage. Repair of
otherwise corrupt adapter rows or documents is outside this change. Rename
remains available and requires a non-empty label.

Default generation happens only after cryptographic verification, so failed or
cancelled ceremonies create no label and no credential. Repeated enrollments on
the same day may share a label; credential ID remains the unique identifier and
users can rename either credential.

## Tests

- Registration with omitted, explicit-null, blank, or whitespace-only label
  stores and returns the correct generated label.
- Synced, internal, removable, missing, and hybrid authenticator metadata produce
  expected labels; multi-device classification wins over internal transport.
- Explicit input is trimmed and preserved; padded 100-code-point and astral
  Unicode labels are accepted, 101 code points are rejected without truncation,
  and non-string input is rejected consistently by backend, BFF, and UI.
- Injected clock uses UTC across a date boundary and is sampled once.
- Legacy null/empty labels serialize as stable non-empty fallback strings across
  later requests without changing stored rows.
- Registration, list, and rename credential JSON always expose label as a string.
- UI enrollment without text accepts and renders backend-generated label.
- Existing labeled enrollment and rename behavior remain unchanged.
