"""Unit tests for privacyfence.audit_forwarding -- centralized
audit-log forwarding."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from privacyfence import audit_forwarding as af
from privacyfence import org_mode


class TestSyslogMessage:
    def test_includes_pri_version_and_json_body(self):
        message = af._syslog_message({
            "timestamp": "2026-01-01T00:00:00+00:00", "decision": "approved", "connector": "gmail",
        })
        assert message.startswith(b"<134>1 2026-01-01T00:00:00+00:00 ")
        assert b'"decision": "approved"' in message
        assert b'"connector": "gmail"' in message

    def test_uses_current_time_when_timestamp_missing(self):
        message = af._syslog_message({"decision": "approved"})
        assert b"<134>1 " in message

    def test_msgid_is_the_decision_value(self):
        message = af._syslog_message({"decision": "auto_accepted"})
        assert b" auto_accepted - " in message

    def test_msgid_falls_back_to_dash_when_decision_missing(self):
        message = af._syslog_message({})
        assert b" - - " in message


class TestSyslogMsgid:
    def test_passes_through_a_clean_token(self):
        assert af._syslog_msgid("auto_accepted") == "auto_accepted"

    def test_strips_disallowed_characters(self):
        assert af._syslog_msgid("weird value!!") == "weirdvalue"

    def test_empty_string_becomes_dash(self):
        assert af._syslog_msgid("") == "-"

    def test_all_disallowed_characters_becomes_dash(self):
        assert af._syslog_msgid("!!! ???") == "-"


class TestMakeSyslogSenderUdp:
    def test_sends_a_udp_datagram(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        srv.settimeout(3)
        port = srv.getsockname()[1]
        try:
            sender = af.make_syslog_sender("127.0.0.1", port, "udp")
            sender({"decision": "approved", "timestamp": "2026-01-01T00:00:00Z"})
            data, _ = srv.recvfrom(65536)
            assert b"approved" in data
        finally:
            srv.close()


class TestMakeSyslogSenderTcp:
    def test_sends_octet_counted_frame(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(3)
        port = srv.getsockname()[1]
        received = []

        def accept():
            conn, _ = srv.accept()
            received.append(conn.recv(65536))
            conn.close()

        t = threading.Thread(target=accept)
        t.start()
        try:
            sender = af.make_syslog_sender("127.0.0.1", port, "tcp")
            sender({"decision": "rejected"})
            t.join(timeout=3)
        finally:
            srv.close()

        raw = received[0]
        length_str, _, body = raw.partition(b" ")
        assert int(length_str) == len(body)
        assert b"rejected" in body


class TestMakeHttpSender:
    """Real TLS isn't exercised here (no cert to stand up in-process) --
    urllib.request.urlopen is monkeypatched to a fake that records what it
    was called with, same as any other outbound-HTTP test in this suite."""

    def test_rejects_non_https_url(self):
        with pytest.raises(ValueError, match="https"):
            af.make_http_sender("http://siem.example.com/ingest", "")

    def test_accepts_https_url(self):
        sender = af.make_http_sender("https://siem.example.com/ingest", "")
        assert callable(sender)

    def test_posts_json_body(self, monkeypatch):
        captured = {}

        class _FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = request.data
            captured["headers"] = dict(request.header_items())
            return _FakeResponse()

        monkeypatch.setattr(af.urllib.request, "urlopen", fake_urlopen)
        sender = af.make_http_sender("https://siem.example.com/ingest", "")
        sender({"decision": "approved"})

        assert captured["url"] == "https://siem.example.com/ingest"
        assert captured["method"] == "POST"
        assert json.loads(captured["body"]) == {"decision": "approved"}
        assert "Authorization" not in captured["headers"]

    def test_sends_bearer_token_read_from_env_var_at_send_time(self, monkeypatch):
        captured = {}

        class _FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.header_items())
            return _FakeResponse()

        monkeypatch.setattr(af.urllib.request, "urlopen", fake_urlopen)
        sender = af.make_http_sender("https://siem.example.com/ingest", "TEST_AUDIT_TOKEN")

        monkeypatch.setenv("TEST_AUDIT_TOKEN", "secret123")
        sender({"decision": "approved"})

        assert captured["headers"]["Authorization"] == "Bearer secret123"

    def test_no_bearer_token_env_configured_omits_the_header(self, monkeypatch):
        captured = {}

        class _FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.header_items())
            return _FakeResponse()

        monkeypatch.setattr(af.urllib.request, "urlopen", fake_urlopen)
        sender = af.make_http_sender("https://siem.example.com/ingest", "")
        sender({"decision": "approved"})

        assert "Authorization" not in captured["headers"]

    def test_non_2xx_3xx_response_raises(self, monkeypatch):
        class _FakeResponse:
            status = 500
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(af.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse())
        sender = af.make_http_sender("https://siem.example.com/ingest", "")
        with pytest.raises(RuntimeError, match="500"):
            sender({"decision": "approved"})


class TestBuildSender:
    def test_syslog_without_host_raises(self):
        config = org_mode.AuditForwardingConfig(enabled=True, kind="syslog", syslog_host="")
        with pytest.raises(ValueError, match="syslog.host"):
            af.build_sender(config)

    def test_syslog_with_host_builds_a_sender(self):
        config = org_mode.AuditForwardingConfig(enabled=True, kind="syslog", syslog_host="siem.example.com")
        assert callable(af.build_sender(config))

    def test_http_without_url_raises(self):
        config = org_mode.AuditForwardingConfig(enabled=True, kind="http", http_url="")
        with pytest.raises(ValueError, match="http.url"):
            af.build_sender(config)

    def test_http_with_url_builds_a_sender(self):
        config = org_mode.AuditForwardingConfig(enabled=True, kind="http", http_url="https://siem.example.com")
        assert callable(af.build_sender(config))


class TestAuditForwarder:
    @pytest.mark.timeout(5)  # TST-11: bounded by its own internal Event.wait(timeout=2.0), not the 30s suite default
    def test_submit_delivers_payload_to_sender(self):
        # TST-11: an Event
        # the worker thread itself sets, waited on with a generous timeout,
        # rather than polling calls in a fixed-interval loop for up to 1s --
        # this resolves the instant the sender actually runs instead of on
        # whichever of the 50 polls happens to land after it.
        calls = []
        delivered = threading.Event()

        def sender(payload):
            calls.append(payload)
            delivered.set()

        forwarder = af.AuditForwarder(sender)
        forwarder.submit({"event_id": "a"})
        assert delivered.wait(timeout=2.0), "sender was never called"
        forwarder.stop()
        assert calls == [{"event_id": "a"}]

    @pytest.mark.timeout(5)  # TST-11: bounded by its own internal Event.wait(timeout=2.0), not the 30s suite default
    def test_full_queue_drops_without_raising(self, caplog):
        # The "picked up immediately by the worker thread" assumption below
        # used to be a blind time.sleep(0.05) -- on a slow enough CI runner,
        # nothing guaranteed the worker had actually dequeued "a" (freeing
        # the size-1 queue's one slot) before "b" was submitted, which would
        # make "b" (not "c") the one silently dropped. picked_up is set from
        # inside the sender itself, the same synchronization point the old
        # sleep was only ever guessing at.
        picked_up = threading.Event()

        def slow_sender(payload):
            picked_up.set()
            time.sleep(1)

        forwarder = af.AuditForwarder(slow_sender, queue_size=1)
        forwarder.submit({"event_id": "a"})
        assert picked_up.wait(timeout=2.0), "worker never picked up the first entry"
        forwarder.submit({"event_id": "b"})  # fills the now-empty queue
        with caplog.at_level("WARNING"):
            forwarder.submit({"event_id": "c"})  # queue full -- dropped
        forwarder.stop(timeout=0.1)
        assert "queue full" in caplog.text

    @pytest.mark.timeout(5)  # TST-11: bounded by its own internal Event.wait(timeout=2.0), not the 30s suite default
    def test_sender_exception_is_caught_and_logged(self, monkeypatch, caplog):
        # Signals off logger.warning itself (via a spy), not off the sender
        # raising -- the sender's exception and the warning that logs it
        # happen in the same worker-thread call, but only the log call is
        # actually what this test asserts on, so that's the one point to
        # synchronize against.
        logged = threading.Event()
        original_warning = af.logger.warning

        def spy_warning(*args, **kwargs):
            original_warning(*args, **kwargs)
            logged.set()

        monkeypatch.setattr(af.logger, "warning", spy_warning)

        def failing_sender(payload):
            raise RuntimeError("boom")

        forwarder = af.AuditForwarder(failing_sender)
        with caplog.at_level("WARNING"):
            forwarder.submit({"event_id": "x"})
            assert logged.wait(timeout=2.0), "warning was never logged"
        forwarder.stop()
        assert "boom" in caplog.text

    def test_stop_joins_the_background_thread(self):
        forwarder = af.AuditForwarder(lambda payload: None)
        forwarder.stop()
        assert not forwarder._thread.is_alive()
