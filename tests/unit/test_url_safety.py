"""Unit coverage for url_safety.py's shared scheme allowlist.

email_markdown.py and markdown_to_html.py are covered end-to-end for the
scheme-allowlist invariant by tests/unit/abuse/test_abuse_markdown_rendering.py
(SEC-01/TST-01a); this module covers is_safe_url() itself directly, including
the urlsplit() ValueError path (a malformed URL, e.g. an unterminated IPv6
literal) that abuse test's payload list doesn't happen to exercise.
"""
from __future__ import annotations

import pytest

from privacyfence.url_safety import ALLOWED_URL_SCHEMES, is_safe_url


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://example.com/path", id="https"),
        pytest.param("http://example.com", id="http"),
        pytest.param("mailto:someone@example.com", id="mailto"),
        pytest.param("HTTPS://example.com", id="uppercase-scheme"),
    ],
)
def test_allowed_schemes_are_safe(url: str) -> None:
    assert is_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("javascript:alert(1)", id="javascript"),
        pytest.param("data:text/html,evil", id="data"),
        pytest.param("vbscript:msgbox(1)", id="vbscript"),
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param("example.com", id="no-scheme"),
        pytest.param("", id="empty"),
    ],
)
def test_disallowed_or_missing_schemes_are_unsafe(url: str) -> None:
    assert not is_safe_url(url)


def test_malformed_url_raising_valueerror_is_unsafe() -> None:
    # urlsplit() raises ValueError on some malformed inputs (e.g. an
    # unterminated IPv6 host literal) rather than returning a scheme --
    # is_safe_url() must treat that as unsafe, not propagate the exception.
    with pytest.raises(ValueError):
        __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit("http://[::1")
    assert not is_safe_url("http://[::1")


def test_allowed_schemes_constant_is_exactly_http_https_mailto() -> None:
    # Pin the allowlist itself -- a caller elsewhere in the codebase could
    # otherwise widen it silently by mutating the set.
    assert ALLOWED_URL_SCHEMES == {"http", "https", "mailto"}
