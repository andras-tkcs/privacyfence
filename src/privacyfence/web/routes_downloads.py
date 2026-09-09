"""``GET /downloads/{token}`` -- the browser-facing half of org-mode
download staging (docs/org-mode-download-delivery-plan.md, Phase 1). A
staged download's token (download_staging.DownloadStagingStore.stage's
return value, base64url-encoded into the URL) is a one-time bearer
credential for a single file: whoever holds the link and is currently
signed in as the principal it was staged for can claim it exactly once.

Auth: reuses ``org_session.authenticated`` exactly as every other org-mode
browser route does -- no new auth mechanism. The 401/redirect-to-/login
branch mirrors routes_connect.py's own pattern for a browser-navigated GET
(session cookie missing or expired sends the human to sign in, not a bare
401 JSON body, since this route is always reached by a human clicking or
pasting a link into their own browser tab, never by a JS fetch).

``{token}`` in the URL is the raw 256-bit token, not the store's internal
``lookup_id`` -- see download_staging.py's own module docstring for why
the two are different values and why only the token, never the
``lookup_id`` alone, can decrypt anything.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..download_staging import DownloadStagingStore, get_download_staging_store
from . import org_session
from .org_session import OrgSessionStore

logger = logging.getLogger(__name__)


def _decode_token(raw: str) -> bytes | None:
    try:
        # urlsafe_b64decode requires correct padding; the token is always
        # 32 raw bytes (44 base64url chars incl. padding) when it was
        # produced by DownloadStagingStore.stage(), but pad defensively
        # rather than 500ing on a hand-edited or truncated URL.
        padded = raw + "=" * (-len(raw) % 4)
        token = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return None
    return token if len(token) == 32 else None


def _audit_staged_download_served(principal_id: str, name: str, size_bytes: int) -> None:
    """This is the moment file content actually left the server for a
    staged-link download -- unlike inline delivery (already covered by the
    gated_call that generated the link), nothing else records that this
    happened at all. Un-gated: this route has no popup/review decision of
    its own to attach to, the original download tool call already went
    through gate.py's normal gated_call audit trail. Never logs the token
    itself -- see download_staging.py's own module docstring on why that
    value must never be persisted anywhere. Wrapped in try/except and
    never allowed to block the response, same posture as gate.py's own
    _audit and daemon_main.log_org_config_bundle_hash."""
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id="",
            connector="",
            tool="",
            tool_name="",
            summary=f"Staged download claimed: {name!r} ({size_bytes} bytes)",
            sender=principal_id,
            decision="staged_download_served",
            auto_accept_rule="",
            latency_seconds=0.0,
            pii_detected=False,
        ))
    except Exception as exc:  # noqa: BLE001 -- audit logging must never break the download
        logger.warning("Audit log write failed for staged download claim: %s", exc)


def build_routes(*, sessions: OrgSessionStore, store: DownloadStagingStore | None = None):
    from starlette.routing import Route

    async def claim(request: Request) -> Response:
        principal = org_session.authenticated(request, sessions)
        if principal is None:
            return RedirectResponse("/login", status_code=302, headers={"Cache-Control": "no-store"})

        # CSRF/cross-site GET note (module docstring): this is a
        # state-changing GET (it deletes the staged file on success), the
        # same shape of risk org_session.check_origin exists to cover for
        # other org-mode routes. Token unguessability (32 random bytes,
        # never logged, never persisted server-side) is the primary
        # defense either way -- this is belt-and-suspenders.
        if not org_session.check_origin(request):
            return PlainTextResponse("Cross-origin request rejected.", status_code=403)

        token = _decode_token(request.path_params["token"])
        if token is None:
            return PlainTextResponse("Not found.", status_code=404)

        registry = store or get_download_staging_store()
        result = registry.claim(token, principal.id)
        if result is None:
            # Missing, expired, wrong-principal, or already-claimed --
            # deliberately indistinguishable (download_staging.claim's own
            # docstring), so this endpoint never discloses which case
            # applies to an attacker guessing tokens.
            return PlainTextResponse("Not found.", status_code=404)

        data, name, mime_type = result
        _audit_staged_download_served(principal.id, name, len(data))
        # ASCII-only fallback filename plus a UTF-8 filename* per RFC 6266
        # -- the same reasoning gmail_client.py's/drive_client.py's own
        # attachment-name handling already applies to filesystem paths,
        # extended to a header value: a name isn't guaranteed to be
        # representable in the legacy `filename=` token.
        safe_name = name.encode("ascii", "replace").decode("ascii")
        quoted_utf8 = _rfc5987_quote(name)
        return Response(
            content=data,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{quoted_utf8}',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return [Route("/downloads/{token}", claim, methods=["GET"])]


def _rfc5987_quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


__all__ = ["build_routes"]
