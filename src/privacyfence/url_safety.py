"""Shared scheme allowlist for link targets rendered as a clickable HTML
``href``.

Used by both email_markdown.py (rich-text Gmail draft bodies) and
markdown_to_html.py (the approval window's preview pane) so a Markdown
``[text](url)`` link never turns into ``href="javascript:..."`` (or any
other scheme a mail client or browser might act on) smuggled in through
extracted, forwarded, or otherwise externally-influenced content. Both
renderers already HTML-escape literal text; this is the other half --
validating the *scheme* of an href before it's ever emitted, not just
escaping its characters.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# Schemes a mail client or browser will actually open as a link. Anything
# else has its href dropped by the caller (the link text still renders,
# just not as a clickable link) rather than emitting a link a recipient
# might act on.
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def is_safe_url(url: str) -> bool:
    """True iff ``url``'s scheme is on the allowlist.

    A bare "example.com" (no scheme) is treated as unsafe rather than
    guessed at -- callers should write "https://example.com".
    """
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return False
    return scheme in ALLOWED_URL_SCHEMES
