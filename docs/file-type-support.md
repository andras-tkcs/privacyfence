# File type support

PrivacyFence extracts a bounded, human-readable preview from supported files so approval cards and PII checks can reason about file content without treating arbitrary binary data as text.

`src/privacyfence/text_extraction.py` and the connector-specific download/preview paths are authoritative.

## Supported extraction

| Type | Current behavior |
|---|---|
| Plain text / CSV / JSON / similar text | decoded as text with bounded output |
| HTML | converted to readable text rather than exposing raw markup as the preview |
| PDF | text extracted with `pypdf` |
| DOCX | document XML parsed with hardened XML handling and converted to text |
| PPTX | slide text extracted from the package XML |
| XLSX | workbook cell values extracted with bounded rows/columns |
| ZIP | archive member names/sizes can be summarized; arbitrary nested binary content is not recursively executed |
| Images | file metadata/attachment presence can be shown; PrivacyFence does not perform OCR as part of the normal extraction path |
| Unknown/binary | metadata-only or safe fallback rather than attempting to decode arbitrary bytes as trusted text |

Extraction is for preview/privacy inspection, not for recreating the original document with full fidelity.

## Bounds

Content previews and PII scan inputs are intentionally bounded. Connector tools also apply provider/tool-specific prefetch limits so a very large remote file/attachment does not require downloading/parsing the entire object merely to render an approval preview.

The approval card may therefore show file metadata (name/type/size/page or sheet information) plus only the extracted prefix needed for review.

## Spreadsheets

XLSX extraction is limited to a bounded number of rows/columns rather than serializing an entire workbook. Cell formulas are treated as document content/values according to the current `openpyxl` extraction code; PrivacyFence does not execute workbook macros.

## Office XML safety

DOCX/PPTX are ZIP containers containing XML. The extraction code uses hardened XML parsing for attacker-controlled embedded XML rather than relying on an unrestricted generic XML parser.

## PDFs

PDF extraction is best-effort text extraction. Scanned/image-only PDFs can contain little or no extractable text because OCR is not part of the normal pipeline. The approval UI should still show the available file metadata so the reviewer understands what object is being requested.

## Org-mode delivery

Preview/PII limits are independent from final approved file delivery. A tool can inspect a bounded preview and, after approval, deliver the complete file inline or through the org-mode staged-download path.

See [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## Security rules

- Do not execute embedded scripts/macros/active content to build a preview.
- Bound decompression/extraction work according to the implementation's limits.
- Treat extracted text as untrusted provider content when rendering HTML.
- Do not include credentials, local file paths, or unrelated provider data in the preview.
- A failed/unsupported extraction must not silently turn a protected read into an unreviewed release.

## Changing support

When adding/changing a file type, update the extractor, unit tests with benign/adversarial samples, any relevant connector prefetch logic, approval-card behavior, and this table in the same PR.
