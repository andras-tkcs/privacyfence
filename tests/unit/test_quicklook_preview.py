"""Unit tests for privacyfence.quicklook_preview -- the macOS QuickLook
thumbnail fallback for previewing non-image file content.

The one invariant that matters most: generate_thumbnail() never raises and
never blocks longer than its wait budget -- a disabled gate, an unsupported
format, a real QuickLook error, or a hung/slow render must all degrade to
None, the same as any other best-effort preview source in this codebase.
Most tests here mock QLThumbnailGenerator's completion-handler call directly
to exercise those paths deterministically and fast; a couple of tests call
the real macOS API (skipped off Darwin) to prove the bridging code actually
works against the system framework, not just a mock of it.
"""
from __future__ import annotations

import sys

import pytest

from privacyfence import quicklook_preview

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires the real QuickLookThumbnailing framework (macOS only)"
)

VALID_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n"
    b"trailer << /Size 4 /Root 1 0 R >>\n"
    b"startxref\n0\n%%EOF"
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Module-level enabled/listener state is reset by tests/conftest.py's
# autouse _reset_singletons fixture, before and after every test -- same as
# every other module-level singleton in this codebase.


class TestEnabledToggle:
    def test_disabled_by_default(self):
        assert quicklook_preview.is_quicklook_enabled() is False

    def test_init_sets_initial_state(self):
        quicklook_preview.init_quicklook_preview(True)
        assert quicklook_preview.is_quicklook_enabled() is True

    def test_set_enabled_toggles_state(self):
        quicklook_preview.set_quicklook_enabled(True)
        assert quicklook_preview.is_quicklook_enabled() is True
        quicklook_preview.set_quicklook_enabled(False)
        assert quicklook_preview.is_quicklook_enabled() is False

    def test_set_enabled_fires_changed_listener(self):
        calls = []
        quicklook_preview.set_quicklook_changed_listener(lambda: calls.append(1))
        quicklook_preview.set_quicklook_enabled(True)
        assert calls == [1]

    def test_no_listener_registered_does_not_raise(self):
        quicklook_preview.set_quicklook_enabled(True)  # no listener set -- must not raise


class TestGenerateThumbnailGating:
    def test_disabled_returns_none_without_touching_quicklook(self, monkeypatch):
        quicklook_preview.init_quicklook_preview(False)

        def boom(*a, **k):
            raise AssertionError("should not be called while disabled")

        monkeypatch.setattr(quicklook_preview, "_request_thumbnail", boom)
        assert quicklook_preview.generate_thumbnail(VALID_PDF, "report.pdf") is None

    def test_empty_data_returns_none(self):
        quicklook_preview.init_quicklook_preview(True)
        assert quicklook_preview.generate_thumbnail(b"", "report.pdf") is None


class TestGenerateThumbnailMocked:
    """Exercises success/error/timeout paths by mocking _request_thumbnail
    directly rather than QLThumbnailGenerator itself -- deterministic and
    fast, unlike depending on the real out-of-process render's timing."""

    def test_successful_generation_returns_png_bytes(self, monkeypatch):
        quicklook_preview.init_quicklook_preview(True)

        class FakeImage:
            def TIFFRepresentation(self):
                from AppKit import NSBitmapImageRep
                # Build a tiny real, backed bitmap so the real
                # _nsimage_to_png conversion path (TIFF -> NSBitmapImageRep
                # -> PNG) is exercised for real, not mocked away entirely.
                rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                    None, 4, 4, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0
                )
                return rep.TIFFRepresentation()

        monkeypatch.setattr(quicklook_preview, "_request_thumbnail", lambda path, wait: FakeImage())

        result = quicklook_preview.generate_thumbnail(VALID_PDF, "report.pdf")

        assert result is not None
        assert result[:8] == PNG_MAGIC

    def test_no_representation_returns_none(self, monkeypatch):
        quicklook_preview.init_quicklook_preview(True)
        monkeypatch.setattr(quicklook_preview, "_request_thumbnail", lambda path, wait: None)

        assert quicklook_preview.generate_thumbnail(VALID_PDF, "report.pdf") is None

    def test_request_thumbnail_exception_is_caught(self, monkeypatch):
        quicklook_preview.init_quicklook_preview(True)

        def boom(path, wait):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(quicklook_preview, "_request_thumbnail", boom)

        assert quicklook_preview.generate_thumbnail(VALID_PDF, "report.pdf") is None

    def test_temp_file_uses_filename_hint_extension(self, monkeypatch):
        quicklook_preview.init_quicklook_preview(True)
        captured = {}

        def fake_request(path, wait):
            captured["path"] = path
            return None

        monkeypatch.setattr(quicklook_preview, "_request_thumbnail", fake_request)

        quicklook_preview.generate_thumbnail(VALID_PDF, "report.pdf")

        assert captured["path"].endswith(".pdf")

    def test_temp_file_is_cleaned_up_after_use(self, monkeypatch):
        import os
        quicklook_preview.init_quicklook_preview(True)
        captured = {}

        def fake_request(path, wait):
            captured["path"] = path
            assert os.path.exists(path)
            return None

        monkeypatch.setattr(quicklook_preview, "_request_thumbnail", fake_request)

        quicklook_preview.generate_thumbnail(VALID_PDF, "report.pdf")

        assert not os.path.exists(captured["path"])

    def test_temp_file_already_gone_does_not_raise(self, monkeypatch):
        # Defensive cleanup: if something else already removed the temp
        # file (or the OS reclaimed it) by the time we try to unlink it,
        # that must not surface as an error from generate_thumbnail.
        import os as os_module

        def fake_request(path, wait):
            os_module.unlink(path)  # gone before generate_thumbnail's own cleanup runs
            return None

        quicklook_preview.init_quicklook_preview(True)
        monkeypatch.setattr(quicklook_preview, "_request_thumbnail", fake_request)

        assert quicklook_preview.generate_thumbnail(VALID_PDF, "report.pdf") is None


class TestNsimageToPng:
    def test_none_image_returns_none(self):
        assert quicklook_preview._nsimage_to_png(None) is None

    def test_none_tiff_representation_returns_none(self):
        class ImageWithNoTiff:
            def TIFFRepresentation(self):
                return None

        assert quicklook_preview._nsimage_to_png(ImageWithNoTiff()) is None

    def test_unparseable_tiff_data_returns_none(self):
        class ImageWithGarbageTiff:
            def TIFFRepresentation(self):
                return b"not valid tiff data"

        assert quicklook_preview._nsimage_to_png(ImageWithGarbageTiff()) is None


class TestRequestThumbnailTimeout:
    def test_handler_never_firing_times_out_and_returns_none(self, monkeypatch, tmp_path):
        # Simulate a hung/slow render: the completion handler is simply
        # never invoked. A short wait budget must still return promptly
        # rather than hanging the test (or, in production, the gate).
        class HangingGenerator:
            def generateBestRepresentationForRequest_completionHandler_(self, request, handler):
                pass  # never calls handler

        class FakeGeneratorClass:
            @staticmethod
            def sharedGenerator():
                return HangingGenerator()

        monkeypatch.setattr(quicklook_preview, "QLThumbnailGenerator", FakeGeneratorClass)

        f = tmp_path / "report.pdf"
        f.write_bytes(VALID_PDF)

        result = quicklook_preview._request_thumbnail(str(f), wait_seconds=0.2)

        assert result is None

    def test_error_result_returns_none(self, monkeypatch, tmp_path):
        class ErroringGenerator:
            def generateBestRepresentationForRequest_completionHandler_(self, request, handler):
                handler(None, "simulated QLThumbnailErrorDomain error")

        class FakeGeneratorClass:
            @staticmethod
            def sharedGenerator():
                return ErroringGenerator()

        monkeypatch.setattr(quicklook_preview, "QLThumbnailGenerator", FakeGeneratorClass)

        f = tmp_path / "report.pdf"
        f.write_bytes(VALID_PDF)

        result = quicklook_preview._request_thumbnail(str(f), wait_seconds=5.0)

        assert result is None


class TestRealQuickLookIntegration:
    """Calls the real macOS QuickLookThumbnailing framework -- no mocks --
    to prove the async-completion-handler bridging actually works against
    the system, not just an assumption about its shape. Mirrors
    test_approval_window.py's own precedent of testing against real
    AppKit/PDFKit objects rather than mocking framework internals."""

    def test_valid_pdf_produces_a_real_png_thumbnail(self):
        quicklook_preview.init_quicklook_preview(True)
        result = quicklook_preview.generate_thumbnail(VALID_PDF, "report.pdf")
        assert result is not None
        assert result[:8] == PNG_MAGIC

    def test_plain_text_produces_a_real_thumbnail(self):
        quicklook_preview.init_quicklook_preview(True)
        result = quicklook_preview.generate_thumbnail(
            b"Hello world, this is plain text.\n" * 5, "notes.txt",
        )
        assert result is not None
        assert result[:8] == PNG_MAGIC

    def test_unsupported_format_returns_none(self):
        quicklook_preview.init_quicklook_preview(True)
        result = quicklook_preview.generate_thumbnail(b"not a real file format at all !!", "x.qlz999notreal")
        assert result is None

    def test_garbage_bytes_with_a_known_extension_returns_none(self):
        quicklook_preview.init_quicklook_preview(True)
        result = quicklook_preview.generate_thumbnail(b"this is not a valid pdf", "report.pdf")
        assert result is None
