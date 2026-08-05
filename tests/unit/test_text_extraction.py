"""Unit tests for privacyfence.text_extraction -- best-effort text
extraction feeding both pii_detector.py's scan and the approval window's
"markdown" preview_blocks entry for attachment/download/upload content.

The one invariant that matters most: extract_text() never raises, for any
input -- a format it can't handle, or fails to parse, must degrade to "",
not propagate an exception into gate.py's request path. Most of these tests
exist to pin that for each supported/unsupported format and each way parsing
can go wrong (corrupt bytes, not actually a zip, malformed XML).

DOCX/PPTX/XLSX/HTML extraction produces Markdown syntax (headings, bold/
italic, bullet/numbered lists, pipe tables) rather than flat prose -- see
test_markdown_to_html.py for the renderer that turns it back into HTML.
"""
from __future__ import annotations

import io
import zipfile

import openpyxl

from privacyfence.text_extraction import MAX_SCAN_CHARS, extract_text, is_prefetch_worthy, preview_blocks_for

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Minimal single-page PDF -- enough for pypdf.PdfReader to parse successfully
# and report its text, not a claim this is a spec-perfect PDF. Reuses the
# same fixture shape test_approval_window.py's TestPdfViewEmbed already
# pinned as parseable by PDFKit's PDFDocument, the API that module still
# uses for rendering (this one no longer does, for extraction).
VALID_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n"
    b"trailer << /Size 4 /Root 1 0 R >>\n"
    b"startxref\n0\n%%EOF"
)

_DOCX_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_PPTX_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)


def make_docx(*paragraphs: str) -> bytes:
    runs = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_DOCX_NS}><w:body>{runs}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_docx_xml(body_xml: str) -> bytes:
    """Like make_docx(), but takes raw <w:p>...</w:p> XML directly -- for
    tests exercising heading styles, bullets, or bold/italic runs the plain
    single-run helper above can't express."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_DOCX_NS}><w:body>{body_xml}</w:body></w:document>"
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
                f"<p:sld {_PPTX_NS}>"
                f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t>"
                "</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
    return buf.getvalue()


def make_pptx_xml(*slide_body_xml: str) -> bytes:
    """Like make_pptx(), but each slide's <p:txBody> inner XML is supplied
    directly -- for tests exercising bullets or bold/italic runs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, body in enumerate(slide_body_xml, start=1):
            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f"<p:sld {_PPTX_NS}>"
                f"<p:cSld><p:spTree><p:sp><p:txBody>{body}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
    return buf.getvalue()


def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_zip_archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
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


class TestHtml:
    def test_html_is_converted_to_markdown_not_raw_tags(self):
        result = extract_text(b"<h1>Title</h1><p>Body text.</p>", "text/html")
        assert result == "# Title\n\nBody text."

    def test_html_bold_becomes_markdown_bold(self):
        result = extract_text(b"<p><b>bold</b> text</p>", "text/html")
        assert result == "**bold** text"


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

    def test_stops_reading_pages_once_max_scan_chars_is_reached(self, monkeypatch):
        # A large multi-hundred-page PDF shouldn't pay for every page's
        # extract_text() call when the first couple already pass
        # MAX_SCAN_CHARS -- extract_text()'s own truncation to that cap
        # happens after _extract_pdf_text returns, so walking the rest of
        # the pages would only be thrown away.
        calls = []

        class FakePage:
            def extract_text(self):
                calls.append(1)
                return "a" * (MAX_SCAN_CHARS // 2 + 1)

        class FakeReader:
            def __init__(self, _stream):
                self.pages = [FakePage() for _ in range(10)]

        monkeypatch.setattr("pypdf.PdfReader", FakeReader)
        result = extract_text(b"%PDF-1.1 fake", "application/pdf")
        assert len(calls) == 2
        assert len(result) <= MAX_SCAN_CHARS


class TestDocx:
    def test_extracts_text_from_a_single_paragraph(self):
        docx = make_docx("Please wire the deposit to DE89370400440532013000.")
        assert extract_text(docx, DOCX_MIME) == "Please wire the deposit to DE89370400440532013000."

    def test_extracts_and_joins_multiple_paragraphs(self):
        docx = make_docx("First paragraph.", "Second paragraph.")
        result = extract_text(docx, DOCX_MIME)
        assert "First paragraph." in result
        assert "Second paragraph." in result
        assert result.index("First paragraph.") < result.index("Second paragraph.")

    def test_heading_style_becomes_markdown_heading(self):
        body = (
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Overview</w:t></w:r></w:p>'
        )
        result = extract_text(make_docx_xml(body), DOCX_MIME)
        assert result == "# Overview"

    def test_heading2_style_becomes_two_hashes(self):
        body = '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Details</w:t></w:r></w:p>'
        result = extract_text(make_docx_xml(body), DOCX_MIME)
        assert result == "## Details"

    def test_bold_run_becomes_markdown_bold(self):
        body = '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>important</w:t></w:r></w:p>'
        result = extract_text(make_docx_xml(body), DOCX_MIME)
        assert result == "**important**"

    def test_numbered_paragraph_becomes_bullet(self):
        body = (
            '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
            "<w:r><w:t>First item</w:t></w:r></w:p>"
        )
        result = extract_text(make_docx_xml(body), DOCX_MIME)
        assert result == "- First item"

    def test_consecutive_bullets_join_as_one_list_not_separate_blocks(self):
        body = "".join(
            '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
            f"<w:r><w:t>{item}</w:t></w:r></w:p>"
            for item in ("First", "Second")
        )
        result = extract_text(make_docx_xml(body), DOCX_MIME)
        # Single blank-line-delimited block -- see _join_markdown_lines --
        # so markdown_to_html.py renders these as one <ul>, not two.
        assert result == "- First\n- Second"

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
        result = extract_text(pptx, PPTX_MIME)
        assert result == "## Slide 1\n\nSlide one has an IBAN DE89370400440532013000."

    def test_extracts_and_joins_multiple_slides_in_order(self):
        pptx = make_pptx("First slide.", "Second slide.")
        result = extract_text(pptx, PPTX_MIME)
        assert result.index("First slide.") < result.index("Second slide.")
        assert "## Slide 1" in result
        assert "## Slide 2" in result

    def test_bullet_paragraph_becomes_markdown_bullet(self):
        body = '<a:p><a:pPr><a:buChar char="-"/></a:pPr><a:r><a:t>bullet point</a:t></a:r></a:p>'
        result = extract_text(make_pptx_xml(body), PPTX_MIME)
        assert result == "## Slide 1\n\n- bullet point"

    def test_bold_run_becomes_markdown_bold(self):
        body = '<a:p><a:r><a:rPr b="1"/><a:t>Title Slide</a:t></a:r></a:p>'
        result = extract_text(make_pptx_xml(body), PPTX_MIME)
        assert result == "## Slide 1\n\n**Title Slide**"

    def test_not_a_zip_returns_empty_string(self):
        assert extract_text(b"not a zip file at all", PPTX_MIME) == ""

    def test_zip_without_any_slides_returns_empty_string(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("some/other/file.xml", "<root/>")
        assert extract_text(buf.getvalue(), PPTX_MIME) == ""


class TestXlsx:
    def test_extracts_sheet_as_markdown_table(self):
        xlsx = make_xlsx({"Sheet1": [["Name", "Amount"], ["Alice", 100], ["Bob", 200]]})
        result = extract_text(xlsx, XLSX_MIME)
        assert result == (
            "## Sheet1\n\n"
            "| Name | Amount |\n"
            "| --- | --- |\n"
            "| Alice | 100 |\n"
            "| Bob | 200 |"
        )

    def test_multiple_sheets_each_get_their_own_section(self):
        xlsx = make_xlsx({
            "Revenue": [["Q", "Total"], ["Q1", 10]],
            "Costs": [["Q", "Total"], ["Q1", 5]],
        })
        result = extract_text(xlsx, XLSX_MIME)
        assert "## Revenue" in result
        assert "## Costs" in result
        assert result.index("## Revenue") < result.index("## Costs")

    def test_empty_sheet_contributes_nothing(self):
        xlsx = make_xlsx({"Empty": []})
        assert extract_text(xlsx, XLSX_MIME) == ""

    def test_not_a_valid_workbook_returns_empty_string(self):
        assert extract_text(b"not an xlsx file at all", XLSX_MIME) == ""


class TestArchive:
    def test_lists_member_files_as_a_markdown_table(self):
        archive = make_zip_archive({"a.txt": b"hello", "b.txt": b"world!!"})
        result = extract_text(archive, "application/zip")
        assert result == (
            "| Name | Size |\n| --- | --- |\n| a.txt | 5 bytes |\n| b.txt | 7 bytes |"
        )

    def test_empty_archive_returns_empty_string(self):
        archive = make_zip_archive({})
        assert extract_text(archive, "application/zip") == ""

    def test_not_a_zip_returns_empty_string(self):
        assert extract_text(b"not a zip file at all", "application/zip") == ""


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
        assert is_prefetch_worthy("text/html") is True

    def test_extractable_document_types_are_worthy(self):
        assert is_prefetch_worthy("application/pdf") is True
        assert is_prefetch_worthy(DOCX_MIME) is True
        assert is_prefetch_worthy(PPTX_MIME) is True
        assert is_prefetch_worthy(XLSX_MIME) is True
        assert is_prefetch_worthy("application/zip") is True

    def test_unrecognized_types_are_not_worthy(self):
        assert is_prefetch_worthy("application/octet-stream") is False
        assert is_prefetch_worthy("video/mp4") is False

    def test_charset_suffix_and_case_are_handled(self):
        assert is_prefetch_worthy("TEXT/PLAIN; charset=utf-8") is True

    def test_empty_mime_type_is_not_worthy(self):
        assert is_prefetch_worthy("") is False


class TestPreviewBlocksFor:
    def test_details_only_when_nothing_extracted(self):
        assert preview_blocks_for("The file above will be downloaded.", "") == [
            {"type": "text", "text": "The file above will be downloaded."},
        ]

    def test_details_and_markdown_when_extraction_found_content(self):
        blocks = preview_blocks_for("Details sentence.", "# Heading\n\nBody.")
        assert blocks == [
            {"type": "text", "text": "Details sentence."},
            {"type": "markdown", "text": "# Heading\n\nBody."},
        ]

    def test_markdown_only_when_no_details_given(self):
        blocks = preview_blocks_for("", "# Heading")
        assert blocks == [{"type": "markdown", "text": "# Heading"}]

    def test_none_when_neither_details_nor_extraction(self):
        assert preview_blocks_for("", "") is None
