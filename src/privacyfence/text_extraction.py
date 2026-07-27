"""Best-effort plain-text extraction from attachment/upload content.

Feeds pii_detector.py's regex scan for formats that carry a real file's
content as opaque bytes (Gmail attachments, Drive downloads/uploads) rather
than the pre-extracted text gate.py's other callers already have (e.g.
Google Docs exports, plain-text bodies). Never raises: a format this module
can't parse, or fails to parse, simply contributes no text to scan -- the
same as if the attachment weren't there. This is a defense-in-depth
heuristic layered on top of human review (see pii_detector.py's own module
docstring), not the primary control, so silently scanning less is always
the safe failure mode, never raising into the gate path.

Images are deliberately out of scope -- no OCR. A caller wanting a scan for
image content gets an empty string here, same as any other unsupported
format.
"""
from __future__ import annotations

import io
import logging
import xml.etree.ElementTree as ET
import zipfile

from Foundation import NSData
from Quartz import PDFDocument

logger = logging.getLogger(__name__)

# Cap on how much extracted text is handed to pii_detector.py's regex scan.
# Every existing caller of that scan already truncates to a much smaller
# figure for *display* purposes (Drive's 2000-char content preview), but a
# document's actual text can run far longer than what's shown in the details
# pane, so this uses a more generous cap of its own rather than inheriting
# that display-oriented number.
MAX_SCAN_CHARS = 20_000

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# MIME types extract_text() can do something with, beyond the text/* prefix.
EXTRACTABLE_MIME_TYPES = frozenset({"application/pdf", _DOCX_MIME, _PPTX_MIME})


def is_prefetch_worthy(mime_type: str) -> bool:
    """Whether fetching an attachment/upload's full bytes pre-approval is
    worth it at all -- for a preview (image/*) or a PII scan (extract_text()
    output). Shared by connectors that only get one shot at the content
    before a human decides (no partial-fetch API, or an unbounded local
    disk read) and want to avoid fetching/reading types neither use case
    can do anything with.
    """
    mime_type = (mime_type or "").split(";")[0].strip().lower()
    return mime_type.startswith("image/") or mime_type.startswith("text/") or mime_type in EXTRACTABLE_MIME_TYPES


def extract_text(data: bytes, mime_type: str) -> str:
    """Best-effort plain text from ``data``, or "" if ``mime_type`` isn't a
    supported format or the content fails to parse. Truncated to
    MAX_SCAN_CHARS.
    """
    if not data:
        return ""
    mime_type = (mime_type or "").split(";")[0].strip().lower()
    try:
        if mime_type.startswith("text/"):
            text = data.decode("utf-8", errors="replace")
        elif mime_type == "application/pdf":
            text = _extract_pdf_text(data)
        elif mime_type == _DOCX_MIME:
            text = _extract_ooxml_text(data, ["word/document.xml"])
        elif mime_type == _PPTX_MIME:
            text = _extract_ooxml_text(data, _pptx_slide_paths(data))
        else:
            return ""
    except Exception:
        logger.warning("extract_text: failed to parse %s content", mime_type, exc_info=True)
        return ""
    return text[:MAX_SCAN_CHARS]


def _extract_pdf_text(data: bytes) -> str:
    ns_data = NSData.dataWithBytes_length_(data, len(data))
    document = PDFDocument.alloc().initWithData_(ns_data)
    if document is None:
        return ""
    text = document.string()
    return str(text) if text else ""


def _pptx_slide_paths(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return sorted(
            n for n in zf.namelist()
            if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        )


def _extract_ooxml_text(data: bytes, paths: list[str]) -> str:
    """Pull every text run out of one or more OOXML part files (docx's
    word/document.xml, or pptx's per-slide XML). Both formats' text runs
    (w:t, a:t) end in "}t" once ElementTree resolves the namespace, so one
    namespace-agnostic walk covers both -- no need to special-case which
    format's namespace URI applies.
    """
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        for path in paths:
            if path not in names:
                continue
            root = ET.fromstring(zf.read(path))
            parts.extend(elem.text for elem in root.iter() if elem.tag.endswith("}t") and elem.text)
    return " ".join(parts)
