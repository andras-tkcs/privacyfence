"""Category-based privacy filter: enforces the ``privacy`` / ``drive_privacy`` /
``slack_privacy`` / ``contacts_privacy`` / ``tasks_privacy`` / ``confluence_privacy`` sections
of ``settings.yaml`` (see ``resources/settings.yaml.example``).

Historically these config sections were documentation of an intended policy that no code
ever actually read -- editing a category from ``allow`` to ``block`` changed nothing. This
module is that policy, made real: connectors call ``apply_text``/``apply_list`` on a
category's data *before* it reaches ``gated_call()``, so a category set to ``block``/
``redact`` never reaches the review UI, the audit log's ``filtered_data``, or Claude --
matching ``coding-and-testing-guidelines.md`` §1.5's "the privacy filter is a floor under
human review, not a substitute for it."

Scope, deliberately narrow: only the connectors with a category schema documented in
``settings.yaml.example`` -- Gmail via the top-level ``privacy`` group, Drive via
``drive_privacy``, Slack via ``slack_privacy``, Contacts via ``contacts_privacy``, Tasks via
``tasks_privacy``, Confluence via ``confluence_privacy`` -- other connectors have no such
schema and this module invents none for them. Within those, only tools that return content
read from an external source (matching exactly which tools ``pii_detector.py`` scans -- see
its module docstring): write tools never pass through here, for the same reason they never
pass through the PII gate.

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

from .principal import PrincipalRegistry

logger = logging.getLogger(__name__)

_VALID_POLICIES = ("allow", "redact", "block")
_BLOCK_MARKER = "[BLOCKED BY PRIVACY FILTER]"

_GROUP_NAMES = (
    "privacy", "drive_privacy", "slack_privacy",
    "contacts_privacy", "tasks_privacy", "confluence_privacy",
)

# Keyed by group name ("privacy", "drive_privacy", "slack_privacy",
# "contacts_privacy", "tasks_privacy", "confluence_privacy"), each value
# {"default_policy": str, "categories": {category: policy}}. Populated once
# per principal, at daemon startup (local mode) or on first use (any other
# principal -- see PrincipalRegistry.get()), by init_privacy_filter(); empty
# dict for any group not yet initialized resolves every category to "allow"
# (fail open on missing config, same posture pii_detector.py takes when
# disabled -- this module only ever narrows what already ships, it never
# adds a new default-block surface a pre-existing install didn't have). One
# dict per principal (P6), not
# one per process -- each user's own privacy policy, isolated the same way
# their auto-accept rules already are.
_REGISTRY: PrincipalRegistry[dict[str, dict[str, Any]]] = PrincipalRegistry(dict)


class PrivacyFilterConfigError(ValueError):
    """Raised by init_privacy_filter() for a privacy-filter settings.yaml
    section that is *present* but malformed -- not a dict, an unrecognised
    ``default_policy``, a non-dict ``categories``, or a category mapped to
    an unrecognised policy (SEC-07). Before this, all four cases silently
    fell back to "allow" -- meaning a typo'd policy value (or a config file
    an attacker could write to) turned "block" into "everything passes
    through" with nothing but a log line most installs never look at.

    A ``ValueError`` subclass for the same reason ``org_mode.
    ConfigurationError`` (SEC-04) is one: daemon_main.py's ``main()`` has no
    special handling for this class specifically, it falls into the same
    "print and refuse to start" path every other startup configuration
    error already takes. There is still exactly one tolerated case, kept
    for backward compatibility with installs that predate this module: a
    group key genuinely absent from settings.yaml altogether (see
    ``_parse_group``'s own docstring).
    """


def init_privacy_filter(config: dict[str, Any], *, org_managed: bool = False) -> None:
    """Parse ``privacy``/``drive_privacy``/``slack_privacy``/``contacts_privacy``/
    ``tasks_privacy``/``confluence_privacy`` out of the loaded settings.yaml dict.
    Call once at daemon startup, same pattern as pii_detector.init_pii_detection().

    Raises ``PrivacyFilterConfigError`` (SEC-07) for a group that's present
    but malformed -- see ``_parse_group``. ``org_managed`` (the caller's
    ``org_mode.resolve_mode(...) == "org"``) picks the fail-safe default
    used for a group that's genuinely absent from settings.yaml: "allow",
    unchanged, for a local-mode install (this module must never turn into a
    new default-block surface a pre-existing install didn't opt into), but
    "block" for an org-managed one -- an organization's centrally deployed
    settings.yaml is expected to state its own privacy policy explicitly,
    not silently inherit the permissive default nobody there configured.
    """
    fail_safe_default = "block" if org_managed else "allow"
    groups = {name: _parse_group(config.get(name), group=name, fail_safe_default=fail_safe_default)
              for name in _GROUP_NAMES}
    _REGISTRY.set(groups)


def _parse_group(raw: Any, *, group: str, fail_safe_default: str = "allow") -> dict[str, Any]:
    """Parse one group's raw settings.yaml value, failing closed (SEC-07) on
    anything present but malformed instead of falling back to "allow":

      - ``raw is None`` (the group key isn't in settings.yaml at all) is the
        one tolerated case, for backward compatibility with installs that
        predate this module -- resolves to ``fail_safe_default`` with no
        categories.
      - Anything else that isn't a dict, a ``default_policy`` present but
        not one of allow/redact/block, a ``categories`` present but not a
        dict, or a category mapped to a value that isn't one of
        allow/redact/block, all raise ``PrivacyFilterConfigError`` rather
        than silently downgrading to "allow".

    ``default_policy``/``categories`` genuinely absent *within* a present
    group dict are fine -- that's just a group that only sets one of the
    two -- and fall back to ``fail_safe_default``.
    """
    if raw is None:
        return {"default_policy": fail_safe_default, "categories": {}}
    if not isinstance(raw, dict):
        raise PrivacyFilterConfigError(
            f"settings.yaml's {group!r} section must be a mapping, got {type(raw).__name__}"
        )
    default_policy = raw.get("default_policy", fail_safe_default)
    if default_policy not in _VALID_POLICIES:
        raise PrivacyFilterConfigError(
            f"settings.yaml's {group}.default_policy must be one of {_VALID_POLICIES}, "
            f"got {default_policy!r}"
        )
    categories_raw = raw.get("categories", {})
    if not isinstance(categories_raw, dict):
        raise PrivacyFilterConfigError(
            f"settings.yaml's {group}.categories must be a mapping, got {type(categories_raw).__name__}"
        )
    categories: dict[str, str] = {}
    for category, policy in categories_raw.items():
        if policy not in _VALID_POLICIES:
            raise PrivacyFilterConfigError(
                f"settings.yaml's {group}.categories.{category} must be one of "
                f"{_VALID_POLICIES}, got {policy!r}"
            )
        categories[category] = policy
    return {"default_policy": default_policy, "categories": categories}


def category_policy(group: str, category: str) -> str:
    """The resolved allow/redact/block for one (group, category) pair -- the
    same lookup apply_text/apply_list use internally, exposed so the "AI will
    receive" review-UI checklist can render the real policy instead of
    re-deriving it."""
    g = _REGISTRY.get().get(group, {"default_policy": "allow", "categories": {}})
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


def check_consistency_warnings() -> list[str]:
    """Advisory config warnings for privacy_filter policies likely to surprise
    the operator -- never affects runtime filtering, only logged once at
    daemon startup (see daemon_main.py's run_app, right after
    init_privacy_filter()).

    Currently checks one thing: Drive's ``file_list`` and ``file_metadata``
    categories cover overlapping fields (a file's name/owners appear in both
    drive_list_files's results and drive_get_file_metadata's result) but gate
    two different tools independently. Restricting one without the other
    still lets that same information through the tool whose category is
    still "allow" -- someone who sets ``file_metadata: block`` expecting
    "Claude can't learn file names/owners" would reasonably assume that
    covers both tools; it silently doesn't.
    """
    warnings: list[str] = []
    file_list = category_policy("drive_privacy", "file_list")
    file_metadata = category_policy("drive_privacy", "file_metadata")
    if file_list == "allow" and file_metadata != "allow":
        warnings.append(
            f"drive_privacy.file_metadata is {file_metadata!r}, but drive_privacy.file_list "
            "is 'allow' -- a file's name/owners still reach Claude via drive_list_files even "
            "though drive_get_file_metadata restricts the same fields."
        )
    elif file_metadata == "allow" and file_list != "allow":
        warnings.append(
            f"drive_privacy.file_list is {file_list!r}, but drive_privacy.file_metadata is "
            "'allow' -- a file's name/owners still reach Claude via drive_get_file_metadata "
            "even though drive_list_files restricts the same fields."
        )
    return warnings
