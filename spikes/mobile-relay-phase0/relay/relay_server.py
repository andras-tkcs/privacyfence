"""Throwaway relay skeleton for PrivacyFence's mobile-approval spike (issue #55, Phase 0).

Implements the "encrypted mailbox" half of the architecture in #55: a paired
mailbox stores at most one pending request at a time, with a short TTL, and
one decision per request ID. This is the ONLY component in the architecture
with any inbound-facing surface -- the Mac daemon and the phone are both
pure clients of it, and neither accepts inbound connections from the other.

What this deliberately does NOT do yet (later phases' scope, not cut
corners):
- No end-to-end encryption of the payload. The relay stores and forwards
  `payload` as an opaque JSON value without looking inside it; in a real
  deployment that value is ciphertext encrypted to the recipient's X25519
  key (Phase 1/2). Phase 0 proves the mailbox/pairing/wake plumbing around
  that opaque blob, not the crypto itself.
- No real pairing UX (QR code, revocation). `/pair` here is a bare HTTP call
  a throwaway PWA can hit directly -- Phase 2's job is turning this into an
  actual pairing flow.
- No daemon involvement. Nothing here is imported by or wired into
  `src/privacyfence` -- the issue's own phasing is explicit that Phase 0 has
  "no daemon involvement yet." `tests/test_roundtrip.py` in this spike plays
  the daemon's role with plain HTTP calls to prove the API contract.

Fail-closed invariants this server enforces (see issue #55's non-negotiable
requirement 1 and the "stale response" gap raised in the issue comments):
- A mailbox with no pending request, or a request past its TTL, is
  indistinguishable from "nothing to approve" to a caller -- never
  fabricates or infers an approval.
- Once a decision is accepted for a request ID, any further decision for
  that same ID -- from either channel, including a genuine near-simultaneous
  double-submit -- is rejected as a no-op, never applied. This is the actual
  correctness guarantee behind "first response wins"; see the issue comment
  thread for why the guard has to live here and not just in the UX layer.
- Wrong or missing pairing token for a mailbox ID is a flat 403, with no
  distinction drawn between "wrong token" and "no such mailbox" (avoids
  leaking mailbox existence to a guesser).
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TTL_SECONDS = 120
# How long a *decided* request is kept around after its decision, purely so
# the daemon's own poll (which may not be watching in real time) still has
# something to read. Separate from DEFAULT_REQUEST_TTL_SECONDS, which bounds
# how long an *undecided* request waits before it fails closed.
DECISION_RETENTION_SECONDS = 60
POLL_INTERVAL_SECONDS = 0.2
SWEEP_INTERVAL_SECONDS = 5


@dataclass
class PendingRequest:
    """One outstanding approval request sitting in a mailbox."""

    request_id: str
    payload: object
    created_at: float
    ttl_seconds: float
    decision: str | None = None
    decision_at: float | None = None
    # Opaque pass-through only -- the relay never generates, validates, or
    # interprets this. A caller that wants a decision authenticated end to
    # end (peer identity + replay protection -- see
    # src/privacyfence/mobile_relay_client.py's compute_auth_tag/
    # verify_auth_tag, added in that real daemon client after this spike)
    # sets it when posting a decision and checks it themselves when reading
    # one back.
    decision_auth: str = ""

    def is_expired(self, now: float) -> bool:
        return now - self.created_at > self.ttl_seconds


@dataclass
class Mailbox:
    mailbox_id: str
    token: str
    created_at: float
    pending: PendingRequest | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class MailboxStore:
    """In-memory mailbox registry. A real deployment would still keep this
    in memory (or Redis-alike with a short TTL) -- the whole point of the
    relay is that it never needs durable storage of plaintext content."""

    def __init__(self) -> None:
        self._mailboxes: dict[str, Mailbox] = {}
        self._store_lock = threading.Lock()

    def create(self) -> Mailbox:
        mailbox_id = secrets.token_urlsafe(16)
        token = secrets.token_urlsafe(32)
        mailbox = Mailbox(mailbox_id=mailbox_id, token=token, created_at=time.time())
        with self._store_lock:
            self._mailboxes[mailbox_id] = mailbox
        return mailbox

    def get_authenticated(self, mailbox_id: str, token: str) -> Mailbox | None:
        with self._store_lock:
            mailbox = self._mailboxes.get(mailbox_id)
        if mailbox is None or not secrets.compare_digest(mailbox.token, token):
            return None
        return mailbox

    def sweep_expired(self) -> None:
        now = time.time()
        with self._store_lock:
            mailboxes = list(self._mailboxes.values())
        for mailbox in mailboxes:
            with mailbox.lock:
                pending = mailbox.pending
                if pending is None:
                    continue
                if pending.decision is None and pending.is_expired(now):
                    logger.info(
                        "request %s in mailbox %s expired unanswered -- fail-closed, "
                        "never auto-approved",
                        pending.request_id,
                        mailbox.mailbox_id,
                    )
                    mailbox.pending = None
                elif (
                    pending.decision is not None
                    and pending.decision_at is not None
                    and now - pending.decision_at > DECISION_RETENTION_SECONDS
                ):
                    mailbox.pending = None


class RelayRequestHandler(BaseHTTPRequestHandler):
    store: MailboxStore  # set by make_server()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        logger.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, body: dict | None = None) -> None:
        payload = json.dumps(body or {}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _send_cors_headers(self) -> None:
        # The PWA is typically served from a different origin than the relay
        # API (a static host, or a different port during local dev) -- see
        # this spike's top-level README. CORS is an over-the-wire concern
        # about which *browsers* may call this API from, not a security
        # boundary the relay itself relies on: authentication is the
        # mailbox token, checked on every request regardless of origin.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib method name
        # Browsers preflight cross-origin POSTs with a JSON body.
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _authenticate(self, mailbox_id: str, query: dict[str, list[str]]) -> Mailbox | None:
        token = query.get("token", [""])[0]
        mailbox = self.store.get_authenticated(mailbox_id, token)
        if mailbox is None:
            self._send_json(403, {"error": "unknown mailbox or bad token"})
        return mailbox

    # --- Routing --- #

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]

        if parts == ["pair"]:
            self._handle_pair()
        elif len(parts) == 2 and parts[0] == "mailbox":
            self._handle_post_request(mailbox_id=parts[1], query=parse_qs(parsed.query))
        elif len(parts) == 3 and parts[0] == "mailbox" and parts[2] == "decision":
            self._handle_post_decision(mailbox_id=parts[1], query=parse_qs(parsed.query))
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)

        if len(parts) == 2 and parts[0] == "mailbox":
            self._handle_get_request(mailbox_id=parts[1], query=query)
        elif len(parts) == 3 and parts[0] == "mailbox" and parts[2] == "decision":
            self._handle_get_decision(mailbox_id=parts[1], query=query)
        else:
            self._send_json(404, {"error": "not found"})

    # --- Handlers --- #

    def _handle_pair(self) -> None:
        """Stand-in for real pairing (Phase 2): mint a mailbox + token pair.

        A real pairing flow carries these over a QR code shown on the Mac,
        scanned by the phone next to it -- not returned over the same
        connection a remote attacker could also reach. Fine for a spike
        proving the mailbox mechanics; not a substitute for Phase 2.
        """
        mailbox = self.store.create()
        self._send_json(201, {"mailbox_id": mailbox.mailbox_id, "token": mailbox.token})

    def _handle_post_request(self, mailbox_id: str, query: dict[str, list[str]]) -> None:
        """Daemon side: post a new pending approval into the mailbox."""
        mailbox = self._authenticate(mailbox_id, query)
        if mailbox is None:
            return

        body = self._read_json_body()
        request_id = body.get("request_id")
        if not request_id:
            self._send_json(400, {"error": "request_id is required"})
            return

        ttl_seconds = float(body.get("ttl_seconds", DEFAULT_REQUEST_TTL_SECONDS))
        pending = PendingRequest(
            request_id=request_id,
            payload=body.get("payload"),
            created_at=time.time(),
            ttl_seconds=ttl_seconds,
        )
        with mailbox.lock:
            mailbox.pending = pending
        self._send_json(201, {"request_id": request_id})

    def _handle_get_request(self, mailbox_id: str, query: dict[str, list[str]]) -> None:
        """Phone side: fetch (optionally long-polling for) the pending request."""
        mailbox = self._authenticate(mailbox_id, query)
        if mailbox is None:
            return

        wait_seconds = float(query.get("wait", ["0"])[0])
        deadline = time.time() + wait_seconds

        while True:
            with mailbox.lock:
                pending = mailbox.pending
                now = time.time()
                if pending is not None and pending.decision is None and pending.is_expired(now):
                    mailbox.pending = None
                    pending = None
                # A request the relay is only still holding onto so the
                # daemon can pick up its decision (see DECISION_RETENTION_SECONDS)
                # is not "pending" from the phone's point of view -- it must
                # never be re-surfaced as if it were a new approval to answer.
                if pending is not None and pending.decision is None:
                    self._send_json(
                        200,
                        {"request_id": pending.request_id, "payload": pending.payload},
                    )
                    return
            if time.time() >= deadline:
                self._send_json(204)
                return
            time.sleep(POLL_INTERVAL_SECONDS)

    def _handle_post_decision(self, mailbox_id: str, query: dict[str, list[str]]) -> None:
        """Phone side: submit a decision for a request ID.

        Idempotent: once a decision is recorded for a request ID, every
        later call for that same ID is rejected as a no-op regardless of
        which channel sent it or what decision it carries -- see the
        module docstring's fail-closed invariants.
        """
        mailbox = self._authenticate(mailbox_id, query)
        if mailbox is None:
            return

        body = self._read_json_body()
        request_id = body.get("request_id")
        decision = body.get("decision")
        if decision not in ("approved", "denied"):
            self._send_json(400, {"error": "decision must be 'approved' or 'denied'"})
            return

        with mailbox.lock:
            pending = mailbox.pending
            if pending is None or pending.request_id != request_id:
                self._send_json(410, {"error": "no such pending request (expired or unknown)"})
                return
            if pending.decision is not None:
                self._send_json(
                    409,
                    {
                        "error": "already decided",
                        "decision": pending.decision,
                    },
                )
                return
            pending.decision = decision
            pending.decision_at = time.time()
            pending.decision_auth = body.get("auth", "")

        self._send_json(200, {"request_id": request_id, "decision": decision})

    def _handle_get_decision(self, mailbox_id: str, query: dict[str, list[str]]) -> None:
        """Daemon side: poll for (optionally long-polling on) a decision."""
        mailbox = self._authenticate(mailbox_id, query)
        if mailbox is None:
            return

        request_id = query.get("request_id", [""])[0]
        wait_seconds = float(query.get("wait", ["0"])[0])
        deadline = time.time() + wait_seconds

        while True:
            with mailbox.lock:
                pending = mailbox.pending
                if pending is not None and pending.request_id == request_id:
                    if pending.decision is not None:
                        self._send_json(
                            200,
                            {
                                "request_id": request_id, "decision": pending.decision,
                                "auth": pending.decision_auth,
                            },
                        )
                        return
                elif pending is None:
                    # Either never existed under this ID, or expired unanswered.
                    # Fail-closed: report "no decision," never "approved."
                    pass
            if time.time() >= deadline:
                # 204 carries no body per HTTP semantics (clients are free to
                # discard one) -- absence of a decision is signaled by the
                # status code alone, never by a body a client might not see.
                self._send_json(204)
                return
            time.sleep(POLL_INTERVAL_SECONDS)


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    store = MailboxStore()

    def sweeper() -> None:
        while True:
            time.sleep(SWEEP_INTERVAL_SECONDS)
            store.sweep_expired()

    handler_cls = type("BoundRelayRequestHandler", (RelayRequestHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler_cls)
    threading.Thread(target=sweeper, daemon=True).start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = make_server(args.host, args.port)
    logger.info("relay spike listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
