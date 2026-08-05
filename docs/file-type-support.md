# File type support: attachment previews & PII scanning

PrivacyFence previews and PII-scans file content for the four tools that move file bytes across
the gate: `drive_download_file`, `gmail_download_attachment`, `confluence_download_attachment`,
`drive_upload_file`. A content **preview** (rendered in the approval window's details pane) and a
**PII scan** of file content cover the same set of formats, since both are driven by the same
extraction step (`src/privacyfence/text_extraction.py`'s `extract_text()`) — summarized here. See
[`approval-window-content-reference.md`](approval-window-content-reference.md) for how the preview
renders in the approval window, and
[`TECHNICAL_REFERENCE.md`'s PII detection gate section](TECHNICAL_REFERENCE.md#pii-detection-gate)
for the scan's gating behavior.

"Preview" below means an actual rendering in the approval window's details pane (an image, or a
rich Markdown-rendered block) — every download/upload tool already shows a metadata summary (file
name, size, owner, destination, etc.) regardless of type; that part is unaffected by any of this.

## 1. Preview support (what renders)

| Operation | Tool | Preview source | Supported types | Falls back to metadata-only when |
|---|---|---|---|---|
| Download | `drive_download_file` | Drive's own `thumbnailLink` (a small, pre-generated preview image Drive serves for many file types) | Whatever Drive generated a thumbnail for — commonly Docs, Sheets, Slides, images, PDFs; not guaranteed for every file | No `thumbnailLink` present, or the fetch fails, and extraction (below) also finds nothing |
| Download | `gmail_download_attachment` | The attachment's own bytes, fetched in full | `image/*` (rendered as an image) or anything `extract_text()` recognizes (rendered as Markdown), ≤5MB | Neither an image nor an extractable type, or size over the cap, or the fetch fails |
| Download | `confluence_download_attachment` | The attachment's own bytes, fetched in full via its Confluence download link | Same as `gmail_download_attachment`, ≤5MB | Same as `gmail_download_attachment` |
| Upload (`local_path`) | `drive_upload_file` | The local file's own bytes, read from disk | Same as above (via `mimetypes.guess_type` on the file name), ≤5MB | Neither an image nor an extractable type, unreadable file, or size over the cap |
| Upload (`content_base64`) | `drive_upload_file` | The already-decoded inline bytes | Same as above (via `mimetypes.guess_type` on the `name` argument) | Neither an image nor an extractable type, or no `name` given to guess from |

Note the asymmetry: Drive downloads get the broadest coverage because Drive itself generates the
thumbnail server-side (so a Doc, Slide, or PDF can get a real preview without PrivacyFence knowing
how to render that format at all) — Gmail attachments, Confluence attachments, and Drive uploads
have no such service to lean on, so they fall through to `extract_text()`'s own format list below.

Pre-existing and unrelated to this work: `drive_get_file_content` already renders PDFs via an
inline `<embed>` when Drive's own category policy allows it (`pdf_bytes`) — a different tool, a
different mechanism, included here only for completeness.

### Rich Markdown preview (`extract_text()` + `markdown_to_html.py`)

For any of the five tools above, once bytes are in hand and the content isn't an image, the
non-image fallback is the file's own extracted content — rendered richly, not dumped as flat text.
Since none of these tools ever return file content to Claude at all, showing the human the real
content here is strictly more useful than a visual-only thumbnail: a formerly page-shaped preview
image told the reviewer "this is a two-page document," while the extracted content tells them
what it actually says.

Two cooperating modules make this work:

- **`src/privacyfence/text_extraction.py`**'s `extract_text()` turns a format's own bytes into
  Markdown syntax (headings, bold/italic, bullet/numbered lists, pipe tables) wherever the format
  has real structure to preserve, or plain text otherwise. This is the same text that feeds the PII
  scan (§2 below) — one extraction, two uses.
- **`src/privacyfence/markdown_to_html.py`**'s `markdown_to_html()` renders that Markdown back into
  real HTML for the approval window's WebKit-based details pane — real `<h1>`-`<h6>`, `<strong>`,
  `<em>`, `<ul>`/`<ol>`, `<table>`, not literal `#`/`**`/`|` characters. Wired in via
  `approval_window_html.py`'s `{"type": "markdown", ...}` preview block (see
  [`approval-window-content-reference.md`](approval-window-content-reference.md)).

| Format | MIME type(s) | What's preserved |
|---|---|---|
| Plain text / CSV | `text/*` (except `text/html`) | Verbatim, UTF-8 decoded |
| HTML | `text/html` | Headings, bold/italic, lists, links, tables — via `html_to_text.py`'s `html_to_markdown()` |
| PDF | `application/pdf` | Flat text only (`pypdf`'s per-page `extract_text()` has no structure to extract beyond reading order) |
| Word | `.docx` (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`) | Headings (`Heading1`-`Heading6`/`Title` styles), bullet/numbered paragraphs, bold/italic runs |
| PowerPoint | `.pptx` (`application/vnd.openxmlformats-officedocument.presentationml.presentation`) | One `## Slide N` heading per slide, plus that slide's own bullet/paragraph text with bold/italic |
| Excel | `.xlsx` (`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`) | One Markdown table per sheet (via `openpyxl`, already a hard dependency), capped at 200 rows × 20 columns |
| Zip archives | `application/zip` | A table of member file names and sizes (not their content) — capped at 200 entries |
| Images | `image/*` | None — no OCR, deliberately out of scope; these render as an actual image instead (see §1 above) |
| Everything else (video, audio, unrecognized binary) | — | None — degrades to the metadata-only preview |

Confluence page bodies (`confluence_get_page`/`confluence_get_page_by_title`) go through the same
`html_to_markdown()` → `markdown_to_html()` pipeline directly on the page's XHTML storage format,
independent of the four download/upload tools above.

Formatting genuinely dropped, on purpose: fonts, colors, page layout, images embedded in a
document's own body. What's kept is exactly what makes a document legible as a document — its
headings, emphasis, lists, and tabular data.

## 2. PII scan support (what content gets analyzed)

`src/privacyfence/text_extraction.py` extracts text from fetched bytes to feed the regex-based PII
scan (`pii_detector.py`) — the same extraction §1 uses for the preview. Extraction is best-effort
and never raises — an unsupported or corrupt format just contributes no text, same as if the
attachment weren't there.

The supported formats and MIME types are exactly the table in §1 above (`extract_text()` is the one
function behind both the preview and the scan). Extracted text is capped at
`text_extraction.MAX_SCAN_CHARS` (20,000 characters) before it reaches the detector.

**Which tools actually reach this path:**

- `drive_download_file` scans regardless of file type — it reuses the existing, already-capped
  `DriveClient.get_file_content()` fetch (100KB), which itself decides text-vs-binary handling per
  file; whatever comes back is run through `extract_text()`.
- `gmail_download_attachment` only prefetches bytes at all — for either the preview or the scan —
  when `is_prefetch_worthy()` recognizes the attachment's MIME type (image, text, or one of the
  extractable document/archive types above) and it's ≤5MB (Gmail has no partial-fetch API, so
  scanning means fully fetching the attachment first). Any other type keeps today's unscanned,
  no-preview behavior.
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
