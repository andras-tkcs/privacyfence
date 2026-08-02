"""privacyfence.settings_window_html -- Linux/CI-portable, no AppKit import.

``build_html()`` renders everything client-side (vanilla JS, driven off
``window.__pfInitialState``/``window.__pfRender`` -- see that module's own
docstring for the bridge protocol) rather than string-interpolating
per-request content the way approval_window_html.build_card_stack_html()
does, so there is no JS engine here to actually execute ``render()`` and
inspect real DOM output. What this file *can* assert on the returned string,
in the same spirit as test_approval_window_html.py's string inspection of
that module's output:

  1. The given ``state`` is embedded byte-for-byte (as JSON) in
     ``window.__pfInitialState`` -- so a page load reflects exactly the
     values SettingsController.snapshot() computed, with no cross-field
     transformation. Section content / toggle on-off / connector rows /
     policy values are all data the JS template reads straight off this
     blob, so correct embedding is the load-bearing part of "does the right
     content show up".
  2. The generic client-side templates that consume each of those fields
     (toggle track/knob, connector row, rule/grant row, policy segmented
     control colors) exist in the shipped JS/CSS and reference the same
     field names the state actually carries -- catching the class of bug
     where a field gets renamed on one side of the bridge and not the
     other.
"""
from __future__ import annotations

import json
import re

from privacyfence.settings_window_html import build_html


def _make_state(**overrides):
    state = {
        "error": "",
        "general": {
            "pii_enabled": True, "pii_ip": True, "pii_financial": False,
            "quicklook_enabled": False, "update_check_enabled": True, "update_check_beta": False,
            "org_installed": True, "org_installed_date": "Jun 14, 2026",
            "org_button_label": "Install/Update Organization Config…", "version": "3.1.1",
        },
        "connectors": [
            {"key": "gmail", "label": "Gmail", "icon": "gmail", "icon_data_uri": "data:image/png;base64,AAA",
             "authed": True, "enabled": True, "busy": False, "has_org": True, "auth_label": "Reconnect…"},
            {"key": "telegram", "label": "Telegram", "icon": "telegram", "icon_data_uri": "",
             "authed": False, "enabled": True, "busy": False, "has_org": False, "auth_label": "Authenticate…"},
        ],
        "rules": {
            "connectors": [{"key": "gmail", "label": "Gmail", "count": 1}],
            "sections_by_connector": {
                "gmail": [{"op_key": "gmail.read_message", "title": "Read message",
                           "rows": [{"rule_type": "i_am_sender", "value": ""}]}],
            },
            "grants_by_connector": {
                "gmail": [],
            },
        },
        "privacy": {
            "groups": [{"key": "privacy", "label": "Gmail"}, {"key": "calendar", "label": "Calendar"}],
            "default_policy": {"privacy": "block"},
            "categories": {"privacy": [{"key": "body", "label": "Message body", "policy": "allow"}]},
            "calendar_free_busy": True,
        },
        "audit": {
            "log_level": "INFO", "log_file": "logs/privacyfence.log",
            "export_hint": "logs/audit/2026-W31.jsonl → 2026-W31.xlsx",
            "recent": [{"connector": "Gmail", "tool": "gmail_get_thread", "decision": "auto_accepted", "time": "2m ago"}],
        },
        "about": {"version": "3.1.1", "license": "Apache-2.0", "repo_url": "https://github.com/andras-tkcs/privacyfence"},
    }
    state.update(overrides)
    return state


def _extract_initial_state(html: str) -> dict:
    match = re.search(r"window\.__pfInitialState = (\{.*?\});</script>", html, re.DOTALL)
    assert match, "window.__pfInitialState assignment not found in build_html() output"
    return json.loads(match.group(1))


class TestDocumentShell:
    def test_has_a_title(self):
        html = build_html(_make_state())
        assert "<title>PrivacyFence Settings</title>" in html

    def test_embeds_an_app_mount_point(self):
        html = build_html(_make_state())
        assert '<div id="app"></div>' in html

    def test_defines_pf_render_entry_point(self):
        html = build_html(_make_state())
        assert "window.__pfRender = render;" in html

    def test_no_external_network_references(self):
        # Self-contained, offline document -- loaded via
        # loadHTMLString_baseURL_(html, None), so nothing may reference a
        # CDN, external stylesheet, or remote script.
        html = build_html(_make_state())
        assert "http://" not in html
        assert "https://" not in html.split("window.__pfInitialState", 1)[0]


class TestStateEmbedding:
    def test_state_round_trips_byte_for_byte(self):
        state = _make_state()
        html = build_html(state)
        assert _extract_initial_state(html) == state

    def test_connector_rows_data_present(self):
        html = build_html(_make_state())
        embedded = _extract_initial_state(html)
        keys = {c["key"] for c in embedded["connectors"]}
        assert keys == {"gmail", "telegram"}
        gmail = next(c for c in embedded["connectors"] if c["key"] == "gmail")
        assert gmail["authed"] is True
        assert gmail["icon_data_uri"] == "data:image/png;base64,AAA"

    def test_toggle_states_present(self):
        state = _make_state()
        state["general"]["pii_enabled"] = False
        html = build_html(state)
        embedded = _extract_initial_state(html)
        assert embedded["general"]["pii_enabled"] is False

    def test_policy_values_present(self):
        state = _make_state()
        state["privacy"]["default_policy"]["privacy"] = "redact"
        html = build_html(state)
        embedded = _extract_initial_state(html)
        assert embedded["privacy"]["default_policy"]["privacy"] == "redact"

    def test_error_banner_text_present_when_set(self):
        html = build_html(_make_state(error="Gmail authentication failed: boom"))
        embedded = _extract_initial_state(html)
        assert embedded["error"] == "Gmail authentication failed: boom"

    def test_html_special_characters_in_state_do_not_break_the_script_tag(self):
        # json.dumps escapes "</script>" sequences by default only if they
        # appear literally -- confirm a value containing one doesn't split
        # the embedding <script> tag early.
        state = _make_state()
        state["general"]["org_installed_date"] = "</script><script>alert(1)</script>"
        html = build_html(state)
        embedded = _extract_initial_state(html)
        assert embedded["general"]["org_installed_date"] == "</script><script>alert(1)</script>"


class TestToggleTemplate:
    def test_toggle_track_and_knob_classes_defined(self):
        html = build_html(_make_state())
        assert ".pf-toggle" in html
        assert ".pf-toggle.on" in html
        assert ".pf-knob" in html

    def test_toggle_bridge_actions_referenced(self):
        html = build_html(_make_state())
        for action in (
            "toggle_pii_detection", "toggle_pii_category", "toggle_quicklook_preview",
            "toggle_update_check", "toggle_update_check_beta", "toggle_connector",
            "toggle_calendar_free_busy", "toggle_grant_capability",
        ):
            assert f"'{action}'" in html, f"missing toggle wiring for {action}"


class TestConnectorRowTemplate:
    def test_connector_row_reads_the_right_fields(self):
        html = build_html(_make_state())
        for field in ("c.icon_data_uri", "c.label", "c.enabled", "c.auth_label", "c.key"):
            assert field in html

    def test_authenticate_action_wired(self):
        html = build_html(_make_state())
        assert "'authenticate_connector'" in html


class TestRulesAndGrantsTemplate:
    def test_rule_row_fields_wired(self):
        html = build_html(_make_state())
        assert "data-rule-field" in html
        assert "update_rule_row" in html
        assert "add_rule_row" in html
        assert "remove_rule_row" in html

    def test_grant_row_fields_wired(self):
        html = build_html(_make_state())
        assert "data-grant-field" in html
        assert "update_grant_row" in html
        assert "add_grant_row" in html
        assert "remove_grant_row" in html

    def test_rules_search_input_present(self):
        html = build_html(_make_state())
        assert "data-rules-search" in html


class TestPrivacySegmentedControl:
    def test_policy_colors_match_the_design(self):
        html = build_html(_make_state())
        # #0071e3 allow / #b76e00 redact / #d92d20 block -- see
        # settings_window_html.py's module docstring.
        assert ".policy-allow { background: #0071e3" in html
        assert ".policy-redact { background: #b76e00" in html
        assert ".policy-block { background: #d92d20" in html

    def test_policy_actions_wired(self):
        html = build_html(_make_state())
        assert "'set_default_policy'" in html
        assert "'set_category_policy'" in html


class TestAuditTemplate:
    def test_log_level_options_present(self):
        html = build_html(_make_state())
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            assert f"'{level}'" in html

    def test_export_action_wired(self):
        html = build_html(_make_state())
        assert "'export_audit_log'" in html

    def test_audit_badge_decision_classes_present(self):
        html = build_html(_make_state())
        assert "auto_accepted" in html
        assert "denied" in html


class TestAboutTemplate:
    def test_quit_and_check_updates_actions_wired(self):
        html = build_html(_make_state())
        assert "'quit_app'" in html
        assert "'check_for_updates'" in html
