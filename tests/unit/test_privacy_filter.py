"""Unit tests for privacyfence.privacy_filter -- the enforcement layer behind
settings.yaml's privacy/drive_privacy/slack_privacy sections.

The one invariant that matters most: a category resolved to "block" must
never leak any part of the original value (not a truncated prefix, not a
length that could be used to reconstruct structure beyond what "redact"
already reveals) -- block means none of it, matching what
settings.yaml.example has always documented even though nothing enforced it
before this module existed.
"""
from __future__ import annotations

from privacyfence.privacy_filter import (
    apply_list,
    apply_text,
    category_policy,
    check_consistency_warnings,
    init_privacy_filter,
)

# Module-level _GROUPS is reset by tests/conftest.py's autouse
# _reset_singletons fixture before and after every test, same as
# pii_detector's and auto_accept's own module globals.


class TestInitAndResolution:
    def test_uninitialized_group_resolves_allow(self):
        # Fail open on missing config -- this module only ever narrows what
        # already ships, never adds a new default-block surface on its own.
        assert category_policy("privacy", "body") == "allow"

    def test_explicit_category_policy_wins(self):
        init_privacy_filter({"privacy": {"default_policy": "allow", "categories": {"body": "block"}}})
        assert category_policy("privacy", "body") == "block"

    def test_undefined_category_falls_back_to_default_policy(self):
        init_privacy_filter({"privacy": {"default_policy": "block", "categories": {"metadata": "allow"}}})
        assert category_policy("privacy", "attachments") == "block"
        assert category_policy("privacy", "metadata") == "allow"

    def test_missing_default_policy_falls_back_to_allow(self):
        init_privacy_filter({"privacy": {"categories": {"body": "block"}}})
        assert category_policy("privacy", "unknown_category") == "allow"

    def test_invalid_default_policy_falls_back_to_allow(self):
        init_privacy_filter({"privacy": {"default_policy": "delete_everything"}})
        assert category_policy("privacy", "anything") == "allow"

    def test_invalid_category_policy_falls_back_to_group_default(self):
        init_privacy_filter({"privacy": {"default_policy": "block", "categories": {"body": "nonsense"}}})
        assert category_policy("privacy", "body") == "block"

    def test_groups_are_independent(self):
        init_privacy_filter({
            "privacy": {"default_policy": "allow"},
            "drive_privacy": {"default_policy": "block"},
        })
        assert category_policy("privacy", "body") == "allow"
        assert category_policy("drive_privacy", "file_content") == "block"
        assert category_policy("slack_privacy", "message_content") == "allow"  # never configured

    def test_contacts_tasks_confluence_groups_are_wired(self):
        init_privacy_filter({
            "contacts_privacy": {"categories": {"notes": "block"}},
            "tasks_privacy": {"categories": {"notes": "redact"}},
            "confluence_privacy": {"categories": {"search_excerpt": "block"}},
        })
        assert category_policy("contacts_privacy", "notes") == "block"
        assert category_policy("tasks_privacy", "notes") == "redact"
        assert category_policy("confluence_privacy", "search_excerpt") == "block"

    def test_non_dict_config_value_does_not_crash(self):
        init_privacy_filter({"privacy": "not a dict"})
        assert category_policy("privacy", "body") == "allow"

    def test_non_dict_categories_value_does_not_crash(self):
        init_privacy_filter({"privacy": {"categories": "not a dict"}})
        assert category_policy("privacy", "body") == "allow"


class TestApplyText:
    def test_allow_passes_through_unchanged(self):
        init_privacy_filter({"privacy": {"categories": {"body": "allow"}}})
        assert apply_text("privacy", "body", "the actual message") == "the actual message"

    def test_block_replaces_with_marker(self):
        init_privacy_filter({"privacy": {"categories": {"body": "block"}}})
        result = apply_text("privacy", "body", "sensitive contract terms")
        assert result == "[BLOCKED BY PRIVACY FILTER]"
        assert "sensitive" not in result
        assert "contract" not in result

    def test_redact_reveals_length_but_not_content(self):
        init_privacy_filter({"privacy": {"categories": {"body": "redact"}}})
        value = "sensitive contract terms"
        result = apply_text("privacy", "body", value)
        assert "sensitive" not in result
        assert "contract" not in result
        assert str(len(value)) in result

    def test_empty_string_passes_through_regardless_of_policy(self):
        init_privacy_filter({"privacy": {"categories": {"body": "block"}}})
        assert apply_text("privacy", "body", "") == ""

    def test_unconfigured_category_defaults_allow(self):
        # No init_privacy_filter call at all for this group.
        assert apply_text("slack_privacy", "message_content", "hello") == "hello"


class TestApplyList:
    def test_allow_passes_through_unchanged(self):
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "allow"}}})
        items = [{"name": "a.txt"}, {"name": "b.txt"}]
        assert apply_list("drive_privacy", "file_list", items) == items

    def test_block_empties_the_list(self):
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "block"}}})
        items = [{"name": "confidential.xlsx"}]
        result = apply_list("drive_privacy", "file_list", items)
        assert result == []

    def test_redact_also_empties_the_list(self):
        # Documented in the module docstring: no canonical "partial" shape
        # for a list of structured records, so redact == block for lists.
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "redact"}}})
        items = [{"name": "confidential.xlsx"}]
        assert apply_list("drive_privacy", "file_list", items) == []

    def test_empty_list_passes_through_regardless_of_policy(self):
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "block"}}})
        assert apply_list("drive_privacy", "file_list", []) == []


class TestConsistencyWarnings:
    """Advisory-only: drive_privacy.file_list and .file_metadata gate two
    different tools (drive_list_files / drive_get_file_metadata) that both
    return a file's name/owners. Restricting one without the other still
    leaks that data through whichever tool's category is still "allow" --
    check_consistency_warnings() surfaces that, but never changes what
    apply_text/apply_list actually do."""

    def test_no_warning_when_both_allow(self):
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "allow", "file_metadata": "allow"}}})
        assert check_consistency_warnings() == []

    def test_no_warning_when_both_restricted(self):
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "block", "file_metadata": "redact"}}})
        assert check_consistency_warnings() == []

    def test_warns_when_file_metadata_restricted_but_file_list_allows(self):
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "allow", "file_metadata": "block"}}})
        warnings = check_consistency_warnings()
        assert len(warnings) == 1
        assert "file_metadata" in warnings[0]
        assert "file_list" in warnings[0]
        assert "drive_list_files" in warnings[0]

    def test_warns_when_file_list_restricted_but_file_metadata_allows(self):
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "redact", "file_metadata": "allow"}}})
        warnings = check_consistency_warnings()
        assert len(warnings) == 1
        assert "file_list" in warnings[0]
        assert "drive_get_file_metadata" in warnings[0]

    def test_no_warning_when_unconfigured(self):
        # Both default to "allow" -- no mismatch.
        assert check_consistency_warnings() == []
