"""Local, regex-based PII detector for Hungarian, English, and German content.

This is the extra confirmation gate: gate.py runs a read tool's (``gate=
"review"``) ``details_text`` (the message body / document / spreadsheet
content shown in the approval popup) through here before the popup is
displayed. When a category matches, the popup is tinted and the user must
clear a second "Are you sure?" confirmation (see show_pii_confirmation_popup
in approval_popup.py) on top of the normal Accept.

Write tools (``gate="popup"``) are never scanned: this gate exists to catch
personal data flowing from an external source into Claude's context, not
content Claude itself generated for an outbound write.

This is a best-effort heuristic, not a compliance-grade PII classifier: it
runs entirely locally (no network calls, no third-party NLP) over plaintext
already destined for the user's own screen, and it can both miss real PII
and flag things that aren't. Treat a hit as "look more carefully before you
approve," not as a guarantee either way.

Only category labels (e.g. "IBAN (bank account number)") ever leave this
module -- the matched substrings themselves are deliberately not returned,
logged, or audited, so the detector itself never becomes a new place PII
gets copied to.

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
    _p("Credit card number", r"\b(?:\d[ -]?){13,19}\b", validator=_luhn_valid),
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


def scan_text(text: str) -> list[PIIMatch]:
    """Return every PII pattern match in ``text``. Matched substrings are
    intentionally not carried in the result -- only category + position."""
    if not text:
        return []
    matches: list[PIIMatch] = []
    for p in _PATTERNS:
        for m in p.pattern.finditer(text):
            if p.validator is not None and not p.validator(m.group(0)):
                continue
            matches.append(PIIMatch(category=p.category, start=m.start(), end=m.end()))
    return matches


def detect_categories(text: str) -> list[str]:
    """Sorted, de-duplicated category labels found in ``text``."""
    return sorted({m.category for m in scan_text(text)})


# ---------------------------------------------------------------------------- #
# Enabled/disabled toggle (menu-bar configurable, hot-reloadable)
# ---------------------------------------------------------------------------- #

_enabled = True
_changed_listener: Callable[[], None] | None = None


def is_pii_detection_enabled() -> bool:
    return _enabled


def init_pii_detection(enabled: bool) -> None:
    """Set the initial enabled state at daemon startup."""
    global _enabled
    _enabled = enabled


def set_pii_detection_enabled(enabled: bool) -> None:
    """Hot-toggle from the menu bar; fires the changed listener like
    auto_accept.reload_rules() does for its own menu rebuild."""
    global _enabled
    _enabled = enabled
    logger.info("PII detection gate %s", "enabled" if enabled else "disabled")
    if _changed_listener is not None:
        _changed_listener()


def set_pii_detection_changed_listener(callback: Callable[[], None] | None) -> None:
    global _changed_listener
    _changed_listener = callback


def detect_pii_categories(text: str) -> list[str]:
    """The one entry point gate.py calls: empty list when disabled or no
    match, otherwise the categories found in ``text``."""
    if not _enabled:
        return []
    return detect_categories(text)
