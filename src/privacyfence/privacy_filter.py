"""Category-based privacy filter: enforces the ``privacy`` / ``drive_privacy`` /
``slack_privacy`` sections of ``settings.yaml`` (see ``resources/settings.yaml.example``).

Historically these config sections were documentation of an intended policy that no code
ever actually read -- editing a category from ``allow`` to ``block`` changed nothing. This
module is that policy, made real: connectors call ``apply_text``/``apply_list`` on a
category's data *before* it reaches ``gated_call()``, so a category set to ``block``/
``redact`` never reaches the review UI, the audit log's ``filtered_data``, or Claude --
matching ``coding-and-testing-guidelines.md`` §1.5's "the privacy filter is a floor under
human review, not a substitute for it."

Scope, deliberately narrow: only the three connectors with a category schema documented in
``settings.yaml.example`` (Gmail via the top-level ``privacy`` group, Drive via
``drive_privacy``, Slack via ``slack_privacy``) -- other connectors have no such schema and
this module invents none for them. Within those three, only tools that return content read
from an external source (matching exactly which tools ``pii_detector.py`` scans -- see its
module docstring): write tools never pass through here, for the same reason they never pass
through the PII gate.

Policy values, applied per category:
  allow  -> value passed through unchanged.
  block  -> text becomes a fixed marker (``[BLOCKED BY PRIVACY FILTER]``); a list becomes
            empty. Never partial -- "block" means none of it reaches the caller.
  redact -> text becomes a length-revealing placeholder that discloses nothing about
            content (mirrors pii_detector.py's own rule: category labels may leave a
            module, matched substrings never do). List categories have no single obviously
            correct "partial" shape (unlike free text, a partially-redacted list of
            structured records -- attachments, channels, files -- has no canonical
            middle ground), so ``redact`` on a list-shaped category currently behaves
            identically to ``block``: it empties the list. Revisit if a future category
            needs finer-grained list redaction than allow/deny.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_VALID_POLICIES = ("allow", "redact", "block")
_BLOCK_MARKER = "[BLOCKED BY PRIVACY FILTER]"

# Keyed by group name ("privacy", "drive_privacy", "slack_privacy"), each value
# {"default_policy": str, "categories": {category: policy}}. Populated once at
# daemon startup by init_privacy_filter(); empty dict for any group not yet
# initialized resolves every category to "allow" (fail open on missing config,
# same posture pii_detector.py takes when disabled -- this module only ever
# narrows what already ships, it never adds a new default-block surface a
# pre-existing install didn't have).
_GROUPS: dict[str, dict[str, Any]] = {}


def init_privacy_filter(config: dict[str, Any]) -> None:
    """Parse ``privacy``/``drive_privacy``/``slack_privacy`` out of the loaded
    settings.yaml dict. Call once at daemon startup, same pattern as
    pii_detector.init_pii_detection()."""
    global _GROUPS
    _GROUPS = {
        "privacy": _parse_group(config.get("privacy")),
        "drive_privacy": _parse_group(config.get("drive_privacy")),
        "slack_privacy": _parse_group(config.get("slack_privacy")),
    }


def _parse_group(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    default_policy = raw.get("default_policy", "allow")
    if default_policy not in _VALID_POLICIES:
        logger.warning("Invalid default_policy %r; falling back to 'allow'", default_policy)
        default_policy = "allow"
    categories_raw = raw.get("categories")
    categories: dict[str, str] = {}
    if isinstance(categories_raw, dict):
        for category, policy in categories_raw.items():
            if policy in _VALID_POLICIES:
                categories[category] = policy
            else:
                logger.warning(
                    "Invalid policy %r for category %r; falling back to default_policy %r",
                    policy, category, default_policy,
                )
                categories[category] = default_policy
    return {"default_policy": default_policy, "categories": categories}


def category_policy(group: str, category: str) -> str:
    """The resolved allow/redact/block for one (group, category) pair -- the
    same lookup apply_text/apply_list use internally, exposed so the "AI will
    receive" review-UI checklist can render the real policy instead of
    re-deriving it."""
    g = _GROUPS.get(group, {"default_policy": "allow", "categories": {}})
    return g["categories"].get(category, g["default_policy"])


def apply_text(group: str, category: str, value: str) -> str:
    """Apply the resolved policy to a text value (a message body, a document's
    extracted text, a single metadata field, ...)."""
    if not value:
        return value
    policy = category_policy(group, category)
    if policy == "allow":
        return value
    if policy == "block":
        return _BLOCK_MARKER
    return _redact_text(value)


def _redact_text(value: str) -> str:
    n = len(value)
    return f"[REDACTED BY PRIVACY FILTER — {n} character{'s' if n != 1 else ''} withheld]"


def apply_list(group: str, category: str, items: list[Any]) -> list[Any]:
    """Apply the resolved policy to a list value (attachments, channels, files,
    folder entries, ...). See module docstring: redact and block are
    identical here -- both empty the list."""
    if not items:
        return items
    policy = category_policy(group, category)
    if policy == "allow":
        return items
    return []
