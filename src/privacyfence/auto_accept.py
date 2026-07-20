"""Auto-accept rule engine for the human review gate."""
from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import yaml

from .resource_grants import build_effective_rules

logger = logging.getLogger(__name__)

# Write operations expected to be called repeatedly against the same file in
# quick succession (e.g. an agent filling in a sheet cell-by-cell, or building
# up formatting one range at a time). These get a lightweight "Allow for 5
# min" popup button, scoped to one file and never persisted to settings.yaml
# — unlike Always allow, it disappears with the daemon and with wall-clock
# time, so it's a much smaller commitment than a standing rule. Maps
# operation key -> the args field that identifies "the same file" for that
# operation.
TEMP_ACCEPT_ELIGIBLE_OPERATIONS: dict[str, str] = {
    "sheets.write_range": "spreadsheet_id",
    "sheets.format_range": "spreadsheet_id",
    "sheets.insert_dimensions": "spreadsheet_id",
    "drive.comment_file": "file_id",
    "docs.edit_content": "file_id",
    "docs.format_content": "file_id",
}
# sheets.delete_dimensions is deliberately absent: unlike insert/format, it
# removes cell content with no undo path through PrivacyFence, so it only
# gets the standing-rule treatment below (like sheets.add_sheet /
# sheets.rename_sheet), never the lighter-weight temp accept.

TEMP_ACCEPT_TTL_SECONDS = 300

# Maps tool name → operation key used in settings.yaml
TOOL_TO_OPERATION: dict[str, str] = {
    "gmail_get_message":              "gmail.read_message",
    "gmail_get_thread":               "gmail.read_thread",
    "gmail_download_attachment":      "gmail.download_attachment",
    "gmail_create_draft":             "gmail.create_draft",
    "gmail_reply_draft":              "gmail.create_draft",
    "gmail_reply_all_draft":          "gmail.create_draft",
    "gmail_add_label":                "gmail.add_label",
    "gmail_remove_label":             "gmail.remove_label",
    "gmail_archive_message":          "gmail.archive_message",
    "gmail_create_filter":            "gmail.create_filter",
    "gmail_update_filter":            "gmail.update_filter",
    "gmail_create_label":             "gmail.create_label",
    "drive_get_file_content":         "drive.read_file_contents",
    "drive_download_file":           "drive.download_file",
    "drive_write_file_content":       "drive.write_file",
    "drive_write_doc_content":        "drive.write_doc",
    "drive_upload_file":              "drive.upload_file",
    "drive_move_file":                "drive.move_file",
    "drive_add_comment":              "drive.comment_file",
    "drive_sheets_get_values":        "sheets.read_values",
    "drive_sheets_write_range":       "sheets.write_range",
    "drive_sheets_add_sheet":         "sheets.add_sheet",
    "drive_sheets_rename_sheet":      "sheets.rename_sheet",
    "drive_sheets_format_range":      "sheets.format_range",
    "drive_sheets_insert_dimensions": "sheets.insert_dimensions",
    "drive_sheets_delete_dimensions": "sheets.delete_dimensions",
    "drive_docs_edit_content":        "docs.edit_content",
    "drive_docs_format_content":      "docs.format_content",
    "slack_get_channel_history":      "slack.read_messages",
    "slack_get_thread_replies":       "slack.read_messages",
    "slack_search_messages":          "slack.read_messages",
    "slack_send_message":             "slack.send_message",
    "calendar_get_event_details":     "calendar.read_event_details",
    "calendar_create_event":          "calendar.create_modify_event",
    "calendar_update_event":          "calendar.create_modify_event",
    "calendar_create_out_of_office":  "calendar.out_of_office",
    "calendar_set_working_location":  "calendar.working_location",
    "calendar_set_event_visibility":  "calendar.set_visibility",
    "salesforce_get_record":          "salesforce.read_record",
    "salesforce_run_report":          "salesforce.run_report",
    "salesforce_search":              "salesforce.search",
    "contacts_update":                "contacts.edit",
    "contacts_create":                "contacts.create",
    "contacts_add_label":             "contacts.add_label",
    "contacts_remove_label":          "contacts.remove_label",
    "jira_get_issue":                 "jira.read_issue",
    "jira_create_issue":              "jira.create_issue",
    "jira_add_comment":               "jira.add_comment",
    "jira_update_issue":              "jira.update_issue",
    "jira_transition_issue":          "jira.transition_issue",
    "confluence_get_page":            "confluence.read_page",
    "confluence_get_page_by_title":   "confluence.read_page",
    "confluence_create_page":         "confluence.create_page",
    "confluence_update_page":         "confluence.update_page",
    "telegram_get_messages":          "telegram.read_chat_messages",
    "telegram_search_messages":       "telegram.search_messages",
    "telegram_send_message":          "telegram.send_message",
    "tasks_create_task":              "tasks.create_task",
    "tasks_update_task":              "tasks.update_task",
    "tasks_complete_task":            "tasks.complete_task",
    "tasks_uncomplete_task":          "tasks.uncomplete_task",
    "tasks_move_task":                "tasks.move_task",
}

# Maps tool name -> the gate it goes through: "auto" (never reaches gated_call
# at all), "review" (gated_call gate="review", the read direction), or
# "popup" (gated_call gate="popup", the write direction). Unlike
# TOOL_TO_OPERATION, this covers every tool, including the unconditionally
# auto-accepted ones, since a preflight check needs a definitive answer for
# those too.
#
# This is the single source of truth the connector tables in
# docs/TECHNICAL_REFERENCE.md are checked against (see
# tests/unit/connectors/test_readme_manifest_alignment.py) -- keep it in sync
# with the gate= argument each connectors/*.py call site actually passes to
# gated_call(), not just with the docs.
TOOL_TO_GATE: dict[str, str] = {
    # Gmail
    "gmail_list_messages":             "auto",
    "gmail_list_threads":              "auto",
    "gmail_get_message":               "review",
    "gmail_get_thread":                "review",
    "gmail_list_message_attachments":  "auto",
    "gmail_download_attachment":       "review",
    "gmail_create_draft":              "popup",
    "gmail_reply_draft":               "popup",
    "gmail_reply_all_draft":           "popup",
    "gmail_add_label":                 "popup",
    "gmail_remove_label":              "popup",
    "gmail_archive_message":           "popup",
    "gmail_list_filters":              "auto",
    "gmail_list_labels":               "auto",
    "gmail_create_filter":             "popup",
    "gmail_update_filter":             "popup",
    "gmail_create_label":              "popup",
    # Google Drive (incl. Sheets)
    "drive_list_files":                "auto",
    "drive_get_file_metadata":         "auto",
    "drive_list_folder":               "auto",
    "drive_list_shared_drives":        "auto",
    "drive_create_blank_file":         "auto",
    "drive_get_file_content":          "review",
    "drive_download_file":             "review",
    "drive_write_file_content":        "popup",
    "drive_upload_file":               "popup",
    "drive_write_doc_content":         "popup",
    "drive_move_file":                 "popup",
    "drive_add_comment":               "popup",
    "drive_sheets_create":             "auto",
    "drive_sheets_get_metadata":       "auto",
    "drive_sheets_get_values":         "review",
    "drive_sheets_write_range":        "popup",
    "drive_sheets_add_sheet":          "popup",
    "drive_sheets_rename_sheet":       "popup",
    "drive_sheets_format_range":       "popup",
    "drive_sheets_insert_dimensions":  "popup",
    "drive_sheets_delete_dimensions":  "popup",
    "drive_docs_edit_content":         "popup",
    "drive_docs_format_content":       "popup",
    # Slack
    "slack_list_channels":             "auto",
    "slack_get_channel_history":       "review",
    "slack_get_thread_replies":        "review",
    "slack_search_messages":           "review",
    "slack_send_message":              "popup",
    # Google Calendar
    "calendar_list_calendars":         "auto",
    "calendar_list_events":            "auto",
    "calendar_get_free_busy":          "auto",
    "calendar_list_rooms":             "auto",
    "calendar_get_event_visibility":   "auto",
    "calendar_get_event_details":      "review",
    "calendar_create_event":           "popup",
    "calendar_update_event":           "popup",
    "calendar_create_out_of_office":   "popup",
    "calendar_set_working_location":   "popup",
    "calendar_set_event_visibility":   "popup",
    # Google Contacts
    "contacts_list":                   "auto",
    "contacts_search":                 "auto",
    "contacts_get":                    "auto",
    "contacts_update":                 "popup",
    "contacts_create":                 "popup",
    "contacts_add_label":              "popup",
    "contacts_remove_label":           "popup",
    # Telegram
    "telegram_list_chats":             "auto",
    "telegram_get_messages":           "review",
    "telegram_search_messages":        "review",
    "telegram_send_message":           "popup",
    # Salesforce
    "salesforce_list_reports":         "auto",
    "salesforce_get_record":           "review",
    "salesforce_run_report":           "review",
    "salesforce_search":               "review",
    # Jira
    "jira_list_projects":              "auto",
    "jira_search_issues":              "auto",
    "jira_get_transitions":            "auto",
    "jira_get_issue":                  "review",
    "jira_create_issue":               "popup",
    "jira_add_comment":                "popup",
    "jira_update_issue":               "popup",
    "jira_transition_issue":           "popup",
    # Confluence
    "confluence_list_spaces":          "auto",
    "confluence_search":               "auto",
    "confluence_cql_search":           "auto",
    "confluence_list_pages":           "auto",
    "confluence_get_page":             "review",
    "confluence_get_page_by_title":    "review",
    "confluence_create_page":          "popup",
    "confluence_update_page":          "popup",
    # Google Tasks
    "tasks_list_task_lists":           "auto",
    "tasks_list_tasks":                "auto",
    "tasks_get_task":                  "auto",
    "tasks_create_task":               "popup",
    "tasks_update_task":               "popup",
    "tasks_complete_task":             "popup",
    "tasks_uncomplete_task":           "popup",
    "tasks_move_task":                 "popup",
}

# Every _rule_* method on AutoAcceptEvaluator classified into exactly one of
# these two sets -- see test_auto_accept.py::test_every_rule_is_classified,
# which enforces that a newly added rule can't be left unclassified.
#
# ARGS_ONLY_RULES only ever reads ctx.args, never ctx.raw_data, so it can be
# evaluated correctly before anything is fetched -- this is what
# AutoAcceptEvaluator.preflight_from_args() (used by privacyfence_check_policy)
# is allowed to run ahead of time.
#
# DATA_DEPENDENT_RULES read ctx.raw_data, i.e. the actual fetched object (a
# message, a file, an event...). A rule belongs here even if it *looks* like
# it could degenerately return a verdict with raw_data missing -- several of
# these are "absence" checks (no_attachments, shared_drive_exclusion,
# no_conferencing_link, no_file_attachments, no_external_attendees) that
# would silently evaluate to a false "matched" the moment raw_data is None,
# since an empty/missing attribute reads the same as a genuinely absent one.
# Preflighting these would produce a confidently wrong "auto_accept" verdict,
# which is worse than admitting "unknown" -- so they stay data-dependent.
ARGS_ONLY_RULES: frozenset[str] = frozenset({
    "approved_spreadsheet",
    "dm_with_myself",
    "send_to_myself",
    "approved_channel",
    "approved_recipient",
    "reply_in_existing_thread",
    "personal_calendar",
    "approved_object_types",
    "approved_report_ids",
    "to_is_myself",
    "approved_recipient_domain",
    "label_name_allowlist",
    "parent_folder_allowlist",
    "no_contact_info_change",
    "approved_project_keys",
    "approved_chats",
    "approved_task_list",
    "approved_space_keys",  # checks ctx.args first; falls back to raw_data
                             # only when args lacks space_key, and that
                             # fallback degrades safely (empty, not a false
                             # match) when raw_data is unavailable.
})

DATA_DEPENDENT_RULES: frozenset[str] = frozenset({
    "i_am_sender",
    "i_am_sole_recipient",
    "trusted_sender_domain",
    "label_match",
    "age_threshold_days",
    "no_attachments",
    "i_am_owner",
    "created_by_me",
    "approved_folder",
    "approved_sandbox_folder",
    "move_within_approved_folders",
    "file_type_allowlist",
    "created_this_session",
    "shared_drive_exclusion",
    "public_channels_only",
    "no_file_attachments",
    "i_am_organizer",
    "no_external_attendees",
    "past_event",
    "time_window_days",
    "no_conferencing_link",
    "i_am_reporter",
    "i_am_assignee",
    "i_am_author",
    "no_media_attachments",
    # non_private_event is *args-derivable for one of its two operations*
    # (calendar.set_visibility, whose args always carry "visibility") but
    # not the other (calendar.read_event_details, which has no such arg and
    # falls back to ctx.raw_data.visibility) -- with raw_data=None that
    # fallback silently defaults to "default" (not private), a false
    # "matched" for a read whose real visibility is unknown. Classification
    # here is per rule name, not per (operation, rule), so this stays
    # data-dependent globally: calendar.set_visibility gets a conservative
    # "unknown" from preflight rather than risk read_event_details getting a
    # false "auto_accept".
    "non_private_event",
})

@dataclass
class ReviewContext:
    connector: str
    tool: str
    args: dict
    raw_data: Any
    my_email: str = ""
    my_domain: str = field(init=False)
    session_created_ids: set = field(default_factory=set)

    def __post_init__(self):
        self.my_domain = self.my_email.split("@", 1)[-1] if "@" in self.my_email else ""

class AutoAcceptEvaluator:
    def __init__(self, rules_config: dict[str, list[dict]]) -> None:
        self._rules = rules_config or {}
        # (operation_key, file_key) -> monotonic expiry. In-memory only, by
        # design: it lives and dies with this evaluator instance (i.e. with
        # the daemon process), unlike the YAML-backed rules above.
        self._temp_accepts: dict[tuple[str, str], float] = {}
        self._temp_accepts_lock = threading.Lock()

    def should_auto_accept(self, operation_key: str, ctx: ReviewContext) -> tuple[bool, str]:
        """Return (should_auto_accept, matched_rule_name)."""
        for rule_cfg in self._rules.get(operation_key) or []:
            rule_name = rule_cfg.get("rule", "")
            value = rule_cfg.get("value")
            try:
                if self._evaluate(rule_name, value, ctx):
                    logger.info("Auto-accept: op=%r matched rule=%r", operation_key, rule_name)
                    return True, rule_name
            except Exception as exc:
                logger.warning("Rule %r evaluation error: %s", rule_name, exc)
        if self._is_temp_accepted(operation_key, temp_accept_key(operation_key, ctx)):
            logger.info("Auto-accept: op=%r matched rule=%r", operation_key, "session_temp_accept")
            return True, "session_temp_accept"
        return False, ""

    def preflight_from_args(
        self, operation_key: str, args: dict, my_email: str = ""
    ) -> tuple[str, str, str]:
        """Predict should_auto_accept()'s outcome without fetching anything.

        Backs privacyfence_check_policy: Claude calls this before making a
        gated call, to find out whether it would need a human. Returns
        (verdict, matched_rule, reason):

          verdict="auto_accept"     -- a temp-accept or an ARGS_ONLY_RULES
                                        match already decides this; the real
                                        call will auto-accept identically.
          verdict="requires_review" -- every configured rule for this
                                        operation is in ARGS_ONLY_RULES and
                                        none matched, so fetching the real
                                        data cannot change the answer.
          verdict="unknown"         -- at least one configured rule needs
                                        ctx.raw_data (see DATA_DEPENDENT_RULES)
                                        and none of the args-only rules
                                        matched first; the real call might
                                        still auto-accept once the item is
                                        fetched, or might not.

        Never touches an external API, opens a popup, or mutates state --
        ctx.raw_data is always None here, which is why only ARGS_ONLY_RULES
        members are safe to evaluate (see that set's docstring for why the
        rest can't be, even speculatively).
        """
        ctx = ReviewContext(connector="", tool="", args=args or {}, raw_data=None, my_email=my_email)

        file_key = temp_accept_key(operation_key, ctx)
        if self._is_temp_accepted(operation_key, file_key):
            return "auto_accept", "session_temp_accept", "Matched an active \"Allow for 5 min\" window."

        configured = self._rules.get(operation_key) or []
        if not configured:
            return "requires_review", "", "No auto-accept rule is configured for this operation."

        undetermined: list[str] = []
        for rule_cfg in configured:
            rule_name = rule_cfg.get("rule", "")
            if rule_name not in ARGS_ONLY_RULES:
                undetermined.append(rule_name)
                continue
            value = rule_cfg.get("value")
            try:
                if self._evaluate(rule_name, value, ctx):
                    return "auto_accept", rule_name, f"Matched args-only rule {rule_name!r}."
            except Exception as exc:
                logger.warning("Preflight rule %r evaluation error: %s", rule_name, exc)

        if undetermined:
            return (
                "unknown",
                "",
                "Depends on the fetched item's data, not just this call's arguments "
                "(rule(s): " + ", ".join(sorted(set(undetermined))) + ").",
            )
        return "requires_review", "", "No configured rule matches these arguments."

    def register_temp_accept(
        self, operation_key: str, file_key: str, ttl_seconds: float = TEMP_ACCEPT_TTL_SECONDS
    ) -> None:
        """Grant a temporary, in-memory auto-accept for one file, for ``ttl_seconds``."""
        with self._temp_accepts_lock:
            self._temp_accepts[(operation_key, file_key)] = time.monotonic() + ttl_seconds

    def _is_temp_accepted(self, operation_key: str, file_key: str | None) -> bool:
        if file_key is None:
            return False
        key = (operation_key, file_key)
        with self._temp_accepts_lock:
            expiry = self._temp_accepts.get(key)
            if expiry is None:
                return False
            if time.monotonic() >= expiry:
                del self._temp_accepts[key]
                return False
            return True

    def _evaluate(self, rule_name: str, value: Any, ctx: ReviewContext) -> bool:
        fn = getattr(self, f"_rule_{rule_name}", None)
        if fn is None:
            logger.warning("Unknown auto-accept rule: %r", rule_name)
            return False
        return fn(value, ctx)

    # ── Gmail ──────────────────────────────────────────────────────────────

    def _rule_i_am_sender(self, _v, ctx):
        sender = getattr(ctx.raw_data, "sender", "") or ""
        return bool(ctx.my_email and ctx.my_email.lower() in sender.lower())

    def _rule_i_am_sole_recipient(self, _v, ctx):
        recips = getattr(ctx.raw_data, "recipients", []) or []
        return len(recips) == 1 and bool(ctx.my_email) and ctx.my_email.lower() in recips[0].lower()

    def _rule_trusted_sender_domain(self, value, ctx):
        if not value:
            return False
        raw_sender = getattr(ctx.raw_data, "sender", "") or ""
        email_part = raw_sender
        if "<" in raw_sender and ">" in raw_sender:
            email_part = raw_sender[raw_sender.index("<") + 1 : raw_sender.index(">")]
        domain = email_part.split("@", 1)[-1].lower().strip()
        allowed = {d.lower().strip() for d in (value if isinstance(value, list) else [value])}
        # Matches subdomains too (mail.trusted.com under "trusted.com") since
        # senders routinely mail from a subdomain of their real domain. The
        # "." separator keeps "eviltrusted.com" from matching "trusted.com".
        return any(domain == d or domain.endswith("." + d) for d in allowed)

    def _rule_label_match(self, value, ctx):
        if not value:
            return False
        labels = {l.lower() for l in (getattr(ctx.raw_data, "labels", []) or [])}
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        return bool(labels & allowed)

    def _rule_age_threshold_days(self, value, ctx):
        if not value:
            return False
        date_str = getattr(ctx.raw_data, "date", "") or ""
        if not date_str:
            return False
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days >= int(value)
        except Exception:
            return False

    def _rule_no_attachments(self, _v, ctx):
        return len(getattr(ctx.raw_data, "attachments", []) or []) == 0

    # ── Drive ─────────────────────────────────────────────────────────────

    def _file_from(self, raw):
        if isinstance(raw, dict):
            return raw.get("file", raw)
        return raw.file if hasattr(raw, "file") else raw

    def _rule_i_am_owner(self, _v, ctx):
        f = self._file_from(ctx.raw_data)
        owners = getattr(f, "owners", []) or []
        return bool(ctx.my_email and any(ctx.my_email.lower() in o.lower() for o in owners))

    def _rule_created_by_me(self, v, ctx):
        return self._rule_i_am_owner(v, ctx)

    def _rule_approved_folder(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        f = self._file_from(ctx.raw_data)
        parents = getattr(f, "parent_ids", []) or []
        return bool(set(parents) & allowed)

    def _rule_approved_sandbox_folder(self, value, ctx):
        return self._rule_approved_folder(value, ctx)

    def _rule_move_within_approved_folders(self, value, ctx):
        return self._rule_approved_folder(value, ctx)

    def _rule_file_type_allowlist(self, value, ctx):
        if not value:
            return False
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        f = self._file_from(ctx.raw_data)
        return (getattr(f, "mime_type", "") or "").lower() in allowed

    def _rule_created_this_session(self, _v, ctx):
        f = self._file_from(ctx.raw_data)
        return getattr(f, "id", "") in ctx.session_created_ids

    def _rule_shared_drive_exclusion(self, _v, ctx):
        # Never auto-accept shared drive files
        f = self._file_from(ctx.raw_data)
        return not getattr(f, "shared", False)

    # ── Drive: Sheets ────────────────────────────────────────────────────

    def _rule_approved_spreadsheet(self, value, ctx):
        """Match a specific spreadsheet, optionally narrowed to one tab.

        Each entry is {"spreadsheet_id": "...", "tab": "..."} — "tab" is
        optional (its absence approves every tab of that spreadsheet).
        Entries without a matching spreadsheet_id never match; an entry with
        a tab only matches calls whose current tab is known and equal.
        """
        if not value:
            return False
        entries = value if isinstance(value, list) else [value]
        spreadsheet_id = ctx.args.get("spreadsheet_id", "") or ""
        if not spreadsheet_id:
            return False
        current_tab = _sheet_tab_of(ctx)
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("spreadsheet_id") != spreadsheet_id:
                continue
            tab = entry.get("tab")
            if not tab:
                return True
            if current_tab and tab.lower() == current_tab.lower():
                return True
        return False

    # ── Slack ─────────────────────────────────────────────────────────────

    def _rule_dm_with_myself(self, _v, ctx):
        cid = ctx.args.get("channel_id", "") or ""
        return cid.startswith("D")

    def _rule_send_to_myself(self, v, ctx):
        return self._rule_dm_with_myself(v, ctx)

    def _rule_approved_channel(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        cid = ctx.args.get("channel_id", "") or ctx.args.get("channel", "") or ""
        return cid in allowed

    def _rule_approved_recipient(self, value, ctx):
        return self._rule_approved_channel(value, ctx)

    def _rule_public_channels_only(self, _v, ctx):
        raw = ctx.raw_data
        items = raw if isinstance(raw, list) else [raw]
        return all(not getattr(m, "is_private", True) for m in items)

    def _rule_no_file_attachments(self, _v, ctx):
        raw = ctx.raw_data
        items = raw if isinstance(raw, list) else [raw]
        return all(not (getattr(m, "files", None)) for m in items)

    def _rule_reply_in_existing_thread(self, _v, ctx):
        return bool(ctx.args.get("thread_ts"))

    # ── Calendar ──────────────────────────────────────────────────────────

    def _rule_i_am_organizer(self, _v, ctx):
        raw = ctx.raw_data
        organizer = (raw.get("organizer_email") if isinstance(raw, dict) else getattr(raw, "organizer_email", "")) or ""
        return bool(ctx.my_email) and ctx.my_email.lower() == organizer.lower()

    def _rule_no_external_attendees(self, _v, ctx):
        if not ctx.my_domain:
            return False
        raw = ctx.raw_data
        attendees = (raw.get("attendees") if isinstance(raw, dict) else getattr(raw, "attendees", None)) or []
        return all(ctx.my_domain in _attendee_email(a) for a in attendees)

    def _rule_personal_calendar(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        return ctx.args.get("calendar_id", "") in allowed

    def _rule_past_event(self, _v, ctx):
        end_str = getattr(ctx.raw_data, "end_time", "") or ""
        if not end_str:
            return False
        try:
            dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt < datetime.now(timezone.utc)
        except Exception:
            return False

    def _rule_time_window_days(self, value, ctx):
        if not value:
            return False
        start_str = getattr(ctx.raw_data, "start_time", "") or ""
        if not start_str:
            return False
        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_ahead = (dt - datetime.now(timezone.utc)).days
            return 0 <= days_ahead <= int(value)
        except Exception:
            return False

    def _rule_no_conferencing_link(self, _v, ctx):
        raw = ctx.raw_data
        return not bool(getattr(raw, "conference_link", "") or getattr(raw, "hangout_link", ""))

    def _rule_non_private_event(self, _v, ctx):
        """Auto-accept when the event involved is not marked private.

        calendar_set_event_visibility's args always carry the visibility
        being requested -- that's the state being approved, not whatever the
        event's prior visibility was. Every other calendar operation this
        applies to (currently calendar_get_event_details) has no
        "visibility" arg, so it falls back to the event's current
        visibility on ctx.raw_data.
        """
        visibility = ctx.args.get("visibility")
        if visibility is None:
            visibility = getattr(ctx.raw_data, "visibility", None)
        return (visibility or "default") != "private"

    # ── Salesforce ────────────────────────────────────────────────────────

    def _rule_approved_object_types(self, value, ctx):
        """Match a single approved object_type (salesforce.read_record) or,
        for salesforce.search's comma-separated object_types, only when
        every object type the search actually touches is on the allowlist —
        a partial match would auto-accept a search that also reaches an
        unapproved object type."""
        if not value:
            return False
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        if "object_types" in ctx.args:
            requested = [t.strip().lower() for t in (ctx.args.get("object_types") or "").split(",") if t.strip()]
            return bool(requested) and all(t in allowed for t in requested)
        return ctx.args.get("object_type", "").lower() in allowed

    def _rule_approved_report_ids(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        return ctx.args.get("report_id", "") in allowed

    # ── Gmail (writes) ───────────────────────────────────────────────────

    def _rule_to_is_myself(self, _v, ctx):
        to = ctx.args.get("to", "") or ""
        # gmail_reply_all_draft passes the full expanded audience (a list) so
        # this only matches if every recipient is you; plain drafts and
        # single replies pass a lone "to" string.
        recipients = to if isinstance(to, list) else [to]
        recipients = [r for r in recipients if r]
        return bool(ctx.my_email) and bool(recipients) and all(ctx.my_email.lower() in r.lower() for r in recipients)

    def _rule_approved_recipient_domain(self, value, ctx):
        if not value:
            return False
        to = ctx.args.get("to", "") or ""
        # See _rule_to_is_myself: reply-all passes a list of every recipient
        # it will actually reach, not just the original sender, so this rule
        # can't be satisfied by a trusted sender while an external Cc slips
        # through unauthorized.
        recipients = to if isinstance(to, list) else [to]
        recipients = [r for r in recipients if r]
        allowed = {d.lower().strip() for d in (value if isinstance(value, list) else [value])}
        return bool(recipients) and all(_domain_of(r) in allowed for r in recipients)

    def _rule_label_name_allowlist(self, value, ctx):
        if not value:
            return False
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        label = (ctx.args.get("label_name") or "").lower()
        return label in allowed

    # ── Drive (writes) ───────────────────────────────────────────────────

    def _rule_parent_folder_allowlist(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        return (ctx.args.get("parent_folder_id") or "") in allowed

    # ── Contacts ──────────────────────────────────────────────────────────

    def _rule_no_contact_info_change(self, _v, ctx):
        return not (ctx.args.get("emails") or ctx.args.get("phones"))

    # ── Jira ──────────────────────────────────────────────────────────────

    def _rule_approved_project_keys(self, value, ctx):
        if not value:
            return False
        allowed = {v.upper() for v in (value if isinstance(value, list) else [value])}
        project_key = ctx.args.get("project_key", "") or ""
        if not project_key:
            issue_key = ctx.args.get("issue_key", "") or ""
            project_key = issue_key.split("-")[0] if "-" in issue_key else ""
        return bool(project_key) and project_key.upper() in allowed

    def _rule_i_am_reporter(self, _v, ctx):
        raw = ctx.raw_data
        reporter = (raw.get("reporter") if isinstance(raw, dict) else getattr(raw, "reporter", "")) or ""
        return bool(ctx.my_email) and ctx.my_email.lower() in reporter.lower()

    def _rule_i_am_assignee(self, _v, ctx):
        raw = ctx.raw_data
        assignee = (raw.get("assignee") if isinstance(raw, dict) else getattr(raw, "assignee", "")) or ""
        return bool(ctx.my_email) and ctx.my_email.lower() in assignee.lower()

    # ── Confluence ────────────────────────────────────────────────────────

    def _rule_approved_space_keys(self, value, ctx):
        if not value:
            return False
        allowed = {v.upper() for v in (value if isinstance(value, list) else [value])}
        raw = ctx.raw_data
        space_key = ctx.args.get("space_key") or (raw.get("space_key") if isinstance(raw, dict) else "") or ""
        return bool(space_key) and space_key.upper() in allowed

    def _rule_i_am_author(self, _v, ctx):
        raw = ctx.raw_data
        author = (raw.get("author") if isinstance(raw, dict) else getattr(raw, "author", "")) or ""
        return bool(ctx.my_email) and ctx.my_email.lower() in author.lower()

    # ── Telegram ──────────────────────────────────────────────────────────

    def _rule_approved_chats(self, value, ctx):
        if not value:
            return False
        allowed = {str(v) for v in (value if isinstance(value, list) else [value])}
        return str(ctx.args.get("chat_id", "")) in allowed

    def _rule_no_media_attachments(self, _v, ctx):
        raw = ctx.raw_data
        items = raw if isinstance(raw, list) else [raw]
        return all(not getattr(m, "media_type", "") for m in items)

    # ── Tasks ─────────────────────────────────────────────────────────────

    def _rule_approved_task_list(self, value, ctx):
        """Match a task write scoped to an approved task list.

        create/update/complete/uncomplete carry a single `task_list_id`;
        `tasks_move_task` carries `source_list_id`/`destination_list_id`
        instead, and only matches when BOTH ends of the move are approved —
        otherwise a move could smuggle a task out of (or into) a list the
        user never approved.
        """
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        if "task_list_id" in ctx.args:
            return ctx.args.get("task_list_id", "") in allowed
        source = ctx.args.get("source_list_id", "")
        destination = ctx.args.get("destination_list_id", "")
        return bool(source) and bool(destination) and source in allowed and destination in allowed


# ── Rule suggestion for the popup's "Always allow" button ────────────────────

def _domain_of(sender: str) -> str:
    email_part = sender
    if "<" in sender and ">" in sender:
        email_part = sender[sender.index("<") + 1 : sender.index(">")]
    return email_part.split("@", 1)[-1].lower().strip()


def _sheet_tab_of(ctx: "ReviewContext") -> str:
    """Identify the tab a sheets call touches, for the approved_spreadsheet rule.

    rename_sheet/format_range pass a numeric sheet_id directly; read_values/
    write_range only have it embedded as the sheet-name prefix of range_a1
    (e.g. "Sheet1!A1:C10" or "'My Tab'!A1:C10"); add_sheet has no existing
    tab to identify. sheet_id is checked first since format_range carries
    both sheet_id and a range_a1 with no "!" prefix.
    """
    if "sheet_id" in ctx.args:
        return str(ctx.args["sheet_id"])
    range_a1 = ctx.args.get("range_a1") or ""
    tab, sep, _ = range_a1.partition("!")
    return tab.strip("'") if sep else ""


def temp_accept_key(operation_key: str, ctx: "ReviewContext") -> str | None:
    """The file identity a temp accept for this operation would be scoped to.

    Returns None when the operation isn't eligible for "Allow for 5 min"
    (see TEMP_ACCEPT_ELIGIBLE_OPERATIONS) or the expected arg is missing —
    either way, gate.py takes that as "don't offer the button."
    """
    arg_name = TEMP_ACCEPT_ELIGIBLE_OPERATIONS.get(operation_key)
    if not arg_name:
        return None
    value = ctx.args.get(arg_name)
    return str(value) if value else None


def _attendee_email(attendee: Any) -> str:
    """Extract an email address from an attendee, whichever shape it's in.

    calendar_get_event_details passes real Attendee dicts/objects with an
    "email" field; calendar_create_event/update_event pass plain email
    strings (parsed from a comma-separated arg) since the event doesn't
    exist yet.
    """
    if isinstance(attendee, dict):
        return attendee.get("email", "") or ""
    if isinstance(attendee, str):
        return attendee
    return getattr(attendee, "email", "") or ""


def suggest_rule(operation_key: str, ctx: ReviewContext) -> tuple[str, Any] | None:
    """Propose one auto-accept rule from the current item's attributes.

    Returns (rule_name, value) — value is None for rules that take none —
    or None if nothing sensible can be suggested for this operation. The
    popup only offers "Always allow" when this returns a suggestion, so the
    button never proposes a rule broader than what the item itself supports.
    """
    if operation_key in ("gmail.read_message", "gmail.read_thread", "gmail.download_attachment", "gmail.archive_message"):
        sender = getattr(ctx.raw_data, "sender", "") or ""
        if ctx.my_email and ctx.my_email.lower() in sender.lower():
            return ("i_am_sender", None)
        domain = _domain_of(sender)
        return ("trusted_sender_domain", [domain]) if domain else None

    if operation_key in ("drive.read_file_contents", "drive.download_file"):
        f = ctx.raw_data.file if hasattr(ctx.raw_data, "file") else ctx.raw_data
        owners = getattr(f, "owners", []) or []
        if ctx.my_email and any(ctx.my_email.lower() in o.lower() for o in owners):
            return ("i_am_owner", None)
        parents = list(getattr(f, "parent_ids", []) or [])
        return ("approved_folder", parents) if parents else None

    if operation_key == "sheets.read_values":
        spreadsheet_id = ctx.args.get("spreadsheet_id", "") or ""
        if not spreadsheet_id:
            return None
        entry: dict[str, Any] = {"spreadsheet_id": spreadsheet_id}
        tab = _sheet_tab_of(ctx)
        if tab:
            entry["tab"] = tab
        return ("approved_spreadsheet", [entry])

    if operation_key == "slack.read_messages":
        cid = ctx.args.get("channel_id", "") or ctx.args.get("channel", "") or ""
        if cid.startswith("D"):
            return ("dm_with_myself", None)
        return ("approved_channel", [cid]) if cid else None

    if operation_key == "calendar.read_event_details":
        organizer = getattr(ctx.raw_data, "organizer_email", "") or ""
        if ctx.my_email and ctx.my_email.lower() == organizer.lower():
            return ("i_am_organizer", None)
        if ctx.my_domain:
            attendees = getattr(ctx.raw_data, "attendees", []) or []
            all_internal = all(ctx.my_domain in _attendee_email(a) for a in attendees)
            if all_internal:
                return ("no_external_attendees", None)
        visibility = getattr(ctx.raw_data, "visibility", "default") or "default"
        if visibility != "private":
            return ("non_private_event", None)
        return None

    if operation_key == "salesforce.read_record":
        object_type = ctx.args.get("object_type", "")
        return ("approved_object_types", [object_type]) if object_type else None

    if operation_key == "salesforce.search":
        object_types = [t.strip() for t in (ctx.args.get("object_types") or "").split(",") if t.strip()]
        # An unscoped search reaches Salesforce's whole default set of
        # globally-searchable objects -- too broad to derive a narrow rule
        # from, the same reasoning gmail_create_filter has no rule at all.
        return ("approved_object_types", object_types) if object_types else None

    if operation_key == "jira.read_issue":
        raw = ctx.raw_data
        reporter = (raw.get("reporter") if isinstance(raw, dict) else getattr(raw, "reporter", "")) or ""
        if ctx.my_email and ctx.my_email.lower() in reporter.lower():
            return ("i_am_reporter", None)
        assignee = (raw.get("assignee") if isinstance(raw, dict) else getattr(raw, "assignee", "")) or ""
        if ctx.my_email and ctx.my_email.lower() in assignee.lower():
            return ("i_am_assignee", None)
        issue_key = ctx.args.get("issue_key", "") or ""
        project_key = issue_key.split("-")[0] if "-" in issue_key else ""
        return ("approved_project_keys", [project_key]) if project_key else None

    if operation_key == "confluence.read_page":
        raw = ctx.raw_data
        author = (raw.get("author") if isinstance(raw, dict) else getattr(raw, "author", "")) or ""
        if ctx.my_email and ctx.my_email.lower() in author.lower():
            return ("i_am_author", None)
        space_key = (
            (raw.get("space_key") if isinstance(raw, dict) else getattr(raw, "space_key", ""))
            or ctx.args.get("space_key", "")
        )
        return ("approved_space_keys", [space_key]) if space_key else None

    if operation_key == "telegram.read_chat_messages":
        chat_id = ctx.args.get("chat_id", "")
        return ("approved_chats", [str(chat_id)]) if chat_id != "" else None

    return None


def known_rule_names() -> frozenset[str]:
    """Every rule name AutoAcceptEvaluator actually knows how to evaluate --
    the `_rule_*` methods it dispatches to in `_evaluate()`. `_evaluate()`
    itself only logs a warning and treats an unrecognized name as a
    non-match (see its docstring), so a rule persisted under a misspelled
    or invalid name doesn't error, it just silently never matches anything.
    That's fine for a name that was always typed by hand into settings.yaml
    by whoever wrote the rule, but gate.propose_rule_change() lets Claude
    supply rule_name directly (unlike the "Always allow" flow, which only
    ever offers names suggest_rule() itself produces), so it validates
    against this set before ever showing a confirmation popup -- the same
    "don't ship an unreachable rule silently" principle menu_bar.py's
    TestRuleUiCompleteness applies to the UI side.
    """
    return frozenset(
        name[len("_rule_"):]
        for name in vars(AutoAcceptEvaluator)
        if name.startswith("_rule_") and callable(getattr(AutoAcceptEvaluator, name))
    )


_RULE_DESCRIPTIONS: dict[str, str] = {
    "i_am_sender":           "Gmail message/thread reads where you are the sender",
    "trusted_sender_domain": "Gmail message/thread reads from senders at: {value}",
    "i_am_owner":            "Drive file reads for files you own",
    "approved_folder":       "Drive file reads for files in folder(s): {value}",
    "dm_with_myself":        "Slack reads in your own DM channel",
    "approved_channel":      "Slack reads in channel(s): {value}",
    "i_am_organizer":        "Calendar event reads for events you organize",
    "no_external_attendees": "Calendar event reads with no external attendees",
    "non_private_event":     "Calendar event reads/writes where the event is not marked private",
    "approved_object_types": "Salesforce record reads for object type(s): {value}",
    "i_am_reporter":         "Jira issue reads where you are the reporter",
    "i_am_assignee":         "Jira issue reads where you are the assignee",
    "approved_project_keys": "Jira issue reads in project(s): {value}",
    "i_am_author":           "Confluence page reads where you are the author",
    "approved_space_keys":   "Confluence page reads in space(s): {value}",
    "approved_chats":        "Telegram chat reads in chat(s): {value}",
    "approved_spreadsheet":  "Sheets calls scoped to: {value}",
}


def _format_spreadsheet_entry(entry: Any) -> str:
    if not isinstance(entry, dict):
        return str(entry)
    tab = entry.get("tab")
    return f"{entry.get('spreadsheet_id', '')}" + (f" (tab: {tab})" if tab else "")


def _format_rule_value(value: Any) -> str:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return ", ".join(_format_spreadsheet_entry(v) for v in value)
    if isinstance(value, list):
        return ", ".join(value)
    return str(value)


def describe_rule(rule_name: str, value: Any) -> str:
    """Human-readable description of a proposed auto-accept rule."""
    template = _RULE_DESCRIPTIONS.get(rule_name, rule_name)
    return "Auto-accept future " + template.format(value=_format_rule_value(value))


def describe_rule_change(
    operation: str, operation_key: str, rule_name: str, value: Any = None, old_value: Any = None
) -> str:
    """Human-readable description of a bridge-proposed add/update/remove to
    ``auto_accept_rules``, shown in the confirmation popup gate.propose_rule_change()
    drives -- see that function's docstring. Unlike describe_rule() (used by
    the existing "Always allow" flow), this always names operation_key
    explicitly, since there's no popup already on screen for a specific item
    to supply that context.
    """
    if operation == "remove":
        suffix = f" = {_format_rule_value(value)}" if value is not None else " (every value)"
        return f"Remove auto-accept rule {rule_name!r}{suffix} from {operation_key!r}"
    if operation == "update":
        old_str = _format_rule_value(old_value) if old_value is not None else "(any existing value)"
        return (
            f"Replace auto-accept rule {rule_name!r} on {operation_key!r}: "
            f"{old_str} -> {_format_rule_value(value)}"
        )
    return f"Add auto-accept rule {rule_name!r} = {_format_rule_value(value)} to {operation_key!r}"


# ── Rule persistence (used by the "Always allow" popup button) ──────────────

_config_path: str | None = None
_write_lock = threading.Lock()


def init_config_path(path: str) -> None:
    """Register the on-disk config path so add_auto_accept_rule() can persist."""
    global _config_path
    _config_path = path


def add_auto_accept_rule(operation_key: str, rule_name: str, value: Any) -> None:
    """Append a rule to the config file on disk and hot-reload the evaluator.

    No-ops if an identical rule (same name and value) is already present for
    this operation, so confirming the same "Always allow" suggestion more
    than once doesn't pile up duplicate entries.
    """
    if _config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with _write_lock:
        with open(_config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        rules = cfg.setdefault("auto_accept_rules", {}).setdefault(operation_key, [])
        new_rule: dict[str, Any] = {"rule": rule_name}
        if value is not None:
            new_rule["value"] = value
        if new_rule in rules:
            return
        rules.append(new_rule)
        with open(_config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        reload_rules(build_effective_rules(cfg))


def remove_auto_accept_rule(operation_key: str, rule_name: str, value: Any = None) -> bool:
    """Remove auto-accept rule entries under operation_key matching rule_name.

    With value given, only removes an entry whose value matches exactly
    (mirroring add_auto_accept_rule's own equality check). With value left
    as None, removes every entry for this rule_name under operation_key
    regardless of its value. Returns True if anything was removed; only
    persists to disk and hot-reloads the evaluator when it does.
    """
    if _config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with _write_lock:
        with open(_config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        rules = cfg.get("auto_accept_rules", {}).get(operation_key, [])
        if value is None:
            remaining = [r for r in rules if r.get("rule") != rule_name]
        else:
            target = {"rule": rule_name, "value": value}
            remaining = [r for r in rules if r != target]
        if len(remaining) == len(rules):
            return False
        if remaining:
            cfg["auto_accept_rules"][operation_key] = remaining
        else:
            cfg.get("auto_accept_rules", {}).pop(operation_key, None)
        with open(_config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        reload_rules(build_effective_rules(cfg))
        return True


def get_current_config() -> dict[str, Any]:
    """Read-only snapshot of the persisted ``auto_accept_rules``/
    ``auto_accept_grants`` sections, straight from disk -- not the compiled/
    merged view build_effective_rules() produces. Callers that need to
    identify an existing entry to update or remove (e.g. the bridge's
    privacyfence_list_auto_accept_rules meta tool) want the raw, addressable
    shape, the same one add_auto_accept_rule/remove_auto_accept_rule and the
    menu bar's rule editor operate on.
    """
    if _config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with _write_lock:
        with open(_config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    return {
        "auto_accept_rules": cfg.get("auto_accept_rules") or {},
        "auto_accept_grants": cfg.get("auto_accept_grants") or {},
    }


def mutate_grants(mutator: Callable[[dict[str, Any]], bool]) -> bool:
    """Read settings.yaml, let `mutator` update it in place (typically its
    auto_accept_grants section via resource_grants.apply_grant_upsert/
    apply_grant_removal), and persist + hot-reload only if it reports an
    actual change.

    Shared plumbing for gate.py's propose_rule_change() grant add/update/
    remove paths -- keeps the read-modify-write-reload sequence and the
    write lock in one place rather than duplicating it per grant operation,
    the same way add_auto_accept_rule/remove_auto_accept_rule own that
    sequence for the auto_accept_rules side. `mutator` receives the full
    parsed config (not just the grants section) since resource_grants'
    helpers operate on it via cfg.setdefault("auto_accept_grants", {}).
    """
    if _config_path is None:
        raise RuntimeError("auto_accept config path not initialized")
    with _write_lock:
        with open(_config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        changed = mutator(cfg)
        if changed:
            with open(_config_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
            reload_rules(build_effective_rules(cfg))
        return changed


_INSTANCE: AutoAcceptEvaluator | None = None
_rules_changed_listener: Callable[[], None] | None = None


def get_auto_accept_evaluator() -> AutoAcceptEvaluator:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AutoAcceptEvaluator({})
    return _INSTANCE

def init_auto_accept_evaluator(rules_config: dict) -> AutoAcceptEvaluator:
    global _INSTANCE
    _INSTANCE = AutoAcceptEvaluator(rules_config)
    return _INSTANCE


def set_rules_changed_listener(callback: Callable[[], None] | None) -> None:
    """Register a callback fired whenever the live rule set changes.

    The menu bar uses this to refresh its menu (and the "Manage Auto-accept
    Rules…" window, if open) when a rule is created from the approval popup,
    which runs on the IPC server's own thread rather than the menu bar's
    main thread.
    """
    global _rules_changed_listener
    _rules_changed_listener = callback


def reload_rules(rules_config: dict) -> None:
    """Hot-reload rules into the live evaluator without restarting the daemon."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AutoAcceptEvaluator(rules_config)
    else:
        _INSTANCE._rules = rules_config or {}
    logger.info("Auto-accept rules reloaded live (%d operations)", len(_INSTANCE._rules))
    if _rules_changed_listener is not None:
        _rules_changed_listener()
