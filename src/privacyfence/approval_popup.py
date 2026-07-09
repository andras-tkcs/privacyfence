"""Native macOS approval popups.

Every gated tool call resolves through exactly one blocking dialog here.
There is no separate "show details" step and no pending-approval handshake:
full content is always shown before the decision, so the human always sees
what they're approving before they can click Accept. The main gate
(show_popup / show_read_popup) renders through approval_window.py's custom
AppKit window; show_rule_confirmation_popup and show_pii_confirmation_popup
are smaller secondary prompts (confirming a standing auto-accept rule, or
confirming approval of content the PII detector flagged) and stay on the
simpler osascript `display dialog`.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from .approval_window import show_native_approval


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


# ---------------------------------------------------------------------------- #
# Write gate (actions: send, create, edit, move, comment)
# ---------------------------------------------------------------------------- #

def show_popup(
    title: str,
    preview: dict[str, str],
    details_text: str,
    pii_categories: list[str] | None = None,
    allow_temp_accept: bool = False,
) -> str:
    """Approval popup for write tools.

    Returns 'accept', 'deny', or 'accept_temp' (only offered when
    allow_temp_accept is True -- see gate.py's TEMP_ACCEPT_ELIGIBLE_OPERATIONS
    for which write operations get that button).
    """
    return show_native_approval(
        title=title, preview=preview, details_text=details_text, allow_accept_all=False,
        pii_categories=pii_categories, allow_temp_accept=allow_temp_accept,
    )


# ---------------------------------------------------------------------------- #
# Review gate (reads)
# ---------------------------------------------------------------------------- #

def show_read_popup(
    title: str,
    preview: dict[str, str],
    details_text: str,
    allow_accept_all: bool,
    pii_categories: list[str] | None = None,
) -> str:
    """Approval popup for read tools. Full content is always shown before the
    decision, in a scrollable pane — the user never has to click through to
    a second "show details" step.

    Returns 'accept', 'deny', or 'accept_all' (only offered when
    allow_accept_all is True).
    """
    return show_native_approval(
        title=title, preview=preview, details_text=details_text, allow_accept_all=allow_accept_all,
        pii_categories=pii_categories,
    )


def show_pii_confirmation_popup(categories: list[str]) -> bool:
    """Second-step confirmation shown when the PII detector flagged possible
    personal data in the content just approved.

    Defaults to Cancel, same rationale as show_rule_confirmation_popup:
    hitting Enter shouldn't silently let flagged content through.
    """
    cats = ", ".join(categories) if categories else "personal data"
    lines = [
        f"PrivacyFence detected possible personal data in this content: {cats}.",
        "",
        "Are you sure you want to proceed?",
    ]
    clicked = _display_dialog(
        "PrivacyFence — Possible PII Detected", lines, ["Cancel", "Proceed"], default="Cancel"
    )
    return clicked == "Proceed"


def show_rule_confirmation_popup(description: str) -> bool:
    """Second-step confirmation shown after "Accept All" is clicked.

    Defaults to Cancel — unlike the main gate, hitting Enter here shouldn't
    silently create a standing rule that skips future approvals.
    """
    lines = [
        "PrivacyFence will create an auto-accept rule:",
        "",
        description,
        "",
        "Future matching requests will be approved automatically, without a popup.",
    ]
    clicked = _display_dialog(
        "PrivacyFence — Confirm Auto-Accept Rule", lines, ["Cancel", "Confirm"], default="Cancel"
    )
    return clicked == "Confirm"
