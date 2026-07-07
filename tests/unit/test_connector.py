"""Unit tests for privacyfence.connector — ToolSpec/ToolParam serialization.

The IPC manifest (ipc_server._build_manifest) sends ToolSpec.to_dict() over
the wire and bridge_main reconstructs specs with ToolSpec.from_dict(); a
round-trip mismatch here would silently break tool discovery for Claude.
"""
from __future__ import annotations

from privacyfence.connector import ToolParam, ToolSpec


class TestToolSpecRoundTrip:
    def test_to_dict_contains_all_fields(self):
        spec = ToolSpec(
            name="gmail_get_message",
            description="Fetch a message",
            params=[
                ToolParam("message_id", "str"),
                ToolParam("max_results", "int", required=False, default=10, description="cap"),
            ],
            read_only=True,
        )
        d = spec.to_dict()
        assert d == {
            "name": "gmail_get_message",
            "description": "Fetch a message",
            "params": [
                {"name": "message_id", "annotation": "str", "required": True, "default": None, "description": ""},
                {"name": "max_results", "annotation": "int", "required": False, "default": 10, "description": "cap"},
            ],
            "read_only": True,
        }

    def test_from_dict_reconstructs_equivalent_spec(self):
        original = ToolSpec(
            name="drive_move_file",
            description="Move a file",
            params=[ToolParam("file_id", "str"), ToolParam("folder_id", "str", required=False, default="root")],
            read_only=False,
        )
        rebuilt = ToolSpec.from_dict(original.to_dict())

        assert rebuilt.name == original.name
        assert rebuilt.description == original.description
        assert rebuilt.read_only == original.read_only
        assert len(rebuilt.params) == len(original.params)
        for a, b in zip(rebuilt.params, original.params):
            assert (a.name, a.annotation, a.required, a.default, a.description) == (
                b.name, b.annotation, b.required, b.default, b.description,
            )

    def test_from_dict_defaults_read_only_false_when_absent(self):
        spec = ToolSpec.from_dict({"name": "x", "description": "y", "params": []})
        assert spec.read_only is False

    def test_from_dict_defaults_param_description_when_absent(self):
        spec = ToolSpec.from_dict({
            "name": "x", "description": "y",
            "params": [{"name": "p", "annotation": "str", "required": True}],
        })
        assert spec.params[0].description == ""
        assert spec.params[0].default is None

    def test_no_params_round_trips_to_empty_list(self):
        spec = ToolSpec(name="x", description="y")
        d = spec.to_dict()
        assert d["params"] == []
        rebuilt = ToolSpec.from_dict(d)
        assert rebuilt.params == []
