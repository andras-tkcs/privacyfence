# PII detection keywords

Reference table for exactly what the [PII detection gate](TECHNICAL_REFERENCE.md#pii-detection-gate)
matches, category by category and language by language. The gate itself is implemented in
[`src/privacyfence/pii_detector.py`](../src/privacyfence/pii_detector.py); this doc is a
human-readable index of the same patterns, not a copy the code reads from — if the two drift,
`pii_detector.py` is authoritative.

All patterns are case-insensitive except where noted. `\w*` suffixes mean the match tolerates
trailing letters (plurals, grammatical case/possessive endings); a bare word means only that
literal form matches.

---

## Language-agnostic

These run regardless of language, since they match a structural format rather than a label.

| Category | What matches |
|---|---|
| IBAN (bank account number) | 2 letters + 2 digits + 11–30 alphanumeric characters, and passes an ISO 7064 mod-97-10 checksum (rules out random Drive/Jira-style IDs that happen to look like an IBAN) |
| Credit card number | 13–19 digits (spaces/dashes allowed), and passes a Luhn checksum |
| IP address *(optional, see below)* | standard IPv4 (`a.b.c.d`) |
| Financial figures (currency amounts) *(optional, see below)* | a number adjacent to `$` `€` `£` or `USD/EUR/GBP/HUF/CHF/Ft` — a bare number alone never matches |

---

## Hungarian

| Category | Trigger keywords / pattern |
|---|---|
| Hungarian TAJ number (social security) | `TAJ` optionally followed by `szám(a)`, then a 3-3-3 digit group |
| Hungarian tax ID (adóazonosító jel) | a bare `8` followed by 9 digits |
| Hungarian ID card number | 6 digits + 2 uppercase letters (case-sensitive) |
| Hungarian personal data reference | `személyi szám*`, `lakcím*`, `születési dátum*` / `hely*` / `idő*`, `anyja nev*`, `útlevél szám*` |
| Salary/compensation information | `fizetés*`, `jövedel*` (jövedelem), `bruttó bér*`, `nettó bér*` |

## German

| Category | Trigger keywords / pattern |
|---|---|
| German tax ID (Steuer-IdNr.) | `Steuer(liche)? ID` / `Identifikationsnummer` / `IdNr.` followed by an 11-digit group (2-3-3-3) |
| German social insurance number | 8 digits + 1 letter + 3 digits (case-sensitive) |
| German personal data reference | `Personalausweisnummer*`, `Sozialversicherungsnummer*`, `Geburtsdatum*`, `Geburtsort*`, `Wohnanschrift*`, `Anschrift*`, `Reisepassnummer*`, `Steueridentifikationsnummer*` |
| Salary/compensation information | `Gehalt*`, `Vergütung*`, `(Brutto/Netto/Monats/Jahres)Lohn(abrechnung/steuer/zettel/erhöhung)*` — bare `Lohn` only matches as a whole word (to avoid false-positiving on "lohnend") |

## English

| Category | Trigger keywords / pattern |
|---|---|
| US Social Security Number | `\d{3}-\d{2}-\d{4}` (e.g. `123-45-6789`) |
| UK National Insurance number | 2 letters + 6 digits + a letter A–D (case-sensitive) |
| English personal data reference | `social security number`, `date of birth`, `passport number`, `national insurance number`, `home address`, `driver's license/licence number` |
| Salary/compensation information | `salar*` (salary/salaries), `payslip`, `pay slip`, `take-home pay` |

---

## Individually optional categories

**IP address** and **Financial figures (currency amounts)** can each be turned off on their own,
independent of the gate as a whole, via the **PII Detection Gate** submenu in the menu bar or
`pii_detection.detect_ip_addresses` / `detect_financial_figures` in `config/settings.yaml` (both
`true` by default). Unlike the other categories in this doc, these two show up constantly in
ordinary business correspondence — server logs, invoices, budgets — without being personal data
about anyone, which is what makes them worth muting independently rather than only as part of
disabling the whole gate. Every other category in this doc has no such toggle: it's on whenever
the gate itself is enabled. See the ["PII detection gate"](TECHNICAL_REFERENCE.md#pii-detection-gate)
section of `TECHNICAL_REFERENCE.md` for the full toggle reference.

---

## Deliberately not detected

**Email addresses and phone numbers**, in any language. Nearly everything this gate scans is
email content, and nearly every signature carries the sender's own address and phone number —
matching those formats flagged almost every `review` popup regardless of whether the message
actually contained anything sensitive, training users to click through without reading. See the
module docstring in `pii_detector.py` and [TECHNICAL_REFERENCE.md](TECHNICAL_REFERENCE.md#pii-detection-gate)
for the full reasoning.

---

## Note on scope

This is a local, regex-based heuristic, not a compliance-grade PII classifier — it runs entirely
on-device with no network calls or third-party NLP, and it can both miss real PII and flag things
that aren't. A hit means "look more carefully before approving," not a guarantee either way. Only
the category label (e.g. "IBAN (bank account number)") is ever surfaced or logged — the matched
text itself is never returned, stored, or audited.
