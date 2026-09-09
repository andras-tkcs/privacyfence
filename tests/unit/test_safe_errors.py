"""Unit tests for safe_errors.py (SEC-10, docs/security-remediation-plan.md
Phase 1.7) -- the typed public-message allowlist and the token-shaped-
string redaction used both there and by the log formatter.

See tests/unit/abuse/test_abuse_mcp_error_taxonomy.py for the full-chain
version of this: real exceptions carrying fake secrets, round-tripped
through the actual /mcp endpoint.
"""
from __future__ import annotations

import logging

import pytest

from privacyfence.safe_errors import (
    GENERIC_PUBLIC_MESSAGE,
    SecretRedactingFormatter,
    public_message,
    redact_secrets,
)


class SomeClientError(Exception):
    """Stand-in for gmail_client.GmailClientError and its siblings: a
    connector's own exception type, but not one of the four builtins
    public_message() allowlists -- see safe_errors.py's module docstring
    for why those specifically aren't trusted by type alone."""


class NamedRuntimeErrorSubclass(RuntimeError):
    """Stand-in for gate.GateDeniedError and friends -- a *named*
    RuntimeError subclass, reviewed as carrying only static text."""


class TestPublicMessageAllowlist:
    @pytest.mark.parametrize("exc_type", [ValueError, LookupError, TypeError])
    def test_allowlisted_types_pass_their_message_through(self, exc_type):
        exc = exc_type("Unknown tool: 'not_a_real_tool'")
        assert public_message(exc) == "Unknown tool: 'not_a_real_tool'"

    def test_keyerror_is_allowlisted_via_lookuperror(self):
        # KeyError is a LookupError subclass, so it's allowlisted -- but
        # its str() reprs the single arg (a stdlib quirk), so this can't
        # share the exact-match parametrize above.
        assert "reason" in public_message(KeyError("reason"))
        assert public_message(KeyError("reason")) != GENERIC_PUBLIC_MESSAGE

    def test_bare_runtime_error_gets_the_generic_message(self):
        # Not allowlisted despite RuntimeError itself being on
        # PUBLIC_SAFE_EXCEPTION_TYPES: this is the type every connector's
        # own `_fetch`-style helper wraps a *ClientError into (module
        # docstring), so a bare RuntimeError is exactly as likely to carry
        # wrapped third-party text as the *ClientError it came from.
        exc = RuntimeError("list_messages failed: refresh_token=abcdefgh12345678")
        assert public_message(exc) == GENERIC_PUBLIC_MESSAGE

    def test_named_runtime_error_subclass_passes_its_message_through(self):
        # The other half of the same rule: a *named* subclass -- one
        # someone deliberately defined and reviewed, like
        # gate.GateDeniedError -- is trusted the same as the other three
        # allowlisted builtins.
        exc = NamedRuntimeErrorSubclass("Request denied by user")
        assert public_message(exc) == "Request denied by user"

    def test_unrecognised_exception_type_gets_the_generic_message(self):
        exc = SomeClientError("list_messages failed: token=abc123def456ghi789")
        assert public_message(exc) == GENERIC_PUBLIC_MESSAGE

    def test_bare_exception_gets_the_generic_message(self):
        assert public_message(Exception("anything")) == GENERIC_PUBLIC_MESSAGE

    def test_allowlisted_message_is_still_redacted(self):
        # Defense in depth (module docstring): even a type we trust by
        # construction has its text scrubbed, in case a future edit
        # interpolates something it shouldn't.
        exc = ValueError("refresh_token=" + "a" * 40)
        assert "a" * 40 not in public_message(exc)
        assert "[REDACTED]" in public_message(exc)


class TestRealNamedRuntimeErrorSubclasses:
    """The actual named RuntimeError subclasses this codebase defines --
    not stand-ins -- confirming public_message() trusts them for real, not
    just in the abstract via NamedRuntimeErrorSubclass above."""

    def test_gate_denied_error_passes_through(self):
        from privacyfence.gate import GateDeniedError

        assert public_message(GateDeniedError("Request denied by user")) == "Request denied by user"

    def test_too_many_pending_approvals_error_passes_through(self):
        from privacyfence.approvals import TooManyPendingApprovalsError

        msg = "Too many approvals are already pending (5)"
        assert public_message(TooManyPendingApprovalsError(msg)) == msg

    def test_too_many_principals_error_passes_through(self):
        from privacyfence.connector_registry import TooManyPrincipalsError

        msg = "Connector registry is at capacity (5 principals)"
        assert public_message(TooManyPrincipalsError(msg)) == msg


class TestRedactSecrets:
    def test_leaves_ordinary_text_unchanged(self):
        text = "attachments: no such file: '/tmp/report.pdf'"
        assert redact_secrets(text) == text

    @pytest.mark.parametrize(
        "template",
        [
            'Authorization: Bearer {secret}',
            'access_token={secret}',
            'access_token: "{secret}"',
            "refresh_token={secret}",
            "client_secret={secret}",
            "api_key={secret}",
            "session_id={secret}",
        ],
    )
    def test_redacts_key_value_style_secrets(self, template):
        secret = "s3cr3t-value-1234567890"
        text = template.format(secret=secret)
        redacted = redact_secrets(text)
        assert secret not in redacted
        assert "[REDACTED]" in redacted

    def test_redacts_standalone_bearer_token(self):
        text = f"request failed -- Bearer {'x' * 40} was rejected"
        redacted = redact_secrets(text)
        assert "x" * 40 not in redacted

    def test_redacts_jwt_shaped_string(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        text = f"token verification failed for {jwt}"
        redacted = redact_secrets(text)
        assert jwt not in redacted
        assert "[REDACTED]" in redacted

    @pytest.mark.parametrize(
        "secret",
        [
            "ya29.a0ARrdaM-fake-google-access-token-shape",
            "1//0gfake-google-refresh-token-shape",
            "xoxb-fake-slack-token-shape-123456",
            "ghp_fakegithubtokenshape1234567890abcd",
        ],
    )
    def test_redacts_provider_prefixed_tokens(self, secret):
        text = f"upstream call failed: {secret}"
        assert secret not in redact_secrets(text)

    def test_redacts_multiple_secrets_in_one_message(self):
        text = "exchange failed: access_token=abcdefgh12345678 refresh_token=zyxwvuts87654321"
        redacted = redact_secrets(text)
        assert "abcdefgh12345678" not in redacted
        assert "zyxwvuts87654321" not in redacted

    def test_key_name_is_preserved_for_diagnostic_value(self):
        redacted = redact_secrets("access_token=abcdefgh12345678")
        assert redacted.startswith("access_token")


class TestSecretRedactingFormatter:
    def _record(self, msg, *args):
        return logging.LogRecord(
            name="privacyfence.test", level=logging.INFO, pathname=__file__, lineno=1,
            msg=msg, args=args, exc_info=None,
        )

    def test_redacts_a_secret_embedded_in_a_logged_exception(self):
        formatter = SecretRedactingFormatter("%(message)s")
        secret = "abcdefgh12345678"
        record = self._record("Tool call failed: refresh_token=%s", secret)
        formatted = formatter.format(record)
        assert secret not in formatted
        assert "[REDACTED]" in formatted

    def test_ordinary_log_lines_are_unaffected(self):
        formatter = SecretRedactingFormatter("%(message)s")
        record = self._record("Logging initialized -> /var/log/privacyfence.log")
        assert formatter.format(record) == "Logging initialized -> /var/log/privacyfence.log"
