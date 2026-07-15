"""Unit tests for privacyfence.resource_grants.

This module compiles connector-scoped auto-accept grants into the exact
per-operation-key rule shape ``AutoAcceptEvaluator`` already consumes (see
auto_accept.py) -- these tests verify the compiler and the one-time
migration produce the *same effective auto-accept behavior* a hand-written
``auto_accept_rules`` block would, never a broader one.
"""
from __future__ import annotations

from privacyfence import resource_grants as rg
from privacyfence.auto_accept import TOOL_TO_OPERATION


# --------------------------------------------------------------------------- #
# Manifest sanity
# --------------------------------------------------------------------------- #

class TestManifestSanity:
    def test_every_resource_type_has_a_unique_connector_and_config_key(self):
        keys = [(t.connector, t.config_key) for t in rg.GRANT_RESOURCE_TYPES]
        assert len(keys) == len(set(keys))

    def test_every_capability_target_operation_key_is_a_real_operation(self):
        # Every (operation_key, rule_name) a capability compiles to must be an
        # operation_key that TOOL_TO_OPERATION actually maps some tool onto --
        # otherwise the compiled rule would silently never be evaluated.
        real_operation_keys = set(TOOL_TO_OPERATION.values())
        for rt in rg.GRANT_RESOURCE_TYPES:
            for capability in rt.capabilities.values():
                for op_key, _rule_name in capability.targets:
                    assert op_key in real_operation_keys, (rt.connector, rt.config_key, op_key)

    def test_resource_type_lookup_by_connector_and_key(self):
        rt = rg.resource_type("tasks", "task_lists")
        assert rt is not None
        assert rt.connector == "tasks"

    def test_resource_type_lookup_returns_none_for_unknown(self):
        assert rg.resource_type("nope", "nope") is None

    def test_resource_types_for_connector_drive_includes_folders_and_sandbox(self):
        keys = {rt.config_key for rt in rg.resource_types_for_connector("drive")}
        assert {"folders", "sandbox_folders", "spreadsheets"} <= keys

    def test_resource_types_for_connector_contacts_is_empty(self):
        # contacts.edit's only rule (no_contact_info_change) isn't a resource
        # grant -- there's no ID to trust once, see the module docstring.
        assert rg.resource_types_for_connector("contacts") == []


# --------------------------------------------------------------------------- #
# expand_grants
# --------------------------------------------------------------------------- #

class TestExpandGrants:
    def test_empty_grants_produce_no_rules(self):
        assert rg.expand_grants({}) == {}

    def test_disabled_capability_produces_no_rule(self):
        grants = {"drive": {"folders": [{"id": "F1", "read": False}]}}
        assert rg.expand_grants(grants) == {}

    def test_single_capability_single_target(self):
        grants = {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}
        compiled = rg.expand_grants(grants)
        assert compiled["drive.write_file"] == [
            {"rule": "approved_sandbox_folder", "value": ["F1"], "_grant": True}
        ]

    def test_capability_spanning_multiple_operation_keys(self):
        # This is the "apply to all" behavior the whole module exists for --
        # one grant, six compiled operation keys.
        grants = {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}
        compiled = rg.expand_grants(grants)
        expected_ops = {
            "drive.write_file", "drive.write_doc", "sheets.write_range",
            "sheets.add_sheet", "sheets.rename_sheet", "sheets.format_range",
        }
        assert expected_ops <= compiled.keys()
        for op in expected_ops:
            assert compiled[op] == [{"rule": "approved_sandbox_folder", "value": ["F1"], "_grant": True}]

    def test_multiple_entries_aggregate_into_one_rule_value(self):
        grants = {"drive": {"folders": [{"id": "F1", "read": True}, {"id": "F2", "read": True}]}}
        compiled = rg.expand_grants(grants)
        assert compiled["drive.read_file_contents"][0]["value"] == ["F1", "F2"]

    def test_duplicate_ids_are_not_repeated_in_the_compiled_value(self):
        grants = {"drive": {"folders": [{"id": "F1", "read": True}, {"id": "F1", "read": True}]}}
        compiled = rg.expand_grants(grants)
        assert compiled["drive.read_file_contents"][0]["value"] == ["F1"]

    def test_task_list_complete_capability_covers_complete_and_uncomplete(self):
        grants = {"tasks": {"task_lists": [{"id": "L1", "complete": True}]}}
        compiled = rg.expand_grants(grants)
        assert "tasks.complete_task" in compiled
        assert "tasks.uncomplete_task" in compiled
        assert "tasks.create_task" not in compiled  # create wasn't enabled

    def test_spreadsheet_grant_compiles_pair_dict_value(self):
        grants = {"drive": {"spreadsheets": [{"id": "S1", "tab": "Budget", "read": True}]}}
        compiled = rg.expand_grants(grants)
        assert compiled["sheets.read_values"] == [
            {"rule": "approved_spreadsheet", "value": [{"spreadsheet_id": "S1", "tab": "Budget"}], "_grant": True}
        ]

    def test_spreadsheet_grant_without_tab_omits_tab_key(self):
        grants = {"drive": {"spreadsheets": [{"id": "S1", "read": True}]}}
        compiled = rg.expand_grants(grants)
        assert compiled["sheets.read_values"][0]["value"] == [{"spreadsheet_id": "S1"}]

    def test_jira_project_grant_uses_key_field_not_id(self):
        grants = {"jira": {"projects": [{"key": "ENG", "read": True}]}}
        compiled = rg.expand_grants(grants)
        assert compiled["jira.read_issue"][0]["value"] == ["ENG"]

    def test_unrelated_connector_grants_do_not_leak_into_each_other(self):
        grants = {
            "drive": {"folders": [{"id": "F1", "read": True}]},
            "tasks": {"task_lists": [{"id": "L1", "edit": True}]},
        }
        compiled = rg.expand_grants(grants)
        assert "tasks.update_task" in compiled
        assert "drive.read_file_contents" in compiled
        assert "tasks.update_task" not in [k for k in compiled if "drive" in k]


# --------------------------------------------------------------------------- #
# build_effective_rules
# --------------------------------------------------------------------------- #

class TestBuildEffectiveRules:
    def test_merges_hand_written_rules_and_grants_for_different_operations(self):
        cfg = {
            "auto_accept_rules": {"contacts.edit": [{"rule": "no_contact_info_change"}]},
            "auto_accept_grants": {"drive": {"folders": [{"id": "F1", "read": True}]}},
        }
        effective = rg.build_effective_rules(cfg)
        assert effective["contacts.edit"] == [{"rule": "no_contact_info_change"}]
        assert effective["drive.read_file_contents"][0]["rule"] == "approved_folder"

    def test_grant_rules_are_appended_not_overwritten_for_same_operation(self):
        cfg = {
            "auto_accept_rules": {"drive.read_file_contents": [{"rule": "i_am_owner"}]},
            "auto_accept_grants": {"drive": {"folders": [{"id": "F1", "read": True}]}},
        }
        effective = rg.build_effective_rules(cfg)
        rule_names = {r["rule"] for r in effective["drive.read_file_contents"]}
        assert rule_names == {"i_am_owner", "approved_folder"}

    def test_missing_sections_default_to_empty(self):
        assert rg.build_effective_rules({}) == {}

    def test_does_not_mutate_the_input_config(self):
        cfg = {"auto_accept_rules": {"contacts.edit": [{"rule": "no_contact_info_change"}]}}
        rg.build_effective_rules(cfg)
        assert cfg == {"auto_accept_rules": {"contacts.edit": [{"rule": "no_contact_info_change"}]}}


# --------------------------------------------------------------------------- #
# migrate_rules_to_grants
# --------------------------------------------------------------------------- #

class TestMigrateRulesToGrants:
    def test_full_match_across_all_target_operations_migrates(self):
        cfg = {
            "auto_accept_rules": {
                "drive.read_file_contents": [{"rule": "approved_folder", "value": ["F1"]}],
                "drive.download_file": [{"rule": "approved_folder", "value": ["F1"]}],
                "sheets.read_values": [{"rule": "approved_folder", "value": ["F1"]}],
            }
        }
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)
        assert summary  # something was reported
        assert new_cfg["auto_accept_grants"]["drive"]["folders"] == [{"id": "F1", "read": True}]
        assert "auto_accept_rules" not in new_cfg

    def test_value_order_does_not_prevent_a_full_match(self):
        cfg = {
            "auto_accept_rules": {
                "drive.read_file_contents": [{"rule": "approved_folder", "value": ["F1", "F2"]}],
                "drive.download_file": [{"rule": "approved_folder", "value": ["F2", "F1"]}],
                "sheets.read_values": [{"rule": "approved_folder", "value": ["F1", "F2"]}],
            }
        }
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)
        assert summary
        ids = {e["id"] for e in new_cfg["auto_accept_grants"]["drive"]["folders"]}
        assert ids == {"F1", "F2"}

    def test_partial_match_is_left_untouched(self):
        # approved_folder present on only 2 of the 3 target operation keys --
        # migrating would silently widen auto-accept to sheets.read_values,
        # which the user never configured. Must not migrate.
        cfg = {
            "auto_accept_rules": {
                "drive.read_file_contents": [{"rule": "approved_folder", "value": ["F1"]}],
                "drive.download_file": [{"rule": "approved_folder", "value": ["F1"]}],
            }
        }
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)
        assert summary == []
        assert "auto_accept_grants" not in new_cfg
        assert new_cfg["auto_accept_rules"] == cfg["auto_accept_rules"]

    def test_mismatched_values_across_operations_do_not_migrate(self):
        cfg = {
            "auto_accept_rules": {
                "drive.read_file_contents": [{"rule": "approved_folder", "value": ["F1"]}],
                "drive.download_file": [{"rule": "approved_folder", "value": ["F2"]}],
                "sheets.read_values": [{"rule": "approved_folder", "value": ["F1"]}],
            }
        }
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)
        assert summary == []
        assert "auto_accept_grants" not in new_cfg

    def test_unrelated_rules_survive_migration_untouched(self):
        cfg = {
            "auto_accept_rules": {
                "drive.read_file_contents": [{"rule": "approved_folder", "value": ["F1"]}],
                "drive.download_file": [{"rule": "approved_folder", "value": ["F1"]}],
                "sheets.read_values": [{"rule": "approved_folder", "value": ["F1"]}],
                "contacts.edit": [{"rule": "no_contact_info_change"}],
            }
        }
        new_cfg, _summary = rg.migrate_rules_to_grants(cfg)
        assert new_cfg["auto_accept_rules"] == {"contacts.edit": [{"rule": "no_contact_info_change"}]}

    def test_idempotent_second_run_is_a_no_op(self):
        cfg = {
            "auto_accept_rules": {
                "drive.read_file_contents": [{"rule": "approved_folder", "value": ["F1"]}],
                "drive.download_file": [{"rule": "approved_folder", "value": ["F1"]}],
                "sheets.read_values": [{"rule": "approved_folder", "value": ["F1"]}],
            }
        }
        migrated_once, _ = rg.migrate_rules_to_grants(cfg)
        migrated_twice, summary_twice = rg.migrate_rules_to_grants(migrated_once)
        assert summary_twice == []
        assert migrated_twice == migrated_once

    def test_migration_marker_is_set(self):
        new_cfg, _ = rg.migrate_rules_to_grants({})
        assert new_cfg[rg.MIGRATION_MARKER] is True

    def test_already_marked_config_is_returned_unchanged(self):
        cfg = {rg.MIGRATION_MARKER: True, "auto_accept_rules": {"contacts.edit": [{"rule": "x"}]}}
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)
        assert new_cfg is cfg
        assert summary == []

    def test_migrated_config_has_identical_effective_rules_to_the_original(self):
        # The whole point: migration must never change what auto-accepts.
        cfg = {
            "auto_accept_rules": {
                "drive.read_file_contents": [{"rule": "approved_folder", "value": ["F1", "F2"]}],
                "drive.download_file": [{"rule": "approved_folder", "value": ["F1", "F2"]}],
                "sheets.read_values": [
                    {"rule": "approved_folder", "value": ["F1", "F2"]},
                    {"rule": "i_am_owner"},
                ],
                "tasks.create_task": [{"rule": "approved_task_list", "value": ["L1"]}],
            }
        }
        before = _normalize(rg.build_effective_rules(cfg))
        migrated_cfg, _ = rg.migrate_rules_to_grants(cfg)
        after = _normalize(rg.build_effective_rules(migrated_cfg))
        assert before == after

    def test_spreadsheet_pair_values_migrate_correctly(self):
        cfg = {
            "auto_accept_rules": {
                "sheets.read_values": [
                    {"rule": "approved_spreadsheet", "value": [{"spreadsheet_id": "S1", "tab": "Budget"}]}
                ],
            }
        }
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)
        assert summary
        entries = new_cfg["auto_accept_grants"]["drive"]["spreadsheets"]
        assert entries == [{"id": "S1", "tab": "Budget", "read": True}]


def _normalize(rules: dict) -> dict:
    """Order-independent comparison key for a compiled/effective rules dict."""
    def _value_key(v):
        return tuple(sorted(v.items())) if isinstance(v, dict) else v

    out = {}
    for op_key, entries in rules.items():
        normalized_entries = []
        for entry in entries:
            value = entry.get("value")
            if isinstance(value, list):
                value = tuple(sorted((_value_key(v) for v in value), key=repr))
            normalized_entries.append((entry["rule"], value))
        out[op_key] = sorted(normalized_entries, key=repr)
    return out
