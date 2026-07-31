# File type support: attachment previews & PII scanning

PrivacyFence previews and PII-scans file content for the four tools that move file bytes across
the gate: `drive_download_file`, `gmail_download_attachment`, `confluence_download_attachment`,
`drive_upload_file`. A content
**preview** (image render) and a **PII scan** of file content cover different file types for
different reasons, summarized here. See
[`approval-window-content-reference.md`](approval-window-content-reference.md) for how the preview
renders in the approval window, and
[`TECHNICAL_REFERENCE.md`'s PII detection gate section](TECHNICAL_REFERENCE.md#pii-detection-gate)
for the scan's gating behavior.

"Preview" below means an actual visual render in the approval window's details pane (an image
thumbnail) — every download/upload tool already shows a metadata summary (file name, size, owner,
destination, etc.) regardless of type; that part is unaffected by any of this.

## 1. Preview support (what renders visually)

| Operation | Tool | Preview source | Supported types | Falls back to metadata-only when |
|---|---|---|---|---|
| Download | `drive_download_file` | Drive's own `thumbnailLink` (a small, pre-generated preview image Drive serves for many file types) | Whatever Drive generated a thumbnail for — commonly Docs, Sheets, Slides, images, PDFs; not guaranteed for every file | No `thumbnailLink` present, or the fetch fails, and QuickLook (below) is off or also misses |
| Download | `gmail_download_attachment` | The attachment's own bytes, fetched in full | `image/*` only, ≤5MB | Any non-image type, or size over the cap, or the fetch fails, and QuickLook also misses |
| Download | `confluence_download_attachment` | The attachment's own bytes, fetched in full via its Confluence download link | `image/*` only, ≤5MB | Any non-image type, or size over the cap, or the fetch fails, and QuickLook also misses |
| Upload (`local_path`) | `drive_upload_file` | The local file's own bytes, read from disk | `image/*` only (via `mimetypes.guess_type` on the file name), ≤5MB | Any non-image type, unreadable file, or size over the cap, and QuickLook also misses |
| Upload (`content_base64`) | `drive_upload_file` | The already-decoded inline bytes | `image/*` only (via `mimetypes.guess_type` on the `name` argument) | Non-image type, or no `name` given to guess from, and QuickLook also misses |

Note the asymmetry: Drive downloads get the broadest coverage because Drive itself generates the
thumbnail server-side (so a Doc, Slide, or PDF can get a real preview without PrivacyFence knowing
how to render that format at all) — Gmail attachments, Confluence attachments, and Drive uploads
have no such service to lean on, so the only images that decode without a renderer of our own are
literal image files.

Pre-existing and unrelated to this work: `drive_get_file_content` already renders PDFs via a native
`PDFView` when Drive's own category policy allows it (`pdf_bytes`) — a different tool, a different
mechanism, included here only for completeness.

### QuickLook fallback (off by default)

`src/privacyfence/quicklook_preview.py` adds a second preview source, toggled from the menu bar
("QuickLook Previews → Enabled") or `quicklook_preview.enabled` in `settings.yaml`: macOS's own
QuickLook renderer (`quicklookd`, out-of-process), which recognizes far more formats than the
direct-image path above — PDFs, Office documents, and anything else Quick Look in Finder can
thumbnail. It only ever runs as a **fallback**, after the image-preview check above has already
failed (or doesn't apply) for whatever bytes were already fetched — it doesn't change *which* files
get fetched in the first place, only what's attempted with bytes already in hand (i.e. today, that
means the same PDF/DOCX/PPTX content already fetched for the PII scan, per the table below).

Since `QLThumbnailGenerator`'s real API is callback-based, `generate_thumbnail()` bridges it into a
synchronous call bounded by `quicklook_preview.max_wait_seconds` (5 seconds by default, set in
`settings.yaml`, not menu-bar configurable) — a slow or hung render times out and falls back to the
same metadata-only view a disabled toggle or an unsupported format would, never blocking the
approval popup indefinitely.

## 2. PII scan support (what content gets analyzed)

`src/privacyfence/text_extraction.py` extracts plain text from fetched bytes to feed the regex-based
PII scan (`pii_detector.py`). Extraction is best-effort and never raises — an unsupported or corrupt
format just contributes no text, same as if the attachment weren't there.

| Format | MIME type(s) | Extraction method |
|---|---|---|
| Plain text / CSV | `text/*` | UTF-8 decode |
| PDF | `application/pdf` | `PDFDocument.string()` (PDFKit — the same dependency the preview work already uses) |
| Word | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | stdlib `zipfile` + `xml.etree.ElementTree` over `word/document.xml` |
| PowerPoint | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | stdlib `zipfile` + `xml.etree.ElementTree` over `ppt/slides/slide*.xml` |
| Images | `image/*` | none — no OCR, deliberately out of scope |
| Everything else (video, audio, archives, XLSX, unrecognized binary) | — | none |

Extracted text is capped at `text_extraction.MAX_SCAN_CHARS` (20,000 characters) before it reaches
the detector.

**Which tools actually reach this path:**

- `drive_download_file` scans regardless of file type — it reuses the existing, already-capped
  `DriveClient.get_file_content()` fetch (100KB), which itself decides text-vs-binary handling per
  file; whatever comes back is run through `extract_text()`.
- `gmail_download_attachment` only prefetches bytes at all — for either the preview or the scan —
  when the attachment is `image/*`, `text/*`, PDF, DOCX, or PPTX, and ≤5MB (Gmail has no partial-fetch
  API, so scanning means fully fetching the attachment first). Any other type keeps today's
  unscanned, no-preview behavior.
- `confluence_download_attachment` applies the exact same `is_prefetch_worthy()`/≤5MB gate as
  `gmail_download_attachment`, for the same reason: Confluence's attachment download link has no
  partial-fetch/range support either.
- `drive_upload_file` gates the two input paths differently. For `local_path`, the same
  `is_prefetch_worthy()`/size-cap check as the preview table above gates the disk read itself (an
  unbounded local file, so it's worth guarding before reading at all) — the extracted mime type is
  then reused for both the image-preview check and the scan. For `content_base64`, the bytes are
  already fully decoded in memory regardless (to measure size), so there's no separate gate: any
  guessed mime type is passed straight to `extract_text()`, which simply returns "" on its own for
  a type it doesn't recognize.

**Read vs. write gating differs**: on the read side, a match always forces the same confirmation
every `gate="review"` tool's PII scan does. On the write side, only `drive_upload_file`'s extracted
content gets that real, forced-confirmation treatment — every other write tool still only gets the
weaker, informational-only amber banner over its own drafted text, per this codebase's "writes don't
get the real PII gate" rule (see `TECHNICAL_REFERENCE.md` for why `drive_upload_file` is the one
deliberate exception).
