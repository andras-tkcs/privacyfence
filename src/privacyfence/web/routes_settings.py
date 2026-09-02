"""Settings on the web (docs/https-connector-refactor-plan.md §16, W3/W4):
``GET /settings`` serves settings_window_html.build_html(), wrapped in
web_shell.wrap() so it reads as the same application as ``/approvals``;
``POST /api/settings/{action}`` is the mechanical two-thirds of
SettingsController's ~30 actions, dispatched through an **explicit
allowlist** (§16.2.5) rather than the native dispatcher's bare
``getattr(controller, action)`` -- see _ALLOWED_ACTIONS below for why a
frozenset here, not a decorator on the controller.

Everything that isn't "POST an action, get a fresh snapshot back" gets its
own route instead of being force-fit into that shape (§16.2.4): the org
config bundle is a multipart upload, not a JSON action (there is no
osascript "choose file" dialog to trigger from an HTTP request -- see
settings_controller.install_org_config_bytes's own docstring); the audit
log export is a file download, not a JSON response; ``quit_app`` gets its
own route so it can carry §16.2.8's confirmation + local-mode-only gate
without contaminating the generic dispatcher with one action's special
case.

**Standing rule this module exists to keep true (§16.2.4):** no route here
ever calls ``subprocess.run``/``os.system``/``open`` -- the four call sites
that used to (install_org_config's osascript picker, export_audit_log's
``open <file>``, the update-available alert's ``open <url>``,
settings_window.py's ``open_repo``) are each replaced by a route or a
client-side link/window.open, never a shell-out reachable from this
process's HTTP listener. TestNoSubprocessFromHttp in this module's test
file is what a security review gets to point at instead of re-reading this
comment.
"""
from __future__ import annotations

import inspect
import logging
import typing
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Route

from .. import approval_icons, settings_window_html, web_shell
from ..settings_controller import REPO_URL, SettingsController
from .session_auth import authenticated as _token_authenticated
from .session_auth import check_csrf as _csrf_matches
from .session_auth import check_origin as _origin_ok
from .session_auth import set_session_cookie as _set_session_cookie_on
from .session_auth import unauthorized_html as _unauthorized_response

logger = logging.getLogger(__name__)

# Max upload size for an organization config bundle -- generous for a JSON
# document that is, in practice, a handful of OAuth client IDs/secrets per
# service (org_config.json today runs well under 10KB), while still a real
# bound against something absurd arriving on this endpoint.
MAX_ORG_CONFIG_BYTES = 1_000_000

# ---------------------------------------------------------------------------- #
# §16.2.5's allowlist. A frozenset here, not a @web_action decorator on
# SettingsController -- that class's docstring is proud of having no web
# concerns ("No unguarded AppKit/WebKit imports at module level"), and a
# decorator naming an HTTP-shaped concept would be exactly that. This list
# is every "mechanical" action from §16.4's own table, plus skip_update/
# remind_later_update (§16.2.4's update banner). install_org_config,
# export_audit_log, and quit_app are deliberately absent -- each has its
# own route below instead of going through generic dispatch (see module
# docstring); install_org_config_bytes is never web-reachable by this name
# at all (only the multipart upload route calls it directly, with real
# bytes no JSON body could carry).
# ---------------------------------------------------------------------------- #

_ALLOWED_ACTIONS: frozenset[str] = frozenset({
    "toggle_pii_detection", "toggle_pii_category",
    "toggle_update_check", "toggle_update_check_beta", "check_for_updates_now",
    "skip_update", "remind_later_update",
    "toggle_connector", "refresh_connectors", "authenticate_connector",
    "telegram_start_auth", "telegram_submit_code", "telegram_submit_2fa", "telegram_cancel_auth",
    "update_rule_row", "add_rule_row", "remove_rule_row",
    "toggle_grant_capability", "add_grant_row", "update_grant_row", "remove_grant_row",
    "set_default_policy", "set_category_policy", "toggle_calendar_free_busy",
    "set_log_level",
})


class _BadAction(Exception):
    """Raised by _call_action for a wrong-typed/missing argument -- mapped
    to a 400 at the route, never a 500 (§16.7: "a wrong-typed argument
    returns 400 rather than raising")."""


def _coerce(value: Any, annotation: Any) -> Any:
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise _BadAction("expected an integer")
        try:
            return int(value)
        except ValueError as exc:
            raise _BadAction("expected an integer") from exc
    if annotation is str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise _BadAction("expected a string")
        return value
    if annotation is bool:
        return bool(value)
    return value


def _call_action(controller: SettingsController, action: str, payload: dict[str, Any]) -> Any:
    """Real per-action argument validation (§16.2.5's own "not a copy of
    the pyobjc workaround"): every parameter's type comes from
    SettingsController's own annotations (update_rule_row(op_key: str, idx:
    int, ...), etc.) rather than a single hardcoded "idx is always an int"
    special case -- a wrong type on *any* parameter of *any* allowed action
    is rejected the same way, not just the one the native dispatcher
    happened to guard."""
    method = getattr(controller, action)
    sig = inspect.signature(method)
    # settings_controller.py is `from __future__ import annotations`, so
    # Signature.parameters[name].annotation is the *string* "int"/"str",
    # not the type object -- get_type_hints() is what actually resolves
    # those against the function's own module globals, the way this
    # module's real per-action validation needs (§16.2.5: "not a copy of
    # the pyobjc workaround").
    hints = typing.get_type_hints(method)
    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name not in payload:
            if param.default is inspect.Parameter.empty:
                raise _BadAction(f"missing required argument: {name}")
            continue
        kwargs[name] = _coerce(payload[name], hints.get(name))
    return method(**kwargs)


def _augment_connectors_with_icons(state: dict[str, Any]) -> dict[str, Any]:
    """§16.2.6: the web equivalent of settings_window.py's
    _augment_connectors_with_icons, ported onto approval_icons.py (P1's
    PyObjC-free icon loader, already serving the approval card) instead of
    approval_window's AppKit-tainted private functions -- SettingsController
    itself stays free of any icon-loading concern either way."""
    for connector in state.get("connectors", []):
        icon_path = approval_icons.connector_icon_path(connector.get("icon", ""))
        connector["icon_data_uri"] = approval_icons.icon_data_uri(icon_path)
    return state


def _snapshot(controller: SettingsController) -> dict[str, Any]:
    return _augment_connectors_with_icons(controller.snapshot())


# ---------------------------------------------------------------------------- #
# The bridge shim -- swaps settings_window_html.py's own
# ``window.webkit.messageHandlers.pf.postMessage({action, ...payload})``
# for a ``fetch()`` POST to /api/settings/<action>, the same technique
# web/routes_approvals.py's own _bridge_shim already uses for the approval
# card (see that module's docstring). Four actions are intercepted here
# instead of forwarded, because none of them is "POST an action, get a
# snapshot back" (§16.2.4):
#   - open_repo: a plain link, opened client-side -- no request at all.
#   - install_org_config: triggers the hidden <input type=file> below,
#     which itself POSTs a multipart body to /api/settings/org_config/upload.
#   - export_audit_log: a same-origin navigation to the download route.
#   - quit_app: a client-side confirm() first (§16.2.8), then still POSTed
#     through as an action, but to its own /api/settings/quit_app route
#     (see build_routes below) rather than the generic dispatcher.
# ---------------------------------------------------------------------------- #

def _settings_bridge_shim(*, csrf: str, repo_url: str) -> str:
    return (
        "<input type=\"file\" id=\"pf-org-config-input\" accept=\".json,application/json\" style=\"display:none\">"
        "<script>(function(){"
        f"var CSRF = {csrf!r};"
        "var fileInput = document.getElementById('pf-org-config-input');"
        "fileInput.addEventListener('change', function(){"
        "  if (!fileInput.files || !fileInput.files[0]) return;"
        "  var fd = new FormData();"
        "  fd.append('file', fileInput.files[0]);"
        "  fd.append('csrf', CSRF);"
        "  fetch('/api/settings/org_config/upload', {method:'POST', credentials:'same-origin', body: fd})"
        "    .then(function(r){ return r.json(); })"
        "    .then(function(state){ if (window.__pfRender) { window.__pfRender(state); } })"
        "    .finally(function(){ fileInput.value = ''; });"
        "});"
        "window.webkit = window.webkit || {};"
        "window.webkit.messageHandlers = window.webkit.messageHandlers || {};"
        "window.webkit.messageHandlers.pf = {postMessage: function(payload) {"
        "  var action = payload.action; var rest = Object.assign({}, payload); delete rest.action;"
        f"  if (action === 'open_repo') {{ window.open({repo_url!r}, '_blank', 'noopener'); return; }}"
        "  if (action === 'install_org_config') { fileInput.click(); return; }"
        "  if (action === 'export_audit_log') { window.location = '/api/settings/audit_log/download'; return; }"
        "  var url = '/api/settings/' + encodeURIComponent(action);"
        "  if (action === 'quit_app') {"
        "    if (!window.confirm('Quit PrivacyFence? This stops the daemon, including every open approval and settings page.')) { return; }"
        "    rest.confirmed = true;"
        "  }"
        "  var body = Object.assign({}, rest, {csrf: CSRF});"
        "  fetch(url, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})"
        "    .then(function(r){ return r.json().then(function(state){ return {ok: r.ok, state: state}; }); })"
        "    .then(function(res){ if (res.ok && window.__pfRender) { window.__pfRender(res.state); } "
        "else if (!res.ok) { console.error('PrivacyFence action failed:', action, res.state); } });"
        "}};"
        "})();</script>"
    )


def build_routes(
    controller: SettingsController,
    *,
    token: str,
    allow_quit: bool = True,
) -> list[BaseRoute]:
    """The Route objects themselves, for server.py to fold into the one
    combined app (extra_routes, same pattern web/routes_mcp.py's
    mount_mcp() already established) -- see create_app() below for a
    standalone Starlette app wrapping the same routes, which is what this
    module's own tests construct against.

    A successful mutation's own snapshot is returned directly in this
    request's response, *and* reaches every other open tab via
    web/state_stream.py's StateStream.push_settings, wired as a
    controller.add_change_listener by web/server.py's WebServer -- see that
    class's own docstring. Nothing here needs to know about the stream at
    all; SettingsController's existing on_change/_push_snapshot mechanism
    already fires for every mutating call, this request's own included.
    """

    def _authenticated(request: Request) -> bool:
        return _token_authenticated(request, token)

    async def settings_page(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response()
        state = _snapshot(controller)
        body = settings_window_html.build_html(state)
        body += _settings_bridge_shim(csrf=token, repo_url=REPO_URL)
        html = web_shell.wrap(body, title="PrivacyFence — Settings", active="settings")
        response = HTMLResponse(html, headers={"Cache-Control": "no-store"})
        _set_session_cookie_on(response, token)
        return response

    def _check_mutation(request: Request, payload: Any) -> Response | None:
        if not isinstance(payload, dict) or not _csrf_matches(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _origin_ok(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        return None

    async def settings_action(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response()
        action = request.path_params["action"]
        # The allowlist check happens *before* anything resembling
        # getattr(controller, action) runs -- an unlisted name (including
        # dunders, _load_config, snapshot itself) is a 404, not a lookup
        # that then gets rejected (§16.2.5/§16.7's own required test).
        if action not in _ALLOWED_ACTIONS:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        rejected = _check_mutation(request, payload)
        if rejected is not None:
            return rejected
        body = {k: v for k, v in payload.items() if k != "csrf"}
        try:
            result = _call_action(controller, action, body)
        except _BadAction as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        state = result if isinstance(result, dict) else controller.snapshot()
        return JSONResponse(_augment_connectors_with_icons(state))

    async def quit_action(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response()
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        rejected = _check_mutation(request, payload)
        if rejected is not None:
            return rejected
        if not allow_quit:
            return JSONResponse({"error": "quitting PrivacyFence from the web is disabled"}, status_code=403)
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            # §16.2.8: behind an explicit confirmation -- the page's own
            # confirm() dialog sets this before it ever POSTs; a request
            # without it (a stray script, a replayed form) is refused
            # rather than treated as consent.
            return JSONResponse({"error": "confirmation required"}, status_code=400)
        controller.quit_app()
        return JSONResponse({"status": "quitting"})

    async def org_config_upload(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response()
        form = await request.form()
        if not _csrf_matches(request, form.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _origin_ok(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "missing file"}, status_code=400)
        raw = await upload.read()
        if len(raw) > MAX_ORG_CONFIG_BYTES:
            return JSONResponse({"error": "file too large"}, status_code=400)
        controller.install_org_config_bytes(raw)
        return JSONResponse(_snapshot(controller))

    async def audit_log_download(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized_response()
        xlsx_path = controller.export_audit_log_path()
        if xlsx_path is None:
            return JSONResponse({"error": controller.error or "No audit log to export yet."}, status_code=404)
        return FileResponse(
            xlsx_path,
            filename=Path(xlsx_path).name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Cache-Control": "no-store"},
        )

    return [
        Route("/settings", settings_page),
        Route("/api/settings/quit_app", quit_action, methods=["POST"]),
        Route("/api/settings/org_config/upload", org_config_upload, methods=["POST"]),
        Route("/api/settings/audit_log/download", audit_log_download),
        Route("/api/settings/{action}", settings_action, methods=["POST"]),
    ]


def create_app(controller: SettingsController, *, token: str, allow_quit: bool = True) -> Starlette:
    """Standalone Starlette app wrapping build_routes() -- what this
    module's own tests construct against, the same "no filesystem/global-
    singleton dependency" convention web/routes_approvals.py's create_app()
    already established."""
    return Starlette(routes=build_routes(controller, token=token, allow_quit=allow_quit))
