"""Tests for McpDispatcher -- the /mcp endpoint's connector-call dispatch
(web/mcp_dispatch.py). Ported test-for-test from the equivalent
IPCServer/_call_connector/_check_policy/_list_rules/_propose_rule_change/
unattended-session coverage in tests/unit/test_ipc_server.py, since
mcp_dispatch.py is itself a port of that logic onto a session key that isn't
id(writer) -- see that module's own docstring for why this is a separate
implementation rather than a shared refactor.

No socket, no ASGI, no MCP protocol framing here -- that's
tests/unit/web/test_routes_mcp.py's job. This file exercises McpDispatcher's
own methods directly, the same "hand-rolled client speaks the wire protocol
directly" vs. "test the dispatch logic itself" split test_ipc_server.py
already draws between itself and test_bridge_daemon_contract.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.auto_accept import init_auto_accept_evaluator
from privacyfence.connector import Connector, ToolSpec
from privacyfence.gate import is_unattended
from privacyfence.principal import Principal, principal_scope
from privacyfence.web.mcp_dispatch import McpDispatcher


class FakeConnector(Connector):
    def __init__(
        self, name: str, *, result=None, error: Exception | None = None, delay: float = 0.0, my_email: str = "",
    ):
        self._name = name
        self._result = result
        self._error = error
        self._delay = delay
        self.my_email = my_email
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return self._name

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name=f"{self._name}_tool", description="test read tool", read_only=True),
            ToolSpec(name=f"{self._name}_write_tool", description="test write tool", read_only=False),
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._result


class UnattendedAwareConnector(Connector):
    def __init__(self, name: str):
        self._name = name
        self.observed_unattended: list[bool] = []

    @property
    def name(self) -> str:
        return self._name

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=f"{self._name}_tool", description="t", read_only=True)]

    async def call(self, tool: str, args: dict) -> object:
        self.observed_unattended.append(is_unattended())
        return "ok"


def _dispatcher(connectors: dict[str, Connector] | None = None, **kwargs) -> McpDispatcher:
    store = dict(connectors or {})
    return McpDispatcher(lambda: store, **kwargs)


# --------------------------------------------------------------------------- #
# build_manifest / connectors
# --------------------------------------------------------------------------- #

def test_build_manifest_reflects_the_live_connector_set():
    connector = FakeConnector("gmail")
    dispatcher = _dispatcher({"gmail": connector})
    manifest = dispatcher.build_manifest()
    assert manifest["connectors"][0]["name"] == "gmail"
    assert {t["name"] for t in manifest["connectors"][0]["tools"]} == {"gmail_tool", "gmail_write_tool"}


def test_connectors_property_polls_the_provider_live():
    store: dict[str, Connector] = {}
    dispatcher = McpDispatcher(lambda: store)
    assert dispatcher.connectors == {}
    store["gmail"] = FakeConnector("gmail")
    assert list(dispatcher.connectors) == ["gmail"]


# --------------------------------------------------------------------------- #
# call() -- dedupe/staleness, ported from TestCallConnector in
# test_ipc_server.py
# --------------------------------------------------------------------------- #

class TestCall:
    async def test_calls_the_named_connector_tool(self):
        connector = FakeConnector("gmail", result={"ok": True})
        dispatcher = _dispatcher({"gmail": connector})
        result = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert result == {"ok": True}
        assert connector.calls == [("gmail_tool", {"x": 1})]

    async def test_reason_is_popped_before_reaching_the_connector(self):
        connector = FakeConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1, "reason": "because"})
        assert connector.calls == [("gmail_tool", {"x": 1})]

    async def test_unknown_connector_raises(self):
        dispatcher = _dispatcher({})
        with pytest.raises(ValueError, match="Unknown connector"):
            await dispatcher.call("s1", "nope", "nope_tool", {})

    async def test_concurrent_identical_calls_are_coalesced(self):
        connector = FakeConnector("gmail", result="r", delay=0.05)
        dispatcher = _dispatcher({"gmail": connector})
        results = await asyncio.gather(
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
        )
        assert results == ["r", "r"]
        assert len(connector.calls) == 1

    async def test_completed_read_result_is_reused_within_the_ttl(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_a_plain_write_tools_completed_result_is_still_reused_within_the_ttl(self):
        # Not in _DEDUPE_EXEMPT_TOOLS -- an ordinary write gets the same
        # completed-result reuse a read does (the mechanism this guards
        # against is a client-timeout retry double-firing the popup, not
        # "writes never dedupe"). See mcp_dispatch.py's module docstring
        # and _DEDUPE_EXEMPT_TOOLS.
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_write_tool", {"x": 1})
        await dispatcher.call("s1", "gmail", "gmail_write_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_dedupe_is_scoped_per_principal_not_shared_across_them(self):
        # P7, docs/https-connector-refactor-plan.md §9: McpDispatcher is one
        # shared instance for the whole process, so its dedupe cache has to
        # key on the current principal too -- otherwise a second principal
        # calling the exact same tool with the exact same arguments within
        # the dedupe TTL would be handed the FIRST principal's actual
        # result instead of getting its own call dispatched. Regression
        # test for exactly that bug (caught by
        # web/test_org_mcp_e2e.py's own end-to-end version of this).
        connector = FakeConnector("gmail", result="depends-on-caller")
        dispatcher = _dispatcher({"gmail": connector})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with principal_scope(Principal(id="bob")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        # Both principals' calls actually reached the connector -- neither
        # was served the other's cached result.
        assert len(connector.calls) == 2

    async def test_dedupe_still_applies_within_the_same_principal(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_a_pending_approval_result_is_never_cached_for_reuse(self):
        # P3 (docs/https-connector-refactor-plan.md §5.2 point 6): a gated
        # call that returned {"status": "approval_pending", ...} must be
        # re-runnable immediately -- Claude re-issuing the identical call
        # is exactly how it collects the real decision from gate.py's
        # ledger, and that re-issue has to actually reach the connector
        # again, not be handed the same stale pending blob back by this
        # dispatcher's own (pre-P3) completed-result cache.
        class OnceThenRealDataConnector(FakeConnector):
            def __init__(self):
                super().__init__("gmail")

            async def call(self, tool, args):
                self.calls.append((tool, args))
                if len(self.calls) == 1:
                    return {"status": "approval_pending", "approval_id": "a1"}
                return "the real data"

        connector = OnceThenRealDataConnector()
        dispatcher = _dispatcher({"gmail": connector})

        first = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        second = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})

        assert first == {"status": "approval_pending", "approval_id": "a1"}
        assert second == "the real data"
        assert len(connector.calls) == 2  # the re-issue actually ran the connector again

    async def test_an_exempt_write_tool_always_reruns_after_completion(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_create_label", {"name": "x"})
        await dispatcher.call("s1", "gmail", "gmail_create_label", {"name": "x"})
        assert len(connector.calls) == 2

    async def test_read_result_older_than_a_write_to_the_same_connector_is_stale(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        await dispatcher.call("s1", "gmail", "gmail_write_tool", {"y": 1})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 3  # read, write, read again (not reused)

    async def test_a_write_by_one_principal_does_not_stale_another_principals_cached_read(self):
        # P7: the same staleness check above, but the write and the
        # cached read belong to two different principals -- one user's
        # write to their own connector account has nothing to say about
        # whether another user's already-cached read of theirs is stale.
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with principal_scope(Principal(id="bob")):
            await dispatcher.call("s1", "gmail", "gmail_write_tool", {"y": 1})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        # alice's second read is a cache hit -- bob's write never touched
        # her own staleness key. alice's read + bob's write = 2 calls
        # reaching the connector, not 3.
        assert len(connector.calls) == 2

    async def test_error_from_original_call_propagates_to_a_concurrent_deduped_retry(self):
        # Concurrent in-flight coalescing (both fired before either
        # completes) -- ported from
        # test_ipc_server.py::test_error_from_original_call_propagates_to_deduped_retry.
        connector = FakeConnector("gmail", error=ValueError("boom"), delay=0.05)
        dispatcher = _dispatcher({"gmail": connector})
        results = await asyncio.gather(
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
            return_exceptions=True,
        )
        assert all(isinstance(r, ValueError) and str(r) == "boom" for r in results)
        assert len(connector.calls) == 1

    async def test_a_failed_call_completed_result_is_also_reused_within_the_ttl(self):
        # Sequential, not concurrent: the failed future is still "done", so
        # the same completed-result reuse a successful call gets applies
        # here too (the exception propagates from the cached future).
        connector = FakeConnector("gmail", error=RuntimeError("boom"))
        dispatcher = _dispatcher({"gmail": connector})
        with pytest.raises(RuntimeError):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with pytest.raises(RuntimeError):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_different_args_are_not_deduped(self):
        connector = FakeConnector("gmail", result="ok")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 2})
        assert len(connector.calls) == 2

    async def test_dedupe_window_expires_after_ttl(self):
        connector = FakeConnector("gmail", result="ok")
        dispatcher = _dispatcher({"gmail": connector})
        dispatcher._DEDUPE_TTL_SECONDS = 0.05
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        await asyncio.sleep(0.1)
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 2

    async def test_unattended_scope_reflects_this_sessions_flag(self):
        connector = UnattendedAwareConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector}, unattended_sessions_enabled=True)
        await dispatcher.call("s1", "gmail", "gmail_tool", {})
        dispatcher.begin_unattended_session("s1", "scheduled run")
        await dispatcher.call("s1", "gmail", "gmail_tool", {"z": 1})
        assert connector.observed_unattended == [False, True]

    async def test_a_different_sessions_unattended_flag_does_not_leak(self):
        connector = UnattendedAwareConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector}, unattended_sessions_enabled=True)
        dispatcher.begin_unattended_session("s1", "scheduled run")
        await dispatcher.call("s2", "gmail", "gmail_tool", {})
        assert connector.observed_unattended == [False]


# --------------------------------------------------------------------------- #
# check_policy -- ported from TestCheckPolicyDispatch
# --------------------------------------------------------------------------- #

class TestCheckPolicy:
    @pytest.fixture(autouse=True)
    def _audit_dir(self, tmp_path):
        init_audit_logger(str(tmp_path))
        self._audit_dir = tmp_path

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    def test_auto_gated_tool_is_always_auto_accept(self):
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail")})
        result = dispatcher.check_policy("gmail", "gmail_list_messages", {})
        assert result == {
            "gate": "auto", "verdict": "auto_accept", "matched_rule": None,
            "reason": "Unconditionally auto-accepted -- never reaches the review gate.",
            "pii_gate_may_apply": False,
        }

    def test_never_calls_the_connector(self):
        connector = FakeConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector})
        dispatcher.check_policy("gmail", "gmail_get_message", {})
        assert connector.calls == []

    def test_popup_tool_matching_args_only_rule_is_auto_accept(self):
        init_auto_accept_evaluator({"gmail.create_draft": [{"rule": "to_is_myself"}]})
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail", my_email="me@example.com")})
        result = dispatcher.check_policy(
            "gmail", "gmail_create_draft", {"to": "me@example.com", "subject": "x", "body": "y"},
        )
        assert result["gate"] == "popup"
        assert result["verdict"] == "auto_accept"
        assert result["matched_rule"] == "to_is_myself"

    def test_unknown_tool_raises(self):
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail")})
        with pytest.raises(ValueError, match="Unknown tool"):
            dispatcher.check_policy("gmail", "not_a_real_tool", {})

    def test_unknown_connector_raises(self):
        dispatcher = _dispatcher({})
        with pytest.raises(ValueError, match="Unknown connector"):
            dispatcher.check_policy("nope", "gmail_get_message", {})

    def test_records_a_policy_check_audit_entry_not_a_real_decision(self):
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail")})
        dispatcher.check_policy("gmail", "gmail_list_messages", {}, "Planning ahead.")
        entries = self._read_entries()
        assert len(entries) == 1
        assert entries[0]["decision"] == "policy_check"
        assert entries[0]["claude_reason"] == "Planning ahead."


# --------------------------------------------------------------------------- #
# list_rules -- ported from TestListRulesDispatch
# --------------------------------------------------------------------------- #

class TestListRules:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from privacyfence import auto_accept
        init_audit_logger(str(tmp_path / "audit"))
        self._audit_dir = tmp_path / "audit"
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept_rules: {gmail.read_message: [{rule: i_am_sender}]}\n", encoding="utf-8")
        auto_accept.init_config_path(str(config_path))

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    def test_returns_the_persisted_rules(self):
        result = McpDispatcher.list_rules()
        assert result["auto_accept_rules"]["gmail.read_message"] == [{"rule": "i_am_sender"}]

    def test_records_a_rules_listed_audit_entry(self):
        McpDispatcher.list_rules("checking before a scheduled run")
        entries = self._read_entries()
        assert entries[0]["decision"] == "rules_listed"
        assert entries[0]["claude_reason"] == "checking before a scheduled run"


# --------------------------------------------------------------------------- #
# propose_rule_change -- ported from TestProposeRuleChangeDispatch
# --------------------------------------------------------------------------- #

class TestProposeRuleChange:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        from privacyfence import auto_accept, gate
        init_audit_logger(str(tmp_path / "audit"))
        self._config_path = tmp_path / "settings.yaml"
        self._config_path.write_text("auto_accept_rules: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(self._config_path))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)

    async def test_confirmed_rule_add_is_persisted_to_disk(self):
        dispatcher = _dispatcher({})
        result = await dispatcher.propose_rule_change({
            "target": "rule", "operation": "add", "reason": "Trusting example.com.",
            "operation_key": "gmail.read_message", "rule_name": "trusted_sender_domain",
            "value": ["example.com"],
        })
        assert result["confirmed"] is True
        assert "trusted_sender_domain" in self._config_path.read_text(encoding="utf-8")

    async def test_declined_confirmation_raises(self, monkeypatch):
        from privacyfence import gate
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: False)
        dispatcher = _dispatcher({})
        with pytest.raises(RuntimeError, match="denied"):
            await dispatcher.propose_rule_change({
                "target": "rule", "operation": "add", "reason": "x",
                "operation_key": "gmail.read_message", "rule_name": "i_am_sender",
            })


# --------------------------------------------------------------------------- #
# Unattended sessions -- ported from TestUnattendedSessionDispatch
# --------------------------------------------------------------------------- #

class TestUnattendedSessions:
    def test_disabled_by_default_raises(self):
        dispatcher = _dispatcher({})
        with pytest.raises(ValueError, match="disabled"):
            dispatcher.begin_unattended_session("s1", "why")

    def test_begin_then_end_clears_the_flag(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        assert dispatcher.begin_unattended_session("s1", "why") == {"unattended": True}
        assert dispatcher.unattended_session_count() == 1
        assert dispatcher.end_unattended_session("s1") == {"unattended": False}
        assert dispatcher.unattended_session_count() == 0

    def test_end_session_clears_an_unattended_flag_like_a_dropped_connection(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        dispatcher.begin_unattended_session("s1", "why")
        dispatcher.end_session("s1")
        assert dispatcher.unattended_session_count() == 0

    def test_end_session_on_a_session_that_was_never_unattended_is_a_no_op(self):
        dispatcher = _dispatcher({})
        dispatcher.end_session("s1")  # must not raise
        assert dispatcher.unattended_session_count() == 0

    def test_changed_listener_fires_on_begin_and_end(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        events = []
        dispatcher.set_unattended_changed_listener(lambda: events.append(1))
        dispatcher.begin_unattended_session("s1", "why")
        dispatcher.end_unattended_session("s1")
        assert len(events) == 2


class TestAwaitApproval:
    """privacyfence_await_approval's handler (P3, docs/https-connector-
    refactor-plan.md §5.2 point 7): long-poll the registry, status only."""

    async def test_no_registry_reports_every_id_as_unknown(self):
        dispatcher = _dispatcher({})  # registry=None, the default
        result = await dispatcher.await_approval(["a1", "a2"], timeout_seconds=1)
        assert result == {"a1": "unknown", "a2": "unknown"}

    async def test_empty_id_list_returns_immediately_with_no_status(self):
        dispatcher = _dispatcher({}, registry=PendingApprovalRegistry())
        result = await dispatcher.await_approval([], timeout_seconds=5)
        assert result == {}

    async def test_returns_as_soon_as_a_pending_approval_is_decided(self):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )

        async def _decide_soon():
            await asyncio.sleep(0.05)
            registry.finalize(approval.id, "accept")

        results = await asyncio.gather(
            dispatcher.await_approval([approval.id], timeout_seconds=10),
            _decide_soon(),
        )
        assert results[0] == {approval.id: "approved"}

    async def test_times_out_and_reports_pending_if_nobody_decides(self):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )

        result = await dispatcher.await_approval([approval.id], timeout_seconds=1)

        assert result == {approval.id: "pending"}

    async def test_timeout_is_clamped_into_a_sane_range(self):
        # Never actually waits the full (absurd) requested duration -- the
        # clamp, not the caller's number, decides how long this can block.
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        dispatcher._AWAIT_APPROVAL_MAX_TIMEOUT = 1  # keep the test itself fast
        dispatcher._AWAIT_APPROVAL_POLL_SECONDS = 0.05

        result = await asyncio.wait_for(
            dispatcher.await_approval([approval.id], timeout_seconds=999_999), timeout=2.0,
        )

        assert result == {approval.id: "pending"}

    async def test_mixed_ids_report_each_ones_own_status(self):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "deny")

        result = await dispatcher.await_approval([approval.id, "not-a-real-id"], timeout_seconds=1)

        assert result == {approval.id: "denied", "not-a-real-id": "unknown"}
