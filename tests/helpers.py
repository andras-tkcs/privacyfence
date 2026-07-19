"""Test-only helpers shared across the unit test suite."""
from __future__ import annotations

import json

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.auto_accept import ReviewContext
from privacyfence.connector import Connector, ToolSpec


def make_ctx(**overrides) -> ReviewContext:
    """Build a ReviewContext with sane defaults, override via kwargs."""
    defaults = dict(
        connector="gmail",
        tool="gmail_get_message",
        args={},
        raw_data=None,
        my_email="",
        session_created_ids=set(),
    )
    defaults.update(overrides)
    return ReviewContext(**defaults)


_DUMMY_BY_ANNOTATION = {"str": "stub", "int": 1, "bool": False, "float": 1.0}


def build_stub_args(spec: ToolSpec, overrides: dict | None = None) -> dict:
    """Build a minimal-but-plausible args dict for a tool from its ToolSpec,
    modeling what a connector method actually receives -- i.e. after
    ipc_server.py._call_connector() has already popped "reason" out (every
    gated/auto ToolSpec declares it as a required param on the MCP schema,
    but no connector method signature accepts it -- see gate.py's
    reason_scope docstring). ``reason`` is deliberately excluded here for
    the same reason: passing it to connector.call() directly, as this
    helper's only caller does, would raise a TypeError on every tool.

    Optional params use the spec's own default (the exact value production
    sends when a caller omits them); required params get a type-appropriate
    dummy. Some tools validate their args before touching the client/gate
    (e.g. "provide exactly one of X or Y", or "must be valid JSON") — pass
    ``overrides`` for those specific param names.
    """
    args = {}
    for p in spec.params:
        if p.name == "reason":
            continue
        args[p.name] = p.default if not p.required else _DUMMY_BY_ANNOTATION.get(p.annotation, "stub")
    args.update(overrides or {})
    return args


async def assert_all_tools_leave_an_audit_trail(
    connector: Connector,
    connector_module,
    monkeypatch,
    tmp_path,
    arg_overrides: dict[str, dict] | None = None,
) -> None:
    """Call every tool a connector declares and prove each one is either
    routed through gated_call (gate.py's own tests already prove that path
    always audits, on every decision branch) or writes its own audit-log
    entry directly. Fails loudly, naming every tool with no audit trail at
    all, rather than spot-checking a handful of tools per connector.
    """
    init_audit_logger(str(tmp_path))
    audit_file = tmp_path / f"{current_week()}.jsonl"
    arg_overrides = arg_overrides or {}

    gated_calls: list[dict] = []

    async def fake_gated_call(**kwargs):
        gated_calls.append(kwargs)
        return kwargs.get("filtered_data")

    # Some connectors (e.g. tasks.py) never import gated_call at all -- every
    # tool is unconditionally auto-approved -- so the attribute may not exist.
    monkeypatch.setattr(connector_module, "gated_call", fake_gated_call, raising=False)

    def audit_entries_for(tool_name: str) -> int:
        if not audit_file.exists():
            return 0
        count = 0
        for line in audit_file.read_text(encoding="utf-8").splitlines():
            if json.loads(line).get("tool") == tool_name:
                count += 1
        return count

    unaudited: list[str] = []
    for spec in connector.tool_specs():
        args = build_stub_args(spec, arg_overrides.get(spec.name))
        gated_before = len(gated_calls)
        audited_before = audit_entries_for(spec.name)

        try:
            await connector.call(spec.name, args)
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            unaudited.append(f"{spec.name} (raised {exc!r} with stub args {args!r})")
            continue

        was_gated = len(gated_calls) > gated_before and gated_calls[-1].get("tool") == spec.name
        was_audited = audit_entries_for(spec.name) > audited_before
        if not (was_gated or was_audited):
            unaudited.append(spec.name)

    assert unaudited == [], f"Tools with no audit trail: {unaudited}"


def assert_no_placeholder_fields(
    preview: dict, placeholders: tuple = ("", "(unknown)", None)
) -> None:
    """Assert a gated_call preview dict has no fallback/placeholder value, for
    a fixture that's supposed to be fully populated. Catches a _parse_*
    field mapping (or the connector code reading its output) silently
    degrading to a default -- see the confluence last_modified bug
    documented in tests/unit/connectors/test_confluence_connector.py --
    without needing to already know the bug exists.
    """
    blank = {k: v for k, v in preview.items() if v in placeholders}
    assert not blank, f"Preview fields fell back to a placeholder: {blank}"
