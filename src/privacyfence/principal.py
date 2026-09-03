"""Principal identity and per-request scoping (P6,
docs/https-connector-refactor-plan.md §9.1-§9.2).

``local`` mode has exactly one user, but the code from here on treats that
user as ``Principal(id="local")`` — a principal like any other — rather than
as an implicit absence of multi-tenancy. That is what makes org mode (P7+)
additive: nothing downstream of this module needs to learn a new concept
when a second principal shows up, it just starts seeing a second id.

This mirrors a pattern the codebase already had, twice, before this phase:
``gate.py``'s ``reason_scope``/``unattended_scope`` are ``contextvars`` set
once, centrally, by whichever surface is dispatching a call, so that call
sites deep in the policy engine never need a "which session is this"
parameter threaded through them. ``principal_scope`` is the same mechanism
for "which user is this."

Two entry points exist today, both wired to ``LOCAL_PRINCIPAL`` because
neither surface has real per-user identity yet (that's P7's OIDC/OAuth 2.1
authorization server, and P8's per-user service authorization) — see
web/mcp_auth.py's ``principal_from_access_token`` (the ``/mcp`` endpoint)
and web/server.py's ``_PrincipalScopeMiddleware`` (the browser surfaces).
Both already resolve through a real ``Principal``/``principal_scope`` call,
not a hardcoded shortcut, so P7 only has to change what those two functions
resolve to — nothing else in this module, or in any of the per-principal
registries built on top of it, needs to change.
"""
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

LOCAL_PRINCIPAL_ID = "local"
ANONYMOUS_PRINCIPAL_ID = "anonymous"


@dataclass(frozen=True)
class Principal:
    """An opaque, stable identity. ``id`` is the only field anything in this
    codebase keys storage or state on -- ``email``/``display_name`` are
    cosmetic (a label for a settings page or an audit entry), never a lookup
    key, and are empty for the ``local`` principal, exactly as today.

    ``is_admin`` (P7, docs/https-connector-refactor-plan.md §9.4: "Group/
    claim mapping decides who is an admin ... versus a plain user") is
    resolved once, at sign-in, from whatever the org's IdP claims say
    (org_identity.py's ``principal_from_claims``) -- never recomputed
    per-request, so a change to a user's group membership takes effect on
    their next login, not mid-session. Always ``False`` for the local
    principal and for ``ANONYMOUS_PRINCIPAL`` below; there is exactly one
    user in local mode, so "admin" has never meant anything there."""

    id: str
    email: str = ""
    display_name: str = ""
    is_admin: bool = False


LOCAL_PRINCIPAL = Principal(id=LOCAL_PRINCIPAL_ID)

# The principal an unauthenticated org-mode HTTP request is scoped to for
# the (brief) window before its own route's auth check runs and rejects it
# (P7, §9.1: principal_scope() is entered once per request, before any
# route-specific logic, including the auth check itself -- see
# web/server.py's _PrincipalScopeMiddleware). Deliberately not
# LOCAL_PRINCIPAL: an org deployment conflating "not yet authenticated"
# with "the local single-user principal" would be actively misleading if a
# future route ever forgot to auth-gate before touching per-principal
# state. Nothing should ever read or write state under this principal;
# it exists so current_principal() always resolves to *something* well-
# defined rather than raising or falling through to another user's data.
ANONYMOUS_PRINCIPAL = Principal(id=ANONYMOUS_PRINCIPAL_ID)

# Default is LOCAL_PRINCIPAL, not None: every call site written before this
# phase runs with no principal_scope() around it at all (daemon_main.py's
# startup sequence, every existing test, every connector method) and must
# keep behaving exactly as if there were exactly one user -- which is true
# by construction if "no scope entered" and "the local principal's scope"
# resolve to the same thing. See this phase's own exit criterion: "local
# mode byte-identical to before."
_principal_ctx: contextvars.ContextVar[Principal] = contextvars.ContextVar(
    "privacyfence_principal", default=LOCAL_PRINCIPAL
)


def current_principal() -> Principal:
    return _principal_ctx.get()


class principal_scope:  # noqa: N801 (context-manager-style name, like gate.py's reason_scope)
    """Run the wrapped code as ``principal`` -- every per-principal registry
    in this codebase (auto_accept.py, audit_log.py, pii_detector.py,
    privacy_filter.py, resource_names.py, connector_registry.py) resolves
    against whatever current_principal() returns at the moment it's asked,
    so entering this once around a request's whole dispatch (not around
    each individual call within it) is enough for everything downstream to
    see the right principal -- exactly how reason_scope/unattended_scope
    already work in gate.py."""

    def __init__(self, principal: Principal) -> None:
        self._principal = principal
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "principal_scope":
        self._token = _principal_ctx.set(self._principal)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _principal_ctx.reset(self._token)


T = TypeVar("T")


class PrincipalRegistry(Generic[T]):
    """Turns a module-level singleton into a per-principal one, behind the
    same accessor names the singleton already had (§9.2: "Each global ...
    becomes a per-principal registry behind its existing accessor name, so
    call sites do not change"). ``auto_accept.py``, ``audit_log.py``,
    ``pii_detector.py``, ``privacy_filter.py`` and ``resource_names.py``
    each hold one of these instead of a bare ``_INSTANCE`` (or, for
    ``pii_detector``/``privacy_filter``, a small private state object
    bundling what used to be several bare module globals).

    Not every per-process global becomes one of these -- ``approval_ui.py``
    stays a true singleton deliberately: it's the *implementation* of how
    approvals are shown (native popup vs. the web surface), not per-user
    data, and in org mode one ``WebApprovalUI`` instance still serves every
    principal (its ``PendingApprovalRegistry`` gains the principal dimension
    internally instead -- see approvals.py and docs/
    https-connector-refactor-plan.md §6's "New coalescing case").

    Thread-safe: ``get_audit_logger()`` in particular is reachable from more
    than one thread (the web server's event loop, background cache-warm
    threads), and the original module-level singleton used a lock for
    exactly that reason.
    """

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._instances: dict[str, T] = {}

    def get(self) -> T:
        """The value for ``current_principal()``, building it via
        ``factory()`` (itself run under the same principal, so a factory
        that needs to know who it's building for can call
        current_principal() too -- see e.g. audit_log.py's default log
        directory) the first time this principal is seen."""
        principal_id = current_principal().id
        with self._lock:
            instance = self._instances.get(principal_id)
            if instance is None:
                instance = self._factory()
                self._instances[principal_id] = instance
            return instance

    def set(self, instance: T) -> T:
        """Explicitly install ``instance`` for the current principal --
        mirrors each module's own ``init_xxx()``/``set_xxx()`` entry point
        (called once at daemon startup, or by a test), which always
        replaces whatever was there rather than only filling in a gap."""
        principal_id = current_principal().id
        with self._lock:
            self._instances[principal_id] = instance
        return instance

    def reset(self) -> None:
        """Test-only: drop every principal's instance. Called from
        tests/conftest.py's autouse fixture, the same role clearing a bare
        ``_INSTANCE = None`` played before this phase."""
        with self._lock:
            self._instances.clear()

    def discard(self, principal_id: str | None = None) -> None:
        """Evict one principal's cached instance -- the current principal's
        by default, or an explicit id (e.g. an idle-eviction sweep)."""
        with self._lock:
            self._instances.pop(principal_id or current_principal().id, None)


__all__ = [
    "ANONYMOUS_PRINCIPAL",
    "ANONYMOUS_PRINCIPAL_ID",
    "LOCAL_PRINCIPAL",
    "LOCAL_PRINCIPAL_ID",
    "Principal",
    "PrincipalRegistry",
    "current_principal",
    "principal_scope",
]
