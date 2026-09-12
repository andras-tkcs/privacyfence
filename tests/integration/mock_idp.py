"""A minimal, real OIDC identity provider double, listening on a real
loopback socket -- the "mocked IdP boundary" tests/integration/
test_org_ubuntu_release_smoke.py (TST-16) drives the daemon-under-test
against.

Deliberately a real HTTP server, not a monkeypatch of ``requests.get``/
``requests.post`` the way tests/unit/test_org_identity.py and
tests/unit/web/test_org_mcp_e2e.py both use: those tests exercise
org_identity.py's own logic in-process, but a release-workflow smoke test
(this module's one caller) is specifically about proving the *real*,
packaged daemon -- a separate OS process with no access to this process's
monkeypatches -- can actually complete OIDC discovery, an authorization-
code redirect dance, and JWKS-verified ID token validation against
something reachable only over the network, the same way a real IdP (Okta,
Entra ID, Google, Auth0, Keycloak -- see org_identity.py's own
``discover_idp`` docstring) would be.

Implements just enough of OIDC Discovery + Authorization Code + PKCE to
satisfy org_identity.py's own client-side implementation:

  - ``GET /.well-known/openid-configuration`` -- OIDC discovery document.
  - ``GET /jwks``                             -- this IdP's own signing key,
                                                   as a JWK Set.
  - ``GET /authorize``                        -- immediately "authenticates"
                                                   (no real login UI) and
                                                   redirects back with a
                                                   single-use code.
  - ``POST /token``                           -- redeems that code (after
                                                   checking its PKCE
                                                   challenge), returns a
                                                   freshly RS256-signed ID
                                                   token.

Every identity issued is explicit, not implicit: call ``enqueue_identity()``
before whatever ``/authorize`` hit should carry it (each call consumes one
queued identity, FIFO) -- there is no session/cookie/password concept here,
since login UI is exactly the part a real IdP already owns and this double
has no reason to reimplement.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class MockIdp:
    """One instance per test process is enough -- ``enqueue_identity()``
    lets each test scope its own claims per ``/authorize`` round trip
    without needing a fresh server."""

    def __init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._kid = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._pending_claims: list[dict] = []
        self._codes: dict[str, dict] = {}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mock-idp", daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def enqueue_identity(self, **claims: object) -> None:
        """Queues the OIDC claims (``sub`` at minimum) the *next*
        ``/authorize`` redirect should resolve to on redemption. Consumed
        FIFO, one identity per call -- lets a test drive two independent
        principals through the same daemon by enqueueing twice before its
        two separate ``/login`` (or MCP ``/authorize``) round trips."""
        with self._lock:
            self._pending_claims.append(dict(claims))

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def _next_identity(self) -> dict:
        with self._lock:
            if self._pending_claims:
                return self._pending_claims.pop(0)
        # No test-enqueued identity -- still a valid, distinct principal
        # rather than an error, so a test that doesn't care about identity
        # (only about the mechanics of the dance) doesn't have to enqueue
        # one first.
        return {"sub": f"walk-in-{uuid.uuid4().hex[:8]}", "email": "", "name": "Unqueued Test User"}

    def _jwks(self) -> dict:
        numbers = self._private_key.public_key().public_numbers()
        return {"keys": [{
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": self._kid,
            "n": _b64url_uint(numbers.n), "e": _b64url_uint(numbers.e),
        }]}

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:  # noqa: N802 -- silence per-request stderr spam
                pass

            def _send_json(self, payload: dict, *, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/.well-known/openid-configuration":
                    self._send_json({
                        "issuer": idp.base_url,
                        "authorization_endpoint": f"{idp.base_url}/authorize",
                        "token_endpoint": f"{idp.base_url}/token",
                        "jwks_uri": f"{idp.base_url}/jwks",
                    })
                    return
                if parsed.path == "/jwks":
                    self._send_json(idp._jwks())
                    return
                if parsed.path == "/authorize":
                    self._handle_authorize(parsed.query)
                    return
                self.send_response(404)
                self.end_headers()

            def _handle_authorize(self, query: str) -> None:
                qs = {k: v[0] for k, v in parse_qs(query).items()}
                redirect_uri = qs.get("redirect_uri", "")
                code = uuid.uuid4().hex
                # _next_identity() takes idp._lock itself -- must be called
                # before acquiring it here too (threading.Lock is not
                # reentrant; holding it across that call deadlocks this
                # request's handler thread forever).
                identity = idp._next_identity()
                with idp._lock:
                    idp._codes[code] = {
                        "claims": identity,
                        "nonce": qs.get("nonce", ""),
                        "client_id": qs.get("client_id", ""),
                        "code_challenge": qs.get("code_challenge", ""),
                    }
                location = f"{redirect_uri}?{urlencode({'code': code, 'state': qs.get('state', '')})}"
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/token":
                    self._handle_token()
                    return
                self.send_response(404)
                self.end_headers()

            def _handle_token(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
                with idp._lock:
                    record = idp._codes.pop(form.get("code", ""), None)
                if record is None:
                    self._send_json({"error": "invalid_grant"}, status=400)
                    return
                verifier = form.get("code_verifier", "")
                digest = hashlib.sha256(verifier.encode("ascii")).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
                if record["code_challenge"] and challenge != record["code_challenge"]:
                    self._send_json({"error": "invalid_grant", "error_description": "PKCE mismatch"}, status=400)
                    return
                now = int(time.time())
                claims = {
                    "iss": idp.base_url, "aud": record["client_id"], "sub": "unset",
                    "iat": now, "exp": now + 300, "nonce": record["nonce"],
                }
                claims.update(record["claims"])
                id_token = jwt.encode(claims, idp._private_key, algorithm="RS256", headers={"kid": idp._kid})
                self._send_json({
                    "id_token": id_token, "access_token": uuid.uuid4().hex,
                    "token_type": "Bearer", "expires_in": 300,
                })

        return Handler
