"""Shared icon-asset loading for approval surfaces.

Locates the bundled shield/connector PNGs (resources/icon_*.png,
resources/connector_icons/<name>.png) and returns them as base64 data URIs
for embedding directly into a card-stack HTML document -- see
approval_window_html.py's module docstring for why that document must never
trigger a network fetch to render.

Plain filesystem + base64, no AppKit/PyObjC dependency -- both
approval_window.py's native host (AppKit/WKWebView) and web_approval_ui.py's
browser host need the exact same data URIs, so this is factored out here
rather than duplicated, and importable on any platform. approval_window.py
keeps its own private _icon_path/_connector_icon_path/_icon_data_uri (not
migrated onto this module) so this change stays scoped to the new web
surface without touching the native path's own, separately-tested code.
"""
from __future__ import annotations

import base64
from pathlib import Path

_RESOURCES = Path(__file__).parent / "resources"

_icon_data_uri_cache: dict[str, str] = {}


def shield_icon_path() -> str | None:
    """PrivacyFence's own shield mark, top-right of every card -- see
    approval_window_html.py's _header_html. Same silent-skip fallback as
    connector_icon_path(): no bundled asset just means no icon, never an
    error."""
    for name in ("icon_64.png", "icon_512.png", "icon_32.png"):
        p = _RESOURCES / name
        if p.exists():
            return str(p)
    return None


def connector_icon_path(connector: str) -> str | None:
    """Real per-service brand icon (Gmail/Drive/Slack/etc.), top-left,
    alongside the "PrivacyFence" kicker -- see resources/connector_icons/README
    for where the bundled assets come from."""
    if not connector:
        return None
    p = _RESOURCES / "connector_icons" / f"{connector}.png"
    return str(p) if p.exists() else None


def icon_data_uri(path: str | None) -> str:
    """Base64 data: URI for a vendored PNG icon, or "" if missing. Cached
    (these are a fixed, small set of bundled resources, not user data) so
    repeated approvals don't re-read/re-encode the same file."""
    if not path:
        return ""
    if path not in _icon_data_uri_cache:
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        _icon_data_uri_cache[path] = f"data:image/png;base64,{data}"
    return _icon_data_uri_cache[path]
