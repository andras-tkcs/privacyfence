"""First-run setup wizard for Loopline.

Shown automatically when running from the .app bundle and ~/.loopline/setup_complete
does not exist. Walks the user through:
  1. Welcome
  2. Import Google OAuth client_secret.json
  3. Authorize Google services (Gmail, Drive, Calendar, Contacts, Tasks)
  4. Slack bot token (optional)
  5. Telegram (optional)
  6. Salesforce (optional)
  7. Install LaunchAgent
  8. Done — show MCP config snippet
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox
from typing import Callable

from .paths import app_bundle_path, data_dir

logger = logging.getLogger("loopline.setup_wizard")

# ── colour palette (matches floating_window.py) ──────────────────────────────
BG       = "#1e1e2e"
SURFACE  = "#313244"
ACCENT   = "#89b4fa"
TEXT     = "#cdd6f4"
SUBTEXT  = "#a6adc8"
GREEN    = "#a6e3a1"
RED      = "#f38ba8"
YELLOW   = "#f9e2af"

WIN_W = 620
WIN_H = 520


# ── helpers ───────────────────────────────────────────────────────────────────

def _data() -> Path:
    return data_dir()


def _credentials_dir() -> Path:
    d = _data() / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_dir() -> Path:
    d = _data() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _logs_dir() -> Path:
    d = _data() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _client_secret_path() -> Path:
    return _credentials_dir() / "client_secret.json"


def _settings_path() -> Path:
    return _config_dir() / "settings.yaml"


def _sentinel_path() -> Path:
    return _data() / "setup_complete"


def _bridge_path() -> str:
    bundle = app_bundle_path()
    if bundle:
        return str(bundle / "Contents" / "MacOS" / "loopline-bridge")
    # Dev fallback
    return shutil.which("loopline-bridge") or "loopline-bridge"


def _plist_label() -> str:
    return "com.loopline.app"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_plist_label()}.plist"


def _daemon_path() -> str:
    bundle = app_bundle_path()
    if bundle:
        return str(bundle / "Contents" / "MacOS" / "loopline-app")
    return shutil.which("loopline-app") or "loopline-app"


def _write_plist() -> None:
    daemon = _daemon_path()
    log = str(_logs_dir() / "loopline-daemon.log")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{_plist_label()}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{daemon}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
  <key>ThrottleInterval</key>
  <integer>5</integer>
</dict>
</plist>
"""
    _plist_path().write_text(plist, encoding="utf-8")


def _write_settings(
    slack_user_token: str = "",
    tg_api_id: str = "",
    tg_api_hash: str = "",
    sf_instance_url: str = "",
    sf_username: str = "",
    sf_password: str = "",
    sf_security_token: str = "",
) -> None:
    example_src = Path(__file__).parent / "resources" / "settings.yaml.example"
    dest = _settings_path()
    if not dest.exists():
        if example_src.exists():
            shutil.copy(example_src, dest)
        else:
            dest.write_text(
                "logging:\n  level: INFO\n  file: logs/loopline.log\n",
                encoding="utf-8",
            )

    import yaml
    with open(dest, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        cfg = {}

    if slack_user_token.strip():
        cfg.setdefault("slack", {})["user_token"] = slack_user_token.strip()

    if tg_api_id.strip() and tg_api_hash.strip():
        tg = cfg.setdefault("telegram", {})
        tg["api_id"] = int(tg_api_id.strip())
        tg["api_hash"] = tg_api_hash.strip()
        tg.setdefault("session_file", "credentials/telegram.session")

    if sf_instance_url.strip() and sf_username.strip() and sf_password.strip():
        sf = cfg.setdefault("salesforce", {})
        sf["instance_url"] = sf_instance_url.strip()
        sf["username"] = sf_username.strip()
        sf["password"] = sf_password.strip()
        if sf_security_token.strip():
            sf["security_token"] = sf_security_token.strip()

    with open(dest, "w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, allow_unicode=True, default_flow_style=False)


def _mcp_snippet() -> str:
    bridge = _bridge_path()
    return json.dumps(
        {
            "mcpServers": {
                "loopline": {
                    "command": bridge,
                }
            }
        },
        indent=2,
    )


# ── OAuth runners (called from background threads) ────────────────────────────

def _run_oauth(service: str, on_done: Callable[[bool, str], None]) -> None:
    try:
        creds = str(_client_secret_path())
        if service == "gmail":
            from .gmail_client import GmailClient, GmailClientError
            client = GmailClient(
                credentials_file=creds,
                token_file=str(_credentials_dir() / "token.json"),
            )
            client.authorize_interactive()
            email = client.check_connection()
            on_done(True, f"Gmail authorized as {email}")

        elif service == "drive":
            from .drive_client import DriveClient, DriveClientError
            client = DriveClient(
                credentials_file=creds,
                token_file=str(_credentials_dir() / "drive_token.json"),
            )
            client.authorize_interactive()
            email = client.check_connection()
            on_done(True, f"Drive authorized as {email}")

        elif service == "calendar":
            from .calendar_client import CalendarClient, CalendarClientError
            client = CalendarClient(
                credentials_file=creds,
                token_file=str(_credentials_dir() / "calendar_token.json"),
            )
            client.authorize_interactive()
            email = client.check_connection()
            on_done(True, f"Calendar authorized for {email}")

        elif service == "contacts":
            from .contacts_client import ContactsClient, ContactsClientError
            client = ContactsClient(
                credentials_file=creds,
                token_file=str(_credentials_dir() / "contacts_token.json"),
            )
            client.authorize_interactive()
            result = client.check_connection()
            on_done(True, f"Contacts authorized ({result})")

        elif service == "tasks":
            from .tasks_client import TasksClient, TasksClientError
            client = TasksClient(
                credentials_file=creds,
                token_file=str(_credentials_dir() / "tasks_token.json"),
            )
            client.authorize_interactive()
            result = client.check_connection()
            on_done(True, f"Tasks authorized ({result})")

    except Exception as exc:  # noqa: BLE001
        on_done(False, str(exc))


# ── Wizard window ─────────────────────────────────────────────────────────────

class SetupWizard:
    PAGES = ["welcome", "google_creds", "google_oauth", "slack", "telegram", "salesforce", "launch_agent", "done"]

    def __init__(self, parent: tk.Misc | None = None) -> None:
        if parent is None:
            self.root = tk.Tk()
            self._is_toplevel = False
        else:
            self.root = tk.Toplevel(parent)
            self._is_toplevel = True
        self.root.title("Loopline Setup")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        if self._is_toplevel:
            self.root.grab_set()

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - WIN_W) // 2
        y = (sh - WIN_H) // 2
        self.root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

        self._page_idx = 0
        self._slack_user_token = tk.StringVar()
        self._oauth_states: dict[str, str] = {}  # service → "idle"|"running"|"ok"|"error: ..."
        self._google_services = ["gmail", "drive", "calendar", "contacts", "tasks"]
        # Telegram state
        self._tg_api_id = tk.StringVar()
        self._tg_api_hash = tk.StringVar()
        self._tg_phone = tk.StringVar()
        self._tg_code = tk.StringVar()
        self._tg_phone_code_hash: str | None = None
        self._tg_client = None  # telethon client kept alive between steps
        # Salesforce state
        self._sf_instance_url = tk.StringVar()
        self._sf_username = tk.StringVar()
        self._sf_password = tk.StringVar()
        self._sf_security_token = tk.StringVar()

        self._build_layout()
        self._show_page()

    # ── layout skeleton ───────────────────────────────────────────────────────

    def _build_layout(self) -> None:
        self._header = tk.Label(self.root, bg=BG, fg=TEXT, anchor="w",
                                font=("Helvetica Neue", 18, "bold"), padx=28, pady=20)
        self._header.pack(fill="x")

        sep = tk.Frame(self.root, bg=SURFACE, height=1)
        sep.pack(fill="x")

        self._body = tk.Frame(self.root, bg=BG)
        self._body.pack(fill="both", expand=True, padx=28, pady=16)

        sep2 = tk.Frame(self.root, bg=SURFACE, height=1)
        sep2.pack(fill="x")

        nav = tk.Frame(self.root, bg=BG, pady=14)
        nav.pack(fill="x", padx=28)

        self._back_btn = tk.Button(nav, text="← Back", command=self._go_back,
                                   bg=SURFACE, fg=TEXT, relief="flat",
                                   padx=16, pady=6, cursor="hand2",
                                   activebackground="#45475a", activeforeground=TEXT)
        self._back_btn.pack(side="left")

        self._next_btn = tk.Button(nav, text="Next →", command=self._go_next,
                                   bg=ACCENT, fg=BG, relief="flat",
                                   padx=20, pady=6, cursor="hand2",
                                   font=("Helvetica Neue", 13, "bold"),
                                   activebackground="#74c7ec", activeforeground=BG)
        self._next_btn.pack(side="right")

    def _clear_body(self) -> None:
        for w in self._body.winfo_children():
            w.destroy()

    def _label(self, text: str, fg: str = TEXT, size: int = 13,
               bold: bool = False, wrap: int = 560) -> tk.Label:
        weight = "bold" if bold else "normal"
        return tk.Label(self._body, text=text, bg=BG, fg=fg, anchor="w",
                        justify="left", wraplength=wrap,
                        font=("Helvetica Neue", size, weight))

    # ── navigation ────────────────────────────────────────────────────────────

    def _go_next(self) -> None:
        if not self._can_advance():
            return
        self._page_idx = min(self._page_idx + 1, len(self.PAGES) - 1)
        self._show_page()

    def _go_back(self) -> None:
        self._page_idx = max(self._page_idx - 1, 0)
        self._show_page()

    def _can_advance(self) -> bool:
        page = self.PAGES[self._page_idx]
        if page == "google_creds":
            if not _client_secret_path().exists():
                messagebox.showwarning(
                    "Missing credentials",
                    "Please import your Google OAuth client_secret.json first.",
                    parent=self.root,
                )
                return False
        return True

    def _show_page(self) -> None:
        self._clear_body()
        page = self.PAGES[self._page_idx]
        is_first = self._page_idx == 0
        is_last = self._page_idx == len(self.PAGES) - 1

        self._back_btn.config(state="normal" if not is_first else "disabled")
        self._next_btn.config(text="Finish" if is_last else "Next →",
                              command=self._finish if is_last else self._go_next)

        getattr(self, f"_page_{page}")()

    # ── pages ─────────────────────────────────────────────────────────────────

    def _page_welcome(self) -> None:
        self._header.config(text="Welcome to Loopline")
        logo = tk.Label(self._body, text="🔒", bg=BG, font=("Helvetica Neue", 48))
        logo.pack(pady=(10, 4))
        self._label(
            "Loopline is a privacy proxy that sits between Claude AI and your "
            "accounts (Gmail, Drive, Calendar, Contacts, Tasks, Slack, Telegram, "
            "Salesforce). Every request Claude makes goes through you — nothing "
            "passes without approval.",
            fg=TEXT, size=14,
        ).pack(anchor="w", pady=(0, 12))
        self._label(
            "This wizard will connect your accounts and install Loopline to start "
            "automatically at login. It takes about 2 minutes.",
            fg=SUBTEXT,
        ).pack(anchor="w")

    def _page_google_creds(self) -> None:
        self._header.config(text="Google Credentials")

        exists = _client_secret_path().exists()
        status_color = GREEN if exists else YELLOW
        status_text = f"✓  Found at {_client_secret_path().name}" if exists else "No file imported yet"

        self._label(
            "Loopline uses an OAuth client secret to connect to Google services. "
            "You need to download this once from Google Cloud Console.",
            fg=SUBTEXT,
        ).pack(anchor="w", pady=(0, 16))

        steps_text = (
            "1. Go to console.cloud.google.com → APIs & Services → Credentials\n"
            "2. Create an OAuth 2.0 Client ID of type 'Desktop app'\n"
            "3. Download the JSON file and click Import below"
        )
        self._label(steps_text, fg=TEXT, size=12).pack(anchor="w", pady=(0, 16))

        self._creds_status = tk.Label(self._body, text=status_text, bg=BG, fg=status_color,
                                      font=("Helvetica Neue", 12), anchor="w")
        self._creds_status.pack(anchor="w", pady=(0, 12))

        btn = tk.Button(
            self._body, text="Import client_secret.json…",
            command=self._import_client_secret,
            bg=SURFACE, fg=TEXT, relief="flat", padx=14, pady=6,
            cursor="hand2", activebackground="#45475a", activeforeground=TEXT,
        )
        btn.pack(anchor="w")

    def _import_client_secret(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Select Google OAuth client_secret.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            shutil.copy(path, _client_secret_path())
            self._creds_status.config(text=f"✓  Imported successfully", fg=GREEN)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Import failed", str(exc), parent=self.root)

    def _page_google_oauth(self) -> None:
        self._header.config(text="Connect Google Services")
        self._label(
            "Click Authorize for each service you want Claude to access. "
            "Your browser will open to complete the sign-in.",
            fg=SUBTEXT,
        ).pack(anchor="w", pady=(0, 16))

        self._oauth_labels: dict[str, tk.Label] = {}

        service_names = {
            "gmail": "Gmail (read & send email)",
            "drive": "Google Drive (read & write files)",
            "calendar": "Google Calendar (read & create events)",
            "contacts": "Google Contacts (read contacts)",
            "tasks": "Google Tasks (read & manage tasks)",
        }

        for svc, display in service_names.items():
            row = tk.Frame(self._body, bg=BG)
            row.pack(fill="x", pady=3)

            state = self._oauth_states.get(svc, "idle")
            status_text, status_color = self._oauth_display(state)

            name_lbl = tk.Label(row, text=display, bg=BG, fg=TEXT,
                                font=("Helvetica Neue", 13), anchor="w", width=34)
            name_lbl.pack(side="left")

            status_lbl = tk.Label(row, text=status_text, bg=BG, fg=status_color,
                                  font=("Helvetica Neue", 12), anchor="w", width=18)
            status_lbl.pack(side="left")
            self._oauth_labels[svc] = status_lbl

            btn = tk.Button(
                row, text="Authorize",
                command=lambda s=svc: self._start_oauth(s),
                bg=SURFACE, fg=TEXT, relief="flat", padx=10, pady=3,
                cursor="hand2", activebackground="#45475a", activeforeground=TEXT,
            )
            btn.pack(side="right")

    def _oauth_display(self, state: str) -> tuple[str, str]:
        if state == "idle":
            return "Not connected", SUBTEXT
        if state == "running":
            return "Connecting…", YELLOW
        if state == "ok" or state.startswith("ok:"):
            return "✓  Connected", GREEN
        return f"✗  Error", RED

    def _start_oauth(self, service: str) -> None:
        if not _client_secret_path().exists():
            messagebox.showwarning(
                "Missing credentials",
                "Import client_secret.json first (previous page).",
                parent=self.root,
            )
            return
        self._oauth_states[service] = "running"
        lbl = self._oauth_labels.get(service)
        if lbl:
            lbl.config(text="Connecting…", fg=YELLOW)

        def on_done(ok: bool, msg: str) -> None:
            self._oauth_states[service] = f"ok:{msg}" if ok else f"error:{msg}"
            if lbl:
                text, color = self._oauth_display(self._oauth_states[service])
                self.root.after(0, lambda: lbl.config(text=text, fg=color))

        threading.Thread(target=_run_oauth, args=(service, on_done), daemon=True).start()

    def _page_slack(self) -> None:
        self._header.config(text="Slack (Optional)")
        self._label(
            "Paste your Slack User OAuth Token (xoxp-…) to give Claude the same "
            "Slack access you have — no bot to invite. Leave blank to skip.",
            fg=SUBTEXT,
        ).pack(anchor="w", pady=(0, 12))
        self._label(
            "Required scopes: channels:read, groups:read, im:read, mpim:read,\n"
            "channels:history, groups:history, im:history, mpim:history,\n"
            "users:read, users:read.email, search:read, chat:write",
            fg=SUBTEXT, size=11,
        ).pack(anchor="w", pady=(0, 10))
        user_entry = tk.Entry(self._body, textvariable=self._slack_user_token,
                              bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                              relief="flat", font=("Courier", 12), width=52)
        user_entry.pack(anchor="w", ipady=6)
        user_entry.insert(0, self._slack_user_token.get() or "xoxp-")

    def _page_telegram(self) -> None:
        self._header.config(text="Telegram (Optional)")
        self._label(
            "To give Claude access to your Telegram messages, enter your API credentials "
            "from my.telegram.org/apps and authorize your account. Leave blank to skip.",
            fg=SUBTEXT,
        ).pack(anchor="w", pady=(0, 12))

        def _field(label: str, var: tk.StringVar, show: str = "") -> tk.Entry:
            tk.Label(self._body, text=label, bg=BG, fg=TEXT,
                     font=("Helvetica Neue", 12), anchor="w").pack(anchor="w", pady=(6, 0))
            e = tk.Entry(self._body, textvariable=var, bg=SURFACE, fg=TEXT,
                         insertbackground=TEXT, relief="flat",
                         font=("Courier", 12), width=44, show=show)
            e.pack(anchor="w", ipady=5)
            return e

        self._tg_api_id_entry = _field("API ID (from my.telegram.org/apps)", self._tg_api_id)
        self._tg_api_hash_entry = _field("API Hash", self._tg_api_hash, show="•")
        self._tg_phone_entry = _field("Phone number (with country code, e.g. +1234567890)", self._tg_phone)

        self._tg_status = tk.Label(self._body, text="", bg=BG, fg=SUBTEXT,
                                   font=("Helvetica Neue", 12), anchor="w", wraplength=560)
        self._tg_status.pack(anchor="w", pady=(8, 0))

        btn_row = tk.Frame(self._body, bg=BG)
        btn_row.pack(anchor="w", pady=(8, 0))

        self._tg_send_btn = tk.Button(
            btn_row, text="Send Code",
            command=self._tg_send_code,
            bg=SURFACE, fg=TEXT, relief="flat", padx=12, pady=4,
            cursor="hand2", activebackground="#45475a", activeforeground=TEXT,
        )
        self._tg_send_btn.pack(side="left", padx=(0, 8))

        # Code entry + authorize — hidden until code is sent
        self._tg_code_frame = tk.Frame(self._body, bg=BG)
        self._tg_code_frame.pack(anchor="w", pady=(8, 0))
        tk.Label(self._tg_code_frame, text="Verification code:", bg=BG, fg=TEXT,
                 font=("Helvetica Neue", 12), anchor="w").pack(anchor="w")
        tk.Entry(self._tg_code_frame, textvariable=self._tg_code, bg=SURFACE, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Courier", 12), width=20,
                 ).pack(anchor="w", ipady=5, pady=(2, 6))
        tk.Button(
            self._tg_code_frame, text="Authorize",
            command=self._tg_authorize,
            bg=ACCENT, fg=BG, relief="flat", padx=12, pady=4,
            cursor="hand2", font=("Helvetica Neue", 12, "bold"),
            activebackground="#74c7ec", activeforeground=BG,
        ).pack(anchor="w")
        self._tg_code_frame.pack_forget()  # hide until needed

    def _tg_send_code(self) -> None:
        api_id_str = self._tg_api_id.get().strip()
        api_hash = self._tg_api_hash.get().strip()
        phone = self._tg_phone.get().strip()
        if not api_id_str or not api_hash or not phone:
            self._tg_status.config(text="Please fill in all three fields.", fg=RED)
            return
        try:
            api_id = int(api_id_str)
        except ValueError:
            self._tg_status.config(text="API ID must be a number.", fg=RED)
            return

        self._tg_status.config(text="Sending code…", fg=YELLOW)
        self._tg_send_btn.config(state="disabled")

        def _run() -> None:
            import asyncio
            try:
                from telethon import TelegramClient
                session_file = str(_credentials_dir() / "telegram.session")
                client = TelegramClient(session_file, api_id, api_hash)
                asyncio.run(_send(client, phone))
            except Exception as exc:
                self.root.after(0, lambda: (
                    self._tg_status.config(text=f"Error: {exc}", fg=RED),
                    self._tg_send_btn.config(state="normal"),
                ))

        async def _send(client, phone: str) -> None:
            await client.connect()
            result = await client.send_code_request(phone)
            self._tg_phone_code_hash = result.phone_code_hash
            self._tg_client = client
            self.root.after(0, lambda: (
                self._tg_status.config(text="Code sent! Check your Telegram app.", fg=GREEN),
                self._tg_code_frame.pack(anchor="w", pady=(8, 0)),
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _tg_authorize(self) -> None:
        code = self._tg_code.get().strip()
        phone = self._tg_phone.get().strip()
        if not code:
            self._tg_status.config(text="Enter the verification code.", fg=RED)
            return

        self._tg_status.config(text="Authorizing…", fg=YELLOW)

        def _run() -> None:
            import asyncio
            try:
                asyncio.run(_sign_in())
            except Exception as exc:
                self.root.after(0, lambda: self._tg_status.config(text=f"Error: {exc}", fg=RED))

        async def _sign_in() -> None:
            client = self._tg_client
            await client.sign_in(phone, code, phone_code_hash=self._tg_phone_code_hash)
            me = await client.get_me()
            name = f"{me.first_name or ''} {me.last_name or ''}".strip()
            await client.disconnect()
            self._tg_client = None
            self.root.after(0, lambda: (
                self._tg_status.config(text=f"✓  Authorized as {name}", fg=GREEN),
                self._tg_code_frame.pack_forget(),
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _page_salesforce(self) -> None:
        self._header.config(text="Salesforce (Optional)")
        self._label(
            "Enter your Salesforce credentials to give Claude access to your org. "
            "Leave blank to skip.",
            fg=SUBTEXT,
        ).pack(anchor="w", pady=(0, 12))

        def _field(label: str, var: tk.StringVar, show: str = "") -> None:
            tk.Label(self._body, text=label, bg=BG, fg=TEXT,
                     font=("Helvetica Neue", 12), anchor="w").pack(anchor="w", pady=(4, 0))
            tk.Entry(self._body, textvariable=var, bg=SURFACE, fg=TEXT,
                     insertbackground=TEXT, relief="flat",
                     font=("Courier", 12), width=44, show=show).pack(anchor="w", ipady=5)

        _field("Instance URL (e.g. https://yourorg.my.salesforce.com)", self._sf_instance_url)
        _field("Username", self._sf_username)
        _field("Password", self._sf_password, show="•")
        _field("Security Token", self._sf_security_token, show="•")

        self._sf_status = tk.Label(self._body, text="", bg=BG, fg=SUBTEXT,
                                   font=("Helvetica Neue", 12), anchor="w", wraplength=560)
        self._sf_status.pack(anchor="w", pady=(8, 0))

        tk.Button(
            self._body, text="Test Connection",
            command=self._sf_test,
            bg=SURFACE, fg=TEXT, relief="flat", padx=12, pady=4,
            cursor="hand2", activebackground="#45475a", activeforeground=TEXT,
        ).pack(anchor="w", pady=(8, 0))

    def _sf_test(self) -> None:
        instance_url = self._sf_instance_url.get().strip()
        username = self._sf_username.get().strip()
        password = self._sf_password.get().strip()
        security_token = self._sf_security_token.get().strip()
        if not instance_url or not username or not password:
            self._sf_status.config(text="Instance URL, username, and password are required.", fg=RED)
            return

        self._sf_status.config(text="Connecting…", fg=YELLOW)

        def _run() -> None:
            try:
                from .salesforce_client import SalesforceClient
                cfg = {
                    "instance_url": instance_url,
                    "username": username,
                    "password": password,
                    "security_token": security_token,
                }
                org = SalesforceClient(cfg).check_connection()
                self.root.after(0, lambda: self._sf_status.config(
                    text=f"✓  Connected to {org}", fg=GREEN))
            except Exception as exc:
                self.root.after(0, lambda: self._sf_status.config(
                    text=f"Error: {exc}", fg=RED))

        threading.Thread(target=_run, daemon=True).start()

    def _page_launch_agent(self) -> None:
        self._header.config(text="Start at Login")
        self._label(
            "Install Loopline as a Login Item so it starts automatically when "
            "you log in. This writes a LaunchAgent plist to ~/Library/LaunchAgents/.",
            fg=SUBTEXT,
        ).pack(anchor="w", pady=(0, 16))

        installed = _plist_path().exists()
        self._agent_status = tk.Label(
            self._body,
            text="✓  Already installed" if installed else "Not installed yet",
            bg=BG, fg=GREEN if installed else SUBTEXT,
            font=("Helvetica Neue", 13), anchor="w",
        )
        self._agent_status.pack(anchor="w", pady=(0, 12))

        btn = tk.Button(
            self._body,
            text="Install LaunchAgent",
            command=self._install_launch_agent,
            bg=SURFACE, fg=TEXT, relief="flat", padx=14, pady=6,
            cursor="hand2", activebackground="#45475a", activeforeground=TEXT,
        )
        btn.pack(anchor="w")

    def _install_launch_agent(self) -> None:
        try:
            _write_plist()
            subprocess.run(
                ["launchctl", "load", str(_plist_path())],
                check=True, capture_output=True,
            )
            self._agent_status.config(text="✓  Installed and loaded", fg=GREEN)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"").decode().strip()
            messagebox.showerror("LaunchAgent install failed", err or str(exc), parent=self.root)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", str(exc), parent=self.root)

    def _page_done(self) -> None:
        self._header.config(text="All Done!")
        self._next_btn.config(text="Finish")

        self._label(
            "Loopline is ready. Add it to Claude's MCP configuration by copying "
            "the snippet below into your claude_desktop_config.json.",
            fg=TEXT, size=13,
        ).pack(anchor="w", pady=(0, 12))

        snippet = _mcp_snippet()
        box = tk.Text(self._body, bg=SURFACE, fg=TEXT,
                      font=("Courier", 11), relief="flat",
                      height=7, wrap="none")
        box.insert("1.0", snippet)
        box.config(state="disabled")
        box.pack(fill="x", pady=(0, 10))

        copy_btn = tk.Button(
            self._body, text="Copy to Clipboard",
            command=lambda: (self.root.clipboard_clear(),
                             self.root.clipboard_append(snippet),
                             copy_btn.config(text="Copied ✓", fg=GREEN)),
            bg=SURFACE, fg=TEXT, relief="flat", padx=14, pady=5,
            cursor="hand2", activebackground="#45475a", activeforeground=TEXT,
        )
        copy_btn.pack(anchor="w", pady=(0, 8))

        config_path = (
            "~/Library/Application Support/Claude/claude_desktop_config.json"
        )
        self._label(
            f"Config file location:\n{config_path}",
            fg=SUBTEXT, size=11,
        ).pack(anchor="w")

    # ── finish ────────────────────────────────────────────────────────────────

    def _finish(self) -> None:
        try:
            _write_settings(
                slack_user_token=self._slack_user_token.get(),
                tg_api_id=self._tg_api_id.get(),
                tg_api_hash=self._tg_api_hash.get(),
                sf_instance_url=self._sf_instance_url.get(),
                sf_username=self._sf_username.get(),
                sf_password=self._sf_password.get(),
                sf_security_token=self._sf_security_token.get(),
            )
            _sentinel_path().touch()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write settings on finish: %s", exc)
        self.root.destroy()

    def run(self) -> None:
        if self._is_toplevel:
            self.root.wait_window()
        else:
            self.root.mainloop()


# ── public entry point ────────────────────────────────────────────────────────

def run_setup_wizard(parent: tk.Misc | None = None) -> None:
    """Run the setup wizard and block until the window is closed."""
    wizard = SetupWizard(parent=parent)
    wizard.run()
