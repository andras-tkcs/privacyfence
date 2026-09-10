"""Release-workflow smoke test for the Ubuntu org-mode service (TST-16,
docs/security-remediation-plan.md Phase 3 item 3.11).

Every other org-mode test in this repo (tests/unit/web/test_server_org_
mode.py, test_org_mcp_e2e.py, test_org_session.py, ...) drives web/
server.py's ``build_app(org=...)`` directly -- a real ASGI app, sometimes a
real socket (test_mcp_daemon_contract.py's style), but always the *same
Python process* the test itself runs in, hand-assembling an ``OrgAuth``
from whatever pieces that one test cares about. None of that exercises the
one layer only a packaged, independently-running install actually goes
through: ``daemon_main.main()`` end to end -- CLI parsing, config
bootstrap, ``load_org_config()``'s fail-closed signature/mode checks,
``build_connectors()``, the instance lock, the *real* uvicorn thread bound
to a *real* socket in a *separate OS process* -- started, stopped, and
restarted the way systemd (docs/org-mode-setup-guide.md's own Step 7,
``privacyfence.service``) actually would, with a reverse proxy's Host/
X-Forwarded-* handling (the guide's Caddy config, §6) simulated at the
HTTP layer rather than assumed.

Covers, against one synthetic, Ed25519-signed ``org_config.json`` and a
real (loopback) mocked IdP (tests/integration/mock_idp.py) -- never a real
external IdP, real TLS certificate, or real reverse proxy binary:

  - **Strict startup**: a `mode: org` bundle that is malformed, unsigned, or
    missing its ``idp`` section makes the daemon refuse to start (nonzero
    exit, no listening socket) rather than silently degrading -- SEC-04/
    SEC-05's fail-closed posture, proven against the real process exit
    code, not just the ``ConfigurationError`` type.
  - **Reverse-proxy / Host handling**: a request carrying the issuer's own
    Host header (what Caddy's ``reverse_proxy`` actually sends) is served;
    an unexpected Host header is rejected -- the DNS-rebinding defense
    web/server.py's ``_HostAllowlistMiddleware`` implements, exercised over
    a real socket instead of an in-process ASGI transport.
  - **Route mounting**: org mode's own route set is reachable and local
    mode's ``/settings``/``/api/state/stream`` are not, on the real running
    service.
  - **Per-principal session creation**: a real browser ``/login`` round
    trip through the mocked IdP mints an isolated, per-principal session;
    a second principal's login neither collides with nor is disturbed by
    logging the first one out.
  - **MCP OAuth discovery**: DCR (``/register``), the authorization-code +
    PKCE dance (``/authorize`` -> mocked IdP -> ``/oauth/idp/callback``),
    ``/token``, and a real ``tools/list`` call against ``/mcp`` with the
    resulting bearer token -- and that the per-principal storage directory
    (``paths.user_dir()``) it causes to be created on disk is exactly the
    signed-in principal's own, proving the token really did resolve to
    that principal end to end, not just that the dance completed.
  - **Clean shutdown/restart**: SIGTERM (what ``systemctl stop`` actually
    sends) brings the process down promptly, and a second instance started
    right after against the same ``$HOME`` comes up cleanly -- the
    instance lock and any on-disk state left behind survive a stop/start
    cycle the way an admin running ``systemctl restart privacyfence``
    needs them to.

Deliberately NOT covered here (out of this item's scope, per this plan's
own "Notes on scope not in the source review"): a real systemd unit, a
real Caddy process, a real external IdP, or the packaged ``.deb``
specifically -- see "Why a --target install, not the .deb" below for why the
last of those isn't needed for what this test actually checks.

Why a --target install, not the .deb
-------------------------------------
``paths.data_dir()`` resolves to the repo root itself for an editable dev
checkout (see that module's own docstring) -- running ``daemon_main.main()``
that way would read/write this actual repository's own ``config/``/``org/``
directories and instance lock, not an isolated sandbox, and would collide
with any other daemon already running against this same checkout. A real
(non-editable) install is what flips ``paths._is_installed_package()`` to
True, which is what makes ``data_dir()`` resolve under ``$HOME/
.privacyfence`` instead -- and *that* is genuinely what
docs/org-mode-setup-guide.md's own Step 3 documents for a real Ubuntu
server (``pipx install privacyfence`` / ``pip install .``), not the
desktop-oriented ``.deb`` (autostart entry, app icons) scripts/
build_deb.sh produces. Building it fresh here (module-scoped, once per test
session) with ``pip install --no-deps --target <dir>`` (see the
``installed_privacyfence`` fixture's own comment for why a plain venv isn't
used instead) reuses every dependency already importable in the process
running this test and costs only the one small ``privacyfence`` package
build -- seconds, not the minutes a full PyInstaller bundle
(scripts/build_deb.sh's own job) would add.

Gating
------
Opt-in via ``PRIVACYFENCE_RUN_RELEASE_SMOKE_TESTS=1`` -- unset (the
default) skips this whole module instantly, so a contributor's ordinary
``pytest`` run (and tests.yml's own fast per-PR matrix) never pays this
module's setup cost. ``.github/workflows/build.yml``'s ``build-deb`` job
(the release workflow TST-16's own plan-table wording refers to) sets it.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]

if not os.environ.get("PRIVACYFENCE_RUN_RELEASE_SMOKE_TESTS"):
    pytest.skip(
        "release-workflow smoke test -- set PRIVACYFENCE_RUN_RELEASE_SMOKE_TESTS=1 to run "
        "(see this module's own docstring; .github/workflows/build.yml's build-deb job does this)",
        allow_module_level=True,
    )

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client) not installed -- pip install -e '.[test]'"
)
import httpx  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from tests.integration.mock_idp import MockIdp  # noqa: E402

pytestmark = pytest.mark.timeout(300)

ISSUER_HOST = "pf.example.internal"
ISSUER_URL = f"https://{ISSUER_HOST}"
CLAUDE_REDIRECT_URI = "https://claude.example/oauth/callback"


# ---------------------------------------------------------------------------- #
# A real, non-editable `privacyfence` install (see module docstring's "Why a
# --target install, not the .deb" section) -- built once and reused by every
# test below.
#
# `pip install --target <dir>` rather than a fresh venv: a nested venv's own
# --system-site-packages inherits from the *base* interpreter that ultimately
# created the whole chain (sys.base_prefix), not from whatever environment
# happens to be running this test right now -- on a contributor's machine
# (or in this repo's own sandboxed dev session) that's very often itself
# already a venv, several layers away from anything with this package's
# actual dependencies installed. Installing this checkout's `privacyfence`
# package (--no-deps: nothing else needs reinstalling) into an isolated
# target directory and prepending that directory to the child process's own
# PYTHONPATH sidesteps the whole nesting question: every dependency import
# (starlette, cryptography, mcp, ...) still resolves from *this* process's
# own already-working environment (however it was set up), while `import
# privacyfence` resolves to the freshly built, non-editable copy instead of
# this checkout's editable `src/` tree -- which is the one thing that
# actually matters here (see "Why a --target install, not the .deb" above:
# paths._is_installed_package() has to see a real install path).
# ---------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def installed_privacyfence(tmp_path_factory: pytest.TempPathFactory) -> str:
    site_dir = tmp_path_factory.mktemp("release-smoke-site") / "site-packages"
    # --force-reinstall: without it, pip sees this exact version already
    # "installed" (this checkout's own editable dev install, on sys.path
    # already) and silently skips copying anything into --target at all.
    # Build isolation stays on (no --no-build-isolation): setuptools_scm
    # (needed to resolve __version__ from git tags, [tool.setuptools_scm]
    # in pyproject.toml) is a build-time-only dependency that a plain `pip
    # install -e ".[dev]"` does not leave importable in this process's own
    # environment, only inside pip's own isolated build env for that
    # install -- so this needs a real (proxy-reachable) build env of its
    # own, not a borrowed one.
    install = subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--force-reinstall",
            "--target", str(site_dir), str(REPO_ROOT),
        ],
        check=False, capture_output=True, text=True,
    )
    assert install.returncode == 0, (
        f"pip install of {REPO_ROOT} into an isolated site-packages failed "
        f"(this is what a real `pip install privacyfence` on a fresh Ubuntu server does -- see "
        f"docs/org-mode-setup-guide.md Step 3):\n{install.stdout}\n{install.stderr}"
    )
    entry = site_dir / "privacyfence" / "daemon_main.py"
    assert entry.exists(), f"privacyfence package was not installed at {entry}"
    return str(site_dir)


# ---------------------------------------------------------------------------- #
# Synthetic org_config.json
# ---------------------------------------------------------------------------- #

def _signed_org_config(*, idp_issuer: str, port: int, with_idp: bool = True) -> dict:
    from privacyfence.org_bundle_signing import generate_keypair, sign_bundle

    cfg: dict = {
        "mode": "org",
        "server": {
            "issuer_url": ISSUER_URL,
            "bind_host": "127.0.0.1",
            "port": port,
            # The one thing standing in for docs/org-mode-setup-guide.md
            # §6's Caddy `reverse_proxy 127.0.0.1:8765` here -- this test's
            # own HTTP client connects from 127.0.0.1 too, so trusting it
            # is what makes the Host-header simulation below meaningful at
            # all (§10.2: never honored without this, in either mode).
            "trusted_proxies": ["127.0.0.1"],
        },
    }
    if with_idp:
        cfg["idp"] = {"issuer": idp_issuer, "client_id": "privacyfence-smoke", "client_secret": "smoke-test-secret"}
    private_key, _ = generate_keypair()
    return sign_bundle(cfg, private_key)


def _write_org_config(home: Path, config: dict | bytes) -> None:
    org_dir = home / ".privacyfence" / "org"
    org_dir.mkdir(parents=True, exist_ok=True)
    path = org_dir / "org_config.json"
    if isinstance(config, bytes):
        path.write_bytes(config)
    else:
        path.write_text(json.dumps(config), encoding="utf-8")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------- #
# Process lifecycle
# ---------------------------------------------------------------------------- #

@contextlib.contextmanager
def _daemon(installed_privacyfence: str, *, home: Path, extra_env: dict[str, str] | None = None):
    """Starts the real ``privacyfence.daemon_main:main`` entry point --
    ``python -m privacyfence.daemon_main`` is exactly what the
    ``privacyfence-app`` console script itself does, and what systemd would
    exec -- as its own OS process, ``$HOME`` pointed at an isolated sandbox,
    and always terminates it on the way out: SIGTERM first (see
    TestCleanShutdownAndRestart's own docstring for why that signal
    specifically), SIGKILL if it hasn't exited a few seconds later (a hung/
    deadlocked daemon under test must never leak a process past the test
    that started it)."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    # Shadows this checkout's own editable `src/` install with the real,
    # non-editable one the installed_privacyfence fixture built -- see that
    # fixture's own comment for why this has to be a PYTHONPATH prepend
    # rather than a fresh venv.
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = installed_privacyfence + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    # discover_idp() requires HTTPS for a real deployment (SEC-11) -- this
    # dev-only override is exactly what org_identity.py's own docstring
    # names it for: "local development against a plain-HTTP test IdP."
    env["PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP"] = "1"
    if extra_env:
        env.update(extra_env)
    log_path = home / "daemon.log"
    with open(log_path, "wb") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "privacyfence.daemon_main"],
            env=env, cwd=str(home), stdout=log_fh, stderr=subprocess.STDOUT,
        )
        try:
            yield proc, log_path
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _wait_until_ready(proc: subprocess.Popen, host: str, port: int, log_path: Path, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"daemon exited early (code {proc.returncode}) instead of starting -- log:\n"
                f"{log_path.read_text(errors='replace')}"
            )
        with contextlib.suppress(OSError):
            with socket.create_connection((host, port), timeout=0.5):
                return
        time.sleep(0.2)
    raise AssertionError(f"daemon never started listening on {host}:{port} -- log:\n{log_path.read_text(errors='replace')}")


# ---------------------------------------------------------------------------- #
# HTTP helpers -- the reverse-proxy simulation: every request is sent to the
# real loopback socket the daemon is bound to, but with the issuer's own
# Host header attached by hand (exactly what Caddy's `reverse_proxy` does
# for a real request, per docs/org-mode-setup-guide.md §6), and redirects
# are followed manually rather than automatically, since a `Location:
# https://pf.example.internal/...` the daemon issues has to be rewritten
# back onto this same loopback socket -- there is no DNS entry (and no TLS
# certificate) for that hostname in this test.
# ---------------------------------------------------------------------------- #

class LoopbackClient:
    def __init__(self, port: int, *, host_header: str = ISSUER_HOST) -> None:
        self.port = port
        self.host_header = host_header
        self.session = requests.Session()

    def request(self, method: str, path_and_query: str, *, host_header: str | None = None, **kwargs) -> requests.Response:
        url = f"http://127.0.0.1:{self.port}{path_and_query}"
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Host", self.host_header if host_header is None else host_header)
        return self.session.request(method, url, headers=headers, allow_redirects=False, timeout=10, **kwargs)

    def get(self, path_and_query: str, **kwargs) -> requests.Response:
        return self.request("GET", path_and_query, **kwargs)

    def post(self, path_and_query: str, **kwargs) -> requests.Response:
        return self.request("POST", path_and_query, **kwargs)


def _loopback_target(location: str) -> str:
    """A daemon-issued ``Location`` header, stripped to a bare path+query
    -- reissuing it against :class:`LoopbackClient` re-targets it at the
    real loopback socket regardless of whether the header was absolute
    (``https://pf.example.internal/...``) or already relative."""
    parsed = urlparse(location)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _extract_cookie(response: requests.Response, name: str) -> str:
    raw = response.headers.get("set-cookie", "")
    assert raw.startswith(f"{name}="), f"expected a {name!r} cookie in Set-Cookie, got: {raw!r}"
    return raw.split(";", 1)[0].split("=", 1)[1]


def _complete_browser_login(client: LoopbackClient, idp: MockIdp, *, sub: str, next_path: str = "") -> str:
    """Drives a full ``/login`` -> (real HTTP hop to the mocked IdP) ->
    ``/oauth/idp/login-callback`` round trip and returns the resulting
    session cookie value."""
    idp.enqueue_identity(sub=sub, email=f"{sub}@example.com", name=sub.title())
    query = f"?next={next_path}" if next_path else ""
    to_idp = client.get(f"/login{query}")
    assert to_idp.status_code == 302, to_idp.text
    at_idp = requests.get(to_idp.headers["location"], allow_redirects=False, timeout=10)
    assert at_idp.status_code == 302, at_idp.text
    back_at_daemon = client.get(_loopback_target(at_idp.headers["location"]))
    assert back_at_daemon.status_code == 302, back_at_daemon.text
    return _extract_cookie(back_at_daemon, "pf_org_session")


# ---------------------------------------------------------------------------- #
# Strict startup (SEC-04/SEC-05): a broken org-mode bundle must never start
# ---------------------------------------------------------------------------- #

class TestStrictStartup:
    def test_refuses_to_start_on_an_unsigned_org_mode_bundle(self, installed_privacyfence, tmp_path):
        # "mode": "org" with no signature at all -- load_org_config()'s own
        # documented refusal (org_bundle_signing.py's TOFU model is opt-in
        # for local mode, mandatory for org mode).
        home = tmp_path / "home"
        home.mkdir()
        _write_org_config(home, {
            "mode": "org",
            "server": {"issuer_url": ISSUER_URL, "bind_host": "127.0.0.1", "port": _free_port()},
            "idp": {"issuer": "https://idp.invalid", "client_id": "x", "client_secret": "y"},
        })
        with _daemon(installed_privacyfence, home=home) as (proc, log_path):
            assert proc.wait(timeout=20) != 0
            log = log_path.read_text(errors="replace")
        assert "signed" in log.lower(), log

    def test_refuses_to_start_on_malformed_json(self, installed_privacyfence, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        _write_org_config(home, b"{not valid json")
        with _daemon(installed_privacyfence, home=home) as (proc, log_path):
            assert proc.wait(timeout=20) != 0
            log = log_path.read_text(errors="replace")
        assert "not valid json" in log.lower(), log

    def test_refuses_to_start_without_an_idp_section(self, installed_privacyfence, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        port = _free_port()
        # idp_issuer is irrelevant here (with_idp=False leaves it unused) --
        # this bundle never gets far enough to look at it (see assertion below).
        _write_org_config(home, _signed_org_config(idp_issuer="unused", port=port, with_idp=False))
        with _daemon(installed_privacyfence, home=home) as (proc, log_path):
            assert proc.wait(timeout=20) != 0
            log = log_path.read_text(errors="replace")
        assert "idp" in log.lower(), log
        # Never even opened the listening socket -- a startup failure this
        # early must fail closed, not fail open onto an unauthenticated port.
        with pytest.raises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass


@pytest.fixture
def mock_idp():
    idp = MockIdp()
    yield idp
    idp.shutdown()


# ---------------------------------------------------------------------------- #
# One real, running org-mode service -- reverse-proxy/Host handling, route
# mounting, per-principal sessions, and MCP OAuth discovery are all facets
# of the same running process, so this class spins up exactly one (see its
# own _service fixture) and shares it across every test method rather than
# paying a fresh daemon-boot cost per assertion.
# ---------------------------------------------------------------------------- #

class TestRunningOrgModeService:
    @pytest.fixture(autouse=True)
    def _service(self, installed_privacyfence, tmp_path, mock_idp):
        home = tmp_path / "home"
        home.mkdir()
        port = _free_port()
        _write_org_config(home, _signed_org_config(idp_issuer=mock_idp.base_url, port=port))
        with _daemon(installed_privacyfence, home=home) as (proc, log_path):
            _wait_until_ready(proc, "127.0.0.1", port, log_path)
            self.proc = proc
            self.port = port
            self.home = home
            self.idp = mock_idp
            self.client = LoopbackClient(port)
            yield

    # -- Reverse-proxy / Host handling ------------------------------------ #

    def test_the_issuer_host_header_is_accepted(self):
        r = self.client.get("/login")
        assert r.status_code == 302

    def test_an_unexpected_host_header_is_rejected(self):
        # DNS-rebinding defense (_HostAllowlistMiddleware): a Host header
        # that is neither the configured issuer host nor the bind address
        # itself must never resolve as "this server", the way a malicious
        # page tricking a victim's browser into hitting this loopback port
        # with a forged Host header otherwise would.
        r = self.client.get("/login", host_header="evil.example")
        assert r.status_code == 400
        assert "invalid host" in r.text.lower()

    # -- Route mounting ---------------------------------------------------- #

    def test_org_mode_routes_are_mounted(self):
        assert self.client.get("/login").status_code == 302
        assert self.client.get("/.well-known/oauth-authorization-server").status_code == 200
        assert self.client.get("/approvals").status_code == 302  # not signed in yet -> redirect to /login

    def test_local_mode_only_routes_are_not_mounted(self):
        assert self.client.get("/settings").status_code == 404
        assert self.client.get("/api/state/stream").status_code == 404

    # -- Per-principal session creation ------------------------------------ #

    def test_browser_login_creates_a_working_session(self):
        cookie = _complete_browser_login(self.client, self.idp, sub="alice")
        r = self.client.get("/approvals", headers={"Cookie": f"pf_org_session={cookie}"})
        assert r.status_code == 200

    def test_an_unauthenticated_request_is_redirected_to_login(self):
        r = self.client.get("/approvals")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals"

    def test_two_principals_get_independent_sessions(self):
        cookie_a = _complete_browser_login(self.client, self.idp, sub="alice")
        cookie_b = _complete_browser_login(self.client, self.idp, sub="bob")
        assert cookie_a != cookie_b

        # Logging alice out must not touch bob's still-live session.
        logout = self.client.post("/logout", headers={"Cookie": f"pf_org_session={cookie_a}"})
        assert logout.status_code == 302

        assert self.client.get("/approvals", headers={"Cookie": f"pf_org_session={cookie_a}"}).status_code == 302
        assert self.client.get("/approvals", headers={"Cookie": f"pf_org_session={cookie_b}"}).status_code == 200

    # -- MCP OAuth discovery ------------------------------------------------ #

    async def test_dcr_authorize_token_and_a_real_tool_call_resolve_to_the_signed_in_principal(self):
        from privacyfence.paths import safe_principal_id

        registration = self.client.post("/register", json={
            "redirect_uris": [CLAUDE_REDIRECT_URI], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        })
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]

        from privacyfence import org_identity as oi

        verifier, challenge = oi.generate_pkce_pair()
        self.idp.enqueue_identity(sub="carol", email="carol@example.com", name="Carol")
        to_idp = self.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_REDIRECT_URI,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "claude-state",
        })
        assert to_idp.status_code == 302, to_idp.text
        at_idp = requests.get(to_idp.headers["location"], allow_redirects=False, timeout=10)
        assert at_idp.status_code == 302, at_idp.text
        back_at_daemon = self.client.get(_loopback_target(at_idp.headers["location"]))
        assert back_at_daemon.status_code == 302, back_at_daemon.text
        callback_qs = dict(parse_qsl(urlparse(back_at_daemon.headers["location"]).query))
        assert callback_qs["state"] == "claude-state"

        token_resp = self.client.post("/token", data={
            "grant_type": "authorization_code", "code": callback_qs["code"], "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": client_id, "code_verifier": verifier,
        })
        assert token_resp.status_code == 200, token_resp.text
        access_token = token_resp.json()["access_token"]

        # No principal directory exists yet -- ConnectorRegistry.get()
        # (and the per-principal settings.yaml it bootstraps) only runs
        # once something actually asks for that principal's tools.
        users_dir = self.home / ".privacyfence" / "users"
        assert not users_dir.exists() or not any(users_dir.iterdir())

        async with httpx.AsyncClient(headers={"Host": ISSUER_HOST, "Authorization": f"Bearer {access_token}"}) as hc:
            async with streamable_http_client(f"http://127.0.0.1:{self.port}/mcp", http_client=hc) as (r, w, _sid):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    # There are zero connectors in this synthetic config
                    # (no real Google/Slack/... credentials to authenticate
                    # with), so no real tool exists to call -- but
                    # routes_mcp.py's handle_call_tool() resolves and enters
                    # principal_scope() *before* it looks the tool name up
                    # (see that function's own comment), so even a call for
                    # a tool that doesn't exist still exercises the one
                    # thing this test actually cares about: does an MCP
                    # bearer token minted for "carol" really dispatch as
                    # "carol", not as some other/no principal at all. The
                    # "Unknown tool" result that comes back is expected,
                    # not a bug.
                    result = await session.call_tool("smoke_test_nonexistent_tool", {})
                    assert result.isError

        expected_dir = users_dir / safe_principal_id("carol")
        assert expected_dir.is_dir(), (
            f"expected {expected_dir} to exist after an MCP call authenticated as principal "
            f"'carol' -- got {sorted(p.name for p in users_dir.iterdir()) if users_dir.exists() else []}"
        )

    def test_mcp_is_unreachable_without_a_bearer_token(self):
        r = self.client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------- #
# Clean shutdown / restart
# ---------------------------------------------------------------------------- #

class TestCleanShutdownAndRestart:
    def test_sigterm_stops_it_and_a_fresh_instance_then_starts_cleanly(self, installed_privacyfence, tmp_path, mock_idp):
        home = tmp_path / "home"
        home.mkdir()
        port = _free_port()
        _write_org_config(home, _signed_org_config(idp_issuer=mock_idp.base_url, port=port))

        with _daemon(installed_privacyfence, home=home) as (proc, log_path):
            _wait_until_ready(proc, "127.0.0.1", port, log_path)
            # SIGTERM, not SIGINT: this is what `systemctl stop`/`docker
            # stop` actually send (privacyfence.service's default
            # KillSignal) -- Ctrl-C's SIGINT already has its own dedicated
            # handling in daemon_main.py (_wait_for_shutdown's docstring);
            # what an Ubuntu service deployment needs proven is this one.
            proc.terminate()
            exited = proc.wait(timeout=15)
            assert exited is not None  # process.wait() always returns a code; guards a hang above

        # The port is free again -- no lingering listener from the dead process.
        with pytest.raises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                pass

        # A second instance, same $HOME, starts cleanly right after -- the
        # instance lock (daemon_main._acquire_instance_lock) and whatever
        # state the first process left on disk don't get in its way.
        with _daemon(installed_privacyfence, home=home) as (proc2, log_path2):
            _wait_until_ready(proc2, "127.0.0.1", port, log_path2)
            client = LoopbackClient(port)
            assert client.get("/login").status_code == 302
