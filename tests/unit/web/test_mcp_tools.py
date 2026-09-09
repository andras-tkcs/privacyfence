"""Tests for mcp_tools.py's ``ToolSpec`` -> MCP ``Tool``/``CallToolResult``
translation, plus TST-02's three specific coverage gaps (docs/security-
remediation-plan.md phase 1.9): ``privacyfence_begin_unattended_session``
refused when disabled, ``privacyfence_propose_auto_accept_rule_change``
denied inside an unattended session, and ``privacyfence_list_auto_accept_
rules``' disclosure being audited.

Split by layer, same as the existing web/ test suite:
``TestToolSchema``/``TestCallToolResult`` exercise mcp_tools.py's own pure
functions directly -- no dispatcher, no wire protocol, since nothing else in
the suite gave that translation layer its own test module before this.
``TestUnattendedSessionBehavior`` drives the real ``/mcp`` ASGI app with the
official ``mcp`` client end to end (test_routes_mcp.py's own pattern) so the
three behaviors above are proven the way a real MCP client would actually
observe them, not just at whichever internal layer happens to implement
them -- test_mcp_dispatch.py already covers the same branches at the
McpDispatcher-unit level in more detail, including the regression this phase
uncovered (McpDispatcher.propose_rule_change never wrapped its call in
unattended_scope(), so an unattended session's own rule-change proposal fell
through to a real, never-to-be-answered confirmation popup instead of the
immediate denial its own tool description promises -- see that fix's
comment in web/mcp_dispatch.py).
"""
from __future__ import annotations

import contextlib
import json

import httpx
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from privacyfence import auto_accept
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.web import mcp_tools
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.routes_mcp import build_mcp_asgi_app, mcp_lifespan


# --------------------------------------------------------------------------- #
# Tool schema assertions -- ToolSpec -> JSON Schema / mcp.types.Tool
# --------------------------------------------------------------------------- #

class TestToolInputSchema:
    def test_maps_known_annotations_to_json_schema_types(self):
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("a", "str", required=True),
            ToolParam("b", "int", required=True),
            ToolParam("c", "bool", required=True),
            ToolParam("d", "float", required=True),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["properties"]["a"]["type"] == "string"
        assert schema["properties"]["b"]["type"] == "integer"
        assert schema["properties"]["c"]["type"] == "boolean"
        assert schema["properties"]["d"]["type"] == "number"

    def test_unknown_annotation_falls_back_to_string(self):
        # Mirrors tools.ts's own `case "str": default:` fallthrough -- see
        # _param_schema's own comment.
        spec = ToolSpec(name="t", description="d", params=[ToolParam("x", "list", required=True)])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["properties"]["x"]["type"] == "string"

    def test_required_params_are_listed_optional_ones_are_not(self):
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("required_one", "str", required=True),
            ToolParam("optional_one", "str", required=False),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["required"] == ["required_one"]

    def test_no_required_key_at_all_when_every_param_is_optional(self):
        spec = ToolSpec(name="t", description="d", params=[ToolParam("x", "str", required=False)])
        schema = mcp_tools.tool_input_schema(spec)
        assert "required" not in schema

    def test_optional_params_default_only_carries_forward_when_non_none(self):
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("with_default", "str", required=False, default="fallback"),
            ToolParam("without_default", "str", required=False, default=None),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["properties"]["with_default"]["default"] == "fallback"
        assert "default" not in schema["properties"]["without_default"]

    def test_a_required_params_own_default_is_never_surfaced(self):
        # `default` is informational for the client and only makes sense
        # for a param the caller may omit -- see tool_input_schema's own
        # docstring.
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("x", "str", required=True, default="ignored"),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert "default" not in schema["properties"]["x"]

    def test_description_is_carried_when_present_and_omitted_when_blank(self):
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("documented", "str", required=True, description="explains itself"),
            ToolParam("undocumented", "str", required=True, description=""),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["properties"]["documented"]["description"] == "explains itself"
        assert "description" not in schema["properties"]["undocumented"]

    def test_no_params_yields_an_empty_object_schema(self):
        spec = ToolSpec(name="t", description="d", params=[])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema == {"type": "object", "properties": {}}


class TestToMcpTool:
    def test_carries_name_and_description_through_unchanged(self):
        spec = ToolSpec(name="gmail_send", description="Sends an email.")
        tool = mcp_tools.to_mcp_tool(spec)
        assert tool.name == "gmail_send"
        assert tool.description == "Sends an email."

    def test_every_tool_is_advertised_read_only_regardless_of_the_specs_own_flag(self):
        # §8.1: MCP tool annotations are a UI hint, not the real security
        # boundary -- gate.py enforces that server-side. A write tool
        # (read_only=False) must still be advertised uniformly read-only so
        # a client doesn't throw its own redundant confirmation in front of
        # gate.py's real one.
        read_tool = mcp_tools.to_mcp_tool(ToolSpec(name="r", description="d", read_only=True))
        write_tool = mcp_tools.to_mcp_tool(ToolSpec(name="w", description="d", read_only=False))
        for tool in (read_tool, write_tool):
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.idempotentHint is True

    def test_input_schema_matches_tool_input_schema_directly(self):
        spec = ToolSpec(name="t", description="d", params=[ToolParam("x", "int", required=True)])
        tool = mcp_tools.to_mcp_tool(spec)
        assert tool.inputSchema == mcp_tools.tool_input_schema(spec)


class TestCallToolResult:
    def test_none_becomes_empty_content(self):
        result = mcp_tools.to_call_tool_result(None)
        assert result.content == []
        assert result.structuredContent is None

    def test_a_plain_string_becomes_text_content_with_no_structured_content(self):
        result = mcp_tools.to_call_tool_result("hello")
        assert len(result.content) == 1
        assert result.content[0].text == "hello"
        assert result.structuredContent is None

    def test_a_dict_becomes_both_text_and_structured_content(self):
        value = {"a": 1, "b": "two"}
        result = mcp_tools.to_call_tool_result(value)
        assert json.loads(result.content[0].text) == value
        assert result.structuredContent == value

    def test_a_non_dict_json_value_becomes_text_only_no_structured_content(self):
        result = mcp_tools.to_call_tool_result([1, 2, 3])
        assert json.loads(result.content[0].text) == [1, 2, 3]
        assert result.structuredContent is None

    def test_a_non_json_native_value_is_rendered_via_str_fallback(self):
        # json.dumps(..., default=str) -- anything that isn't natively
        # serializable (e.g. a set) still produces readable text instead of
        # raising.
        result = mcp_tools.to_call_tool_result({"weird": {1, 2}})
        assert "1" in result.content[0].text and "2" in result.content[0].text


def test_error_result_sets_is_error_and_carries_the_message():
    result = mcp_tools.error_result("boom")
    assert result.isError is True
    assert result.content[0].text == "boom"


# --------------------------------------------------------------------------- #
# Meta-tool manifest -- names, required "reason", and the frozenset kept in
# sync with the actual tuple (routes_mcp.py's list_tools/dispatch both trust
# these to agree).
# --------------------------------------------------------------------------- #

class TestMetaToolManifest:
    def test_meta_tool_names_frozenset_matches_the_meta_tools_tuple_exactly(self):
        assert mcp_tools.META_TOOL_NAMES == {t.name for t in mcp_tools.META_TOOLS}
        assert len(mcp_tools.META_TOOL_NAMES) == len(mcp_tools.META_TOOLS)

    @pytest.mark.parametrize(
        "tool", [t for t in mcp_tools.META_TOOLS if t.name != mcp_tools.AWAIT_APPROVAL_TOOL.name],
        ids=lambda t: t.name,
    )
    def test_every_meta_tool_that_acts_or_discloses_requires_a_reason(self, tool: types.Tool):
        # Every meta-tool's "reason" is logged in an audit entry with no
        # popup for a human to see it in first -- see each tool's own
        # description. Schema-enforced, not just documented.
        # privacyfence_await_approval is the one exception: it's a pure
        # status poll on approvals another gated call already created (and
        # already carries its own reason) -- there's no new action or
        # disclosure here for a reason to explain.
        assert "reason" in tool.inputSchema["properties"]
        assert "reason" in tool.inputSchema.get("required", [])

    def test_propose_rule_change_requires_target_and_operation_as_enums(self):
        schema = mcp_tools.PROPOSE_RULE_CHANGE_TOOL.inputSchema
        assert schema["properties"]["target"]["enum"] == ["rule", "grant"]
        assert schema["properties"]["operation"]["enum"] == ["add", "update", "remove"]
        assert set(schema["required"]) == {"target", "operation", "reason"}

    def test_check_policy_requires_connector_tool_and_reason(self):
        schema = mcp_tools.CHECK_POLICY_TOOL.inputSchema
        assert set(schema["required"]) == {"connector", "tool", "reason"}

    def test_await_approval_requires_only_approval_ids(self):
        schema = mcp_tools.AWAIT_APPROVAL_TOOL.inputSchema
        assert schema["required"] == ["approval_ids"]
        assert schema["properties"]["approval_ids"]["type"] == "array"


# --------------------------------------------------------------------------- #
# TST-02's three named behaviors, driven end to end over the real /mcp
# Streamable HTTP transport (mirrors test_routes_mcp.py's own fixtures).
# --------------------------------------------------------------------------- #

TOKEN = "mcp-tools-test-token"


@contextlib.asynccontextmanager
async def _connected_session(dispatcher: McpDispatcher, *, token: str = TOKEN):
    app, session_manager = build_mcp_asgi_app(dispatcher, token=token)
    transport = httpx.ASGITransport(app=app)
    async with mcp_lifespan(session_manager):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers={"Authorization": f"Bearer {token}"},
        ) as http_client:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=http_client,
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def _dispatcher(connectors: dict[str, Connector] | None = None, **kwargs) -> McpDispatcher:
    store = dict(connectors or {})
    return McpDispatcher(lambda: store, **kwargs)


def _read_audit_entries(audit_dir) -> list[dict]:
    week_file = audit_dir / f"{current_week()}.jsonl"
    if not week_file.exists():
        return []
    return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]


class TestBeginUnattendedSessionRefusedWhenDisabled:
    async def test_is_a_tool_error_naming_the_reason(self):
        dispatcher = _dispatcher({})  # unattended_sessions_enabled defaults False
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_begin_unattended_session", {"reason": "scheduled run"})
        assert result.isError is True
        assert "disabled" in result.content[0].text
        assert "organization config" in result.content[0].text

    async def test_never_actually_marks_the_session_unattended(self):
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher):
            pass
        assert dispatcher.unattended_session_count() == 0


class TestProposeRuleChangeDeniedWhenUnattended:
    """Regression coverage for the gap TST-02 exists to catch: see this
    fix's own comment on McpDispatcher.propose_rule_change in
    web/mcp_dispatch.py."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        from privacyfence import gate
        self._config_path = tmp_path / "settings.yaml"
        self._config_path.write_text("auto_accept_rules: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(self._config_path))
        self._popup_calls: list[str] = []
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description: self._popup_calls.append(description) or True,
        )

    async def test_denied_without_ever_showing_a_confirmation_popup(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        async with _connected_session(dispatcher) as session:
            await session.call_tool("privacyfence_begin_unattended_session", {"reason": "scheduled run"})
            result = await session.call_tool("privacyfence_propose_auto_accept_rule_change", {
                "target": "rule", "operation": "add", "reason": "trust this sender",
                "operation_key": "gmail.read_message", "rule_name": "i_am_sender",
            })
        assert result.isError is True
        assert "unattended session" in result.content[0].text
        assert self._popup_calls == []
        assert "i_am_sender" not in self._config_path.read_text(encoding="utf-8")

    async def test_still_confirms_normally_outside_an_unattended_session(self):
        # Same session, never marked unattended -- the fix must not turn
        # every proposal into a denial.
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_propose_auto_accept_rule_change", {
                "target": "rule", "operation": "add", "reason": "trust this sender",
                "operation_key": "gmail.read_message", "rule_name": "i_am_sender",
            })
        assert result.isError is False
        assert result.structuredContent["confirmed"] is True
        assert self._popup_calls == ["Add auto-accept rule 'i_am_sender' to 'gmail.read_message'"]


class TestListAutoAcceptRulesDisclosureIsAudited:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        init_audit_logger(str(tmp_path))
        self._audit_dir = tmp_path
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept_rules: {gmail.read_message: [{rule: i_am_sender}]}\n", encoding="utf-8")
        auto_accept.init_config_path(str(config_path))

    async def test_listing_the_rules_writes_an_audit_entry_naming_the_disclosure(self):
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool(
                "privacyfence_list_auto_accept_rules", {"reason": "checking before a scheduled run"},
            )
        assert result.isError is False
        assert result.structuredContent["auto_accept_rules"]["gmail.read_message"] == [{"rule": "i_am_sender"}]

        entries = _read_audit_entries(self._audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "rules_listed"
        assert entries[0]["claude_reason"] == "checking before a scheduled run"

    async def test_every_call_gets_its_own_audit_entry_not_deduped(self):
        # Disclosure of the current rule set must be logged every time it
        # happens, not silently coalesced by the dedupe cache that applies
        # to ordinary connector reads (list_rules is a meta-tool, dispatched
        # outside McpDispatcher.call()'s dedupe path entirely).
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            await session.call_tool("privacyfence_list_auto_accept_rules", {"reason": "first check"})
            await session.call_tool("privacyfence_list_auto_accept_rules", {"reason": "second check"})
        entries = _read_audit_entries(self._audit_dir)
        assert [e["claude_reason"] for e in entries] == ["first check", "second check"]
