"""Local, regex-based PII detector for Hungarian, English, and German content.

This is the extra confirmation gate: gate.py runs a read tool's (``gate=
"review"``) ``details_text`` (the message body / document / spreadsheet
content shown in the approval popup) through here before the popup is
displayed. When a category matches, the popup is tinted and the user must
clear a second "Are you sure?" confirmation (see show_pii_confirmation_popup
in approval_popup.py) on top of the normal Allow once.

Write tools (``gate="popup"``) are never scanned: this gate exists to catch
personal data flowing from an external source into Claude's context, not
content Claude itself generated for an outbound write.

This is a best-effort heuristic, not a compliance-grade PII classifier: it
runs entirely locally (no network calls, no third-party NLP) over plaintext
already destined for the user's own screen, and it can both miss real PII
and flag things that aren't. Treat a hit as "look more carefully before you
approve," not as a guarantee either way.

Only category labels (e.g. "IBAN (bank account number)") ever leave this
module via scan_text()/detect_categories()/detect_pii_categories() -- the
matched substrings themselves are deliberately not returned, logged, or
audited by any of those three, so the detector itself never becomes a new
place PII gets copied to. Every caller outside this module (popup
rendering, the write-gate's informational content flags, ...) goes through
one of those three and is unaffected by the paragraph below.

scan_pii_for_audit() is the one deliberate exception: it returns the
literal matched text alongside its category, for gate.py's opt-in,
off-by-default PII-refinement trial capture (see is_pii_audit_match_
details_enabled() below and audit_log.py's pii_match_details field). Even
with that capture turned on, gate.py never records the literal text for a
denied request (nothing was released, so there's nothing to gain and real
cost to keeping it) and, for categories whose match *is* the sensitive
value rather than a label pointing at one (an IBAN, a credit card number,
a national ID, an IP address, a currency figure -- see describe_match_for_
audit()'s _VALUE_BEARING_CATEGORIES), only ever a redacted form even on an
approved request.

Deliberately NOT detected: email addresses and phone numbers. Nearly every
message this gate scans is an email, and nearly every email signature
contains the sender's own address and phone number, so matching on those
formats flagged almost every `review` dialog regardless of whether the
content actually contained anything sensitive -- see docs/TECHNICAL_REFERENCE.md's
"PII detection gate" section for the reasoning.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from .principal import PrincipalRegistry

logger = logging.getLogger(__name__)


def _luhn_valid(candidate: str) -> bool:
    """Luhn checksum, used to keep the credit-card pattern from matching
    arbitrary long digit runs (file IDs, phone numbers, ...)."""
    digits = [int(c) for c in candidate if c.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _card_grouping_valid(candidate: str) -> bool:
    """Reject digit runs whose separator grouping doesn't match how card
    numbers are actually displayed -- groups of 4 (with the last group
    possibly shorter), or the Amex (4-6-5) / Diners Club (4-6-4)
    exceptions. An ungrouped run (no space/dash separators at all) always
    passes this check; only *inconsistent* grouping is rejected.

    Needed on top of the Luhn checksum: a Luhn-valid 13-19 digit run isn't
    rare enough on its own -- e.g. a calendar's "CW 35  24 25 26 27 28 29
    30" week/date row, flattened by text extraction into "35 24 25 26 27
    28 29 30", is 16 digits grouped in pairs that happens to pass Luhn.
    Real card numbers are never displayed in pairs, so this filters that
    class of false positive without touching the (4,4,4,4)-style grouping
    real cards actually use."""
    groups = [g for g in re.split(r"[ -]", candidate.strip()) if g]
    if len(groups) <= 1:
        return True
    lengths = [len(g) for g in groups]
    if lengths in ([4, 6, 5], [4, 6, 4]):
        return True
    return all(n == 4 for n in lengths[:-1]) and 1 <= lengths[-1] <= 4


def _credit_card_valid(candidate: str) -> bool:
    return _luhn_valid(candidate) and _card_grouping_valid(candidate)


def _iban_valid(candidate: str) -> bool:
    """ISO 7064 mod-97-10 checksum, used to keep the IBAN pattern from
    matching arbitrary alphanumeric identifiers (Drive file IDs, Jira/
    Confluence keys, ...) that happen to start with two letters."""
    s = re.sub(r"[ -]", "", candidate).upper()
    if not (15 <= len(s) <= 34) or not re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]+$", s):
        return False
    rearranged = s[4:] + s[:4]
    try:
        numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    except ValueError:
        return False
    return int(numeric) % 97 == 1


@dataclass(frozen=True)
class _PIIPattern:
    category: str
    pattern: re.Pattern
    validator: Callable[[str], bool] | None = None


def _p(category: str, regex: str, *, validator=None, flags=re.IGNORECASE) -> _PIIPattern:
    return _PIIPattern(category, re.compile(regex, flags), validator)


# Ordered by specificity within each language group; order doesn't affect
# correctness (every pattern is tried against the full text), only the
# order categories are reported in.
_PATTERNS: list[_PIIPattern] = [
    # -- Language-agnostic ---------------------------------------------------
    # Deliberately no "Email address" or "Phone number" patterns here -- see
    # the module docstring for why (email signatures make them near-universal
    # false positives on this gate's typical input).
    _p("IBAN (bank account number)", r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", validator=_iban_valid),
    _p("Credit card number", r"\b(?:\d[ -]?){13,19}\b", validator=_credit_card_valid),
    _p(
        "IP address",
        r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
    ),
    _p(
        # Distinct from "Salary/compensation information" below -- this is
        # about currency-figure content generally (budgets, invoices,
        # quotes, revenue), not personal pay. Anchored on a currency
        # symbol or ISO code adjacent to a number, never a bare number
        # alone -- same reasoning the module docstring gives for leaving
        # out email/phone patterns: an unanchored "any number" match would
        # flag almost every business document regardless of content.
        "Financial figures (currency amounts)",
        r"[$€£]\s?\d[\d,.\s]{0,15}\d"
        r"|\b\d[\d,.\s]{0,15}\d\s?(?:USD|EUR|GBP|HUF|CHF|Ft)\b"
        r"|\b(?:USD|EUR|GBP|HUF|CHF)\s?\d[\d,.\s]{0,15}\d\b",
    ),

    # -- Hungarian ------------------------------------------------------------
    _p(
        "Hungarian TAJ number (social security)",
        r"\bTAJ[ \-:]{0,5}(?:sz[aá]m[aá]?)?[ \-:]{0,5}\d{3}[ -]?\d{3}[ -]?\d{3}\b",
    ),
    _p("Hungarian tax ID (adóazonosító jel)", r"\b8\d{9}\b"),
    _p("Hungarian ID card number", r"\b\d{6}[A-Z]{2}\b", flags=0),
    _p(
        # Base forms with a trailing \w* rather than a closing \b: Hungarian
        # is agglutinative, so labels commonly appear with a possessive/case
        # suffix glued directly onto the word (e.g. "lakcímét", "dátuma").
        "Hungarian personal data reference",
        r"\b(szem[eé]lyi\s+sz[aá]m\w*|lakc[ií]m\w*|"
        r"sz[uü]let[eé]si\s+(?:d[aá]tum|hely|id[oő])\w*|anyja\s+nev\w*|"
        r"[uú]tlev[eé]l\s*sz[aá]m\w*)",
    ),
    _p(
        "Salary/compensation information",
        r"\b(fizet[eé]s\w*|j[oö]vedel\w*|brutt[oó]\s+b[eé]r\w*|nett[oó]\s+b[eé]r\w*)\b",
    ),

    # -- German -----------------------------------------------------------------
    _p(
        "German tax ID (Steuer-IdNr.)",
        r"\bSteuer(?:liche)?[ \-]?(?:ID|Identifikationsnummer|IdNr\.?)"
        r"[ :.\-]{0,5}\d{2}\s?\d{3}\s?\d{3}\s?\d{3}\b",
    ),
    _p("German social insurance number", r"\b\d{8}[A-Z]\d{3}\b", flags=0),
    _p(
        # Deliberately no bare "Steuer-ID" alternative here: it's a
        # substring of "Steuer-IdNr." above, which would double-count every
        # match of the specific tax-ID pattern under this generic label too.
        # The spelled-out "Steueridentifikationsnummer" form is unambiguous
        # on its own and is kept.
        "German personal data reference",
        r"\b(Personalausweisnummer\w*|Sozialversicherungsnummer\w*|Geburtsdatum\w*|"
        r"Geburtsort\w*|Wohnanschrift\w*|Anschrift\w*|Reisepassnummer\w*|"
        r"Steueridentifikationsnummer\w*)",
    ),
    _p(
        # Bare "Lohn" is a common word fragment ("lohnend" = worthwhile), so
        # it's only matched as a whole word or with a known salary-related
        # compound suffix, never via an open-ended \w*.
        "Salary/compensation information",
        r"\b(Gehalt\w*|Verg[uü]tung\w*|"
        r"(?:Brutto|Netto|Monats|Jahres)?Lohn(?:abrechnung\w*|steuer\w*|zettel\w*|erh[oö]hung\w*)?\b)",
    ),

    # -- English ------------------------------------------------------------
    _p("US Social Security Number", r"\b\d{3}-\d{2}-\d{4}\b"),
    _p("UK National Insurance number", r"\b[A-Z]{2}\d{6}[A-D]\b", flags=0),
    _p(
        "English personal data reference",
        r"\b(social security number|date of birth|passport number|"
        r"national insurance number|home address|driver'?s licen[cs]e number)\b",
    ),
    _p(
        "Salary/compensation information",
        r"\b(salar\w*|payslip|pay slip|take-home pay)\b",
    ),
]


@dataclass(frozen=True)
class PIIMatch:
    category: str
    start: int
    end: int


def _iter_raw_matches(text: str):
    """Shared core of scan_text()/scan_pii_for_audit() below: yield
    (category, re.Match) for every pattern hit that also clears its
    validator (Luhn/IBAN checksum, where one applies) and isn't individually
    disabled. Private and untyped in its return -- the two public callers
    below decide what each one carries (position-only vs. the matched text
    itself) rather than this generator committing to a shape either of them
    would have to unpack."""
    disabled_categories = _REGISTRY.get().disabled_categories
    for p in _PATTERNS:
        if p.category in disabled_categories:
            continue
        for m in p.pattern.finditer(text):
            if p.validator is not None and not p.validator(m.group(0)):
                continue
            yield p.category, m


def scan_text(text: str) -> list[PIIMatch]:
    """Return every PII pattern match in ``text``. Matched substrings are
    intentionally not carried in the result -- only category + position."""
    if not text:
        return []
    return [PIIMatch(category=c, start=m.start(), end=m.end()) for c, m in _iter_raw_matches(text)]


def detect_categories(text: str) -> list[str]:
    """Sorted, de-duplicated category labels found in ``text``."""
    return sorted({m.category for m in scan_text(text)})


# ---------------------------------------------------------------------------- #
# Enabled/disabled toggle (menu-bar configurable, hot-reloadable)
# ---------------------------------------------------------------------------- #

# Individually-toggleable categories, keyed by the settings.yaml field name.
# Unlike the other categories (national IDs, TAJ/tax numbers, ...), IP
# addresses and currency figures show up constantly in ordinary business
# correspondence (server logs, invoices, budgets) without being personal
# data about anyone -- so these two are opt-out per-category on top of the
# whole-gate `enabled` switch, while the rest of _PATTERNS always runs
# whenever the gate itself is on.
_OPTIONAL_CATEGORIES: dict[str, str] = {
    "detect_ip_addresses": "IP address",
    "detect_financial_figures": "Financial figures (currency amounts)",
}


class _PiiState:
    """Everything below used to be four bare module globals (_enabled,
    _changed_listener, _audit_match_details_enabled, _disabled_categories)
    -- one PII-detection posture per *process*. P6 (docs/
    https-connector-refactor-plan.md §9.2) makes it one per *principal*
    instead: each user's own PII settings, isolated the same way their
    auto-accept rules already are."""

    def __init__(self) -> None:
        self.enabled = True
        self.changed_listener: Callable[[], None] | None = None
        # Opt-in, off by default: whether gate.py's audit log should also
        # capture the matched text (redacted for value-bearing categories --
        # see describe_match_for_audit() below) behind pii_categories, for a
        # deliberate PII-refinement trial period. See init_pii_detection()'s
        # own docstring and audit_log.py's pii_match_details field for the
        # full contract. Config-only (pii_detection.audit_match_details in
        # settings.yaml) -- deliberately not exposed as a menu-bar/
        # settings-window toggle the way the enabled switch and the two
        # optional categories are, since it's meant to be turned on for a
        # bounded trial window, not left as an everyday user-facing option.
        self.audit_match_details_enabled = False
        self.disabled_categories: set[str] = set()


_REGISTRY: PrincipalRegistry[_PiiState] = PrincipalRegistry(_PiiState)


def is_pii_detection_enabled() -> bool:
    return _REGISTRY.get().enabled


def is_pii_audit_match_details_enabled() -> bool:
    return _REGISTRY.get().audit_match_details_enabled


def init_pii_detection(
    enabled: bool, *, detect_ip_addresses: bool = True, detect_financial_figures: bool = True,
    audit_match_details: bool = False,
) -> None:
    """Set the initial enabled state at daemon startup.

    ``audit_match_details`` is the PII-refinement trial switch (see
    ``_PiiState.audit_match_details_enabled``'s own comment) -- off by
    default, read from ``pii_detection.audit_match_details`` in
    settings.yaml. Unlike ``enabled``/the two optional categories, there's no
    hot-toggle setter for it: it's meant to be set once for a bounded trial
    window via a daemon restart, not flipped live from the menu bar.
    """
    state = _REGISTRY.get()
    state.enabled = enabled
    state.audit_match_details_enabled = audit_match_details
    state.disabled_categories.clear()
    if not detect_ip_addresses:
        state.disabled_categories.add(_OPTIONAL_CATEGORIES["detect_ip_addresses"])
    if not detect_financial_figures:
        state.disabled_categories.add(_OPTIONAL_CATEGORIES["detect_financial_figures"])


def set_pii_detection_enabled(enabled: bool) -> None:
    """Hot-toggle from the menu bar; fires the changed listener like
    auto_accept.reload_rules() does for its own menu rebuild."""
    state = _REGISTRY.get()
    state.enabled = enabled
    logger.info("PII detection gate %s", "enabled" if enabled else "disabled")
    if state.changed_listener is not None:
        state.changed_listener()


def set_pii_category_enabled(category_key: str, enabled: bool) -> None:
    """Hot-toggle one optional category (``category_key`` is a key of
    ``_OPTIONAL_CATEGORIES``, e.g. "detect_ip_addresses") from the menu bar."""
    category = _OPTIONAL_CATEGORIES[category_key]
    state = _REGISTRY.get()
    if enabled:
        state.disabled_categories.discard(category)
    else:
        state.disabled_categories.add(category)
    logger.info("PII category %r %s", category, "enabled" if enabled else "disabled")
    if state.changed_listener is not None:
        state.changed_listener()


def set_pii_detection_changed_listener(callback: Callable[[], None] | None) -> None:
    _REGISTRY.get().changed_listener = callback


def detect_pii_categories(text: str) -> list[str]:
    """The one entry point gate.py calls: empty list when disabled or no
    match, otherwise the categories found in ``text``."""
    if not _REGISTRY.get().enabled:
        return []
    return detect_categories(text)


# ---------------------------------------------------------------------------- #
# Audit-trial capture (opt-in, off by default -- see is_pii_audit_match_
# details_enabled() above). scan_pii_for_audit()/PIIAuditMatch/describe_
# match_for_audit() below are the ONE place in this module that ever hands
# back the literal matched text -- see the module docstring's "deliberate
# exception" paragraph. Nothing above this point is affected: scan_text(),
# detect_categories(), and detect_pii_categories() keep returning category
# labels only, exactly as before.
# ---------------------------------------------------------------------------- #

# Categories where the regex match IS the sensitive value itself (an
# account/card/ID number, an IP address, a currency figure), as opposed to a
# label word merely pointing at one (e.g. "salary", "lakcíme", "date of
# birth"). Used only by describe_match_for_audit() below to decide whether
# an approved request's audit entry gets the literal matched text or a
# redacted form -- label/keyword categories are never personal data on
# their own, so they're always logged as-is when logged at all.
_VALUE_BEARING_CATEGORIES = frozenset({
    "IBAN (bank account number)",
    "Credit card number",
    "IP address",
    "Financial figures (currency amounts)",
    "Hungarian TAJ number (social security)",
    "Hungarian tax ID (adóazonosító jel)",
    "Hungarian ID card number",
    "German tax ID (Steuer-IdNr.)",
    "German social insurance number",
    "US Social Security Number",
    "UK National Insurance number",
})


def _redact_value(text: str) -> str:
    """Keep the first/last 2 alphanumeric characters of a value-bearing PII
    match and mask the rest with '•', leaving separators (spaces, dashes,
    dots) alone so the match's *shape* (an IBAN's country prefix, an IP
    address's octet grouping, ...) stays legible without the value itself
    being reconstructable. Matches with 4 or fewer alphanumeric characters
    are masked in full -- too short to partially reveal without giving away
    the whole thing."""
    alnum_idx = [i for i, c in enumerate(text) if c.isalnum()]
    if len(alnum_idx) <= 4:
        return "".join("•" if c.isalnum() else c for c in text)
    keep = set(alnum_idx[:2]) | set(alnum_idx[-2:])
    return "".join(c if (i in keep or not c.isalnum()) else "•" for i, c in enumerate(text))


@dataclass(frozen=True)
class PIIAuditMatch:
    """Like PIIMatch, but also carries the matched text -- see this
    section's own module-docstring paragraph for why that's safe here and
    nowhere else in this module. Returned only by scan_pii_for_audit()."""
    category: str
    text: str


def scan_pii_for_audit(text: str) -> list[PIIAuditMatch]:
    """gate.py's one entry point for the audit-trial capture: empty list
    when detection is disabled or nothing matched, otherwise every match
    with its literal text attached. Mirrors detect_pii_categories()'s own
    enabled-check so callers don't need to check is_pii_detection_enabled()
    themselves -- unlike detect_pii_categories(), callers of *this* function
    should also gate it behind is_pii_audit_match_details_enabled() first,
    since scanning here is only ever worth doing when that capture is on."""
    if not _REGISTRY.get().enabled or not text:
        return []
    return [PIIAuditMatch(category=c, text=m.group(0)) for c, m in _iter_raw_matches(text)]


def describe_match_for_audit(category: str, text: str) -> str:
    """The literal matched text for a label/keyword category (e.g.
    "salary", "lakcíme" -- never personal data on its own), or a redacted
    form for a value-bearing category (see _VALUE_BEARING_CATEGORIES) where
    the match itself is the sensitive value. Used only by gate.py, only for
    an approved request's pii_match_details -- see audit_log.py's field
    docstring for why a denied request never calls this at all."""
    if category in _VALUE_BEARING_CATEGORIES:
        return _redact_value(text)
    return text
