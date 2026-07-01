"""Native macOS approval popups via osascript — no tkinter dependency.

Every gated tool call resolves through exactly one blocking dialog here.
There is no separate "show details" step and no pending-approval handshake:
full content is either shown inline or opened in TextEdit before the same
dialog that asks for a decision, so the human always sees what they're
approving before they can click Accept.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

_INLINE_LIMIT = 800  # display dialog has no scrollbar; beyond this, use TextEdit


def _as_str(s: str) -> str:
    """Encode a Python string as an AppleScript string expression."""
    parts = s.split('"')
    encoded = ' & quote & '.join(f'"{p}"' for p in parts)
    return encoded or '""'


def _build_message(lines: list[str]) -> str:
    if not lines:
        return '""'
    parts = [_as_str(line) for line in lines]
    return ' & return & '.join(parts)


def _run(script: str) -> str | None:
    """Run an AppleScript string, return the button clicked or None."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".applescript", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        fname = f.name
    try:
        result = subprocess.run(
            ["osascript", fname],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            out = result.stdout.strip()
            if out.startswith("button returned:"):
                return out[len("button returned:"):]
            return out or None
        return None
    finally:
        try:
            os.unlink(fname)
        except OSError:
            pass


def _display_dialog(title: str, lines: list[str], buttons: list[str], default: str) -> str | None:
    """Show a native dialog with the given buttons; returns the clicked label or None."""
    msg = _build_message(lines)
    btns = "{" + ", ".join(f'"{b}"' for b in buttons) + "}"
    script = (
        f"set btn to button returned of "
        f"(display dialog {msg} "
        f"with title {_as_str(title)} "
        f"buttons {btns} "
        f'default button "{default}")\n'
        f"return btn"
    )
    return _run(script)


def _write_temp_file(text: str) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="loopline_details_",
        delete=False, encoding="utf-8",
    ) as f:
        f.write(text)
        return f.name


# ---------------------------------------------------------------------------- #
# Write gate (actions: send, create, edit, move, comment)
# ---------------------------------------------------------------------------- #

def show_popup(title: str, details_text: str) -> str:
    """Approval popup for write tools. Returns 'accept' or 'deny'."""
    body = details_text[:1500] + ("…" if len(details_text) > 1500 else "")
    lines = body.splitlines() or ["(no details)"]
    clicked = _display_dialog(title, lines, ["Deny", "Accept"], default="Accept")
    return "accept" if clicked == "Accept" else "deny"


# ---------------------------------------------------------------------------- #
# Review gate (reads)
# ---------------------------------------------------------------------------- #

def show_read_popup(
    title: str, preview: dict[str, str], details_text: str, allow_accept_all: bool
) -> str:
    """Approval popup for read tools. Full content is always shown before the
    decision — inline if short, otherwise opened in TextEdit first.

    Returns 'accept', 'deny', or 'accept_all' (only offered when
    allow_accept_all is True).
    """
    buttons = ["Deny", "Accept All", "Accept"] if allow_accept_all else ["Deny", "Accept"]
    preview_lines = [f"{k}: {v}" for k, v in preview.items()]

    if len(details_text) <= _INLINE_LIMIT:
        lines = preview_lines + ["", details_text]
        clicked = _display_dialog(title, lines, buttons, default="Accept")
    else:
        fname = _write_temp_file(details_text)
        try:
            subprocess.run(["open", "-t", fname], check=False)
            lines = preview_lines + ["", "Full content opened in TextEdit."]
            clicked = _display_dialog(title, lines, buttons, default="Accept")
        finally:
            try:
                os.unlink(fname)
            except OSError:
                pass

    if clicked == "Accept":
        return "accept"
    if clicked == "Accept All":
        return "accept_all"
    return "deny"


def show_rule_confirmation_popup(description: str) -> bool:
    """Second-step confirmation shown after "Accept All" is clicked.

    Defaults to Cancel — unlike the main gate, hitting Enter here shouldn't
    silently create a standing rule that skips future approvals.
    """
    lines = [
        "Loopline will create an auto-accept rule:",
        "",
        description,
        "",
        "Future matching requests will be approved automatically, without a popup.",
    ]
    clicked = _display_dialog(
        "Loopline — Confirm Auto-Accept Rule", lines, ["Cancel", "Confirm"], default="Cancel"
    )
    return clicked == "Confirm"
