# Extended Markdown Archives and Office Attachments Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend existing synchronized Markdown asset endpoints for validated `.7z`, `.tar`, `.tar.gz`, and `.tgz` archives plus opaque card-only Microsoft Office attachments alongside images and ZIPs.

**Architecture:** Move archive recognition and bounded in-memory validation into `../../../webapp/markdown_archive_validation.py`, and isolate extension-only Office classification in `../../../webapp/markdown_office_formats.py`; keep existing routes and storage namespace in `../../../webapp/markdown_images.py`. Upload requests containing archives acquire one bounded batch slot before any persistence and run decompression in worker threads, while Office bytes remain opaque. One upload-only gate controls all post-ZIP formats; read/download compatibility stays enabled for stored assets.

**Tech Stack:** Python 3.12–3.13, FastAPI/Starlette, stdlib `zipfile`/`tarfile`/`gzip`, `py7zr`, pytest, Ruff, uv.

**Design specs:**

- `../specs/2026-08-13-markdown-archive-attachments-design.md`
- `../specs/2026-08-13-markdown-office-attachments-design.md`

---

## File map

- Create `../../../webapp/markdown_archive_validation.py`: archive format table, candidate detection, normalized types, bounded ZIP/TAR/gzip/7z validators, discard-only 7z writer.
- Create `../../../webapp/markdown_office_formats.py`: authoritative Office extension/family catalog and opaque stored-type helpers.
- Create `../../../tests/test_markdown_archive_validation.py`: focused unit tests for signatures, MIME agreement, integrity, encryption, member count, and actual expanded-byte limits.
- Create `../../../tests/test_markdown_office_formats.py`: complete Office catalog, case matching, misleading suffix, and MIME-independence tests.
- Modify `../../../webapp/markdown_images.py`: delegate archive validation, add upload feature gate, batch limiter, worker-thread dispatch, generalized stored-archive download behavior.
- Modify `../../../tests/test_markdown_image_api.py`: end-to-end mixed upload, normalized metadata, overload atomicity, download, corruption, and cleanup tests for all formats.
- Modify `../../../pyproject.toml` and `../../../uv.lock`: lock `py7zr` runtime support.
- Modify `../../../.env.example`: document `MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED`.

### Task 1: Lock 7z runtime support

**Files:**
- Modify: `pyproject.toml:6-67`
- Modify: `../../../uv.lock`

- [ ] **Step 1: Add the bounded dependency range**

Run:

```bash
uv add "py7zr>=1.1.3,<2"
```

Expected: `../../../pyproject.toml` contains `py7zr>=1.1.3,<2`; `../../../uv.lock` resolves `py7zr` and codec dependencies for Python 3.12/3.13.

- [ ] **Step 2: Verify supported in-memory APIs**

Run:

```bash
uv run python -c "from py7zr import Py7zIO, SevenZipFile, WriterFactory; print(SevenZipFile, Py7zIO, WriterFactory)"
```

Expected: command exits 0 and prints all three classes.

- [ ] **Step 3: Verify lock consistency**

Run:

```bash
uv lock --check
```

Expected: exit 0.

- [ ] **Step 4: Commit dependency change**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add 7z validation dependency"
```

### Task 2: Define archive classification and preserve ZIP behavior

**Files:**
- Create: `../../../webapp/markdown_archive_validation.py`
- Create: `../../../tests/test_markdown_archive_validation.py`
- Modify: `webapp/markdown_images.py:12-52,111-152`

- [ ] **Step 1: Write failing classification and ZIP regression tests**

Add complete table-driven tests:

```python
@pytest.mark.parametrize(
    ("filename", "content_type", "normalized"),
    [
        ("evidence.zip", "application/zip", "application/zip"),
        ("evidence.7z", "application/vnd.7zip", "application/x-7z-compressed"),
        ("evidence.tar", "application/x-tar", "application/x-tar"),
        ("evidence.tar.gz", "application/gzip", "application/gzip"),
        ("evidence.tgz", "application/x-gzip", "application/gzip"),
    ],
)
def test_archive_format_contract(filename, content_type, normalized):
    assert archive_format_for_filename(filename) is not None
    assert normalized_archive_content_type(filename) == normalized
    assert is_archive_upload(filename, content_type)


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("evidence.zip", "application/gzip"),
        ("evidence.tar", "application/zip"),
        ("evidence.png", "application/x-7z-compressed"),
    ],
)
def test_archive_extension_and_declared_mime_must_agree(filename, content_type):
    with pytest.raises(ArchiveValidationError):
        validate_archive(filename, content_type, _ZIP)
```

Keep a real ZIP fixture and assert generic/empty ZIP MIME values still normalize to `application/zip`.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
uv run pytest tests/test_markdown_archive_validation.py -q
```

Expected: FAIL because module/functions do not exist.

- [ ] **Step 3: Implement format table and public validation boundary**

Create these stable exports and keep detailed parser errors private:

```python
class ArchiveValidationError(ValueError):
    """Archive does not satisfy the synchronized-asset contract."""


_FORMATS = {
    ".zip": ArchiveSpec("application/zip", ZIP_MIMES, ZIP_SIGNATURES),
    ".7z": ArchiveSpec("application/x-7z-compressed", SEVEN_Z_MIMES, (b"7z\xbc\xaf'\x1c",)),
    ".tar": ArchiveSpec("application/x-tar", TAR_MIMES, ()),
    ".tar.gz": ArchiveSpec("application/gzip", GZIP_TAR_MIMES, (b"\x1f\x8b",)),
    ".tgz": ArchiveSpec("application/gzip", GZIP_TAR_MIMES, (b"\x1f\x8b",)),
}


def archive_format_for_filename(filename: str) -> str | None:
    lowered = filename.lower()
    return next((suffix for suffix in (".tar.gz", ".tgz", ".zip", ".7z", ".tar") if lowered.endswith(suffix)), None)


def is_archive_upload(filename: str, content_type: str | None) -> bool:
    declared = (content_type or "").lower()
    return archive_format_for_filename(filename) is not None or declared in ARCHIVE_SPECIFIC_MIME_TYPES


def validate_archive(filename: str, content_type: str | None, data: bytes) -> str:
    suffix = archive_format_for_filename(filename)
    spec = _FORMATS.get(suffix or "")
    if spec is None or (content_type or "").lower() not in spec.accepted_mimes:
        raise ArchiveValidationError
    if spec.signatures and not data.startswith(spec.signatures):
        raise ArchiveValidationError
    _VALIDATORS[suffix](data)
    return spec.normalized_content_type
```

Define `ARCHIVE_CONTENT_TYPES`, `is_stored_archive_content_type()`, and `is_extended_archive_filename()` from the same table so route code never duplicates format knowledge.

- [ ] **Step 4: Move current bounded ZIP validator behind the new boundary**

Move existing `ZipFile(BytesIO(data))` member-count, declared-size, encryption, and per-`ZipInfo` draining logic unchanged. Convert every `zipfile`/zlib exception to `ArchiveValidationError` without exposing exception text.

- [ ] **Step 5: Delegate `_validate_asset` and `_is_archive_upload`**

In `../../../webapp/markdown_images.py`, retain image validation and replace ZIP branches with:

```python
def _validate_asset(filename: str, content_type: str | None, data: bytes) -> str:
    if archive_format_for_filename(filename) is not None:
        return validate_archive(filename, content_type, data)
    return _validate_image(filename, content_type, data)
```

Import `is_archive_upload` for ordered error classification.

- [ ] **Step 6: Run unit and existing API suites**

Run:

```bash
uv run pytest tests/test_markdown_archive_validation.py tests/test_markdown_image_api.py -q
```

Expected: all tests pass; existing 32 API tests remain green.

- [ ] **Step 7: Commit classifier and ZIP migration**

```bash
git add webapp/markdown_archive_validation.py webapp/markdown_images.py tests/test_markdown_archive_validation.py
git commit -m "refactor: centralize markdown archive validation"
```

### Task 3: Add bounded TAR and gzip-TAR validation

**Files:**
- Modify: `../../../webapp/markdown_archive_validation.py`
- Modify: `../../../tests/test_markdown_archive_validation.py`

- [ ] **Step 1: Add real TAR and gzip-TAR fixtures**

Use `TarInfo` and `TarFile.addfile()` with `BytesIO`; gzip the TAR bytes with `gzip.compress()`. Do not use filesystem extraction in tests or implementation.

- [ ] **Step 2: Write failing TAR integrity and bound tests**

Cover:

```python
def test_tar_drains_every_regular_member(): ...
def test_tar_rejects_bad_header_checksum(): ...
def test_tar_rejects_declared_expanded_size_over_limit(monkeypatch): ...
def test_tar_rejects_nonzero_member_padding(): ...
def test_tar_requires_two_zero_end_blocks(): ...
def test_tar_rejects_nonzero_trailing_junk(): ...
def test_tar_rejects_excess_metadata_records(monkeypatch): ...
def test_tar_rejects_excess_metadata_payload(monkeypatch): ...
def test_tgz_drains_outer_gzip_to_validate_crc_trailer(): ...
def test_tgz_rejects_actual_decompressed_stream_over_limit(monkeypatch): ...
```

For the CRC test, flip a byte in the final eight-byte gzip trailer. For limits, monkeypatch small module constants and build tiny deterministic archives. Add raw byte fixtures with missing end markers, nonzero padding, and nonzero bytes after end markers.

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
uv run pytest tests/test_markdown_archive_validation.py -k "tar or tgz or gzip" -q
```

Expected: new tests fail because TAR validators are absent.

- [ ] **Step 4: Implement raw TAR validation**

First scan the raw 512-byte TAR blocks without extraction. Parse each header size field, require each derived payload range to fit, require zero-filled per-record padding, require at least two consecutive zero end blocks, allow at most 20 total zero blocks from the first end marker (the stdlib 10 KiB record), and reject any nonzero or partial block after the end markers. Recognize POSIX/GNU/PAX framing metadata type flags `x`, `g`, `L`, and `K`; cap those metadata records at `(2 * MAX_MEMBER_COUNT) + 1` and their combined payload at 1 MiB. Cap other logical members at 1,000 and their combined declared payload at 100 MiB.

Then open the same bytes with `tarfile.open(fileobj=BytesIO(data), mode="r:")` to validate header checksums and format semantics; drain each regular member from `extractfile(member)` in 1 MiB chunks through one global actual-byte counter. Never call `extract()` or `extractall()`.

- [ ] **Step 5: Implement full gzip stream validation**

Define the exact decompressed-stream ceiling as:

```python
MAX_TAR_METADATA_RECORDS = (2 * MAX_MEMBER_COUNT) + 1
MAX_TAR_METADATA_BYTES = 1 * 1024 * 1024
MAX_TAR_ZERO_BLOCKS = 20
MAX_TAR_STREAM_BYTES = (
    MAX_EXPANDED_BYTES
    + MAX_TAR_METADATA_BYTES
    + ((MAX_MEMBER_COUNT + MAX_TAR_METADATA_RECORDS) * 512)
    + ((MAX_MEMBER_COUNT + MAX_TAR_METADATA_RECORDS) * 511)
    + (MAX_TAR_ZERO_BLOCKS * 512)
)
```

Read `gzip.GzipFile(fileobj=BytesIO(data))` to EOF into a bounded `BytesIO`, rejecting before output exceeds `MAX_TAR_STREAM_BYTES`. Full EOF read must validate gzip CRC/trailer before raw TAR framing validation. This ceiling is only a pre-parse allocation bound; the scanner independently enforces every component limit.

- [ ] **Step 6: Normalize parser/decompressor failures**

Catch `tarfile.TarError`, `gzip.BadGzipFile`, `EOFError`, `OSError`, `ValueError`, and `zlib.error` at the validator boundary and raise only `ArchiveValidationError`.

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run pytest tests/test_markdown_archive_validation.py -q
```

Expected: all archive unit tests pass.

- [ ] **Step 8: Commit TAR support**

```bash
git add webapp/markdown_archive_validation.py tests/test_markdown_archive_validation.py
git commit -m "feat: validate tar markdown attachments"
```

### Task 4: Add discard-only bounded 7z validation

**Files:**
- Modify: `../../../webapp/markdown_archive_validation.py`
- Modify: `../../../tests/test_markdown_archive_validation.py`

- [ ] **Step 1: Add in-memory 7z fixtures**

Build valid and password-protected bytes with `SevenZipFile(BytesIO(), "w", password=...)` and `writestr()`. Add a truncated/corrupt fixture by mutating valid bytes.

- [ ] **Step 2: Write failing 7z tests**

Cover every allowed MIME, normalized content type, magic mismatch, encrypted header/member rejection, member-count limit, declared expanded-size limit, actual decoded-byte limit, corrupt CRC/codec exceptions, and zero filesystem writes.

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
uv run pytest tests/test_markdown_archive_validation.py -k "seven or 7z" -q
```

Expected: new tests fail.

- [ ] **Step 4: Implement a shared hard byte budget and discard writer**

Implement `Py7zIO`/`WriterFactory` objects with no payload buffer:

```python
class _DecodedBudget:
    def consume(self, size: int) -> None:
        self.total += size
        if self.total > MAX_EXPANDED_BYTES:
            raise ArchiveValidationError


class _DiscardWriter(Py7zIO):
    def write(self, data: bytes | bytearray) -> int:
        self._budget.consume(len(data))
        self._size += len(data)
        return len(data)

    def read(self, size: int | None = None) -> bytes:
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._size

    def flush(self) -> None:
        return None

    def size(self) -> int:
        return self._size
```

`_DiscardFactory.create()` returns writers sharing one `_DecodedBudget`; it never receives or returns a filesystem path.

- [ ] **Step 5: Implement two-pass 7z validation**

First `SevenZipFile(BytesIO(data), "r")` pass: reject `needs_password()`, more than 1,000 `list()` entries, `archiveinfo().uncompressed > 100 MiB`, unsupported method names, and `test() is False`. Reopen from a fresh `BytesIO` and call `extractall(factory=_DiscardFactory(...))` to drain actual decoded member bytes.

- [ ] **Step 6: Normalize all library and codec errors**

Contain all exceptions raised inside the `py7zr` open/list/test/extract calls and re-raise `ArchiveValidationError` with no third-party message. Preserve `KeyboardInterrupt` and `SystemExit` by catching `Exception`, not `BaseException`.

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run pytest tests/test_markdown_archive_validation.py -q
```

Expected: all ZIP/TAR/gzip/7z tests pass.

- [ ] **Step 8: Commit 7z support**

```bash
git add webapp/markdown_archive_validation.py tests/test_markdown_archive_validation.py
git commit -m "feat: validate 7z markdown attachments"
```

### Task 5: Add opaque Microsoft Office format classification

**Files:**
- Create: `../../../webapp/markdown_office_formats.py`
- Create: `../../../tests/test_markdown_office_formats.py`

- [ ] **Step 1: Write the failing complete-catalog test**

Create one parameterized case per extension from `2026-08-13-markdown-office-attachments-design.md`. For each, assert case-insensitive family lookup, card-family key, and `application/octet-stream` normalized stored type. Pass empty, generic, incorrect, and vendor MIME values to prove MIME is not consulted.

- [ ] **Step 2: Write failing boundary tests**

Assert `report.docx.exe`, missing extensions, generic exports (`.pdf`, `.csv`, `.xml`, `.html`, OpenDocument), images, and archives are not Office uploads. Assert arbitrary bytes are irrelevant to classification.

- [ ] **Step 3: Run tests and confirm failure**

```bash
uv run pytest tests/test_markdown_office_formats.py -q
```

Expected: FAIL because module is absent.

- [ ] **Step 4: Implement one immutable extension-to-family map**

Export:

```python
OFFICE_CONTENT_TYPE = "application/octet-stream"
OFFICE_EXTENSIONS_BY_FAMILY: Mapping[str, frozenset[str]] = {...}


def office_family_for_filename(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    return OFFICE_FAMILY_BY_EXTENSION.get(suffix)


def is_office_upload(filename: str) -> bool:
    return office_family_for_filename(filename) is not None
```

Generate reverse lookup once at import, fail fast on duplicate extensions, and never accept MIME or payload parameters.

- [ ] **Step 5: Run Office unit tests**

```bash
uv run pytest tests/test_markdown_office_formats.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit Office catalog**

```bash
git add webapp/markdown_office_formats.py tests/test_markdown_office_formats.py
git commit -m "feat: classify opaque Office attachments"
```

### Task 6: Integrate gated uploads, off-loop validation, and overload atomicity

**Files:**
- Modify: `webapp/markdown_images.py:247-358`
- Modify: `../../../tests/test_markdown_image_api.py`
- Modify: `../../../.env.example`

- [ ] **Step 1: Write failing mixed-format API tests**

Upload multiple maximum-five batches covering image, ZIP, 7z, TAR, TAR.GZ, TGZ, and every Office family. Assert successful assets preserve input order, archive content types normalize, Office types normalize to `application/octet-stream`, original bytes are stored unchanged, and invalid items remain ordered errors without blocking later valid items.

- [ ] **Step 2: Write failing feature-gate tests**

With `MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED=false`, assert ZIP and images still upload; 7z, TAR, TAR.GZ, TGZ, and Office formats return ordered `extended_attachment_upload_disabled` item errors; downloads for already-stored extended assets remain enabled.

- [ ] **Step 3: Write failing off-loop and overload tests**

Add:

```python
def test_archive_validation_runs_outside_event_loop(...): ...
def test_mixed_batch_overload_returns_503_before_persisting_any_asset(...): ...
```

For overload, monkeypatch limiter capacity to zero and wait timeout to a few milliseconds; upload image then TAR; assert `503`, `Retry-After`, and no namespace contents. Retry after restoring capacity and assert exactly one copy of each asset. Add a separate mixed image/Office case proving opaque Office uploads do not acquire the decompression limiter.

- [ ] **Step 4: Run new API tests and confirm failure**

Run:

```bash
uv run pytest tests/test_markdown_image_api.py -k "extended or overload or off_loop or feature_gate" -q
```

Expected: new tests fail.

- [ ] **Step 5: Add runtime upload gate**

Implement a strict false-value parser for `MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED` (default enabled). Apply it to post-ZIP archives and Office uploads only; never apply while loading stored assets. Document rollout/rollback use in `../../../.env.example`.

- [ ] **Step 6: Acquire one archive slot before any batch write**

After multipart parsing and before the upload loop, detect whether any `UploadFile` is an archive candidate. Acquire a module-level two-slot semaphore with bounded wait; on timeout raise:

```python
HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail="Archive validation is busy",
    headers={"Retry-After": "2"},
)
```

Release in `finally`. Do not call `_store_asset` before successful acquisition.

- [ ] **Step 7: Dispatch archive validation to worker threads**

Use `await asyncio.to_thread(_validate_asset, ...)` for archive candidates. Keep cheap image signature checks inline. Process archive candidates sequentially inside the held batch slot.

- [ ] **Step 8: Generalize ordered archive errors**

Keep code `unsupported_or_mismatched_archive` for archive extension/MIME candidates and change its message to `Only valid ZIP, 7Z, TAR, TAR.GZ, and TGZ archives are supported`. Preserve image errors for image-declared MIME or image extensions. Return `unsupported_or_mismatched_attachment` for every other non-Office upload. Office extensions accept arbitrary bytes and ignore MIME. Preserve `{assets, errors}` and all route paths.

- [ ] **Step 9: Run focused API tests**

Run:

```bash
uv run pytest tests/test_markdown_image_api.py -q
```

Expected: all API tests pass.

- [ ] **Step 10: Commit upload integration**

```bash
git add webapp/markdown_images.py tests/test_markdown_image_api.py .env.example
git commit -m "feat: upload extended markdown archives"
```

### Task 7: Generalize authenticated retrieval and cleanup

**Files:**
- Modify: `webapp/markdown_images.py:200-235,360-422`
- Modify: `../../../tests/test_markdown_image_api.py`

- [ ] **Step 1: Write failing retrieval tests for every new format**

For archive view and `/download`, assert exact opaque bytes, normalized `Content-Type`, attachment `Content-Disposition`, UTF-8 safe filename, and `private, no-store`; mutate each stored archive to same-size invalid bytes and assert `404`. For Office, assert arbitrary exact bytes, `application/octet-stream`, attachment disposition on both endpoints, `private, no-store`, and `X-Content-Type-Options: nosniff`; same-size Office mutation remains serveable by design because only metadata/extension/size are checked.

- [ ] **Step 2: Write failing combined cleanup test**

Store images, every archive type, and every Office family across valid five-item batches; delete namespace once; assert count covers all assets and second delete returns zero.

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
uv run pytest tests/test_markdown_image_api.py -k "archive_routes or corrupt_archive or cleanup" -q
```

Expected: new format cases fail.

- [ ] **Step 4: Generalize stored metadata and attachment detection**

Build `_STORED_CONTENT_TYPES` from image types, exported `ARCHIVE_CONTENT_TYPES`, and `OFFICE_CONTENT_TYPE`. Read the full payload for every stored archive and re-run `validate_archive()`. For Office, verify stored type, cataloged filename extension, and byte size only. Force attachment disposition for all archives and Office assets and add `X-Content-Type-Options: nosniff`.

- [ ] **Step 5: Offload retrieval revalidation under the same limiter**

Read metadata/stat safely, then acquire an archive validation slot and run full archive payload revalidation with `asyncio.to_thread`. Do not consult the upload feature flag. Return overload `503` rather than running unbounded decompression.

- [ ] **Step 6: Run API suite**

Run:

```bash
uv run pytest tests/test_markdown_image_api.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit retrieval and cleanup**

```bash
git add webapp/markdown_images.py tests/test_markdown_image_api.py
git commit -m "feat: download extended markdown archives"
```

### Task 8: Backend verification

**Files:**
- Verify all changed backend files.

- [ ] **Step 1: Run focused tests**

```bash
uv run pytest tests/test_markdown_archive_validation.py tests/test_markdown_image_api.py -q
```

Expected: all pass.

- [ ] **Step 2: Run Ruff**

```bash
uv run ruff check webapp/markdown_archive_validation.py webapp/markdown_office_formats.py webapp/markdown_images.py tests/test_markdown_archive_validation.py tests/test_markdown_office_formats.py tests/test_markdown_image_api.py
```

Expected: no findings.

- [ ] **Step 3: Run OpenAPI/contract tests**

```bash
uv run python scripts/snapshot_openapi.py --check
uv run pytest tests/test_frontend_api_contract.py::test_frontend_used_paths_are_present_in_openapi -q
```

Expected: snapshot and frontend-used route contract remain unchanged.

- [ ] **Step 4: Run broad regression suite with known unrelated snapshot isolated**

```bash
uv run pytest -q -k "not test_aws_image_runtime_contract_matches_snapshot_runtime"
```

Then run `uv run pytest tests/test_langgraph_snapshot.py::test_aws_image_runtime_contract_matches_snapshot_runtime -q` separately and record whether its previously known LangGraph runtime-version snapshot mismatch remains unchanged.

- [ ] **Step 5: Inspect Threadroot score**

```bash
threadroot score latest
```

Expected: verification evidence references focused archive suites.

- [ ] **Step 6: Confirm clean branch state**

```bash
git status --short
git log --oneline main..HEAD
```

Expected: no uncommitted changes; only scoped archive commits.
