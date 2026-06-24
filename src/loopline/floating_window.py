"""Floating window UI for reviewing MCP requests.

Uses tkinter (stdlib) — no extra dependencies. The window polls the
ReviewQueue on a timer and shows each pending request. Two card layouts:

  EmailCard   — for gmail_get_message / gmail_get_thread:
                shows sender / recipients / subject / date header rows and
                the HTML body rendered as plain text in a scrollable widget.

  GenericCard — for any other tool: collapsible JSON tree (legacy).

Attachment list is returned as part of the approved message data without a
separate approval step. Attachment *content* reading (not yet implemented)
would get its own card type when added.
"""

from __future__ import annotations

import html
import html.parser
import logging
import os
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional

from .review_queue import PendingReview, ReviewQueue, get_review_queue

_RESOURCES = os.path.join(os.path.dirname(__file__), "resources")

logger = logging.getLogger(__name__)

POLL_MS = 500
WIN_W = 720
WIN_H = 820

# ── colour palette (dark / catppuccin-inspired) ───────────────────────────────
BG        = "#1e1e2e"
PANEL_BG  = "#2a2a3e"
HEADER_BG = "#313244"
ACCENT    = "#89b4fa"
TEXT      = "#cdd6f4"
MUTED     = "#a6adc8"
GREEN_BG  = "#2d4a3e"
GREEN_FG  = "#a6e3a1"
RED_BG    = "#4a2d2d"
RED_FG    = "#f38ba8"
BORDER    = "#45475a"
META_KEY  = "#94e2d5"   # teal for metadata labels


# ── HTML → plain-text renderer ────────────────────────────────────────────────

class _HtmlStripper(html.parser.HTMLParser):
    """Convert basic HTML to readable plain text for the body preview."""

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
        # Collapse runs of 3+ blank lines to 2.
        import re
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _html_to_text(body: str) -> str:
    """Strip HTML tags and return readable plain text."""
    if not body:
        return ""
    if not body.strip().startswith("<"):
        return body  # already plain text
    stripper = _HtmlStripper()
    try:
        stripper.feed(body)
        return stripper.get_text()
    except Exception:  # noqa: BLE001
        return html.unescape(body)


# ── Shared card base ──────────────────────────────────────────────────────────

class _BaseCard(tk.Frame):
    """Header + action buttons shared by all card types."""

    def __init__(
        self,
        parent: tk.Widget,
        review: PendingReview,
        on_approve: Callable[[str], None],
        on_reject: Callable[[str], None],
        **kw: Any,
    ) -> None:
        super().__init__(
            parent, bg=PANEL_BG,
            highlightbackground=BORDER, highlightthickness=1,
            **kw,
        )
        self._review = review
        self._on_approve = on_approve
        self._on_reject = on_reject
        self._build_header()
        self._build_body()
        self._build_actions()
        tk.Frame(self, bg=PANEL_BG, height=8).pack()

    def _build_header(self) -> None:
        r = self._review
        hdr = tk.Frame(self, bg=HEADER_BG, pady=6)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text=f"  🔧 {r.tool_name}",
            font=("SF Pro Text", 13, "bold"),
            bg=HEADER_BG, fg=ACCENT,
            anchor="w",
        ).pack(side="left", padx=(4, 0))

    def _build_body(self) -> None:
        """Subclasses override this to render their content."""

    def _build_actions(self) -> None:
        btn_row = tk.Frame(self, bg=PANEL_BG, pady=6)
        btn_row.pack(fill="x", padx=10)
        tk.Button(
            btn_row, text="✅  Approve",
            font=("SF Pro Text", 12, "bold"),
            bg=GREEN_BG, fg=GREEN_FG,
            activebackground="#3d6b52", activeforeground=GREEN_FG,
            bd=0, padx=18, pady=6, cursor="hand2",
            command=lambda: self._on_approve(self._review.request_id),
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            btn_row, text="❌  Reject",
            font=("SF Pro Text", 12, "bold"),
            bg=RED_BG, fg=RED_FG,
            activebackground="#6b3d3d", activeforeground=RED_FG,
            bd=0, padx=18, pady=6, cursor="hand2",
            command=lambda: self._on_reject(self._review.request_id),
        ).pack(side="left")


# ── Email preview card ────────────────────────────────────────────────────────

class EmailCard(_BaseCard):
    """Card for gmail_get_message — shows metadata + HTML body as plain text."""

    def _build_body(self) -> None:
        hint = self._review.display_hint
        outer = tk.Frame(self, bg=PANEL_BG)
        outer.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        self._add_meta_row(outer, "From",    hint.get("sender", ""))
        recipients = hint.get("recipients", [])
        self._add_meta_row(outer, "To",      ", ".join(recipients) if recipients else "")
        self._add_meta_row(outer, "Subject", hint.get("subject", ""))
        self._add_meta_row(outer, "Date",    hint.get("date", ""))

        n_att = hint.get("attachment_count", 0)
        if n_att:
            self._add_meta_row(outer, "Attachments", f"{n_att} file(s) included")

        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", pady=(6, 4))

        body_frame = tk.Frame(outer, bg=PANEL_BG)
        body_frame.pack(fill="both", expand=True)

        txt = tk.Text(
            body_frame,
            bg=PANEL_BG, fg=TEXT,
            font=("SF Pro Mono", 11),
            relief="flat", bd=0,
            wrap="word",
            state="disabled",
            height=14,
        )
        vsb = ttk.Scrollbar(body_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        plain = _html_to_text(hint.get("html_body", ""))
        txt.configure(state="normal")
        txt.insert("1.0", plain or "(no body)")
        txt.configure(state="disabled")

    @staticmethod
    def _add_meta_row(parent: tk.Widget, label: str, value: str) -> None:
        row = tk.Frame(parent, bg=PANEL_BG)
        row.pack(fill="x", pady=1)
        tk.Label(
            row, text=f"{label}:",
            font=("SF Pro Text", 11, "bold"),
            bg=PANEL_BG, fg=META_KEY,
            width=12, anchor="e",
        ).pack(side="left")
        tk.Label(
            row, text=value,
            font=("SF Pro Text", 11),
            bg=PANEL_BG, fg=TEXT,
            anchor="w", wraplength=540, justify="left",
        ).pack(side="left", padx=(6, 0), fill="x", expand=True)


class ThreadCard(_BaseCard):
    """Card for gmail_get_thread — shows per-message previews in a notebook."""

    def _build_body(self) -> None:
        hint = self._review.display_hint
        outer = tk.Frame(self, bg=PANEL_BG)
        outer.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        subject = hint.get("subject", "")
        count = hint.get("message_count", 0)
        tk.Label(
            outer,
            text=f"Thread: {subject}  ({count} message(s))",
            font=("SF Pro Text", 12, "bold"),
            bg=PANEL_BG, fg=ACCENT,
            anchor="w", wraplength=640, justify="left",
        ).pack(fill="x", pady=(0, 6))

        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)

        style = ttk.Style()
        style.configure("TNotebook", background=PANEL_BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=HEADER_BG, foreground=TEXT,
                        padding=[8, 4])
        style.map("TNotebook.Tab", background=[("selected", ACCENT)],
                  foreground=[("selected", BG)])

        for i, msg in enumerate(hint.get("messages", []), 1):
            tab = tk.Frame(nb, bg=PANEL_BG)
            nb.add(tab, text=f"#{i}")

            EmailCard._add_meta_row(tab, "From", msg.get("sender", ""))
            recipients = msg.get("recipients", [])
            EmailCard._add_meta_row(tab, "To", ", ".join(recipients) if recipients else "")
            EmailCard._add_meta_row(tab, "Date", msg.get("date", ""))
            n_att = msg.get("attachment_count", 0)
            if n_att:
                EmailCard._add_meta_row(tab, "Attachments", f"{n_att} file(s) included")

            tk.Frame(tab, bg=BORDER, height=1).pack(fill="x", pady=(6, 4))

            body_frame = tk.Frame(tab, bg=PANEL_BG)
            body_frame.pack(fill="both", expand=True)

            txt = tk.Text(
                body_frame,
                bg=PANEL_BG, fg=TEXT,
                font=("SF Pro Mono", 11),
                relief="flat", bd=0,
                wrap="word",
                state="disabled",
                height=10,
            )
            vsb = ttk.Scrollbar(body_frame, orient="vertical", command=txt.yview)
            txt.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y")
            txt.pack(side="left", fill="both", expand=True)

            plain = _html_to_text(msg.get("html_body", ""))
            txt.configure(state="normal")
            txt.insert("1.0", plain or "(no body)")
            txt.configure(state="disabled")


# ── Generic JSON-tree card (fallback) ────────────────────────────────────────

def _to_display(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value).replace("\n", "↵ ").replace("\r", "")
    return s[:300] + "…" if len(s) > 300 else s


class JsonTreeFrame(tk.Frame):
    """Treeview that renders a JSON-like Python value."""

    def __init__(self, parent: tk.Widget, **kw: Any) -> None:
        super().__init__(parent, bg=PANEL_BG, **kw)
        self._build()

    def _build(self) -> None:
        style = ttk.Style(self)
        style.configure("Json.Treeview", background=PANEL_BG, fieldbackground=PANEL_BG,
                        foreground=TEXT, rowheight=22, borderwidth=0)
        style.configure("Json.Treeview.Heading", background=HEADER_BG, foreground=ACCENT)
        style.map("Json.Treeview", background=[("selected", ACCENT)])

        self._tv = ttk.Treeview(self, columns=("value",), show="tree headings",
                                 style="Json.Treeview", selectmode="browse")
        self._tv.heading("#0", text="Key", anchor="w")
        self._tv.heading("value", text="Value", anchor="w")
        self._tv.column("#0", width=200, minwidth=120, stretch=True)
        self._tv.column("value", width=360, minwidth=160, stretch=True)

        vsb = ttk.Scrollbar(self, orient="vertical", command=self._tv.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self._tv.xview)
        self._tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def load(self, data: Any, root_label: str = "data") -> None:
        self._tv.delete(*self._tv.get_children())
        self._insert("", "end", data, root_label, depth=0)

    def _insert(self, parent: str, index: str | int, value: Any, key: str, depth: int) -> None:
        if isinstance(value, dict):
            node = self._tv.insert(parent, index, text=f"  {key}", values=("{…}",), open=depth < 2)
            for k, v in value.items():
                self._insert(node, "end", v, str(k), depth + 1)
        elif isinstance(value, list):
            node = self._tv.insert(parent, index, text=f"  {key}",
                                    values=(f"[{len(value)} items]",),
                                    open=depth < 1 and len(value) <= 10)
            for i, v in enumerate(value):
                self._insert(node, "end", v, f"[{i}]", depth + 1)
        else:
            self._tv.insert(parent, index, text=f"  {key}", values=(_to_display(value),))


class GenericCard(_BaseCard):
    """Fallback card: collapsible JSON tree."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        self._expanded = True
        super().__init__(*args, **kw)

    def _build_header(self) -> None:
        r = self._review
        hdr = tk.Frame(self, bg=HEADER_BG, pady=6)
        hdr.pack(fill="x")
        tk.Label(
            hdr, text=f"  🔧 {r.tool_name}",
            font=("SF Pro Text", 13, "bold"),
            bg=HEADER_BG, fg=ACCENT, anchor="w",
        ).pack(side="left", padx=(4, 0))
        self._toggle_btn = tk.Button(
            hdr, text="▲",
            font=("SF Pro Text", 10),
            bg=HEADER_BG, fg=MUTED, bd=0,
            activebackground=HEADER_BG, cursor="hand2",
            command=self._toggle,
        )
        self._toggle_btn.pack(side="right", padx=8)

    def _build_body(self) -> None:
        r = self._review
        meta = tk.Frame(self, bg=PANEL_BG, pady=2)
        meta.pack(fill="x", padx=10)

        summary_text = r.summary[:120] + ("…" if len(r.summary) > 120 else "")
        tk.Label(meta, text=summary_text, bg=PANEL_BG, fg=TEXT,
                 font=("SF Pro Text", 11), anchor="w", wraplength=560,
                 justify="left").pack(fill="x")
        if r.sender:
            tk.Label(meta, text=f"↳ {r.sender[:100]}", bg=PANEL_BG, fg=MUTED,
                     font=("SF Pro Text", 10), anchor="w").pack(fill="x")

        self._body = tk.Frame(self, bg=PANEL_BG)
        self._body.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        tk.Label(self._body, text="Response data:", bg=PANEL_BG, fg=MUTED,
                 font=("SF Pro Text", 10), anchor="w").pack(fill="x")
        self._tree = JsonTreeFrame(self._body)
        self._tree.pack(fill="both", expand=True)
        self._tree.load(r.filtered_data, root_label="filtered_data")

    def _build_actions(self) -> None:
        btn_row = tk.Frame(self._body, bg=PANEL_BG, pady=6)
        btn_row.pack(fill="x")
        tk.Button(
            btn_row, text="✅  Approve",
            font=("SF Pro Text", 12, "bold"),
            bg=GREEN_BG, fg=GREEN_FG,
            activebackground="#3d6b52", activeforeground=GREEN_FG,
            bd=0, padx=18, pady=6, cursor="hand2",
            command=lambda: self._on_approve(self._review.request_id),
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            btn_row, text="❌  Reject",
            font=("SF Pro Text", 12, "bold"),
            bg=RED_BG, fg=RED_FG,
            activebackground="#6b3d3d", activeforeground=RED_FG,
            bd=0, padx=18, pady=6, cursor="hand2",
            command=lambda: self._on_reject(self._review.request_id),
        ).pack(side="left")
        tk.Frame(self, bg=PANEL_BG, height=8).pack()

    def _toggle(self) -> None:
        if self._expanded:
            self._body.pack_forget()
            self._toggle_btn.config(text="▼")
        else:
            self._body.pack(fill="both", expand=True, padx=10, pady=(4, 0))
            self._toggle_btn.config(text="▲")
        self._expanded = not self._expanded


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

        # Set window icon (used by macOS Mission Control / Dock when detached).
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

        title_bar = tk.Frame(root, bg=HEADER_BG, pady=10)
        title_bar.pack(fill="x")

        _title_icon_path = os.path.join(_RESOURCES, "icon_32.png")
        if os.path.exists(_title_icon_path):
            try:
                self._title_icon_img = tk.PhotoImage(file=_title_icon_path)
                tk.Label(
                    title_bar, image=self._title_icon_img,
                    bg=HEADER_BG,
                ).pack(side="left", padx=(8, 4))
            except Exception:
                pass

        tk.Label(
            title_bar,
            text=self._app_name,
            font=("SF Pro Text", 15, "bold"),
            bg=HEADER_BG, fg=ACCENT,
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            title_bar, text="Quit",
            font=("SF Pro Text", 11),
            bg=HEADER_BG, fg=MUTED,
            activebackground=RED_BG, activeforeground=RED_FG,
            bd=0, padx=10, pady=2, cursor="hand2",
            command=self._quit,
        ).pack(side="right", padx=8)

        toolbar = tk.Frame(root, bg=BG, pady=6)
        toolbar.pack(fill="x", padx=12)
        self._status_lbl = tk.Label(
            toolbar, text="No pending requests",
            font=("SF Pro Text", 11),
            bg=BG, fg=MUTED,
        )
        self._status_lbl.pack(side="left")
        tk.Button(
            toolbar, text="Deny All",
            font=("SF Pro Text", 11, "bold"),
            bg=RED_BG, fg=RED_FG,
            activebackground="#6b3d3d", activeforeground=RED_FG,
            bd=0, padx=12, pady=4, cursor="hand2",
            command=self._reject_all,
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            toolbar, text="Accept All",
            font=("SF Pro Text", 11, "bold"),
            bg=GREEN_BG, fg=GREEN_FG,
            activebackground="#3d6b52", activeforeground=GREEN_FG,
            bd=0, padx=12, pady=4, cursor="hand2",
            command=self._approve_all,
        ).pack(side="right")

        tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

        container = tk.Frame(root, bg=BG)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._card_frame = tk.Frame(canvas, bg=BG)
        self._canvas_window = canvas.create_window((0, 0), window=self._card_frame, anchor="nw")

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
            text="No pending requests.\nWaiting for Claude to make a call…",
            font=("SF Pro Text", 13),
            bg=BG, fg=MUTED,
            justify="center", pady=60,
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
        self._status_lbl.config(
            text=f"{count} pending request(s)" if count else "No pending requests"
        )

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
                card.pack(fill="x", padx=10, pady=(10, 0))
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
