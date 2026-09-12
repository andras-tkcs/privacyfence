"""Forward each audit-log entry to a centralized syslog server or a generic
HTTP/webhook log collector, for org mode.

Before this, org mode's audit log (docs/security-and-compliance.md's own,
formerly accurate, description) was "a local file on that server, not
forwarded anywhere (e.g. to a SIEM)" -- an organization that wanted its
audit trail to survive a compromise of the daemon's own host, or to feed an
existing SIEM/log pipeline, had nothing to plug into. This module is the
"centralized forwarding" half of SEC-23; ``audit_log.py``'s per-entry HMAC
hash chain is the other half (append-integrity for the local file itself).

Design:
  - **Best-effort, off the decision path.** ``audit_log.AuditLogger.record()``
    -- the local JSONL write -- is always the durable record of a decision;
    forwarding never blocks it and a forwarding failure never loses or
    delays a gate decision. ``AuditForwarder`` below runs one small
    background thread pulling off a bounded queue; a full queue or a send
    failure is logged at ``warning`` and that entry is dropped from the
    *forwarded* stream only -- it is already safely on disk locally, via the
    hash chain, regardless of whether this ever ships anywhere else.
  - **Two transports**, chosen per org via ``org_mode.AuditForwardingConfig``:
      - ``syslog``: RFC 5424 formatted messages, RFC 6587 octet-counting
        framing for TCP (so an embedded newline in the JSON body can never
        be mistaken for a message boundary). TLS is deliberately NOT
        implemented here -- the documented pattern (matching
        org-mode-setup-guide.md's own "a reverse proxy terminates TLS, this
        daemon speaks plaintext behind it" posture for HTTPS) is to point
        this at a local relay (stunnel, syslog-ng/rsyslog with a
        TLS-terminating listener bound to ``127.0.0.1``) rather than
        reimplementing TLS-over-syslog by hand for one Python module.
      - ``http``: a plain HTTPS POST of one JSON object per entry, with an
        optional bearer token read from an environment variable at send
        time (never stored in ``org_config.json`` -- see
        ``AuditForwardingConfig.http_bearer_token_env``'s own docstring).
        This is deliberately NOT a full OTLP SDK -- it's a generic
        JSON-over-HTTPS webhook, which is what most "OTLP-over-HTTP/JSON"
        log receivers and SIEM HTTP-intake endpoints (Splunk HEC, Datadog's
        Logs API, an Elastic ingest pipeline, a custom collector) actually
        expect on the wire. Point it at whichever of those your real
        collector exposes.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .org_mode import AuditForwardingConfig

logger = logging.getLogger(__name__)

Sender = Callable[[dict[str, Any]], None]

_QUEUE_MAXSIZE = 1000
_SEND_TIMEOUT_SECONDS = 5.0


class AuditForwarder:
    """Owns one background thread draining a bounded queue of audit-entry
    payloads into ``sender``. One instance per daemon process -- built once
    in ``daemon_main.run_app()`` when org mode's ``audit_forwarding`` config
    is enabled -- shared by every principal's ``AuditLogger`` (``record()``
    calls ``submit()``, never ``sender`` directly, so a slow or unreachable
    collector can never make a gated decision wait on it).
    """

    def __init__(self, sender: Sender, *, queue_size: int = _QUEUE_MAXSIZE) -> None:
        self._sender = sender
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=queue_size)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audit-forwarder", daemon=True)
        self._thread.start()

    def submit(self, payload: dict[str, Any]) -> None:
        """Non-blocking: a full queue means the collector can't keep up (or
        is down) -- drop this one entry from the forwarded stream and say
        so, rather than backing up memory or, worse, blocking the caller
        (audit_log.AuditLogger.record(), on the actual decision path)."""
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            logger.warning(
                "Audit forwarding queue full (%d entries) -- dropping one entry from the "
                "forwarded stream; it is still recorded (with its own hash-chain entry) in the "
                "local audit log.",
                self._queue.maxsize,
            )

    def _run(self) -> None:
        while True:
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            try:
                self._sender(payload)
            except Exception as exc:
                logger.warning(
                    "Audit forwarding failed (entry %s already recorded locally): %s",
                    payload.get("event_id", "?"), exc,
                )

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the background thread to exit once its queue drains, and
        wait up to ``timeout`` for it. The thread is itself a daemon thread
        (started with ``daemon=True``), so an unresponsive collector during
        shutdown can never hang process exit even if ``timeout`` elapses
        first -- this is a best-effort drain, not a guarantee every queued
        entry ships before returning."""
        self._stop_event.set()
        self._thread.join(timeout=timeout)


# ---------------------------------------------------------------------------- #
# syslog (RFC 5424 message format, RFC 6587 octet-counting TCP framing)
# ---------------------------------------------------------------------------- #

# facility=16 (local0, the conventional "custom application" facility),
# severity=6 (informational) -> 16*8 + 6.
_SYSLOG_PRI = 16 * 8 + 6

_MSGID_ALLOWED_EXTRA = "_-"


def _syslog_msgid(decision: str) -> str:
    """RFC 5424's MSGID is a single PRINTUSASCII token (no spaces) --
    ``decision`` values in this codebase (e.g. "auto_accepted",
    "denied_unattended") already satisfy that, but this strips anything
    that wouldn't, rather than trusting every current and future decision
    string to stay token-safe."""
    cleaned = "".join(ch for ch in decision if ch.isalnum() or ch in _MSGID_ALLOWED_EXTRA)
    return cleaned or "-"


def _syslog_message(payload: dict[str, Any]) -> bytes:
    hostname = (socket.gethostname() or "-").split(".")[0][:255] or "-"
    timestamp = payload.get("timestamp") or datetime.now(timezone.utc).isoformat()
    msgid = _syslog_msgid(str(payload.get("decision", "")))
    body = json.dumps(payload, sort_keys=True)
    header = f"<{_SYSLOG_PRI}>1 {timestamp} {hostname} privacyfence {os.getpid()} {msgid} -"
    return f"{header} {body}".encode("utf-8")


def _send_syslog_udp(host: str, port: int, message: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(_SEND_TIMEOUT_SECONDS)
        sock.sendto(message, (host, port))


def _send_syslog_tcp(host: str, port: int, message: bytes) -> None:
    # RFC 6587 octet-counting framing ("<octet-count> <syslog-msg>") --
    # unambiguous even if the JSON body happened to contain a literal
    # newline, unlike the non-transparent-framing (trailing "\n")
    # alternative the RFC also allows.
    framed = f"{len(message)} ".encode("ascii") + message
    with socket.create_connection((host, port), timeout=_SEND_TIMEOUT_SECONDS) as sock:
        sock.sendall(framed)


def make_syslog_sender(host: str, port: int, protocol: str) -> Sender:
    def send(payload: dict[str, Any]) -> None:
        message = _syslog_message(payload)
        if protocol == "udp":
            _send_syslog_udp(host, port, message)
        else:
            _send_syslog_tcp(host, port, message)

    return send


# ---------------------------------------------------------------------------- #
# Generic HTTPS JSON webhook
# ---------------------------------------------------------------------------- #

def make_http_sender(url: str, bearer_token_env: str) -> Sender:
    # Re-checked here, not just at org_mode.AuditForwardingConfig.
    # from_org_config()'s own parse-time validation -- this is the actual
    # network call site, and urllib.request.urlopen() will happily follow
    # whatever scheme a URL names (including "file://"), so this is also
    # what stops a build_sender() call made outside that config's own
    # validation (there is none today, but nothing prevents one later)
    # from ever reaching urlopen() with a non-https:// URL.
    if not url.startswith("https://"):
        raise ValueError(f'audit_forwarding.http.url must use https://, got {url!r}')

    def send(payload: dict[str, Any]) -> None:
        # Read at send time, not at sender-construction time -- picks up a
        # rotated token without a daemon restart, same as every other
        # env-var-sourced secret in this codebase.
        token = os.environ.get(bearer_token_env, "") if bearer_token_env else ""
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=_SEND_TIMEOUT_SECONDS) as response:  # nosec B310 -- url scheme checked above
            if response.status >= 300:
                raise RuntimeError(f"audit forwarding endpoint returned HTTP {response.status}")

    return send


def build_sender(config: "AuditForwardingConfig") -> Sender:
    """One ``Sender`` closure for ``config.kind`` -- raises ``ValueError``
    for a configuration that's structurally enabled but missing the field
    its own kind needs (validated here rather than in
    ``org_mode.AuditForwardingConfig.from_org_config`` since a *disabled*
    config is allowed to carry an incomplete section, e.g. mid-rollout)."""
    if config.kind == "syslog":
        if not config.syslog_host:
            raise ValueError('audit_forwarding.syslog.host is required when kind is "syslog"')
        return make_syslog_sender(config.syslog_host, config.syslog_port, config.syslog_protocol)
    if config.kind == "http":
        if not config.http_url:
            raise ValueError('audit_forwarding.http.url is required when kind is "http"')
        return make_http_sender(config.http_url, config.http_bearer_token_env)
    raise ValueError(f"Unknown audit_forwarding kind: {config.kind!r}")


__all__ = ["AuditForwarder", "Sender", "build_sender", "make_http_sender", "make_syslog_sender"]
