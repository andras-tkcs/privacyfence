"""approval_list_html.py -- the /approvals list page (docs/approval-list-
ui-ux.md §2, the P1-compatible slice; docs/https-connector-refactor-plan.md
§16's W7)."""
from __future__ import annotations

import time
from types import SimpleNamespace

from privacyfence import approval_list_html


def _card(**overrides):
    defaults = dict(
        id="abc123", kind="card", connector="gmail", tool="gmail_read_message",
        tool_name="Read Gmail message", summary="", created_at=time.time(),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestRowFromApproval:
    def test_carries_the_display_fields(self):
        row = approval_list_html.row_from_approval(_card())
        assert row["id"] == "abc123"
        assert row["connector"] == "gmail"
        assert row["tool_name"] == "Read Gmail message"
        assert row["created_at"]


class TestBuildListHtml:
    def test_empty_state_when_no_rows(self):
        html = approval_list_html.build_list_html([], csrf="tok")
        assert "Nothing is waiting" in html
        assert "PrivacyFence is watching" in html

    def test_renders_a_row_per_pending_approval(self):
        rows = [approval_list_html.row_from_approval(_card(id="a")), approval_list_html.row_from_approval(_card(id="b"))]
        html = approval_list_html.build_list_html(rows, csrf="tok")
        assert 'data-approval-id="a"' in html
        assert 'data-approval-id="b"' in html
        assert "Read Gmail message" in html

    def test_never_renders_an_allow_button(self):
        # §2.2's central asymmetry: Deny is on the row, Allow never is.
        rows = [approval_list_html.row_from_approval(_card())]
        html = approval_list_html.build_list_html(rows, csrf="tok")
        assert "data-deny=" in html
        assert ">Allow<" not in html
        assert "allow" not in html.lower().replace("allow once", "").replace("pf-btn-review", "")

    def test_deny_button_and_review_link_are_present(self):
        rows = [approval_list_html.row_from_approval(_card(id="xyz"))]
        html = approval_list_html.build_list_html(rows, csrf="tok")
        assert 'data-deny="xyz"' in html
        assert 'href="/approvals/xyz"' in html

    def test_heading_pluralizes(self):
        html_one = approval_list_html.build_list_html([approval_list_html.row_from_approval(_card())], csrf="t")
        html_two = approval_list_html.build_list_html(
            [approval_list_html.row_from_approval(_card(id="a")), approval_list_html.row_from_approval(_card(id="b"))],
            csrf="t",
        )
        assert "1 approval pending" in html_one
        assert "2 approvals pending" in html_two

    def test_title_and_kicker_are_escaped(self):
        card = _card(tool_name='<script>alert(1)</script>')
        html = approval_list_html.build_list_html([approval_list_html.row_from_approval(card)], csrf="t")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_csrf_is_embedded_for_the_deny_button(self):
        html = approval_list_html.build_list_html([], csrf="my-token-value")
        assert "my-token-value" in html

    def test_confirm_kind_labels_as_confirmation_not_approval(self):
        card = _card(kind="confirm", tool_name="", summary="")
        html = approval_list_html.build_list_html([approval_list_html.row_from_approval(card)], csrf="t")
        assert "Confirmation" in html
