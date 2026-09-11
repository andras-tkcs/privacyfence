"""The real default browser-launch path of run_browser_oauth() (docs/
automated-test-strategy-plan.md Phase 2.3 -- "the browser-launch
abstraction").

``open_browser`` is deliberately injectable (oauth_loopback.py's own
docstring, §16.2.7) so tests can simulate the provider round trip without a
real browser -- and every existing test of it
(tests/unit/test_oauth_loopback.py) does exactly that, always passing its
own ``open_browser`` stand-in. That leaves the actual default this
codebase ships -- ``opener is None`` falling through to a lazily-imported
``webbrowser.open`` -- never exercised by anything: production's real
entry point into the OS's own browser-launch mechanism (``open``/
``xdg-open``/``os.startfile`` under the hood, depending on platform) has no
test proving it's even reached with the right URL. This module closes that
one gap, still without opening a real browser: it monkeypatches
``webbrowser.open`` itself (the same module object oauth_loopback.py's
lazy ``import webbrowser`` resolves to) rather than injecting
``open_browser``, so the assertion is specifically "the default wiring
reaches the real stdlib entry point," not "some callable gets called."
"""
from __future__ import annotations

import socket
import webbrowser

import pytest
import requests

from privacyfence.oauth_loopback import run_browser_oauth

pytestmark = pytest.mark.platform

# Same reasoning as tests/unit/test_oauth_loopback.py's own session: a
# loopback redirect must never go through an HTTP(S)_PROXY a CI runner's
# environment happens to set.
_NO_PROXY_SESSION = requests.Session()
_NO_PROXY_SESSION.trust_env = False


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_default_opener_falls_through_to_webbrowser_open(monkeypatch):
    calls: list[str] = []

    def fake_open(url: str) -> bool:
        calls.append(url)
        # Simulate the provider round trip completing, the same way a real
        # browser would hit the loopback redirect_uri after consent --
        # captured["redirect_uri"]/state aren't known until build_authorize_url
        # runs, so this reads them back off the URL run_browser_oauth built.
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(url).query)
        state = query["state"][0]
        _NO_PROXY_SESSION.get(redirect_uri, params={"code": "auth-code-123", "state": state}, timeout=5)
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)

    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    def build_authorize_url(uri: str, state: str, code_challenge: str) -> str:
        return f"https://provider.example/authorize?state={state}&redirect_uri={uri}"

    exchanged: dict = {}

    def exchange(code: str, uri: str, verifier: str) -> dict:
        exchanged.update(code=code, redirect_uri=uri, verifier=verifier)
        return {"access_token": "tok-123"}

    # No open_browser= kwarg -- this is the point of the test: production's
    # real default, not an injected stand-in. A short timeout: fake_open()
    # completes the round trip synchronously before returning, so a real
    # wait here would only ever mean something is already broken.
    result = run_browser_oauth(build_authorize_url, exchange, port=port, timeout=10)

    assert calls, "webbrowser.open was never reached -- the default opener wiring is broken"
    assert calls[0].startswith("https://provider.example/authorize?state=")
    assert exchanged["code"] == "auth-code-123"
    assert result == {"access_token": "tok-123"}


def test_default_opener_surfaces_a_clear_error_when_webbrowser_cannot_open_anything(monkeypatch):
    """webbrowser.open() returns False (rather than raising) when it could
    not find any browser controller to hand the URL to -- e.g. a headless
    Linux box with no $BROWSER and no known GUI browser installed. The
    default path must turn that into OAuthLoopbackError, same as an
    injected opener returning False already does."""
    from privacyfence.oauth_loopback import OAuthLoopbackError

    monkeypatch.setattr(webbrowser, "open", lambda url: False)

    port = _free_port()
    with pytest.raises(OAuthLoopbackError, match="Could not open a browser"):
        run_browser_oauth(
            lambda uri, state, challenge: "https://provider.example/authorize",
            lambda code, uri, verifier: {}, port=port, timeout=2,
        )
