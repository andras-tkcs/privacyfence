"""Floating window UI for reviewing MCP requests.

Uses tkinter (stdlib) — no extra dependencies. The window polls the
ReviewQueue on a timer and shows each pending request. Two card layouts:

  EmailCard   — for gmail_get_message / gmail_get_thread:
                shows sender / recipients / subject / date header rows and
                the HTML body rendered as plain text in a scrollable widget.

  GenericCard — for any other tool: key/value parameter display.

Attachment list is returned as part of the approved message data without a
separate approval step. Attachment *content* reading (not yet implemented)
would get its own card type when added.
"""

from __future__ import annotations

import copy
import html
import html.parser
import logging
import os
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from .auto_accept import TOOL_TO_OPERATION
from .review_queue import PendingReview, ReviewQueue, get_review_queue

_RESOURCES = os.path.join(os.path.dirname(__file__), "resources")

logger = logging.getLogger(__name__)

POLL_MS = 500
WIN_W = 680
WIN_H = 820

# ── macOS-native colour palette ───────────────────────────────────────────────
BG          = "#F2F2F7"   # system gray 6 (page background)
CARD_BG     = "#FFFFFF"
TITLEBAR_BG = "#ECECEC"   # approximates NSVisualEffectView light
TOOLBAR_BG  = "#F7F7F7"
HEADER_BG   = "#FAFAFA"   # card header tint
TEXT        = "#1C1C1E"   # label primary
MUTED       = "#8E8E93"   # label secondary
HINT        = "#AEAEB2"   # label tertiary
BORDER      = "#E0E0E5"   # hairline border
ACCENT      = "#007AFF"   # system blue

# Semantic action colours
GREEN       = "#34C759"
GREEN_TINT  = "#EBF9EF"
RED         = "#FF3B30"
RED_TINT    = "#FFF0EF"
ORANGE      = "#FF9500"
ORANGE_TINT = "#FFF4E5"

# Tool icon tints keyed by hint type
_ICON_COLORS: dict[str, tuple[str, str]] = {
    "email":   ("#EAF2FF", ACCENT),
    "thread":  ("#EDFAF2", GREEN),
    "generic": (ORANGE_TINT, ORANGE),
}


# ── HTML → plain-text renderer ────────────────────────────────────────────────

class _HtmlStripper(html.parser.HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
               "blockquote", "pre", "hr", "table", "ul", "ol"}
    _SKIP  = {"style", "script", "head", "meta", "link"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._last_was_newline = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in self._BLOCK:
            self._add_newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth == 0 and tag in self._BLOCK:
            self._add_newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.replace("\r\n", "\n").replace("\r", "\n")
        if text.strip():
            self._parts.append(text)
            self._last_was_newline = text.endswith("\n")

    def _add_newline(self) -> None:
        if not self._last_was_newline:
            self._parts.append("\n")
            self._last_was_newline = True

    def get_text(self) -> str:
        raw = "".join(self._parts)
        import re
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _html_to_text(body: str) -> str:
    if not body:
        return ""
    if not body.strip().startswith("<"):
        return body
    stripper = _HtmlStripper()
    try:
        stripper.feed(body)
        return stripper.get_text()
    except Exception:  # noqa: BLE001
        return html.unescape(body)


# ── Small reusable widgets ────────────────────────────────────────────────────

def _hairline(parent: tk.Widget, bg: str = CARD_BG, **kw: Any) -> tk.Frame:
    return tk.Frame(parent, bg=BORDER, height=1, **kw)


def _pill_tag(parent: tk.Widget, text: str,
              bg: str = "#EAF2FF", fg: str = ACCENT,
              card_bg: str = CARD_BG) -> tk.Label:
    return tk.Label(
        parent, text=text,
        font=("SF Pro Text", 10, "bold"),
        bg=bg, fg=fg,
        padx=7, pady=2,
        relief="flat",
    )


# ── Shared card base ──────────────────────────────────────────────────────────

class _BaseCard(tk.Frame):
    """White card with a header, body, and action row."""

    _hint_type: str = "generic"

    def __init__(
        self,
        parent: tk.Widget,
        review: PendingReview,
        on_approve: Callable[[str], None],
        on_reject: Callable[[str], None],
        **kw: Any,
    ) -> None:
        super().__init__(
            parent,
            bg=CARD_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            **kw,
        )
        self._review = review
        self._on_approve = on_approve
        self._on_reject = on_reject
        self._build_header()
        _hairline(self).pack(fill="x")
        self._build_body()
        _hairline(self).pack(fill="x")
        self._build_actions()

    def _build_header(self) -> None:
        r = self._review
        icon_bg, icon_fg = _ICON_COLORS.get(self._hint_type, _ICON_COLORS["generic"])

        hdr = tk.Frame(self, bg=HEADER_BG)
        hdr.pack(fill="x")

        icon_box = tk.Label(
            hdr,
            text=self._icon_char(),
            font=("SF Pro Text", 15),
            bg=icon_bg, fg=icon_fg,
            width=2, pady=6,
        )
        icon_box.pack(side="left", padx=(12, 8), pady=10)

        name_col = tk.Frame(hdr, bg=HEADER_BG)
        name_col.pack(side="left", fill="y", pady=9)
        tk.Label(
            name_col,
            text=r.tool_name,
            font=("SF Pro Text", 13, "bold"),
            bg=HEADER_BG, fg=TEXT, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            name_col,
            text=self._subtitle(),
            font=("SF Pro Text", 11),
            bg=HEADER_BG, fg=MUTED, anchor="w",
        ).pack(anchor="w")

        tag_bg, tag_fg = _ICON_COLORS.get(self._hint_type, _ICON_COLORS["generic"])
        _pill_tag(hdr, self._hint_type, bg=tag_bg, fg=tag_fg, card_bg=HEADER_BG).pack(
            side="right", padx=12, pady=12,
        )

    def _icon_char(self) -> str:
        return "⚙"  # ⚙

    def _subtitle(self) -> str:
        s = self._review.summary
        return s[:72] + ("…" if len(s) > 72 else "")

    def _build_body(self) -> None:
        pass

    def _build_actions(self) -> None:
        row = tk.Frame(self, bg=CARD_BG)
        row.pack(fill="x", padx=12, pady=10)

        approve = tk.Button(
            row, text="  Allow  ",
            font=("SF Pro Text", 12, "bold"),
            bg=GREEN, fg="white",
            activebackground="#2DB84D", activeforeground="white",
            relief="flat", bd=0, pady=8, cursor="hand2",
            command=lambda: self._on_approve(self._review.request_id),
        )
        approve.pack(side="left", fill="x", expand=True, padx=(0, 6))

        reject = tk.Button(
            row, text="  Deny  ",
            font=("SF Pro Text", 12, "bold"),
            bg=RED_TINT, fg=RED,
            activebackground="#FFD9D7", activeforeground=RED,
            relief="flat", bd=0, pady=8, cursor="hand2",
            command=lambda: self._on_reject(self._review.request_id),
        )
        reject.pack(side="left", fill="x", expand=True)


# ── Email preview card ────────────────────────────────────────────────────────

class EmailCard(_BaseCard):
    _hint_type = "email"

    def _icon_char(self) -> str:
        return "✉"  # ✉

    def _subtitle(self) -> str:
        return "Reading email content"

    def _build_body(self) -> None:
        hint = self._review.display_hint
        outer = tk.Frame(self, bg=CARD_BG)
        outer.pack(fill="both", expand=True, padx=14, pady=10)

        self._meta_row(outer, "From",    hint.get("sender", ""))
        recipients = hint.get("recipients", [])
        self._meta_row(outer, "To",      ", ".join(recipients) if recipients else "")
        self._meta_row(outer, "Subject", hint.get("subject", ""), bold=True)
        self._meta_row(outer, "Date",    hint.get("date", ""), muted=True)

        n_att = hint.get("attachment_count", 0)
        if n_att:
            self._meta_row(outer, "Attachments", f"{n_att} file(s)")

        _hairline(outer).pack(fill="x", pady=8)

        body_frame = tk.Frame(outer, bg=CARD_BG)
        body_frame.pack(fill="both", expand=True)

        txt = tk.Text(
            body_frame,
            bg=CARD_BG, fg=MUTED,
            font=("SF Pro Text", 11),
            relief="flat", bd=0,
            wrap="word",
            state="disabled",
            height=6,
            cursor="arrow",
        )
        vsb = ttk.Scrollbar(body_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        raw_body = hint.get("html_body", "")
        plain = _html_to_text(raw_body)
        txt.configure(state="normal")
        txt.insert("1.0", plain or raw_body or "(no body)")
        txt.configure(state="disabled")

    @staticmethod
    def _meta_row(parent: tk.Widget, label: str, value: str,
                  bold: bool = False, muted: bool = False) -> None:
        row = tk.Frame(parent, bg=CARD_BG)
        row.pack(fill="x", pady=2)
        tk.Label(
            row, text=label,
            font=("SF Pro Text", 11),
            bg=CARD_BG, fg=HINT,
            width=10, anchor="e",
        ).pack(side="left")
        tk.Label(
            row, text=value,
            font=("SF Pro Text", 11, "bold" if bold else "normal"),
            bg=CARD_BG,
            fg=MUTED if muted else TEXT,
            anchor="w", wraplength=490, justify="left",
        ).pack(side="left", padx=(8, 0), fill="x", expand=True)


# ── Thread card ───────────────────────────────────────────────────────────────

class ThreadCard(_BaseCard):
    _hint_type = "thread"

    def _icon_char(self) -> str:
        return "✉"  # ✉

    def _subtitle(self) -> str:
        hint = self._review.display_hint
        n = hint.get("message_count", 0)
        return f"Reading thread — {n} message{'s' if n != 1 else ''}"

    def _build_body(self) -> None:
        hint = self._review.display_hint
        outer = tk.Frame(self, bg=CARD_BG)
        outer.pack(fill="both", expand=True, padx=14, pady=10)

        EmailCard._meta_row(outer, "Subject", hint.get("subject", ""), bold=True)

        for i, msg in enumerate(hint.get("messages", []), 1):
            msg_frame = tk.Frame(
                outer, bg=BG,
                highlightbackground=BORDER, highlightthickness=1,
            )
            msg_frame.pack(fill="x", pady=(8, 0))

            inner = tk.Frame(msg_frame, bg=BG)
            inner.pack(fill="both", padx=10, pady=8)

            tk.Label(
                inner, text=f"#{i}",
                font=("SF Pro Text", 10, "bold"),
                bg=BG, fg=MUTED, anchor="w",
            ).pack(anchor="w", pady=(0, 4))

            recipients = msg.get("recipients", [])
            EmailCard._meta_row(inner, "From", msg.get("sender", ""))
            EmailCard._meta_row(inner, "To",   ", ".join(recipients) if recipients else "")
            EmailCard._meta_row(inner, "Date", msg.get("date", ""), muted=True)

            n_att = msg.get("attachment_count", 0)
            if n_att:
                EmailCard._meta_row(inner, "Attachments", f"{n_att} file(s)")

            raw_body = msg.get("html_body", "")
            plain = _html_to_text(raw_body) or raw_body
            if plain:
                preview = plain[:200].replace("\n", " ")
                if len(plain) > 200:
                    preview += "…"
                tk.Label(
                    inner, text=preview,
                    font=("SF Pro Text", 11),
                    bg=BG, fg=MUTED,
                    anchor="w", wraplength=560, justify="left",
                ).pack(anchor="w", pady=(6, 0))


# ── Generic card (fallback) ───────────────────────────────────────────────────

def _flatten_params(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                rows.extend(_flatten_params(v, key))
            else:
                val = str(v) if v is not None else "null"
                rows.append((key, val[:120] + ("…" if len(val) > 120 else "")))
    elif isinstance(data, list):
        for i, v in enumerate(data[:8]):
            key = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)):
                rows.extend(_flatten_params(v, key))
            else:
                rows.append((key, str(v)[:120]))
        if len(data) > 8:
            rows.append((f"{prefix}[…]", f"{len(data) - 8} more items"))
    return rows


class GenericCard(_BaseCard):
    _hint_type = "generic"

    def _build_body(self) -> None:
        r = self._review
        outer = tk.Frame(self, bg=CARD_BG)
        outer.pack(fill="both", expand=True, padx=14, pady=10)

        rows = _flatten_params(r.filtered_data)

        if rows:
            tk.Label(
                outer, text="Parameters",
                font=("SF Pro Text", 10, "bold"),
                bg=CARD_BG, fg=HINT, anchor="w",
            ).pack(anchor="w", pady=(0, 6))

            params_bg = tk.Frame(
                outer, bg=BG,
                highlightbackground=BORDER, highlightthickness=1,
            )
            params_bg.pack(fill="x")

            inner = tk.Frame(params_bg, bg=BG)
            inner.pack(fill="x", padx=10, pady=8)

            for key, val in rows:
                row = tk.Frame(inner, bg=BG)
                row.pack(fill="x", pady=2)
                tk.Label(
                    row, text=key,
                    font=("SF Pro Mono", 10),
                    bg=BG, fg=MUTED, anchor="w",
                    width=22,
                ).pack(side="left")
                tk.Label(
                    row, text=val,
                    font=("SF Pro Mono", 10),
                    bg=BG, fg=TEXT,
                    anchor="w", wraplength=360, justify="left",
                ).pack(side="left", padx=(8, 0), fill="x", expand=True)
        else:
            tk.Label(
                outer, text="(no parameters)",
                font=("SF Pro Text", 11),
                bg=CARD_BG, fg=HINT, anchor="w",
            ).pack(anchor="w")

        if r.sender:
            tk.Label(
                outer, text=f"Sender: {r.sender[:120]}",
                font=("SF Pro Text", 10),
                bg=CARD_BG, fg=HINT, anchor="w",
            ).pack(anchor="w", pady=(8, 0))


# ── Card factory ──────────────────────────────────────────────────────────────

def _make_card(
    parent: tk.Widget,
    review: PendingReview,
    on_approve: Callable[[str], None],
    on_reject: Callable[[str], None],
) -> _BaseCard:
    hint_type = review.display_hint.get("type", "")
    if hint_type == "email":
        return EmailCard(parent, review, on_approve, on_reject)
    if hint_type == "thread":
        return ThreadCard(parent, review, on_approve, on_reject)
    return GenericCard(parent, review, on_approve, on_reject)


# ── Auto-accept rules editor ──────────────────────────────────────────────────
#
# Rule metadata: maps rule_name → (display_label, value_kind)
#   value_kind: None = boolean toggle, "list" = comma-sep string, "int" = integer
#
_RULE_META: dict[str, tuple[str, Any]] = {
    # Gmail
    "i_am_sender":            ("I am the sender",                    None),
    "i_am_sole_recipient":    ("I am the sole recipient",            None),
    "trusted_sender_domain":  ("Trusted domains",                    "list"),
    "label_match":            ("Gmail labels match",                 "list"),
    "age_threshold_days":     ("Email is older than N days",         "int"),
    "no_attachments":         ("Email has no attachments",           None),
    # Drive
    "i_am_owner":             ("I am the file owner",                None),
    "created_by_me":          ("File was created by me",             None),
    "approved_folder":        ("File is in approved folder IDs",     "list"),
    "approved_sandbox_folder":("File is in sandbox folder IDs",      "list"),
    "move_within_approved_folders": ("Move within approved folder IDs", "list"),
    "file_type_allowlist":    ("Allowed MIME types",                 "list"),
    "created_this_session":   ("File was created this session",      None),
    "shared_drive_exclusion": ("Skip shared drive files",            None),
    # Slack
    "dm_with_myself":         ("Channel is my DM with myself",       None),
    "send_to_myself":         ("Sending to my own DM",               None),
    "approved_channel":       ("Approved channel IDs",               "list"),
    "approved_recipient":     ("Approved recipient channel/user IDs","list"),
    "public_channels_only":   ("Public channels only",               None),
    "no_file_attachments":    ("Messages have no file attachments",  None),
    "reply_in_existing_thread":("Replying in an existing thread",    None),
    # Calendar
    "i_am_organizer":         ("I am the event organizer",           None),
    "no_external_attendees":  ("No external attendees",              None),
    "personal_calendar":      ("Approved calendar IDs",              "list"),
    "past_event":             ("Event is in the past",               None),
    "time_window_days":       ("Event starts within N days",         "int"),
    "no_conferencing_link":   ("Event has no conferencing link",     None),
    # Salesforce
    "approved_object_types":  ("Approved object types",              "list"),
    "approved_report_ids":    ("Approved report IDs",                "list"),
}

# Connector → list of (operation_key, display_name, [rule_names])
_CONNECTOR_OPERATIONS: list[tuple[str, str, list[tuple[str, str, list[str]]]]] = [
    ("Gmail", "✉", [
        ("gmail.read_message", "Read Message", [
            "i_am_sender", "i_am_sole_recipient", "trusted_sender_domain",
            "label_match", "age_threshold_days", "no_attachments",
        ]),
        ("gmail.read_thread", "Read Thread", [
            "i_am_sender", "i_am_sole_recipient", "trusted_sender_domain",
            "label_match", "age_threshold_days", "no_attachments",
        ]),
    ]),
    ("Drive", "🗂", [
        ("drive.read_file_contents", "Read File", [
            "i_am_owner", "file_type_allowlist", "created_this_session", "shared_drive_exclusion",
        ]),
        ("drive.write_file", "Write File", [
            "i_am_owner", "approved_folder", "approved_sandbox_folder",
            "file_type_allowlist", "created_this_session",
        ]),
        ("drive.move_file", "Move File", [
            "move_within_approved_folders",
        ]),
        ("drive.comment_file", "Comment on File", [
            "created_by_me", "created_this_session",
        ]),
    ]),
    ("Slack", "💬", [
        ("slack.read_messages", "Read Messages", [
            "dm_with_myself", "approved_channel", "public_channels_only", "no_file_attachments",
        ]),
        ("slack.send_message", "Send Message", [
            "send_to_myself", "approved_recipient", "reply_in_existing_thread",
        ]),
    ]),
    ("Calendar", "📅", [
        ("calendar.read_event_details", "Read Event", [
            "i_am_organizer", "no_external_attendees", "personal_calendar", "past_event",
        ]),
        ("calendar.create_modify_event", "Create / Modify Event", [
            "i_am_organizer", "no_external_attendees", "personal_calendar",
            "time_window_days", "no_conferencing_link",
        ]),
    ]),
    ("Salesforce", "☁", [
        ("salesforce.read_record", "Read Record", [
            "approved_object_types",
        ]),
        ("salesforce.run_report", "Run Report", [
            "approved_report_ids",
        ]),
    ]),
]

# Hint text for value fields
_VALUE_HINTS: dict[str, str] = {
    "trusted_sender_domain":        "e.g. example.com, partner.org",
    "label_match":                  "e.g. INBOX, IMPORTANT",
    "age_threshold_days":           "e.g. 30",
    "approved_folder":              "Google Drive folder IDs",
    "approved_sandbox_folder":      "Google Drive folder IDs",
    "move_within_approved_folders": "Google Drive folder IDs",
    "file_type_allowlist":          "e.g. application/pdf, image/png",
    "approved_channel":             "Slack channel IDs, e.g. C012AB3CD",
    "approved_recipient":           "Slack channel or user IDs",
    "personal_calendar":            "e.g. primary, or calendar ID",
    "time_window_days":             "e.g. 14",
    "approved_object_types":        "e.g. Account, Contact, Lead",
    "approved_report_ids":          "Salesforce report IDs",
}


class _RulesEditorWindow:
    """Structured GUI for editing auto_accept_rules — no YAML knowledge required.

    Saves to settings.yaml and hot-reloads the live evaluator so changes take
    effect immediately without a daemon restart.
    """

    def __init__(self, parent: tk.Widget) -> None:
        self._win = tk.Toplevel(parent)
        self._win.title("Auto-Accept Rules")
        self._win.configure(bg=BG)
        self._win.resizable(True, True)
        self._win.geometry("740x620")
        self._win.grab_set()

        self._settings_path = self._resolve_settings_path()

        # State: (operation_key, rule_name) → (BooleanVar, StringVar|None)
        self._rule_vars: dict[tuple[str, str], tuple[tk.BooleanVar, Optional[tk.StringVar]]] = {}

        self._build()
        self._load()

    @staticmethod
    def _resolve_settings_path() -> str:
        from .paths import data_dir
        return str(data_dir() / "config" / "settings.yaml")

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        win = self._win

        # Header
        hdr_frame = tk.Frame(win, bg=TITLEBAR_BG)
        hdr_frame.pack(fill="x")
        tk.Label(
            hdr_frame, text="Auto-Accept Rules",
            font=("SF Pro Text", 14, "bold"),
            bg=TITLEBAR_BG, fg=TEXT, anchor="w",
            padx=16, pady=12,
        ).pack(side="left")
        tk.Label(
            hdr_frame,
            text="Changes apply immediately — no restart needed",
            font=("SF Pro Text", 11),
            bg=TITLEBAR_BG, fg=MUTED, anchor="e",
            padx=16,
        ).pack(side="right")
        _hairline(win, bg=TITLEBAR_BG).pack(fill="x")

        # Notebook (one tab per connector)
        style = ttk.Style()
        style.configure("Rules.TNotebook", background=BG, borderwidth=0)
        style.configure("Rules.TNotebook.Tab",
                        font=("SF Pro Text", 11),
                        padding=(14, 6))

        nb = ttk.Notebook(win, style="Rules.TNotebook")
        nb.pack(fill="both", expand=True, padx=0, pady=0)

        for connector_name, icon, operations in _CONNECTOR_OPERATIONS:
            tab = tk.Frame(nb, bg=BG)
            nb.add(tab, text=f"{icon}  {connector_name}")
            self._build_connector_tab(tab, operations)

        # Footer
        _hairline(win, bg=BG).pack(fill="x")
        footer = tk.Frame(win, bg=BG)
        footer.pack(fill="x", padx=16, pady=10)

        self._status_lbl = tk.Label(
            footer, text="",
            font=("SF Pro Text", 11),
            bg=BG, fg=MUTED, anchor="w",
        )
        self._status_lbl.pack(side="left")

        tk.Button(
            footer, text="Cancel",
            font=("SF Pro Text", 12),
            bg=BG, fg=MUTED,
            activebackground=BG, activeforeground=TEXT,
            relief="flat", bd=0, padx=14, pady=6,
            cursor="hand2",
            command=win.destroy,
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            footer, text="Save & Apply",
            font=("SF Pro Text", 12, "bold"),
            bg=ACCENT, fg="white",
            activebackground="#0066DD", activeforeground="white",
            relief="flat", bd=0, padx=18, pady=6,
            cursor="hand2",
            command=self._save,
        ).pack(side="right")

    def _build_connector_tab(
        self,
        tab: tk.Frame,
        operations: list[tuple[str, str, list[str]]],
    ) -> None:
        # Scrollable inner area
        canvas = tk.Canvas(tab, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=BG)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1 * e.delta / 120), "units"))

        for op_key, op_label, rules in operations:
            self._build_operation_section(inner, op_key, op_label, rules)

        # Bottom padding
        tk.Frame(inner, bg=BG, height=16).pack(fill="x")

    def _build_operation_section(
        self,
        parent: tk.Frame,
        op_key: str,
        op_label: str,
        rule_names: list[str],
    ) -> None:
        # Section card
        section = tk.Frame(
            parent, bg=CARD_BG,
            highlightbackground=BORDER, highlightthickness=1,
        )
        section.pack(fill="x", padx=14, pady=(12, 0))

        # Operation header
        hdr = tk.Frame(section, bg=HEADER_BG)
        hdr.pack(fill="x")
        tk.Label(
            hdr, text=op_label,
            font=("SF Pro Text", 12, "bold"),
            bg=HEADER_BG, fg=TEXT, anchor="w",
            padx=14, pady=8,
        ).pack(side="left")
        tk.Label(
            hdr, text=op_key,
            font=("SF Pro Mono", 10),
            bg=HEADER_BG, fg=HINT, anchor="e",
            padx=14,
        ).pack(side="right")
        _hairline(section).pack(fill="x")

        # Rule rows
        body = tk.Frame(section, bg=CARD_BG)
        body.pack(fill="x", padx=14, pady=8)

        for i, rule_name in enumerate(rule_names):
            label, value_kind = _RULE_META.get(rule_name, (rule_name, None))
            self._build_rule_row(body, op_key, rule_name, label, value_kind, i)

    def _build_rule_row(
        self,
        parent: tk.Frame,
        op_key: str,
        rule_name: str,
        label: str,
        value_kind: Any,
        index: int,
    ) -> None:
        row_bg = CARD_BG if index % 2 == 0 else "#FAFAFA"
        row = tk.Frame(parent, bg=row_bg)
        row.pack(fill="x", pady=1)

        enabled_var = tk.BooleanVar(value=False)
        val_var: Optional[tk.StringVar] = tk.StringVar() if value_kind else None

        cb = tk.Checkbutton(
            row,
            text=label,
            variable=enabled_var,
            font=("SF Pro Text", 11),
            bg=row_bg, fg=TEXT,
            activebackground=row_bg,
            selectcolor=row_bg,
            relief="flat", bd=0,
            anchor="w",
        )
        cb.pack(side="left", padx=(4, 0), pady=4)

        if value_kind and val_var is not None:
            hint = _VALUE_HINTS.get(rule_name, "")
            entry = tk.Entry(
                row,
                textvariable=val_var,
                font=("SF Pro Text", 11),
                bg=BG, fg=TEXT,
                relief="flat",
                highlightbackground=BORDER, highlightthickness=1,
                width=32,
            )
            # Placeholder hint via focus bindings
            if hint:
                entry.insert(0, hint)
                entry.config(fg=HINT)

                def _on_focus_in(e, w=entry, h=hint, v=val_var):
                    if w.get() == h:
                        w.delete(0, "end")
                        w.config(fg=TEXT)

                def _on_focus_out(e, w=entry, h=hint, v=val_var):
                    if not w.get():
                        w.insert(0, h)
                        w.config(fg=HINT)

                entry.bind("<FocusIn>", _on_focus_in)
                entry.bind("<FocusOut>", _on_focus_out)

            entry.pack(side="left", padx=(10, 4), pady=4)

        self._rule_vars[(op_key, rule_name)] = (enabled_var, val_var)

    # ── Load ─────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            import yaml
            path = self._settings_path
            if not os.path.exists(path):
                return
            with open(path, encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            rules: dict = cfg.get("auto_accept_rules") or {}
            self._populate_vars(rules)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rules editor load error: %s", exc)

    def _populate_vars(self, rules: dict) -> None:
        hint_set = set(_VALUE_HINTS.values())
        for op_key, rule_list in rules.items():
            if not isinstance(rule_list, list):
                continue
            for rule_cfg in rule_list:
                rule_name = rule_cfg.get("rule", "")
                value = rule_cfg.get("value")
                key = (op_key, rule_name)
                if key not in self._rule_vars:
                    continue
                enabled_var, val_var = self._rule_vars[key]
                enabled_var.set(True)
                if val_var is not None and value is not None:
                    text = (
                        ", ".join(str(v) for v in value)
                        if isinstance(value, list) else str(value)
                    )
                    # Clear placeholder and set real value
                    current = val_var.get()
                    if current in hint_set:
                        val_var.set("")
                    val_var.set(text)

    # ── Save & hot-reload ─────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            import yaml
            from .auto_accept import reload_rules

            new_rules = self._collect_rules()

            path = self._settings_path
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
            else:
                cfg = {}

            cfg["auto_accept_rules"] = new_rules
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                yaml.dump(cfg, fh, allow_unicode=True, default_flow_style=False)

            reload_rules(new_rules)

            n = sum(len(v) for v in new_rules.values())
            self._status_lbl.config(
                text=f"✓ Applied {n} rule{'s' if n != 1 else ''} — no restart needed",
                fg=GREEN,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc), parent=self._win)

    def _collect_rules(self) -> dict:
        hint_set = set(_VALUE_HINTS.values())
        rules: dict[str, list[dict]] = {}

        for (op_key, rule_name), (enabled_var, val_var) in self._rule_vars.items():
            if not enabled_var.get():
                continue

            entry: dict[str, Any] = {"rule": rule_name}
            if val_var is not None:
                raw = val_var.get().strip()
                if raw in hint_set or not raw:
                    raw = ""
                _, value_kind = _RULE_META.get(rule_name, (None, None))
                if value_kind == "list" and raw:
                    entry["value"] = [v.strip() for v in raw.split(",") if v.strip()]
                elif value_kind == "int" and raw:
                    try:
                        entry["value"] = int(raw)
                    except ValueError:
                        entry["value"] = raw
                elif raw:
                    entry["value"] = raw

            rules.setdefault(op_key, []).append(entry)

        return rules


# ── Main floating window ──────────────────────────────────────────────────────

class GuardFloatingWindow:
    """The main floating window.

    Runs on the main thread. Polls the ReviewQueue via ``root.after`` and
    re-renders the card list whenever the pending set changes.
    """

    def __init__(
        self,
        privacy_filter: Any,
        review_queue: Optional[ReviewQueue] = None,
        on_quit: Optional[Callable[[], None]] = None,
        app_name: str = "Loopline",
    ) -> None:
        self._filter = privacy_filter
        self._queue = review_queue or get_review_queue()
        self._on_quit = on_quit
        self._app_name = app_name
        self._last_ids: tuple[str, ...] = ()
        self._cards: dict[str, _BaseCard] = {}

        self._root = tk.Tk()
        self._root.title(self._app_name)
        self._root.configure(bg=BG)
        self._root.protocol("WM_DELETE_WINDOW", self._quit)

        _icon_path = os.path.join(_RESOURCES, "icon_64.png")
        if os.path.exists(_icon_path):
            try:
                self._icon_img = tk.PhotoImage(file=_icon_path)
                self._root.wm_iconphoto(True, self._icon_img)
            except Exception:
                pass

        self._build()
        self._position_window()
        self._poll()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = self._root

        # title bar
        title_bar = tk.Frame(root, bg=TITLEBAR_BG)
        title_bar.pack(fill="x")

        dots = tk.Frame(title_bar, bg=TITLEBAR_BG)
        dots.pack(side="left", padx=(14, 0), pady=14)
        for dot_color in ("#FF5F57", "#FEBC2E", "#28C840"):
            c = tk.Canvas(dots, width=12, height=12, bg=TITLEBAR_BG,
                          highlightthickness=0)
            c.create_oval(1, 1, 11, 11, fill=dot_color, outline="")
            c.pack(side="left", padx=3)

        tk.Label(
            title_bar,
            text=self._app_name,
            font=("SF Pro Text", 13, "bold"),
            bg=TITLEBAR_BG, fg=TEXT,
        ).pack(side="left", padx=(10, 0))

        self._badge_lbl = tk.Label(
            title_bar,
            text="",
            font=("SF Pro Text", 10, "bold"),
            bg=ACCENT, fg="white",
            padx=7, pady=1,
        )

        _hairline(title_bar, bg=TITLEBAR_BG).pack(side="bottom", fill="x")

        # toolbar
        toolbar = tk.Frame(root, bg=TOOLBAR_BG)
        toolbar.pack(fill="x")

        self._status_lbl = tk.Label(
            toolbar, text="No pending requests",
            font=("SF Pro Text", 11),
            bg=TOOLBAR_BG, fg=MUTED,
        )
        self._status_lbl.pack(side="left", padx=14, pady=8)

        self._deny_all_btn = tk.Button(
            toolbar, text="Deny all",
            font=("SF Pro Text", 11, "bold"),
            bg=RED_TINT, fg=RED,
            activebackground="#FFD9D7", activeforeground=RED,
            relief="flat", bd=0, padx=12, pady=4,
            cursor="hand2",
            command=self._reject_all,
        )
        self._deny_all_btn.pack(side="right", padx=(6, 14), pady=8)

        self._allow_all_btn = tk.Button(
            toolbar, text="Allow all",
            font=("SF Pro Text", 11, "bold"),
            bg=GREEN_TINT, fg=GREEN,
            activebackground="#D4F5DF", activeforeground=GREEN,
            relief="flat", bd=0, padx=12, pady=4,
            cursor="hand2",
            command=self._approve_all,
        )
        self._allow_all_btn.pack(side="right", pady=8)

        tk.Button(
            toolbar, text="⚙ Rules",
            font=("SF Pro Text", 11),
            bg=TOOLBAR_BG, fg=MUTED,
            activebackground=BG, activeforeground=TEXT,
            relief="flat", bd=0, padx=10, pady=4,
            cursor="hand2",
            command=self._open_rules_editor,
        ).pack(side="right", pady=8)

        tk.Button(
            toolbar, text="＋ Add accounts",
            font=("SF Pro Text", 11),
            bg=TOOLBAR_BG, fg=MUTED,
            activebackground=BG, activeforeground=TEXT,
            relief="flat", bd=0, padx=10, pady=4,
            cursor="hand2",
            command=self._open_setup_wizard,
        ).pack(side="right", pady=8)

        _hairline(toolbar, bg=TOOLBAR_BG).pack(side="bottom", fill="x")

        # scrollable card area
        container = tk.Frame(root, bg=BG)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._card_frame = tk.Frame(canvas, bg=BG)
        self._canvas_window = canvas.create_window(
            (0, 0), window=self._card_frame, anchor="nw",
        )

        self._card_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(self._canvas_window, width=e.width),
        )
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )
        self._canvas = canvas

        self._empty_lbl = tk.Label(
            self._card_frame,
            text="All clear\nWaiting for Claude to make a request…",
            font=("SF Pro Text", 13),
            bg=BG, fg=MUTED,
            justify="center", pady=80,
        )
        self._empty_lbl.pack()

    def _position_window(self) -> None:
        self._root.update_idletasks()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = sw - WIN_W - 24
        y = (sh - WIN_H) // 2
        self._root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

    # ── polling / rendering ───────────────────────────────────────────────────

    def _poll(self) -> None:
        pending = self._queue.list_pending()
        current_ids = tuple(r.request_id for r in pending)
        if current_ids != self._last_ids:
            self._render(pending)
            self._last_ids = current_ids
        self._root.after(POLL_MS, self._poll)

    def _render(self, pending: list[PendingReview]) -> None:
        count = len(pending)

        if count:
            self._status_lbl.config(
                text=f"{count} request{'s' if count != 1 else ''} waiting for review",
                fg=TEXT,
            )
            self._badge_lbl.config(text=f" {count} ")
            self._badge_lbl.pack(side="left", padx=(6, 0))
            self._allow_all_btn.config(state="normal", cursor="hand2")
            self._deny_all_btn.config(state="normal", cursor="hand2")
        else:
            self._status_lbl.config(text="No pending requests", fg=MUTED)
            self._badge_lbl.pack_forget()
            self._allow_all_btn.config(state="disabled", cursor="")
            self._deny_all_btn.config(state="disabled", cursor="")

        current_ids = {r.request_id for r in pending}
        for rid in list(self._cards):
            if rid not in current_ids:
                self._cards[rid].destroy()
                del self._cards[rid]

        if count == 0:
            self._empty_lbl.pack()
        else:
            self._empty_lbl.pack_forget()

        existing_ids = set(self._cards)
        for review in pending:
            if review.request_id not in existing_ids:
                card = _make_card(
                    self._card_frame, review,
                    on_approve=self._approve,
                    on_reject=self._reject,
                )
                card.pack(fill="x", padx=12, pady=(10, 0))
                self._cards[review.request_id] = card

        self._canvas.yview_moveto(0)

    # ── actions ───────────────────────────────────────────────────────────────

    def _approve(self, request_id: str) -> None:
        if self._queue.approve(request_id):
            logger.info("Approved request %s via UI", request_id)

    def _reject(self, request_id: str) -> None:
        if self._queue.reject(request_id):
            logger.info("Rejected request %s via UI", request_id)

    def _approve_all(self) -> None:
        for review in self._queue.list_pending():
            self._queue.approve(review.request_id)
        logger.info("Approved all pending requests via UI")

    def _reject_all(self) -> None:
        self._queue.reject_all("Rejected by user (Deny All)")
        logger.info("Rejected all pending requests via UI")

    def _open_rules_editor(self) -> None:
        _RulesEditorWindow(self._root)

    def _open_setup_wizard(self) -> None:
        from .setup_wizard import SetupWizard
        wizard = SetupWizard(parent=self._root)
        wizard.run()

    def _quit(self) -> None:
        logger.info("Quit requested from floating window")
        self._queue.reject_all("Application shutting down")
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_quit handler raised: %s", exc)
        self._root.destroy()

    # ── entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the tkinter main loop (blocks until the window is closed)."""
        self._root.mainloop()
