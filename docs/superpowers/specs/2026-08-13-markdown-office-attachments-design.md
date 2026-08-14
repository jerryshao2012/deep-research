# Markdown Office Attachments Design

## Goal

Extend synchronized intro Markdown attachments to native and legacy Microsoft Office file families. Office files are opaque binary downloads: they are never parsed, previewed, extracted, converted, inspected for macros, or executed.

This design extends `2026-08-13-markdown-archive-attachments-design.md`. Existing PNG, JPEG, GIF, WebP, ZIP, 7z, TAR, TAR.GZ, and TGZ behavior remains unchanged except that new archive formats and Office formats share one rollout gate for new uploads, and the shared card's unknown-filename fallback becomes the type-neutral label `Attachment`.

## Repositories

- Backend: `/Users/jerryshao/Documents/projects/IBM/ai/deep-research`
- Frontend: `/Users/jerryshao/Documents/projects/IBM/ai/bmo-deepagent-ui`

Backend support deploys before frontend selection support. Both repositories merge back to local `main` only after focused and broad verification.

## Stable Contract

- Keep frontend routes under `/api/markdown-images/{markdown_id}/...`.
- Keep backend routes under `/markdown-threads/{markdown_id}/images/...`.
- Keep ordered multipart field name `files` and response shape `{ assets, errors }`.
- Keep canonical attachment Markdown:

  ```markdown
  [filename.ext](/__markdown-attachment/<uuid> "size=<decimal-bytes>")
  ```

- Never put Office family, extension, MIME type, or attachment type in logical or HTTP URL paths.
- Keep 10 MiB per file and five files per paste/drop across images, archives, and Office files combined.
- Preserve ordered partial success: one invalid item does not block later valid items in the batch.

## Office Extension Catalog

Filename extension is authoritative and case-insensitive. Browser-declared MIME is ignored for Office files.

| Family | Accepted extensions | Card label |
|---|---|---|
| Word | `.doc`, `.docx`, `.docm`, `.dot`, `.dotx`, `.dotm`, `.rtf`, `.wbk` | `Word document` |
| Excel | `.xls`, `.xlsx`, `.xlsm`, `.xlsb`, `.xlt`, `.xltx`, `.xltm`, `.xla`, `.xlam`, `.xll`, `.xlm`, `.xlw` | `Excel workbook` |
| PowerPoint | `.ppt`, `.pptx`, `.pptm`, `.pot`, `.potx`, `.potm`, `.pps`, `.ppsx`, `.ppsm`, `.ppa`, `.ppam`, `.sldx`, `.sldm`, `.thmx` | `PowerPoint presentation` |
| Access | `.accdb`, `.accde`, `.accdr`, `.accdt`, `.accdc`, `.mdb`, `.mde`, `.mda`, `.mdw`, `.ade`, `.adp` | `Access database` |
| Visio | `.vsd`, `.vsdx`, `.vsdm`, `.vss`, `.vssx`, `.vssm`, `.vst`, `.vstx`, `.vstm`, `.vdw`, `.vdx`, `.vsx`, `.vtx` | `Visio drawing` |
| OneNote | `.one`, `.onepkg`, `.onetoc2` | `OneNote file` |
| Project | `.mpp`, `.mpt`, `.mpd`, `.mpx` | `Project file` |
| Outlook | `.pst`, `.ost`, `.msg`, `.oft` | `Outlook file` |
| Publisher | `.pub` | `Publisher document` |
| InfoPath | `.xsn` | `InfoPath form` |

Generic export/interchange formats such as PDF, CSV, XML, HTML, OpenDocument, text, video, and standalone image formats are not accepted through the Office allowlist. Existing supported images continue through the image pipeline.

## Backend Design

Add a focused Office-format module containing the extension-to-family map, `office_family_for_filename()`, `is_office_upload()`, and the stored Office content type. The route layer uses those exports rather than duplicating extension lists.

For an Office filename:

1. Sanitize the original filename with existing `safe_filename()` behavior.
2. Match the final case-insensitive extension against the catalog. Compound or misleading names such as `report.docx.exe` do not match.
3. Ignore `UploadFile.content_type` and do not inspect payload contents.
4. Enforce the existing 10 MiB file and five-item request limits.
5. Persist original bytes unchanged in the existing per-asset directory.
6. Store normalized metadata content type `application/octet-stream`, safe original filename, and byte size.

Retrieval validates metadata shape, filename extension, stored content type, and byte size. It intentionally does not validate Office structure or detect same-size content mutation. Both existing view and download endpoints return Office files with:

- `Content-Type: application/octet-stream`
- `Content-Disposition: attachment` with existing safe UTF-8 filename behavior
- `Cache-Control: private, no-store`
- `X-Content-Type-Options: nosniff`

Namespace deletion remains format-neutral and removes images, archives, and Office assets together after pending uploads settle.

Backend item-error classification is deterministic and ordered: archive extension or archive-specific MIME produces `unsupported_or_mismatched_archive`; an image-declared MIME or supported image extension produces `unsupported_or_mismatched_image`; every other non-Office upload produces `unsupported_or_mismatched_attachment`. `is_office_upload()` identifies only cataloged Office extensions and is not used to guess unsupported files.

## Frontend Design

Add one pure attachment-format catalog shared by clipboard/drop validation, authoritative upload-response classification, and card descriptions.

- Office selection uses filename extension only; empty, generic, incorrect, or vendor MIME values do not change acceptance.
- Clipboard/drop preserves one mixed ordered batch and the existing five-item cap.
- Backend `application/octet-stream` Office assets produce canonical attachment Markdown. Images continue using image Markdown; normalized archives continue using attachment Markdown.
- Rendering infers only the human-readable card family from the safe filename. It never uses filename to choose image versus attachment Markdown.
- Reuse `SyncedMarkdownAttachment` with an appropriate Office/category icon, filename truncation, binary size label, always-visible accessible Download action, and existing failure toast.
- Rendering does not fetch Office bytes and does not reuse `DocumentViewerPanel`; Office attachments have no preview.
- Unknown filenames rendered through a pre-existing canonical attachment reference fall back to `Attachment` without blocking download.
- Existing frontend proxy GET handling forwards backend `Content-Disposition`, `Content-Type`, `Cache-Control`, and `X-Content-Type-Options` unchanged for both view and download forms. It does not infer a type from URL or filename.

## Rollout and Rollback

Use one upload-only gate for formats added after ZIP:

- Backend: `MARKDOWN_EXTENDED_ATTACHMENT_UPLOADS_ENABLED`
- Frontend: `NEXT_PUBLIC_MARKDOWN_EXTENDED_ATTACHMENTS_ENABLED`

When false, images and ZIP continue uploading; 7z, TAR, TAR.GZ, TGZ, and Office files cannot start new uploads. Read/download support never consults the gate.

Deployment order:

1. Deploy backend with extended read support enabled and extended uploads disabled.
2. Smoke-test existing images/ZIP and stored extended downloads.
3. Enable backend extended uploads.
4. Deploy frontend with extended selection enabled.

Rollback order:

1. Disable/redeploy frontend extended selection.
2. Disable backend extended uploads.
3. Keep the extended backend read/download implementation deployed for already-stored assets; once extended assets exist, roll forward rather than returning to a pre-support backend binary.

## Error Handling

- Unsupported filename extension: ordered `unsupported_file` client rejection or `unsupported_or_mismatched_attachment` backend item error.
- Disabled extended upload: ordered `extended_attachment_upload_disabled` item error.
- File over 10 MiB: existing ordered `file_too_large` item error.
- More than five mixed files: existing request/gesture cap.
- Unsafe filename: existing ordered `invalid_filename` item error.
- Storage failure: existing request rollback behavior.
- Download failure: existing attachment toast; card remains visible.

No parser, decompressor, macro engine, Office viewer, or executable loader is invoked for Office files.

## Tests

Frontend tests cover:

- Every catalog extension and case-insensitive matching.
- MIME independence, misleading suffix rejection, shared 10 MiB limit, and combined five-item cap.
- Mixed image/archive/Office ordering and authoritative backend content-type Markdown generation.
- Type-neutral logical and HTTP URLs with no family/format keywords.
- Every family card label, unknown fallback, filename truncation, size fallback, accessibility, Download behavior, disabled-download context, and failure toast.
- Proxy regression proving both GET forms preserve attachment disposition, octet-stream type, no-store, and nosniff headers.
- Explicit component regression proving Office card render performs no fetch and never mounts or invokes `DocumentViewerPanel` or an Office viewer.
- Unchanged image preview and ordinary Markdown link/image behavior.
- Upload gate disabling new extended formats without disabling stored-card rendering/download.

Backend tests cover:

- Every catalog extension with arbitrary opaque bytes and arbitrary/empty declared MIME.
- Rejection of unsupported and misleading final extensions.
- Normalized `application/octet-stream` metadata and exact opaque persistence.
- Mixed ordering/partial success, shared limits, safe filenames, storage rollback, and combined namespace cleanup.
- Authenticated view/download forcing attachment, octet-stream, no-store, nosniff, safe `Content-Disposition`, and exact bytes.
- Retrieval metadata/size/extension checks without Office structure validation.
- Upload gate behavior and read compatibility for assets stored before the gate is disabled.
- Unchanged image and structurally validated archive behavior.

Verification remains:

- Backend focused asset suites, Ruff, OpenAPI snapshot, and broad regression suite with the known unrelated LangGraph runtime snapshot checked separately.
- Frontend asset/card tests, preview-sync tests, contract check, deployment-script tests, lint, and production build.
