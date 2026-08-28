"""Tests for card_builder.py -- the pure (no AppKit) translation from
gate.py's show_popup/show_read_popup argument shapes into
approval_window_html.build_card_stack_html()'s own shape. Mirrors what
approval_window.py's ApprovalWindowController does for the native host; see
that controller's docstring for the same reasoning these mirror.
"""
from __future__ import annotations

from privacyfence import card_builder
from privacyfence.approval_window_html import NARROW, WIDE


class TestReadingTimeLabel:
    def test_short_text_is_seconds(self):
        assert card_builder._reading_time_label("just a couple words") == "~1 sec read"

    def test_long_text_is_minutes(self):
        label = card_builder._reading_time_label(" ".join(["word"] * 400))
        assert label == "~2 min read"

    def test_empty_text_floors_at_one_second(self):
        assert card_builder._reading_time_label("") == "~1 sec read"


class TestSeenCountText:
    def test_singular(self):
        assert card_builder._seen_count_text(1) == "Seen 1 time this week"

    def test_plural(self):
        assert card_builder._seen_count_text(3) == "Seen 3 times this week"


class TestDisclosureRows:
    def test_write_calls_never_get_disclosure_rows(self):
        assert card_builder._disclosure_rows(False, {"To": "a@b.com"}, {"Body": "allow"}) == []

    def test_new_info_comes_before_visibility_derived_rows(self):
        rows = card_builder._disclosure_rows(True, {"Location": "Room 1"}, {"Description": "allow"})
        assert rows[0] == ("Location", "Room 1")
        assert rows[1][0] == "Description"

    def test_no_new_info_or_visibility_yields_no_rows(self):
        assert card_builder._disclosure_rows(True, None, None) == []


class TestBuildCardHtml:
    def _kwargs(self, **overrides):
        kwargs = dict(
            title="Read Message",
            preview={"From": "someone@example.com"},
            details_text="The message body.",
            is_read=True,
            layout=NARROW,
        )
        kwargs.update(overrides)
        return kwargs

    def test_renders_a_full_document(self):
        html = card_builder.build_card_html(**self._kwargs())
        assert html.startswith("<!DOCTYPE html>")
        assert "The message body." not in html  # NARROW has no preview pane

    def test_wide_layout_renders_the_details_text_in_the_preview_pane(self):
        html = card_builder.build_card_html(**self._kwargs(layout=WIDE))
        assert "The message body." in html
        assert "~1 sec read" in html

    def test_accept_all_label_uses_the_short_hint(self):
        html = card_builder.build_card_html(
            **self._kwargs(accept_all_choices=[("sender_domain", "this sender domain")])
        )
        assert "Always allow — this sender domain" in html

    def test_accept_all_label_falls_back_to_plain_when_no_hint(self):
        html = card_builder.build_card_html(**self._kwargs(accept_all_choices=[("always_allow", "")]))
        assert "Always allow" in html
        assert "Always allow — " not in html

    def test_write_call_never_shows_pii_categories_card(self):
        # Write calls carry write_content_flags, not pii_categories -- see
        # gate.py's own module docstring for why these are mutually
        # exclusive and rendered differently.
        html = card_builder.build_card_html(
            **self._kwargs(is_read=False, write_content_flags=["Email address"])
        )
        assert "Email address" in html
        assert "Review carefully before approving" not in html

    def test_read_call_with_pii_gets_the_read_risk_card(self):
        html = card_builder.build_card_html(**self._kwargs(pii_categories=["Email address"]))
        assert "Review carefully before approving" in html

    def test_temp_accept_eligible_shows_the_disclosure_caption(self):
        html = card_builder.build_card_html(**self._kwargs(temp_accept_eligible=True))
        assert "Approving this also allows further calls like this" in html

    def test_not_temp_accept_eligible_hides_the_disclosure_caption(self):
        html = card_builder.build_card_html(**self._kwargs(temp_accept_eligible=False))
        assert "Approving this also allows further calls like this" not in html

    def test_pdf_bytes_render_as_an_embed_data_uri(self):
        html = card_builder.build_card_html(**self._kwargs(layout=WIDE, pdf_bytes=b"%PDF-1.4 fake"))
        assert "data:application/pdf;base64," in html

    def test_image_bytes_render_as_an_img_data_uri(self):
        html = card_builder.build_card_html(
            **self._kwargs(layout=WIDE, preview_bytes=b"\x89PNG fake", preview_mime_type="image/png")
        )
        assert "data:image/png;base64," in html

    def test_table_only_suppresses_details_text_when_a_table_is_present(self):
        html = card_builder.build_card_html(
            **self._kwargs(
                layout=WIDE, table_only=True,
                preview_tables=[{"headers": ["A"], "rows": [["1"]]}],
            )
        )
        assert "The message body." not in html
        assert "<table" in html

    def test_seen_count_zero_shows_no_caption(self):
        html = card_builder.build_card_html(**self._kwargs(seen_count=0))
        assert "Seen" not in html

    def test_seen_count_positive_shows_caption(self):
        html = card_builder.build_card_html(**self._kwargs(seen_count=2))
        assert "Seen 2 times this week" in html
