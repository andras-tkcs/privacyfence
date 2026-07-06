"""Race-condition tests for privacyfence.menu_bar.

rumps/AppKit is not thread-safe: every long-running flow (OAuth, the IPC
server's own thread) does its work off the main thread and is required to
hand the result back via ``PyObjCTools.AppHelper.callAfter`` before touching
the menu. These tests target two shipped bugs where that contract was
violated and the menu bar went stale until the app was restarted:

  * "Always Allow" rules added from the approval popup (which runs on the
    IPC server's thread) never appeared in the menu until restart, because
    only in-process menu edits triggered a rebuild.
  * "Authenticate…" for a connector completed successfully but the connector
    still showed as not connected until restart, because the result crossed
    threads without being marshaled onto the main thread first.

Rather than asserting on the final menu bitmap (fragile, and rumps has no
headless render target worth inspecting), these tests intercept
``AppHelper.callAfter`` itself: they prove that (a) the state-mutating
callback is *never* invoked directly on the background thread that produced
the result, and (b) once the callback is actually drained (simulating the
main run loop pumping it), the state update that the user was missing
without a restart does in fact happen.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from privacyfence import auto_accept, menu_bar


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(menu_bar, "_find_icon", lambda: None)
    monkeypatch.setattr(menu_bar, "load_org_config", lambda: {})
    monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: None)

    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")

    ipc_calls = []
    ipc_server = SimpleNamespace(set_connectors=lambda conns: ipc_calls.append(conns))

    instance = menu_bar.PrivacyFenceMenuBar(str(config_path), connectors=[], ipc_server=ipc_server)
    instance._ipc_calls = ipc_calls  # test-only hook
    return instance


class TestRunAsyncMarshaling:
    """_run_async is the mechanism every threaded flow in this module funnels
    through. If it ever regresses to invoking on_done directly on the worker
    thread, every one of the bugs described in the module docstring comes
    back at once.
    """

    def test_success_result_never_delivered_directly_on_worker_thread(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))

        done_calls = []
        work_thread = {}

        def work():
            work_thread["thread"] = threading.current_thread()
            return "alice@example.com"

        def done(ok, result):
            done_calls.append((ok, result))

        app._run_async(work, done)

        assert wait_until(lambda: recorded), "work() never handed its result to AppHelper.callAfter"
        # work() really did run off the calling thread...
        assert work_thread["thread"] is not threading.current_thread()
        # ...and `done` must not have been invoked yet: it may only run once
        # something actually pumps the main run loop's queued callback.
        assert done_calls == []

        func, args, kwargs = recorded[0]
        assert args == (True, "alice@example.com")

        func(*args, **kwargs)
        assert done_calls == [(True, "alice@example.com")]

    def test_exception_in_work_is_also_marshaled_not_raised_on_worker_thread(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))

        boom = ValueError("auth failed")

        def work():
            raise boom

        done_calls = []

        def done(ok, result):
            done_calls.append((ok, result))

        app._run_async(work, done)

        assert wait_until(lambda: recorded)
        assert done_calls == []
        func, args, kwargs = recorded[0]
        assert args == (False, boom)
        func(*args, **kwargs)
        assert done_calls == [(False, boom)]


class TestAuthenticateDoesNotRequireRestart:
    """Regression for: 'authenticate a service was not displayed in the
    menubar until restart'. Simulates a successful Google OAuth flow end to
    end through the two chained background hops (_authenticate_google's
    `done` calls `_refresh_connectors`, which itself runs a second
    background hop) and asserts the menu-visible state only ever changes
    once the main thread actually drains each callback -- and that it does
    change, without any extra manual rebuild.
    """

    def test_successful_auth_updates_connectors_only_after_callafter_drains(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))

        class FakeGmailClient:
            def __init__(self, client_config, token_file):
                pass

            def authorize_interactive(self):
                pass

            def check_connection(self):
                return "alice@example.com"

        monkeypatch.setitem(menu_bar._GOOGLE_CLIENTS, "gmail", FakeGmailClient)
        monkeypatch.setattr(
            menu_bar, "build_connectors", lambda cfg, org: [SimpleNamespace(name="gmail")]
        )

        org_config = {"google": {"client_id": "id", "client_secret": "secret"}}
        app._authenticate_google("gmail", org_config)

        # Hop 1: the OAuth flow finished on a background thread. Its result
        # must be sitting in the callAfter queue, not yet applied.
        assert wait_until(lambda: len(recorded) == 1)
        assert app._connectors == []  # still stale -- this is the "not shown" symptom pre-drain
        assert app._ipc_calls == []

        hop1_func, hop1_args, hop1_kwargs = recorded.pop(0)
        hop1_func(*hop1_args, **hop1_kwargs)  # simulate the main run loop draining it

        # That drained callback (`done`) calls _refresh_connectors(), which
        # kicks off a *second* background hop rather than mutating state
        # inline -- so the connectors list is still stale until this second
        # hop is also drained.
        assert wait_until(lambda: len(recorded) == 1)
        assert app._connectors == []

        hop2_func, hop2_args, hop2_kwargs = recorded.pop(0)
        hop2_func(*hop2_args, **hop2_kwargs)

        # Only now -- after both hops drained, exactly as a pumped run loop
        # would do automatically -- does the menu bar's connected state
        # reflect the finished authentication. No restart, no extra
        # explicit rebuild call from the test was needed.
        assert app._connectors == ["gmail"]
        assert app._ipc_calls == [[SimpleNamespace(name="gmail")]]

    def test_failed_auth_rebuilds_without_marking_connected(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))

        class FailingGmailClient:
            def __init__(self, client_config, token_file):
                pass

            def authorize_interactive(self):
                raise RuntimeError("user cancelled")

            def check_connection(self):
                raise AssertionError("should not be reached")

        monkeypatch.setitem(menu_bar._GOOGLE_CLIENTS, "gmail", FailingGmailClient)
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))

        app._authenticate_google("gmail", {"google": {"client_id": "id", "client_secret": "s"}})

        assert wait_until(lambda: len(recorded) == 1)
        assert rebuild_calls == []
        func, args, kwargs = recorded[0]
        assert args[0] is False
        func(*args, **kwargs)
        assert rebuild_calls == [1]
        assert app._connectors == []  # never marked connected


class TestRulesChangedMarshaling:
    """Regression for: 'always allow did not add the new rule to the menu
    ... until restart'. auto_accept.reload_rules() is called from the IPC
    server's own thread when a rule is created via Accept All. The listener
    it fires must schedule the rebuild through AppHelper.callAfter -- if a
    regression calls self._rebuild() directly instead, this test catches it
    because the rebuild would show up on the background thread before any
    drain happens.
    """

    def test_reload_from_background_thread_schedules_rebuild_but_does_not_run_it_inline(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            menu_bar.AppHelper,
            "callAfter",
            lambda f, *a, **k: recorded.append((f, a, k, threading.current_thread())),
        )
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(threading.current_thread()))

        bg_done = threading.Event()

        def ipc_server_thread_body():
            # Mirrors what happens inside gate.py's add_auto_accept_rule ->
            # reload_rules() after an Accept All confirmation, called from
            # whatever thread is running the IPC server's request handling.
            auto_accept.reload_rules({"gmail.read_message": [{"rule": "i_am_sender"}]})
            bg_done.set()

        t = threading.Thread(target=ipc_server_thread_body)
        t.start()
        t.join(timeout=2)
        assert bg_done.is_set()

        # The rule reload completed, but the menu must not have rebuilt yet
        # on that thread -- only *scheduled* to, via callAfter.
        assert rebuild_calls == []
        assert len(recorded) == 1
        func, args, kwargs, calling_thread = recorded[0]
        assert calling_thread is not threading.current_thread()

        func(*args, **kwargs)
        assert rebuild_calls == [threading.current_thread()]


class TestConcurrentAuthAndRuleChangeDoNotLoseUpdates:
    """Two independent background flows finishing close together (an
    Authenticate… completing and a rule being added via Accept All) each
    queue their own callAfter callback. Draining them in either order must
    not let one clobber the other's state -- the two update disjoint pieces
    of state ( _connectors / ipc_server vs the rules submenu), but they share
    the same _rebuild() plumbing, so a naive implementation could lose one
    rebuild if it coalesced callbacks incorrectly.
    """

    def test_both_updates_are_visible_regardless_of_drain_order(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(menu_bar, "build_connectors", lambda cfg, org: [SimpleNamespace(name="slack")])

        class FakeSlackAuth:
            def __call__(self, **kwargs):
                return {"team_name": "Acme"}

        monkeypatch.setattr(menu_bar, "slack_authorize_interactive", FakeSlackAuth())

        # Flow 1: successful Slack auth.
        app._authenticate_slack({"slack": {"client_id": "id", "client_secret": "s"}})
        assert wait_until(lambda: len(recorded) == 1)

        # Flow 2: a rule gets added from another thread while flow 1's
        # result is still sitting undrained in the queue.
        bg_done = threading.Event()

        def add_rule_from_bg_thread():
            auto_accept.reload_rules({"gmail.read_message": [{"rule": "i_am_sender"}]})
            bg_done.set()

        threading.Thread(target=add_rule_from_bg_thread).start()
        assert wait_until(lambda: bg_done.is_set())
        assert wait_until(lambda: len(recorded) == 2)

        # Drain in arrival order: auth's `done` first (spawns a second hop
        # via _refresh_connectors), then the rules-changed rebuild.
        auth_func, auth_args, auth_kwargs = recorded.pop(0)
        auth_func(*auth_args, **auth_kwargs)
        assert wait_until(lambda: len(recorded) == 2)  # refresh_connectors' own hop queued

        rules_func, rules_args, rules_kwargs = recorded.pop(0)
        rules_func(*rules_args, **rules_kwargs)

        refresh_func, refresh_args, refresh_kwargs = recorded.pop(0)
        refresh_func(*refresh_args, **refresh_kwargs)

        assert app._connectors == ["slack"]
        assert app._ipc_calls == [[SimpleNamespace(name="slack")]]
