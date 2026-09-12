"""Sanitization at the boundary between an internal exception and anything
that leaves the process -- an MCP tool-call error result handed back to the
client, or a line written to the local log file (SEC-10).

Today (pre-SEC-10) ``routes_mcp.py``'s ``handle_call_tool`` and
``idp_callback`` both do the equivalent of ``str(exc)`` straight into a
client-visible response or a log line. Most of what flows through a
``raise SomeError(...)`` in this codebase is written by us and carries
nothing sensitive (``f"Unknown tool: {tool!r}"``, ``"Request denied by
user"``), but plenty of it isn't: every connector's own ``*ClientError``
(gmail_client.py and friends) routinely wraps a third-party HTTP client's
own exception text via ``f"... failed: {exc}"``, and the OAuth modules
(google_oauth.py, atlassian_oauth.py) have at least one call site that
stringifies an entire token-exchange response dict into an error message --
exactly the kind of text that can carry a bearer token, an authorization
code, or a refresh token.

Complication a first pass misses: that wrapped, untrustworthy text doesn't
reach ``handle_call_tool`` as a ``*ClientError`` -- every connector's own
``_fetch``-style helper (see e.g. connectors/gmail.py) already catches its
``*ClientError`` and re-raises ``RuntimeError(str(exc))``, per
docs/coding-and-testing-guidelines.md §2.7's own "new connector code
catches it and re-raises as RuntimeError" rule. So a plain ``RuntimeError``
is, by a repo-wide convention older than this fix, exactly the type that
*does* carry arbitrary wrapped text -- while ``gate.py``'s own denial
raises (``"Request denied by user"`` and friends) also used to be plain
``RuntimeError``, composed only of static text. Same builtin type, two very
different trust levels, indistinguishable by ``isinstance`` alone. SEC-10
resolves this by giving every self-authored "denied, not failed" raise site
its own named ``RuntimeError`` subclass instead (``gate.GateDeniedError``;
``approvals.TooManyPendingApprovalsError`` and
``connector_registry.TooManyPrincipalsError`` already were one) and having
``public_message()`` below trust a *named* subclass but not the bare
``RuntimeError`` class itself -- so a connector's wrapped failure still
falls through to the generic message, and a future call site gets the
right trust level by construction the moment it reaches for a named type
instead of bare ``RuntimeError(...)``.

Two independent layers, matching the two places that text can leak:

* ``public_message()`` -- a *typed* allowlist. An exception type this
  module doesn't recognise as "self-authored, never carries third-party
  text" gets a fixed, generic message; nothing about its actual content is
  trusted enough to forward, redacted or not -- regex-based secret
  detection is necessarily incomplete (see ``redact_secrets``'s own
  docstring), so a type it hasn't reviewed doesn't get the benefit of the
  doubt. This is what a client-facing boundary (routes_mcp.py's
  ``handle_call_tool``) uses for whatever reaches the caller.
* ``redact_secrets()`` / ``SecretRedactingFormatter`` -- applied to the
  *detailed* diagnostic that still goes to the local log file, for every
  exception, not just the allowlisted ones, so a real secret that does make
  it into a log line is scrubbed there too rather than relying on the
  allowlist alone. Installed once, on the root logger, by
  ``daemon_main.setup_logging`` -- every logger in the process inherits it.

``public_message()`` also runs its result through ``redact_secrets()``
before returning it: defense in depth, not a substitute for the allowlist
-- an allowlisted type's message is expected to already be safe, but
nothing stops a future edit from interpolating something it shouldn't.
"""
from __future__ import annotations

import logging
import re

# ----------------------------------------------------------------------- #
# Typed public-message mapping
# ----------------------------------------------------------------------- #

# Exception types raised *by this codebase's own control-flow code* --
# "unknown tool", "unknown connector", "request denied by user", a missing
# required key in a meta-tool's arguments -- reviewed at every current
# raise site (see this module's docstring) as composed only of static text
# plus values already visible to the caller (a tool/connector name it just
# supplied itself). Deliberately narrow: every connector's own *ClientError
# family and every OAuth error type is a plain ``Exception`` subclass, not
# one of these, precisely because those *do* sometimes wrap a third party's
# own exception text or response body. A new call site that raises one of
# these four builtins with interpolated third-party text would be a bug in
# the raise site, not something this module can catch -- keep that in mind
# before reaching for ValueError/RuntimeError as "the safe one" elsewhere.
# RuntimeError carries one further carve-out on top of this list --
# public_message() below trusts it only when the exception's type is a
# *named* subclass, not bare ``RuntimeError`` itself; see that function's
# own docstring and this module's docstring for why.
PUBLIC_SAFE_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    ValueError,
    RuntimeError,
    LookupError,
    TypeError,
)

GENERIC_PUBLIC_MESSAGE = "Tool call failed. See the PrivacyFence log for details."


def public_message(exc: BaseException) -> str:
    """The text a client-facing surface (an MCP tool-call error result, an
    HTTP error page) may show for ``exc``. Exceptions of an unrecognised
    type get ``GENERIC_PUBLIC_MESSAGE`` instead of their own ``str()`` --
    the caller learns the call failed, not why, unless ``exc``'s type is on
    ``PUBLIC_SAFE_EXCEPTION_TYPES``. The detailed diagnostic is still
    available -- to an operator, not this caller -- in the local log.

    One narrower rule inside that allowlist (see this module's docstring):
    an exception whose type is *exactly* ``RuntimeError`` -- not a named
    subclass of it -- is treated as untrusted despite ``RuntimeError``
    being on the allowlist. That's the type every connector's own
    ``_fetch``-style helper wraps a ``*ClientError`` into
    (docs/coding-and-testing-guidelines.md §2.7), so a bare ``RuntimeError``
    is exactly as likely to carry wrapped third-party text as one of the
    *ClientError types themselves. A named ``RuntimeError`` subclass
    (``gate.GateDeniedError``, ``approvals.TooManyPendingApprovalsError``,
    ``connector_registry.TooManyPrincipalsError``) is still trusted --
    whoever defined it reviewed what goes into it, the same review this
    module's own docstring describes for the other three builtins."""
    if isinstance(exc, PUBLIC_SAFE_EXCEPTION_TYPES) and type(exc) is not RuntimeError:
        return redact_secrets(str(exc))
    return GENERIC_PUBLIC_MESSAGE


# ----------------------------------------------------------------------- #
# Token-shaped-string redaction
# ----------------------------------------------------------------------- #

_REDACTED = "[REDACTED]"

# Deliberately pattern-based rather than an exhaustive per-provider list --
# a new connector adds a new token shape faster than this module could be
# kept in sync with it. Every pattern matches a *token shape*, not "this
# exact secret", so it also catches a secret this process never minted
# itself (an authorization code lifted from an IdP redirect, a third
# party's own session token quoted back in an error body). Order matters
# only in that the key=value pattern should run first so it can claim the
# key name for context before a later, keyless pattern would otherwise eat
# just the value.
_KEY_VALUE_SECRET = re.compile(
    r'(?i)\b(authorization|bearer|access_token|refresh_token|id_token|'
    r'client_secret|api[_-]?key|session_id|session_token|bootstrap|'
    r'password|secret)\b(\s*[:=]\s*)"?[A-Za-z0-9\-_.~+/]{8,}={0,2}"?'
)

_KEYLESS_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # RFC 6750 Authorization header value, standalone (no "key=" prefix of
    # its own for _KEY_VALUE_SECRET to have already matched).
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.~+/]{8,}={0,2}"),
    # JSON Web Token: three base64url segments joined by dots.
    re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # Provider-prefixed token shapes seen across this codebase's connectors
    # and OAuth clients.
    re.compile(r"\bya29\.[A-Za-z0-9_-]+"),        # Google OAuth access token
    re.compile(r"\b1//[A-Za-z0-9_-]+"),           # Google OAuth refresh token
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+"),    # Slack token
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub token
)


def _redact_key_value(match: re.Match[str]) -> str:
    # Keep the key name and separator -- useful for the diagnostic -- and
    # redact only the value.
    return f"{match.group(1)}{match.group(2)}{_REDACTED}"


def redact_secrets(text: str) -> str:
    """Best-effort scrub of token-shaped substrings in ``text``. Not a
    guarantee -- a secret shape this module doesn't recognise passes
    through unchanged, which is exactly why ``public_message()`` above
    doesn't rely on this alone for anything not already on its allowlist.
    Safe to call on text with nothing to redact; returns it unchanged."""
    text = _KEY_VALUE_SECRET.sub(_redact_key_value, text)
    for pattern in _KEYLESS_SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class SecretRedactingFormatter(logging.Formatter):
    """Drop-in replacement for ``logging.Formatter`` that redacts
    token-shaped substrings from the fully-formatted line -- after
    ``%``-style interpolation, so it catches a secret embedded in an
    exception's own ``str()`` (an f-string built far from the log call)
    just as well as one in the log call's own format-string arguments."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))
