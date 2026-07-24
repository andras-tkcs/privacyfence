"""Unit tests for privacyfence.resource_grants.

This module compiles connector-scoped auto-accept grants into the exact
per-operation-key rule shape ``AutoAcceptEvaluator`` already consumes (see
auto_accept.py) -- these tests verify the compiler and the one-time
migration produce the *same effective auto-accept behavior* a hand-written
``auto_accept_rules`` block would, never a broader one.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from privacyfence import resource_grants as rg
from privacyfence.auto_accept import TOOL_TO_OPERATION
from privacyfence.calendar_client import CalendarListEntry
from privacyfence.confluence_client import ConfluenceSpace
from privacyfence.drive_client import DriveFile
from privacyfence.jira_client import JiraProject
from privacyfence.salesforce_client import SalesforceReport
from privacyfence.slack_client import SlackChannel
from privacyfence.tasks_client import TaskList
from privacyfence.telegram_client import TelegramChat


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
# Name resolvers (rt.resolver) -- turn a raw resource ID into the
# human-readable label shown in approval popups and the grants menu. Each
# one swallows any connector-client exception via `except Exception: return
# None` -- a resolution failure must never crash the menu, only fall back to
# showing the raw ID (see resource_names.py's ResourceNameResolver, which is
# the caller that relies on this contract).
# --------------------------------------------------------------------------- #

class TestResolveDriveFile:
    def test_success_returns_the_file_name(self):
        rt = rg.resource_type("drive", "folders")
        client = MagicMock()
        client.get_file_metadata.return_value = DriveFile(
            id="F1", name="Reports", mime_type="application/vnd.google-apps.folder", size=0
        )
        assert rt.resolver(client, "F1") == "Reports"
        client.get_file_metadata.assert_called_once_with("F1")

    def test_client_exception_is_swallowed_and_returns_none(self):
        rt = rg.resource_type("drive", "folders")
        client = MagicMock()
        client.get_file_metadata.side_effect = Exception("Drive API unavailable")
        assert rt.resolver(client, "F1") is None

    def test_empty_file_name_falls_back_to_none_not_empty_string(self):
        # DriveFile.name can legitimately come back "" -- the resolver must
        # not surface a blank label, it should look exactly like "unresolved"
        # (None) so the caller falls back to showing the raw ID instead.
        rt = rg.resource_type("drive", "folders")
        client = MagicMock()
        client.get_file_metadata.return_value = DriveFile(
            id="F1", name="", mime_type="application/vnd.google-apps.folder", size=0
        )
        assert rt.resolver(client, "F1") is None


class TestResolveTaskList:
    def test_success_returns_the_list_title(self):
        rt = rg.resource_type("tasks", "task_lists")
        client = MagicMock()
        client.list_task_lists.return_value = [TaskList(id="L1", title="Groceries", updated="")]
        assert rt.resolver(client, "L1") == "Groceries"

    def test_client_exception_is_swallowed_and_returns_none(self):
        rt = rg.resource_type("tasks", "task_lists")
        client = MagicMock()
        client.list_task_lists.side_effect = Exception("not authorized")
        assert rt.resolver(client, "L1") is None

    def test_id_not_present_in_the_listing_returns_none(self):
        rt = rg.resource_type("tasks", "task_lists")
        client = MagicMock()
        client.list_task_lists.return_value = [TaskList(id="OTHER", title="Groceries", updated="")]
        assert rt.resolver(client, "L1") is None

    def test_unexpected_none_return_from_the_client_is_handled_gracefully(self):
        # `_find_by` iterates `items or []` -- a client returning None
        # (instead of an empty list) must not raise.
        rt = rg.resource_type("tasks", "task_lists")
        client = MagicMock()
        client.list_task_lists.return_value = None
        assert rt.resolver(client, "L1") is None


class TestResolveSlackChannel:
    def test_success_returns_the_name_with_a_hash_prefix(self):
        rt = rg.resource_type("slack", "channels")
        client = MagicMock()
        client.list_channels.return_value = [SlackChannel(id="C1", name="general")]
        assert rt.resolver(client, "C1") == "#general"

    def test_client_exception_is_swallowed_and_returns_none(self):
        rt = rg.resource_type("slack", "channels")
        client = MagicMock()
        client.list_channels.side_effect = Exception("rate limited")
        assert rt.resolver(client, "C1") is None

    def test_id_not_present_in_the_listing_returns_none_not_a_bare_hash(self):
        rt = rg.resource_type("slack", "channels")
        client = MagicMock()
        client.list_channels.return_value = [SlackChannel(id="OTHER", name="general")]
        assert rt.resolver(client, "C1") is None


class TestResolveTelegramChat:
    def test_success_returns_the_chat_name(self):
        rt = rg.resource_type("telegram", "chats")
        client = AsyncMock()
        client.list_chats.return_value = [
            TelegramChat(id=123, name="Alice", username="", chat_type="user", unread_count=0, is_self=False)
        ]
        assert rt.resolver(client, "123") == "Alice"

    def test_client_exception_is_swallowed_and_returns_none(self):
        rt = rg.resource_type("telegram", "chats")
        client = AsyncMock()
        client.list_chats.side_effect = Exception("connection lost")
        assert rt.resolver(client, "123") is None

    def test_id_not_present_in_the_listing_returns_none(self):
        rt = rg.resource_type("telegram", "chats")
        client = AsyncMock()
        client.list_chats.return_value = [
            TelegramChat(id=999, name="Bob", username="", chat_type="user", unread_count=0, is_self=False)
        ]
        assert rt.resolver(client, "123") is None


class TestResolveJiraProject:
    def test_success_returns_the_project_name(self):
        rt = rg.resource_type("jira", "projects")
        client = MagicMock()
        client.list_projects.return_value = [JiraProject(key="ENG", name="Engineering")]
        assert rt.resolver(client, "ENG") == "Engineering"

    def test_client_exception_is_swallowed_and_returns_none(self):
        rt = rg.resource_type("jira", "projects")
        client = MagicMock()
        client.list_projects.side_effect = Exception("no access")
        assert rt.resolver(client, "ENG") is None

    def test_key_not_present_in_the_listing_returns_none(self):
        rt = rg.resource_type("jira", "projects")
        client = MagicMock()
        client.list_projects.return_value = [JiraProject(key="OTHER", name="Engineering")]
        assert rt.resolver(client, "ENG") is None


class TestResolveConfluenceSpace:
    def test_success_returns_the_space_name(self):
        rt = rg.resource_type("confluence", "spaces")
        client = MagicMock()
        client.list_spaces.return_value = [ConfluenceSpace(key="ENG", name="Engineering")]
        assert rt.resolver(client, "ENG") == "Engineering"

    def test_client_exception_is_swallowed_and_returns_none(self):
        rt = rg.resource_type("confluence", "spaces")
        client = MagicMock()
        client.list_spaces.side_effect = Exception("no access")
        assert rt.resolver(client, "ENG") is None

    def test_key_not_present_in_the_listing_returns_none(self):
        rt = rg.resource_type("confluence", "spaces")
        client = MagicMock()
        client.list_spaces.return_value = [ConfluenceSpace(key="OTHER", name="Engineering")]
        assert rt.resolver(client, "ENG") is None


class TestResolveCalendar:
    def test_success_returns_the_calendar_summary(self):
        rt = rg.resource_type("calendar", "calendars")
        client = MagicMock()
        client.list_calendars.return_value = [
            CalendarListEntry(id="cal1", summary="Work", description="", primary=True, access_role="owner")
        ]
        assert rt.resolver(client, "cal1") == "Work"

    def test_client_exception_is_swallowed_and_returns_none(self):
        rt = rg.resource_type("calendar", "calendars")
        client = MagicMock()
        client.list_calendars.side_effect = Exception("token expired")
        assert rt.resolver(client, "cal1") is None

    def test_id_not_present_in_the_listing_returns_none(self):
        rt = rg.resource_type("calendar", "calendars")
        client = MagicMock()
        client.list_calendars.return_value = [
            CalendarListEntry(id="other", summary="Work", description="", primary=True, access_role="owner")
        ]
        assert rt.resolver(client, "cal1") is None


class TestResolveSalesforceReport:
    def test_success_returns_the_report_name(self):
        rt = rg.resource_type("salesforce", "reports")
        client = MagicMock()
        client.list_reports.return_value = [
            SalesforceReport(id="R1", name="Pipeline", report_type="tabular", folder_name="Sales", description="")
        ]
        assert rt.resolver(client, "R1") == "Pipeline"

    def test_client_exception_is_swallowed_and_returns_none(self):
        rt = rg.resource_type("salesforce", "reports")
        client = MagicMock()
        client.list_reports.side_effect = Exception("session expired")
        assert rt.resolver(client, "R1") is None

    def test_id_not_present_in_the_listing_returns_none(self):
        rt = rg.resource_type("salesforce", "reports")
        client = MagicMock()
        client.list_reports.return_value = [
            SalesforceReport(id="OTHER", name="Pipeline", report_type="tabular", folder_name="Sales", description="")
        ]
        assert rt.resolver(client, "R1") is None


# --------------------------------------------------------------------------- #
# Candidate listers (rt.list_candidates) -- power the "+ Add..." live picker
# in the grants menu for connectors with a cheap listing call.
# --------------------------------------------------------------------------- #

class TestListCandidates:
    def test_task_lists_returns_id_title_pairs(self):
        rt = rg.resource_type("tasks", "task_lists")
        client = MagicMock()
        client.list_task_lists.return_value = [
            TaskList(id="L1", title="Groceries", updated=""),
            TaskList(id="L2", title="Errands", updated=""),
        ]
        assert rt.list_candidates(client) == [("L1", "Groceries"), ("L2", "Errands")]

    def test_slack_channels_returns_id_hash_name_pairs(self):
        rt = rg.resource_type("slack", "channels")
        client = MagicMock()
        client.list_channels.return_value = [SlackChannel(id="C1", name="general")]
        assert rt.list_candidates(client) == [("C1", "#general")]

    def test_telegram_chats_returns_stringified_id_name_pairs(self):
        rt = rg.resource_type("telegram", "chats")
        client = AsyncMock()
        client.list_chats.return_value = [
            TelegramChat(id=123, name="Alice", username="", chat_type="user", unread_count=0, is_self=False)
        ]
        # TelegramChat.id is an int -- the lister must stringify it to match
        # the string resource IDs grants are keyed by everywhere else.
        assert rt.list_candidates(client) == [("123", "Alice")]

    def test_jira_projects_returns_key_dash_name_pairs(self):
        rt = rg.resource_type("jira", "projects")
        client = MagicMock()
        client.list_projects.return_value = [JiraProject(key="ENG", name="Engineering")]
        assert rt.list_candidates(client) == [("ENG", "ENG — Engineering")]

    def test_confluence_spaces_returns_key_dash_name_pairs(self):
        rt = rg.resource_type("confluence", "spaces")
        client = MagicMock()
        client.list_spaces.return_value = [ConfluenceSpace(key="ENG", name="Engineering")]
        assert rt.list_candidates(client) == [("ENG", "ENG — Engineering")]

    def test_calendars_returns_id_summary_pairs(self):
        rt = rg.resource_type("calendar", "calendars")
        client = MagicMock()
        client.list_calendars.return_value = [
            CalendarListEntry(id="cal1", summary="Work", description="", primary=True, access_role="owner")
        ]
        assert rt.list_candidates(client) == [("cal1", "Work")]

    def test_salesforce_reports_returns_id_name_pairs(self):
        rt = rg.resource_type("salesforce", "reports")
        client = MagicMock()
        client.list_reports.return_value = [
            SalesforceReport(id="R1", name="Pipeline", report_type="tabular", folder_name="Sales", description="")
        ]
        assert rt.list_candidates(client) == [("R1", "Pipeline")]

    def test_drive_resource_types_have_no_list_candidates(self):
        # No cheap "list every folder/spreadsheet I can see" API -- these
        # fall back to paste-ID-or-URL entry in the menu (see menu_bar.py).
        for config_key in ("folders", "sandbox_folders", "spreadsheets"):
            assert rg.resource_type("drive", config_key).list_candidates is None


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

    def test_sandbox_folder_write_also_covers_dimensions_and_docs_ops(self):
        # sheets.insert_dimensions/delete_dimensions and docs.edit_content/
        # docs.format_content all use approved_sandbox_folder too -- a grant
        # should cover them alongside the original six write operations.
        grants = {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}
        compiled = rg.expand_grants(grants)
        for op in ("sheets.insert_dimensions", "sheets.delete_dimensions", "docs.edit_content", "docs.format_content"):
            assert compiled[op] == [{"rule": "approved_sandbox_folder", "value": ["F1"], "_grant": True}]

    def test_sandbox_folder_write_also_covers_comment_upload_and_move(self):
        # drive.comment_file uses approved_sandbox_folder like the rest;
        # drive.upload_file/drive.move_file use their own existing rule
        # names (parent_folder_allowlist/move_within_approved_folders) --
        # all three are now targets of the same "write" capability, so one
        # sandbox-folder grant covers commenting on, uploading into, and
        # moving out of it, not just writing to a file already there.
        grants = {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}
        compiled = rg.expand_grants(grants)
        assert compiled["drive.comment_file"] == [
            {"rule": "approved_sandbox_folder", "value": ["F1"], "_grant": True}
        ]
        assert compiled["drive.upload_file"] == [
            {"rule": "parent_folder_allowlist", "value": ["F1"], "_grant": True}
        ]
        assert compiled["drive.move_file"] == [
            {"rule": "move_within_approved_folders", "value": ["F1"], "_grant": True}
        ]

    def test_spreadsheet_write_also_covers_dimensions_ops(self):
        grants = {"drive": {"spreadsheets": [{"id": "S1", "write": True}]}}
        compiled = rg.expand_grants(grants)
        for op in ("sheets.insert_dimensions", "sheets.delete_dimensions"):
            assert compiled[op] == [{"rule": "approved_spreadsheet", "value": [{"spreadsheet_id": "S1"}], "_grant": True}]

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

    def test_null_operation_value_is_treated_as_no_rules(self):
        # A hand-edited settings.yaml with a bare "contacts.edit:" key (no
        # list under it) parses to None for that operation, not []. Must not
        # crash (regression for #81, 'NoneType' object is not iterable).
        cfg = {"auto_accept_rules": {"contacts.edit": None}}
        assert rg.build_effective_rules(cfg) == {"contacts.edit": []}


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

    def test_capability_with_no_target_operation_keys_is_skipped_without_crashing(self, monkeypatch):
        # Defensive branch: `first_op_key, first_rule_name = capability.targets[0]`
        # on the next line would IndexError if `targets` were empty and this
        # `if not op_keys: continue` guard didn't skip first. No entry in the
        # real manifest has empty targets today, but this pins the guard so a
        # future capability added without any wired operation key degrades to
        # a no-op instead of crashing the migration for every connector.
        fake_rt = rg.GrantResourceType(
            connector="fake", config_key="things", id_field="id",
            label="Fake Things", singular="thing",
            capabilities={"cap": rg.GrantCapability("Cap", ())},
            resolver=lambda client, resource_id: None,
        )
        monkeypatch.setattr(rg, "GRANT_RESOURCE_TYPES", (fake_rt,))
        cfg = {"auto_accept_rules": {"fake.op": [{"rule": "cap_rule", "value": ["X1"]}]}}

        new_cfg, summary = rg.migrate_rules_to_grants(cfg)

        assert summary == []
        assert "auto_accept_grants" not in new_cfg
        assert new_cfg["auto_accept_rules"] == cfg["auto_accept_rules"]

    def test_empty_resource_id_within_a_matched_value_produces_no_grant_entry(self):
        # A legacy rule value of [""] would technically satisfy "matched
        # across every target op key" (there's only one target here), but an
        # empty-string ID must never become a grant entry -- that would be a
        # garbage/empty-ID grant, and the `if not resource_id: continue`
        # guard exists precisely to skip creating one rather than ever
        # writing something broader than what the original rule expressed.
        cfg = {
            "auto_accept_rules": {
                "jira.read_issue": [{"rule": "approved_project_keys", "value": [""]}],
            }
        }
        new_cfg, _summary = rg.migrate_rules_to_grants(cfg)

        assert "jira" not in new_cfg.get("auto_accept_grants", {})

    def test_empty_resource_id_mixed_with_a_valid_one_only_grants_the_valid_id(self):
        cfg = {
            "auto_accept_rules": {
                "jira.read_issue": [{"rule": "approved_project_keys", "value": ["", "ENG"]}],
            }
        }
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)

        assert summary
        entries = new_cfg["auto_accept_grants"]["jira"]["projects"]
        assert entries == [{"key": "ENG", "read": True}]

    def test_migration_merges_into_a_pre_existing_grant_entry_with_the_same_id(self):
        # A resource can already have a grant entry (added by hand through
        # the menu) before migration runs for a *different* capability --
        # the migrated capability must land on that same entry, not create a
        # second duplicate entry for the same project key.
        cfg = {
            "auto_accept_grants": {"jira": {"projects": [{"key": "ENG", "create": True}]}},
            "auto_accept_rules": {
                "jira.read_issue": [{"rule": "approved_project_keys", "value": ["ENG"]}],
            },
        }
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)

        assert summary
        entries = new_cfg["auto_accept_grants"]["jira"]["projects"]
        assert entries == [{"key": "ENG", "create": True, "read": True}]

    def test_scalar_non_list_rule_value_still_migrates(self):
        # A rule's "value" isn't required to be a list -- a hand-written
        # single-value rule can be a bare string. `_values_equal`'s
        # non-list fallback (plain `a == b`) must still recognize this as a
        # match for a single-target capability.
        cfg = {
            "auto_accept_rules": {
                "jira.read_issue": [{"rule": "approved_project_keys", "value": "ENG"}],
            }
        }
        new_cfg, summary = rg.migrate_rules_to_grants(cfg)

        assert summary
        entries = new_cfg["auto_accept_grants"]["jira"]["projects"]
        assert entries == [{"key": "ENG", "read": True}]


class TestApplyGrantUpsert:
    """apply_grant_upsert/apply_grant_removal -- shared by menu_bar.py-style
    editing and gate.propose_rule_change()'s bridge-facing counterpart (see
    that function's docstring in gate.py). Both mutate the full config dict
    in place, the shape auto_accept.mutate_grants() expects a mutator to
    receive."""

    def test_add_new_entry_with_capabilities(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        cfg: dict = {}
        changed = rg.apply_grant_upsert(cfg, rt, "folder1", name="Team sandbox", capabilities={"write": True})
        assert changed is True
        assert cfg["auto_accept_grants"]["drive"]["sandbox_folders"] == [
            {"id": "folder1", "name": "Team sandbox", "write": True}
        ]

    def test_upsert_updates_an_existing_entry_in_place_rather_than_duplicating(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "folder1", "write": False}]}}}
        rg.apply_grant_upsert(cfg, rt, "folder1", capabilities={"write": True})
        assert cfg["auto_accept_grants"]["drive"]["sandbox_folders"] == [{"id": "folder1", "write": True}]

    def test_upsert_on_spreadsheets_matches_by_id_and_tab_together(self):
        rt = rg.resource_type("drive", "spreadsheets")
        cfg = {
            "auto_accept_grants": {
                "drive": {"spreadsheets": [{"id": "sheet1", "tab": "Sheet1", "read": True}]}
            }
        }
        # Same id, different tab -- must be a new entry, not an update of Sheet1's.
        rg.apply_grant_upsert(cfg, rt, "sheet1", tab="Sheet2", capabilities={"read": True})
        entries = cfg["auto_accept_grants"]["drive"]["spreadsheets"]
        assert {"id": "sheet1", "tab": "Sheet1", "read": True} in entries
        assert {"id": "sheet1", "tab": "Sheet2", "read": True} in entries
        assert len(entries) == 2

    def test_unknown_capability_key_raises(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        cfg: dict = {}
        try:
            rg.apply_grant_upsert(cfg, rt, "folder1", capabilities={"nonexistent": True})
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_remove_existing_entry(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "folder1", "write": True}]}}}
        changed = rg.apply_grant_removal(cfg, rt, "folder1")
        assert changed is True
        assert cfg["auto_accept_grants"] == {}

    def test_remove_nonexistent_entry_is_a_no_op_reported_as_unchanged(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "folder1", "write": True}]}}}
        changed = rg.apply_grant_removal(cfg, rt, "does-not-exist")
        assert changed is False
        assert cfg["auto_accept_grants"]["drive"]["sandbox_folders"] == [{"id": "folder1", "write": True}]


class TestDescribeGrantChange:
    def test_add_lists_enabled_capability_labels(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        description = rg.describe_grant_change(
            "add", rt, "folder1", name="Team sandbox", capabilities={"write": True}
        )
        assert "Team sandbox" in description
        assert "Write auto-accept" in description
        assert description.startswith("Add ")

    def test_update_uses_update_verb(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        description = rg.describe_grant_change("update", rt, "folder1", capabilities={"write": True})
        assert description.startswith("Update ")

    def test_remove_names_the_resource_and_group_label(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        description = rg.describe_grant_change("remove", rt, "folder1", name="Team sandbox")
        assert description == "Remove folder 'Team sandbox' from Sandbox Folders"

    def test_falls_back_to_resource_id_when_no_name_given(self):
        rt = rg.resource_type("drive", "sandbox_folders")
        description = rg.describe_grant_change("remove", rt, "folder1")
        assert "folder1" in description


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
