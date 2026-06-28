"""Native macOS approval popups via osascript — no tkinter dependency."""
from __future__ import annotations

import os
import subprocess
import tempfile


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


def show_review_popup(title: str, preview: dict[str, str], details_text: str) -> str:
    """Preview popup for read tools (review gate).

    Returns 'accept', 'deny', or calls show_details_popup if user clicks
    'Show Details', then returns accept/deny from that second dialog.
    """
    lines = [f"{k}: {v}" for k, v in preview.items()]
    msg = _build_message(lines)
    script = (
        f"set btn to button returned of "
        f"(display dialog {msg} "
        f"with title {_as_str(title)} "
        f'buttons {{"Deny", "Show Details", "Accept"}} '
        f'default button "Accept")\n'
        f"return btn"
    )
    clicked = _run(script)
    if clicked == "Accept":
        return "accept"
    if clicked == "Show Details":
        return _show_details_then_decide(title, details_text)
    return "deny"


def show_popup(title: str, details_text: str) -> str:
    """Approval popup for write tools (popup gate).

    Returns 'accept' or 'deny'.
    """
    # Truncate very long content for the dialog body; full content visible
    # via Show Details only available on review-gate reads.
    body = details_text[:1500] + ("…" if len(details_text) > 1500 else "")
    msg = _build_message(body.splitlines() or ["(no details)"])
    script = (
        f"set btn to button returned of "
        f"(display dialog {msg} "
        f"with title {_as_str(title)} "
        f'buttons {{"Deny", "Accept"}} '
        f'default button "Accept")\n'
        f"return btn"
    )
    clicked = _run(script)
    return "accept" if clicked == "Accept" else "deny"


def _show_details_then_decide(title: str, details_text: str) -> str:
    """Write full content to a temp file, open it, then show Accept/Deny."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="loopline_details_",
        delete=False, encoding="utf-8",
    ) as f:
        f.write(details_text)
        fname = f.name
    try:
        subprocess.run(["open", "-t", fname], check=False)
        script = (
            f"set btn to button returned of "
            f"(display dialog {_as_str('Review the full content in TextEdit, then choose:')} "
            f"with title {_as_str(title)} "
            f'buttons {{"Deny", "Accept"}} '
            f'default button "Accept")\n'
            f"return btn"
        )
        clicked = _run(script)
        return "accept" if clicked == "Accept" else "deny"
    finally:
        try:
            os.unlink(fname)
        except OSError:
            pass
