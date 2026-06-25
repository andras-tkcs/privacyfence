"""Auto-accept rule engine for the human review gate."""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Maps tool name → operation key used in settings.yaml
TOOL_TO_OPERATION: dict[str, str] = {
    "gmail_get_message":          "gmail.read_message",
    "gmail_get_thread":           "gmail.read_thread",
    "drive_get_file_content":     "drive.read_file_contents",
    "drive_write_file_content":   "drive.write_file",
    "drive_move_file":            "drive.move_file",
    "drive_add_comment":          "drive.comment_file",
    "slack_get_channel_history":  "slack.read_messages",
    "slack_get_thread_replies":   "slack.read_messages",
    "slack_search_messages":      "slack.read_messages",
    "slack_send_message":         "slack.send_message",
    "calendar_get_event_details": "calendar.read_event_details",
    "calendar_create_event":      "calendar.create_modify_event",
    "calendar_update_event":      "calendar.create_modify_event",
    "salesforce_get_record":      "salesforce.read_record",
    "salesforce_run_report":      "salesforce.run_report",
    "contacts_update":            "contacts.edit",
}

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

    def should_auto_accept(self, operation_key: str, ctx: ReviewContext) -> tuple[bool, str]:
        """Return (should_auto_accept, matched_rule_name)."""
        for rule_cfg in self._rules.get(operation_key, []):
            rule_name = rule_cfg.get("rule", "")
            value = rule_cfg.get("value")
            try:
                if self._evaluate(rule_name, value, ctx):
                    logger.info("Auto-accept: op=%r matched rule=%r", operation_key, rule_name)
                    return True, rule_name
            except Exception as exc:
                logger.warning("Rule %r evaluation error: %s", rule_name, exc)
        return False, ""

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
        return domain in allowed

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
        organizer = getattr(ctx.raw_data, "organizer_email", "") or ""
        return bool(ctx.my_email) and ctx.my_email.lower() == organizer.lower()

    def _rule_no_external_attendees(self, _v, ctx):
        if not ctx.my_domain:
            return False
        attendees = getattr(ctx.raw_data, "attendees", []) or []
        return all(
            ctx.my_domain in (a.get("email", "") if isinstance(a, dict) else getattr(a, "email", ""))
            for a in attendees
        )

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

    # ── Salesforce ────────────────────────────────────────────────────────

    def _rule_approved_object_types(self, value, ctx):
        if not value:
            return False
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        return ctx.args.get("object_type", "").lower() in allowed

    def _rule_approved_report_ids(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        return ctx.args.get("report_id", "") in allowed


_INSTANCE: AutoAcceptEvaluator | None = None

def get_auto_accept_evaluator() -> AutoAcceptEvaluator:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AutoAcceptEvaluator({})
    return _INSTANCE

def init_auto_accept_evaluator(rules_config: dict) -> AutoAcceptEvaluator:
    global _INSTANCE
    _INSTANCE = AutoAcceptEvaluator(rules_config)
    return _INSTANCE
