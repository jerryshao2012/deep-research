# Markdown Archive Attachments Design

## Goal

Extend synchronized Markdown attachments from ZIP-only support to `.7z`, `.tar`, `.tar.gz`, and `.tgz` while preserving existing image and ZIP behavior, existing HTTP routes, shared limits, type-neutral logical URLs, opaque storage, download behavior, and cleanup.

## Repositories

- Backend: `/Users/jerryshao/Documents/projects/IBM/ai/deep-research`
- Frontend: `/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui`

Both changes ship together. Backend deploys first so newly accepted frontend formats cannot reach an incompatible upload service.

## Stable Contract

- Keep frontend routes under `/api/markdown-images/{markdown_id}/...`.
- Keep backend routes under `/markdown-threads/{markdown_id}/images/...`.
- Keep ordered multipart field name `files` and response shape `{ assets, errors }`.
- Keep canonical attachment Markdown:

  ```markdown
  [filename.ext](/__markdown-attachment/<uuid> "size=<decimal-bytes>")
  ```

- Never put archive format, extension, or MIME type in logical or HTTP URL paths.
- Keep 10 MiB per uploaded file and five files per paste/drop across images and all archive formats.
- Keep archives opaque: validate in memory, persist original bytes, never extract to filesystem.

## Accepted Formats

Backend returns normalized content types:

| Filename | Accepted declared MIME values | Signature/structure | Returned content type |
|---|---|---|---|
| `.zip` | empty, `application/octet-stream`, `application/zip`, `application/x-zip-compressed` | ZIP magic, bounded members/expanded size, every physical member readable with valid CRC, no encryption | `application/zip` |
| `.7z` | empty, `application/octet-stream`, `application/x-7z-compressed`, `application/7z`, `application/vnd.7zip` | 7z magic, valid header, bounded members/expanded size, no encryption, every member drained with packed/member CRC validation where supplied | `application/x-7z-compressed` |
| `.tar` | empty, `application/octet-stream`, `application/x-tar`, `application/tar` | valid TAR headers/checksums, bounded members/expanded size, every regular member readable | `application/x-tar` |
| `.tar.gz`, `.tgz` | empty, `application/octet-stream`, `application/gzip`, `application/x-gzip`, `application/x-compressed-tar`, `application/x-gtar`, `application/x-tgz` | gzip magic containing valid TAR, bounded members/actual expanded bytes, every regular member readable, gzip stream drained to EOF with valid CRC/trailer | `application/gzip` |

Extension, declared MIME, and detected structure must agree. Disguised files, encrypted 7z archives, malformed compression streams, unsupported compression methods, excessive member counts, or excessive declared expanded size become ordered per-item `unsupported_or_mismatched_archive` errors. A bad item does not prevent later valid files in the same batch from succeeding.

Use existing archive safety bounds: at most 1,000 members and at most 100 MiB expanded member content. Enforce both declared totals and bytes actually produced while validating. For TAR framing, permit only the deterministic 512-byte headers, padding, and end markers associated with the bounded member set; framing bytes do not weaken the 100 MiB member-content cap. These bounds limit integrity checks; upload bytes remain capped at 10 MiB.

The 7z MIME list deliberately includes the IANA-style browser value, the established `x-` value, generic/empty clipboard values, and two compatibility values observed from upload clients. MIME acceptance never substitutes for extension and structural validation.

## Backend Design

Generalize `../../../webapp/markdown_images.py` archive detection into format-specific validators behind one asset validator:

- ZIP continues using Python `zipfile` and drains each `ZipInfo` directly.
- TAR uses Python `tarfile`; inspect members and drain each regular-file payload through a global counting reader without calling extraction APIs. Reject when declared totals or actual bytes cross the limit.
- Compressed TAR wraps the in-memory upload in `gzip.GzipFile` and a hard-counting decompressed reader, then reads TAR sequentially. After TAR end markers, continue draining the gzip reader to EOF so gzip CRC and trailer validation always run. Cap actual member bytes at 100 MiB and allow only bounded TAR framing overhead.
- 7z uses `py7zr`, a pure-Python library supporting Python 3.12 and 3.13. Open `SevenZipFile` on the in-memory upload, reject encrypted headers, password requirements, and encrypted members, then inspect `list()`/`archiveinfo()` metadata before decoding. Drain every member with `extractall(factory=...)` using a custom bounded discard-only `WriterFactory`: writers count actual decoded bytes, retain no payload, and never expose a filesystem path. Run archive CRC testing as an additional check where CRCs exist. Normalize every `py7zr`, codec, password, CRC, truncation, and writer-limit exception into the existing ordered archive error; do not leak library exception text.
- Run all archive parsing/decompression outside the FastAPI event loop through a dedicated worker limiter. A request containing any archive candidate must acquire one batch-level limiter slot before validating or persisting any item; permit at most two such batches per process and process archive files within a batch sequentially. Bound acquisition wait time and return `503 archive_validation_busy` with `Retry-After` before any request asset is written when saturated. This makes overload retry-safe even for mixed image/archive batches and avoids unbounded decompression work.
- Stored metadata records normalized content type, original safe filename, and original byte size.
- Retrieval revalidates payload against stored filename/content type, forces attachment disposition for every archive content type, and preserves safe `Content-Disposition` filename handling.
- Namespace deletion remains unchanged and deletes images plus every archive format.

Add `py7zr` as a locked runtime dependency. OpenAPI route shapes remain unchanged.

## Frontend Design

Extend shared Markdown asset helpers:

- Recognize `.7z`, `.tar`, `.tar.gz`, and `.tgz` using allowed common/generic/empty browser MIME values.
- Preserve clipboard/drop order and shared five-file cap.
- Render attachment Markdown based on authoritative normalized `content_type` returned by backend, not filename alone.
- Continue using `/__markdown-attachment/<uuid>` for every archive.
- Generalize attachment card description to `ZIP archive`, `7Z archive`, `TAR archive`, or `Gzipped TAR archive`, inferred from safe display filename. Unknown attachment filenames fall back to `Archive`.
- Keep size formatting, truncation, accessibility, always-visible Download action, download-failure toast, and no render-time byte fetch.

## Tests

Frontend tests cover supported MIME/extension combinations, mixed ordering, shared limits, authoritative content-type Markdown generation, card labels for all formats, unchanged image/ordinary-link behavior, and the absence of format-specific URL paths.

Backend tests create real ZIP, 7z, TAR, and gzipped-TAR bytes. They cover accepted MIME variants (including every listed 7z value), normalized metadata, mixed ordering/partial success, signature and extension mismatch, encrypted headers/members, corrupt/unsupported archives, gzip CRC/trailer corruption, declared-versus-actual expanded-size limits, normalized `py7zr`/codec exceptions, opaque persistence, exact authenticated downloads, safe filenames, revalidation after corruption, combined cleanup, event-loop offloading, concurrency limiting, overload response behavior, and a mixed image/archive overload retry proving zero assets are persisted before a `503`.

Verification:

- Backend focused asset suite, Ruff, OpenAPI snapshot, and broad regression suite.
- Frontend asset/card tests, preview-sync tests, contract check, lint, and production build.

## Deployment

Gate new-format uploads independently from read/download support. Build and deploy backend with read support enabled and new uploads initially disabled, smoke-test it, enable backend uploads, then deploy frontend acceptance. No route migration or stored-data migration is needed; existing image and ZIP assets remain readable.

Rollback frontend acceptance first, then disable backend new-format uploads while leaving extended read/download validators deployed for already-stored assets. Once any extended archive is stored, do not roll the backend back to a pre-support binary; roll forward with read compatibility instead. Deployment tests cover frontend/backend version skew, disabled-upload behavior, and download access for assets stored before rollback.
