"""macOS QuickLook thumbnail generation for approval-window previews.

Off by default -- see is_quicklook_enabled()/set_quicklook_enabled(), toggled
from the menu bar's "QuickLook Previews" item exactly like pii_detector.py's
own enabled flag. When on, this is the fallback preview source for a
prefetched file that isn't an image (approval_window.py's NSImageView path
already handles those): PDFs, Office documents, and anything else QuickLook's
own renderer (quicklookd) recognizes.

QLThumbnailGenerator's real API is callback-based and runs the render
out-of-process via quicklookd -- an OS-level safety property for rendering
untrusted, not-yet-approved file bytes, but it means this module has to
bridge an async completion handler into a synchronous call the (synchronous)
gate/connector code can use. generate_thumbnail() does that with a
threading.Event and a bounded wait: a slow or hung render times out rather
than blocking the approval flow indefinitely, and the caller degrades to
today's plain-text/metadata-only view exactly as it would for a disabled
gate or an unsupported format -- this is a best-effort preview source, not a
guarantee, same framing as text_extraction.py's own module docstring.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Callable

from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep
from Foundation import NSURL
from QuickLookThumbnailing import (
    QLThumbnailGenerationRequest,
    QLThumbnailGenerationRequestRepresentationTypeThumbnail,
    QLThumbnailGenerator,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_WAIT_SECONDS = 5.0
# Rendered at a fixed, modest size -- this feeds the same details-pane
# NSImageView the direct-image path does, not a full-resolution viewer.
_THUMBNAIL_SIZE = (512, 512)
_THUMBNAIL_SCALE = 1.0

# ---------------------------------------------------------------------------- #
# Enabled/disabled toggle (menu-bar configurable, hot-reloadable) -- mirrors
# pii_detector.py's own _enabled/_changed_listener pattern.
# ---------------------------------------------------------------------------- #

_enabled = False
_max_wait_seconds = DEFAULT_MAX_WAIT_SECONDS
_changed_listener: Callable[[], None] | None = None


def is_quicklook_enabled() -> bool:
    return _enabled


def init_quicklook_preview(enabled: bool, max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS) -> None:
    """Set the initial enabled state (and wait budget) at daemon startup."""
    global _enabled, _max_wait_seconds
    _enabled = bool(enabled)
    _max_wait_seconds = float(max_wait_seconds) if max_wait_seconds else DEFAULT_MAX_WAIT_SECONDS


def set_quicklook_enabled(enabled: bool) -> None:
    """Hot-toggle from the menu bar."""
    global _enabled
    _enabled = bool(enabled)
    logger.info("QuickLook preview %s", "enabled" if enabled else "disabled")
    if _changed_listener is not None:
        _changed_listener()


def set_quicklook_changed_listener(callback: Callable[[], None] | None) -> None:
    global _changed_listener
    _changed_listener = callback


# ---------------------------------------------------------------------------- #
# Thumbnail generation
# ---------------------------------------------------------------------------- #

def generate_thumbnail(
    data: bytes, filename_hint: str, max_wait_seconds: float | None = None
) -> bytes | None:
    """Best-effort QuickLook thumbnail for ``data``, or None if disabled,
    empty, unsupported, timed out, or failed. Never raises.

    Returns PNG-encoded image bytes (not an NSImage) so the caller can feed
    the result straight into the same preview_bytes/preview_mime_type
    channel a directly-fetched image already uses -- approval_window.py's
    NSImageView render path doesn't need to know a thumbnail came from
    QuickLook rather than the file itself.

    QuickLook identifies a file's format from its extension/UTI, not a MIME
    type, so ``data`` is written to a temp file whose suffix is taken from
    ``filename_hint`` -- without a recognizable extension, QuickLook can't
    determine what renderer to use at all.
    """
    if not _enabled or not data:
        return None
    wait_seconds = max_wait_seconds if max_wait_seconds is not None else _max_wait_seconds
    suffix = os.path.splitext(filename_hint or "")[1]

    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

        image = _request_thumbnail(path, wait_seconds)
        if image is None:
            return None
        return _nsimage_to_png(image)
    except Exception:
        logger.warning("generate_thumbnail: failed for %r", filename_hint, exc_info=True)
        return None
    finally:
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass


def _request_thumbnail(path: str, wait_seconds: float):
    """Bridges QLThumbnailGenerator's async completion-handler API into a
    synchronous, timeout-bounded call. Returns an NSImage, or None on
    timeout, error, or no representation."""
    url = NSURL.fileURLWithPath_(path)
    request = QLThumbnailGenerationRequest.alloc().initWithFileAtURL_size_scale_representationTypes_(
        url, _THUMBNAIL_SIZE, _THUMBNAIL_SCALE, QLThumbnailGenerationRequestRepresentationTypeThumbnail
    )

    event = threading.Event()
    result: dict = {}

    def handler(representation, error) -> None:
        result["representation"] = representation
        result["error"] = error
        event.set()

    QLThumbnailGenerator.sharedGenerator().generateBestRepresentationForRequest_completionHandler_(
        request, handler
    )

    if not event.wait(timeout=wait_seconds):
        logger.warning("generate_thumbnail: timed out after %.1fs for %r", wait_seconds, path)
        return None
    if result.get("error") is not None or result.get("representation") is None:
        return None
    return result["representation"].NSImage()


def _nsimage_to_png(image) -> bytes | None:
    if image is None:
        return None
    tiff_data = image.TIFFRepresentation()
    if tiff_data is None:
        return None
    rep = NSBitmapImageRep.imageRepWithData_(tiff_data)
    if rep is None:
        return None
    png_data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    if png_data is None:
        return None
    return bytes(png_data)
