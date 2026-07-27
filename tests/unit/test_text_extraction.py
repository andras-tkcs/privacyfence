"""Unit tests for privacyfence.text_extraction -- best-effort plain-text
extraction feeding pii_detector.py's scan for attachment/download/upload
content.

The one invariant that matters most: extract_text() never raises, for any
input -- a format it can't handle, or fails to parse, must degrade to "",
not propagate an exception into gate.py's request path. Most of these tests
exist to pin that for each supported/unsupported format and each way parsing
can go wrong (corrupt bytes, not actually a zip, malformed XML).
"""
from __future__ import annotations

import io
import zipfile

from privacyfence.text_extraction import MAX_SCAN_CHARS, extract_text, is_prefetch_worthy

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Minimal single-page PDF -- enough for PDFDocument to parse successfully and
# report its text, not a claim this is a spec-perfect PDF. Reuses the same
# fixture shape test_approval_window.py's TestPdfViewEmbed already pinned as
# parseable by the same PDFDocument API this module calls.
VALID_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n"
    b"trailer << /Size 4 /Root 1 0 R >>\n"
    b"startxref\n0\n%%EOF"
)


def make_docx(*paragraphs: str) -> bytes:
    runs = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{runs}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_pptx(*slide_texts: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, text in enumerate(slide_texts, start=1):
            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t>"
                "</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
    return buf.getvalue()


class TestEmptyOrUnsupported:
    def test_empty_data_returns_empty_string(self):
        assert extract_text(b"", "text/plain") == ""

    def test_unsupported_mime_type_returns_empty_string(self):
        assert extract_text(b"binary junk", "video/mp4") == ""

    def test_image_mime_type_returns_empty_string(self):
        # No OCR -- images are out of scope, same as any other unsupported format.
        assert extract_text(b"\x89PNGfakebytes", "image/png") == ""

    def test_mime_type_with_charset_suffix_is_handled(self):
        assert extract_text(b"hello", "text/plain; charset=utf-8") == "hello"


class TestPlainText:
    def test_decodes_utf8_text(self):
        assert extract_text("héllo wörld".encode("utf-8"), "text/plain") == "héllo wörld"

    def test_invalid_utf8_replaces_instead_of_raising(self):
        result = extract_text(b"\xff\xfe not valid utf-8", "text/plain")
        assert "not valid utf-8" in result

    def test_csv_mime_type_decodes_as_text(self):
        assert extract_text(b"a,b,c\n1,2,3", "text/csv") == "a,b,c\n1,2,3"


class TestPdf:
    def test_valid_pdf_extracts_text_without_raising(self):
        # This minimal fixture has no actual text content (no font/text
        # operators) -- the point here is that a real, parseable PDF returns
        # a string (possibly empty) rather than raising, not that this
        # specific fixture contains extractable text.
        result = extract_text(VALID_PDF, "application/pdf")
        assert isinstance(result, str)

    def test_garbage_pdf_bytes_returns_empty_string(self):
        assert extract_text(b"not a pdf at all", "application/pdf") == ""


class TestDocx:
    def test_extracts_text_from_a_single_paragraph(self):
        docx = make_docx("Please wire the deposit to DE89370400440532013000.")
        assert extract_text(docx, DOCX_MIME) == "Please wire the deposit to DE89370400440532013000."

    def test_extracts_and_joins_multiple_paragraphs(self):
        docx = make_docx("First paragraph.", "Second paragraph.")
        result = extract_text(docx, DOCX_MIME)
        assert "First paragraph." in result
        assert "Second paragraph." in result

    def test_not_a_zip_returns_empty_string(self):
        assert extract_text(b"not a zip file at all", DOCX_MIME) == ""

    def test_zip_without_document_xml_returns_empty_string(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("some/other/file.xml", "<root/>")
        assert extract_text(buf.getvalue(), DOCX_MIME) == ""

    def test_malformed_xml_returns_empty_string(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<unclosed><tag>")
        assert extract_text(buf.getvalue(), DOCX_MIME) == ""


class TestPptx:
    def test_extracts_text_from_a_single_slide(self):
        pptx = make_pptx("Slide one has an IBAN DE89370400440532013000.")
        assert extract_text(pptx, PPTX_MIME) == "Slide one has an IBAN DE89370400440532013000."

    def test_extracts_and_joins_multiple_slides_in_order(self):
        pptx = make_pptx("First slide.", "Second slide.")
        result = extract_text(pptx, PPTX_MIME)
        assert result.index("First slide.") < result.index("Second slide.")

    def test_not_a_zip_returns_empty_string(self):
        assert extract_text(b"not a zip file at all", PPTX_MIME) == ""

    def test_zip_without_any_slides_returns_empty_string(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("some/other/file.xml", "<root/>")
        assert extract_text(buf.getvalue(), PPTX_MIME) == ""


class TestSizeCap:
    def test_extracted_text_is_truncated_to_max_scan_chars(self):
        long_text = "a" * (MAX_SCAN_CHARS + 500)
        result = extract_text(long_text.encode("utf-8"), "text/plain")
        assert len(result) == MAX_SCAN_CHARS


class TestIsPrefetchWorthy:
    def test_image_types_are_worthy(self):
        assert is_prefetch_worthy("image/png") is True
        assert is_prefetch_worthy("image/jpeg") is True

    def test_text_types_are_worthy(self):
        assert is_prefetch_worthy("text/plain") is True
        assert is_prefetch_worthy("text/csv") is True

    def test_extractable_document_types_are_worthy(self):
        assert is_prefetch_worthy("application/pdf") is True
        assert is_prefetch_worthy(DOCX_MIME) is True
        assert is_prefetch_worthy(PPTX_MIME) is True

    def test_unrecognized_types_are_not_worthy(self):
        assert is_prefetch_worthy("application/octet-stream") is False
        assert is_prefetch_worthy("video/mp4") is False
        assert is_prefetch_worthy("application/zip") is False

    def test_charset_suffix_and_case_are_handled(self):
        assert is_prefetch_worthy("TEXT/PLAIN; charset=utf-8") is True

    def test_empty_mime_type_is_not_worthy(self):
        assert is_prefetch_worthy("") is False
