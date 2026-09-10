"""Audit log: records every accept/deny/auto_accept decision.

Entries are appended to JSON-lines files in logs/audit/YYYY-WNN.jsonl
(one file per ISO week). A weekly Excel export (openpyxl) is generated
at daemon startup for any week that has a .jsonl but no .xlsx yet.

SEC-23 (docs/security-remediation-plan.md, Phase 3 item 3.6) added
append-integrity to that JSONL file: every entry is chained to the one
before it with a keyed hash (HMAC-SHA256, see AuditLogger._compute_entry_
hash), so a line inserted, edited, or removed after the fact -- without
also holding this install's own signing key (AuditLogger's
``.audit_chain.key``, generated on first use and never itself written
into the log it protects) -- breaks the chain in a way ``verify_chain()``
(or ``scripts/verify_audit_log.py``) detects. It also added: an explicit,
monotonically-increasing ``schema_version`` per entry (AuditEntry.
schema_version); a stable ``event_id`` unique to that one JSONL line
(distinct from ``request_id``, which is deliberately *shared* across a
deferred-approval's "pending" and its later "decided" entry -- see
AuditEntry.decision's own docstring); a per-install ``deployment_id``
(daemon_main.get_or_create_deployment_id()) so entries centrally
forwarded or aggregated from multiple machines/servers can be told apart;
and a per-entry ``security_config_hash`` fingerprinting the privacy
policy (settings.yaml) in effect when that decision was recorded (see
compute_security_config_hash() below). Centralized forwarding of these
entries to a syslog/SIEM/OTLP collector -- SEC-23's other half, for org
mode -- lives in audit_forwarding.py; AuditLogger.record() calls into it
but doesn't implement any transport itself.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import paths
from .principal import LOCAL_PRINCIPAL_ID, PrincipalRegistry, current_principal
from .secure_files import atomic_write_bytes, atomic_write_json, secure_mkdir

if TYPE_CHECKING:
    from .audit_forwarding import AuditForwarder

logger = logging.getLogger(__name__)

# Bumped whenever AuditEntry's field set changes in a way a downstream
# consumer (the Excel export, a forwarded-log parser, a hand-rolled jq
# pipeline over the .jsonl files) would need to know about. An entry
# written before this field existed at all (schema_version wasn't
# recorded until SEC-23) reconstructs with AuditEntry's own default (1)
# rather than this constant -- see AuditEntry.schema_version's docstring.
# Bump this, and add a line to the history below, the next time a field is
# added, renamed, or repurposed.
#   1 -- implicit, undocumented shape (every entry before SEC-23)
#   2 -- SEC-23: + schema_version, event_id, deployment_id,
#        security_config_hash, prev_hash, entry_hash
CURRENT_SCHEMA_VERSION = 2

# The hash chain's own root -- what the very first entry this install ever
# records (or the first one after a chain-state file goes missing, e.g. a
# restored-from-backup audit directory with no ``.audit_chain_state.json``
# alongside it) reports as its ``prev_hash``. Deliberately the same shape
# (64 lowercase hex characters) as a real SHA-256 digest so nothing
# downstream needs a special case for "this is the start of a chain, not a
# broken link" -- verify_chain() treats a segment starting at this value,
# or one with no chain fields at all (pre-SEC-23 data), as valid.
GENESIS_HASH = "0" * 64

_CHAIN_KEY_FILENAME = ".audit_chain.key"
_CHAIN_STATE_FILENAME = ".audit_chain_state.json"

# Decisions where the AI actually received the data or the write went
# through. Used by AuditLogger.recent_matches() below to count how many
# times a request has already been let through, not merely asked about --
# and by gate.py to decide whether an approved-request's PII match ever got
# released at all, before it will record the literal (or redacted) matched
# text in pii_match_details rather than the "hidden" placeholder. Public
# (no leading underscore) for that second, cross-module use.
APPROVED_LIKE_DECISIONS = frozenset({
    "approved", "auto_accepted", "accepted_via_accept_all", "accepted_via_temp_session",
})


@dataclass
class AuditEntry:
    timestamp: str
    week: str
    request_id: str
    connector: str
    tool: str
    tool_name: str
    summary: str
    sender: str
    decision: str           # "approved" | "rejected" | "auto_accepted" | "accepted_via_accept_all" |
                            # "accepted_via_temp_session" | "denied_unattended" | "policy_check" |
                            # "rules_listed" | "cancelled" | "org_config_startup" |
                            # "unattended_session_started" | "unattended_session_ended" |
                            # "rule_changed_via_bridge_proposal" | "rule_removed_via_bridge_proposal" |
                            # "grant_changed_via_bridge_proposal" | "grant_removed_via_bridge_proposal" |
                            # "bridge_proposal_no_op" | "error" |
                            # "approval_pending" | "expired"
                            # ("approval_pending": gate.py's deferred-approval protocol (P3,
                            #  docs/https-connector-refactor-plan.md §5) -- a human didn't decide
                            #  within the registry's hold window, so gated_call() returned a
                            #  structured pending result to Claude instead of continuing to block.
                            #  The eventual real decision -- approved/rejected/accepted_via_accept_all/
                            #  auto_accepted -- gets its OWN entry, sharing this one's request_id, once
                            #  something (a re-issued identical call, most often) actually releases on
                            #  the strength of it; that entry's decided_at (below) is when the human
                            #  actually clicked, which can be well before this entry's own timestamp
                            #  if it took a while for anything to come back and collect the decision.)
                            # ("expired": a pending approval nobody decided within its TTL (still
                            #  "pending" the whole time -- fail-closed, never silently auto-approved),
                            #  or one a human DID decide but that decision was never reclaimed by a
                            #  re-issued call before the shorter decision-ledger TTL ran out. Either
                            #  way: no data was ever released on this request_id's strength.)
                            # ("error": gate.py's gated_call exited without reaching a normal decision
                            #  branch -- a fallback so an unanticipated failure still leaves a trail)
                            # ("cancelled": the bridge told the daemon to give up on this request
                            #  (ipc.py's "cancel" method) -- the MCP client that issued the
                            #  corresponding tool call gave up on it first, most often because it
                            #  timed out. Distinct from "error": an expected outcome, not a bug.)
                            # ("denied_unattended": gate.py denied the call without ever prompting,
                            #  because the connection was in an unattended session and no auto-accept
                            #  rule matched -- distinct from "rejected", which is a human's own Deny.
                            #  Also used by gate.py's propose_rule_change() for the same reason)
                            # ("policy_check": ipc_server.py's check_policy handler -- a preflight
                            #  question, not a real decision; recorded for pattern-spotting only)
                            # ("rules_listed": ipc_server.py's list_rules handler -- not a decision
                            #  either, but the full current rule/grant set was disclosed, worth its
                            #  own record for the same pattern-spotting reason as "policy_check")
                            # ("org_config_startup": SEC-05 interim -- daemon_main.py's
                            #  log_org_config_bundle_hash(), recorded once per daemon startup that
                            #  finds an org_config.json installed at all, carrying its sha256 in
                            #  `summary` so a tampered bundle between one startup and the next is
                            #  detectable by diffing hashes even on an install that hasn't adopted
                            #  full bundle signing -- see org_bundle_signing.py)
                            # ("unattended_session_started"/"_ended": ipc_server.py's begin/end_
                            #  unattended_session handlers, and the same on disconnect cleanup --
                            #  this connection's gate posture changed, which is worth a record of
                            #  its own even though no specific tool call was involved)
                            # ("rule_changed_via_bridge_proposal"/"rule_removed_via_bridge_proposal"/
                            #  "grant_changed_via_bridge_proposal"/"grant_removed_via_bridge_proposal":
                            #  gate.py's propose_rule_change() -- a bridge-initiated auto_accept_rules/
                            #  auto_accept_grants edit that a human confirmed via the same
                            #  show_rule_confirmation_popup() the "Always allow" flow uses, and that
                            #  actually changed something (config's own `changed` return value was
                            #  True). "rejected" is reused, not a new value, when the human declines
                            #  instead)
                            # ("bridge_proposal_no_op": same propose_rule_change() confirmation flow,
                            #  but the human's "yes" didn't actually change anything -- e.g. Claude
                            #  proposed removing a rule/grant value that was already gone. Distinct
                            #  from "rejected" (the human said no) and from the four decisions above
                            #  (a real change happened) -- confirmed and yet a no-op is its own case)
    auto_accept_rule: str   # rule name if auto_accepted, else ""
    latency_seconds: float
    pii_detected: bool = False  # True if pii_detector.py flagged the content before this decision
    pii_categories: list[str] = field(default_factory=list)  # Which category label(s) pii_detector.py
                              # flagged (e.g. "IBAN (bank account number)") -- always populated
                              # whenever pii_detected is True, regardless of the audit_match_details
                              # trial setting below. Category labels alone were already surfaced to
                              # the popup UI before this field existed (see pii_detector.py's module
                              # docstring), so recording them here is always-on, not opt-in -- unlike
                              # pii_match_details, this carries no matched text, just which of
                              # pii_detector.py's ~20 patterns fired, which is what actually lets a
                              # refinement pass narrow in on which regex is noisy.
    pii_match_details: str = ""  # "" unless pii_detection.audit_match_details is turned on in
                              # settings.yaml (see pii_detector.py's is_pii_audit_match_details_
                              # enabled()) -- the opt-in PII-refinement trial capture, off by
                              # default. When on and pii_categories is non-empty: "User confirmed:
                              # details hidden" if this entry's own `decision` is NOT one of
                              # APPROVED_LIKE_DECISIONS above (rejected, denied_unattended, error --
                              # nothing was released, so nothing is recorded); otherwise
                              # "<category>: <text>" pairs (joined by "; ", one per distinct
                              # category) giving the literal matched text for a label/keyword
                              # category (e.g. "salary") or a partially redacted form for a
                              # value-bearing one (e.g. an IBAN) -- see pii_detector.py's
                              # describe_match_for_audit()/_VALUE_BEARING_CATEGORIES for exactly
                              # which categories get redacted and how.
    claude_reason: str = ""  # Claude's self-reported reason for the call, from the mandatory
                              # "reason" ToolSpec param every gated/auto tool now declares (see
                              # gate.py's reason_scope), or the "reason" param on the three
                              # privacyfence_* meta-tools for "policy_check"/
                              # "unattended_session_started"/"_ended" entries, which have no
                              # underlying gated tool call to take it from otherwise (see
                              # ipc_server.py's _audit_policy_check/_audit_unattended_session_event).
                              # Self-reported and unverified -- never treated as fact. Empty for
                              # the automatic session-end-on-disconnect path, which has no reason
                              # to attribute.
    decided_at: str = ""     # ISO-8601 UTC timestamp of when a human actually decided, distinct from
                              # this entry's own `timestamp` -- set only on a decision that came from
                              # the deferred-approval decision ledger (approvals.py, P3): a real click
                              # that happened separately from, and possibly well before, the
                              # invocation now releasing (or expiring) on the strength of it. Empty
                              # for every ordinary decided-inline entry, where the two timestamps
                              # would be the same instant and a second field would say nothing new.
                              # See gate.py's own module docstring and
                              # docs/https-connector-refactor-plan.md §5.4.
    delivery: str = ""       # "local_disk" | "inline_base64" | "staged_link" | "" -- docs/org-mode-
                              # download-delivery-plan.md's Phase 3: for drive_download_file/
                              # gmail_download_attachment/confluence_download_attachment only, which
                              # transport actually moved (or -- for a denied/expired/pending entry --
                              # would have moved) this call's file bytes. "" for every other tool
                              # (and for every entry recorded before this field existed): "did file
                              # content actually reach the model, or stay server-side" is a
                              # materially different privacy event from the ordinary accept/deny
                              # decision, and was previously only recoverable by cross-referencing
                              # tool-call args, which isn't what the audit log is for. Set by gate.py's
                              # gated_call() (its own ``delivery`` kwarg) -- never inferred here.

    # ---- SEC-23 fields (docs/security-remediation-plan.md, Phase 3 item 3.6) ----
    # All six below default to a value meaning "not yet stamped" and are
    # filled in by AuditLogger.record() itself (see its docstring) rather
    # than at each of this dataclass's ~15 call sites -- record() is
    # already the one place that holds the write lock and knows the chain
    # state, so it's the natural (and only correctness-safe) place to
    # stamp them. A caller MAY set event_id/deployment_id/security_config_
    # hash itself before calling record() (record() only fills in an empty
    # one); prev_hash/entry_hash are always (re-)computed by record(),
    # since they're intrinsic to this specific chain instance, not
    # something a caller could know in advance.
    schema_version: int = 1  # AuditEntry's own shape version -- see
                              # CURRENT_SCHEMA_VERSION's docstring for the version history. Defaults
                              # to 1 (the implicit, undocumented shape every entry had before this
                              # field existed) rather than CURRENT_SCHEMA_VERSION, so an entry
                              # reconstructed from a pre-SEC-23 .jsonl line (which has no
                              # "schema_version" key at all) is correctly identified as legacy rather
                              # than misreported as schema 2. record() always overwrites this to
                              # CURRENT_SCHEMA_VERSION for an entry it's actually recording.
    event_id: str = ""       # A random id unique to *this one JSONL line* -- unlike request_id
                              # (deliberately shared across a deferred-approval's "pending" and its
                              # later "decided" entry, see the `decision` field's own docstring
                              # above), event_id never repeats, which is what a hash-chain
                              # verifier, a forwarded-log deduplicator, or a compliance report citing
                              # "this specific audit line" actually needs. "" for every entry
                              # recorded before this field existed.
    deployment_id: str = ""  # This install's own stable identifier (daemon_main.get_or_create_
                              # deployment_id(), an opaque random id persisted once at
                              # data_dir()/deployment_id -- not a hostname or MAC address). Lets a
                              # centrally forwarded or manually aggregated collection of audit
                              # entries -- from several employees' local-mode machines, or several
                              # org-mode servers -- be told apart by which install produced them.
    security_config_hash: str = ""  # sha256 of the settings.yaml (privacy policy) content in effect
                              # when this decision was recorded -- see compute_security_config_hash()
                              # below. Lets a reviewer (or an automated diff) tell whether the policy
                              # governing a given decision has since changed, without needing
                              # settings.yaml's own (unversioned) edit history. Deliberately does NOT
                              # cover org_config.json -- see compute_security_config_hash()'s own
                              # docstring for why that file's tamper-evidence is handled separately
                              # (daemon_main.log_org_config_bundle_hash, SEC-05).
    prev_hash: str = ""      # This chain segment's previous entry's own entry_hash (or
                              # GENESIS_HASH for the first entry in a chain segment) -- see
                              # AuditLogger._compute_entry_hash and verify_chain().
    entry_hash: str = ""     # HMAC-SHA256 (keyed by this install's own AuditLogger.
                              # ``.audit_chain.key``) over this entry's own fields (prev_hash
                              # included, entry_hash itself excluded) -- the append-integrity
                              # mechanism itself. Recomputing it from the stored fields and comparing
                              # is exactly what verify_chain() does; a mismatch means this line was
                              # altered after AuditLogger wrote it, or the file was reordered/spliced.


@dataclass
class ChainVerificationResult:
    """AuditLogger.verify_chain()'s return value -- also what scripts/
    verify_audit_log.py prints, one of these per week file it checks."""

    week: str
    ok: bool
    entries_checked: int
    first_break_line: int | None = None  # 1-based line number of the first entry that failed
                              # verification, or None if `ok` is True.
    detail: str = ""


def compute_security_config_hash(config: dict[str, Any]) -> str:
    """SEC-23: a stable fingerprint of the privacy-policy configuration in
    effect when a decision is recorded -- ``config`` is settings.yaml's own
    parsed content (see daemon_main.py's module docstring: that file
    "carries no secrets", so hashing it whole -- unlike org_config.json --
    is safe to do on every single decision without redacting anything
    first). Two installs with byte-identical settings.yaml content get the
    same hash; that's deliberate, this fingerprints *which policy*, not
    *which install* (deployment_id, above, is the latter).

    org_config.json is deliberately NOT folded in here: SEC-05's own
    startup hash (daemon_main.log_org_config_bundle_hash) already gives
    that file tamper-evidence, separately, once per daemon startup --
    re-deriving the same signal on every decision would add nothing new,
    and risks a future org_config.json field that isn't secret-free the
    way settings.yaml is documented to be.
    """
    canonical = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# SEC-03: characters that, as the first character of a cell's string value,
# a spreadsheet application (or openpyxl itself, for a leading "=" -- see
# _excel_literal's docstring) will interpret as the start of a formula
# rather than literal text. `pii_match_details`, `claude_reason`, `summary`
# and `sender` all carry externally-influenced content (email subjects,
# sender display names, matched PII text, Claude's self-reported reason),
# so any of them can smuggle a formula into the audit export.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _excel_literal(value: str) -> str:
    """Neutralise spreadsheet formula injection in a cell value bound for
    ws.append().

    openpyxl's own Cell.value setter treats any string starting with "="
    as a formula and sets the cell's data type accordingly -- so writing
    an attacker-controlled string like "=WEBSERVICE(...)" straight into a
    cell doesn't just *display* as a formula in Excel, openpyxl itself
    already classifies it as one. Excel additionally treats a cell whose
    *displayed* content starts with "+", "-", "@", or a leading tab/CR
    (often used to smuggle a real trigger character past a naive
    startswith("=") check) as formula-like on some import/paste paths, so
    those are covered too, even though openpyxl doesn't special-case them
    itself.

    Prefixing with a single quote forces the value off that path -- for
    "=" it stops openpyxl from ever treating it as a formula in the first
    place, and for the rest it's the standard convention (matching what
    typing an apostrophe before such a value in Excel does) for "this is
    text, not a formula." Values that don't start with a trigger character
    pass through unchanged.
    """
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value


class AuditLogger:
    def __init__(
        self,
        log_dir: str,
        *,
        deployment_id: str = "",
        security_config_hash: str = "",
        forwarder: "AuditForwarder | None" = None,
    ) -> None:
        self._log_dir = Path(log_dir)
        # secure_mkdir (SEC-09 posture), not a bare mkdir: this directory now
        # also holds the hash-chain's own HMAC key (_CHAIN_KEY_FILENAME) --
        # see _load_or_create_chain_key's docstring for what that key
        # protects and, honestly, what it doesn't.
        secure_mkdir(self._log_dir)
        self._lock = threading.Lock()
        self._deployment_id = deployment_id
        self._security_config_hash = security_config_hash
        self._forwarder = forwarder
        self._chain_key = _load_or_create_chain_key(self._log_dir / _CHAIN_KEY_FILENAME)
        self._chain_state_path = self._log_dir / _CHAIN_STATE_FILENAME
        self._last_hash = _load_chain_state(self._chain_state_path)

    def set_security_config_hash(self, value: str) -> None:
        """Called (settings_controller.py's ``_save_config``) whenever
        settings.yaml is rewritten, so every entry recorded *after* a
        policy change carries the new hash without needing a daemon
        restart -- the value passed to ``__init__`` is only ever this
        install's *startup*-time snapshot (daemon_main.run_app)."""
        with self._lock:
            self._security_config_hash = value

    def _stamp_entry(self, entry: AuditEntry) -> None:
        """Fill in every SEC-23 field record() owns, in place. Must be
        called with self._lock held -- prev_hash/entry_hash/self._last_hash
        form a single mutable chain of state that a concurrent record()
        call must never interleave with."""
        if not entry.event_id:
            entry.event_id = uuid.uuid4().hex
        if not entry.deployment_id:
            entry.deployment_id = self._deployment_id
        if not entry.security_config_hash:
            entry.security_config_hash = self._security_config_hash
        # Always overwritten (not "only if unset" like the three above):
        # schema_version and the chain fields describe *this recording*,
        # not something a caller could meaningfully pre-supply.
        entry.schema_version = CURRENT_SCHEMA_VERSION
        entry.prev_hash = self._last_hash
        entry.entry_hash = self._compute_entry_hash(entry)
        self._last_hash = entry.entry_hash

    def _hash_canonical(self, data: dict[str, Any]) -> str:
        """HMAC-SHA256, keyed by this install's own chain key, over every
        field of ``data`` except ``entry_hash`` itself -- shared by
        _compute_entry_hash (write time, from an AuditEntry) and
        _recompute_hash_for_verification (verify time, from a dict parsed
        back out of the .jsonl file), so the two can never drift apart on
        what "canonical" means."""
        trimmed = {k: v for k, v in data.items() if k != "entry_hash"}
        canonical = json.dumps(trimmed, sort_keys=True, default=str).encode("utf-8")
        return hmac.new(self._chain_key, canonical, hashlib.sha256).hexdigest()

    def _compute_entry_hash(self, entry: AuditEntry) -> str:
        return self._hash_canonical(asdict(entry))

    def _persist_chain_state(self) -> None:
        try:
            atomic_write_json(self._chain_state_path, {"last_hash": self._last_hash})
        except OSError as exc:
            logger.warning("Could not persist audit chain state at %s: %s", self._chain_state_path, exc)

    def record(self, entry: AuditEntry) -> None:
        week_file = self._log_dir / f"{entry.week}.jsonl"
        with self._lock:
            self._stamp_entry(entry)
            line = json.dumps(asdict(entry)) + "\n"
            with open(week_file, "a", encoding="utf-8") as fh:
                fh.write(line)
            self._persist_chain_state()
        logger.debug("Audit: %s %s/%s", entry.decision, entry.connector, entry.tool)
        if self._forwarder is not None:
            self._forwarder.submit(asdict(entry))

    def verify_chain(self, week: str) -> ChainVerificationResult:
        """Recompute and check every entry's hash chain for one week's
        .jsonl file: each entry's ``entry_hash`` must recompute correctly
        from its own stored fields (proves that entry wasn't altered after
        AuditLogger wrote it), and each entry's ``prev_hash`` must match
        the previous entry's ``entry_hash`` (proves no entry was inserted,
        removed, or reordered). A line with no chain fields at all (a
        pre-SEC-23 entry) can't be verified -- it's skipped, and it resets
        the "previous entry" expectation for the line after it, since the
        chain never covered it in the first place.

        Cross-*file* continuity (this week's first prev_hash matching last
        week's last entry_hash) is intentionally not checked here --
        AuditLogger.recent_entries()'s own week-rollover handling is the
        only place that already reasons about adjacent week files, and
        wiring the same lookup into every verify_chain() call would make
        an otherwise-fast, single-file check silently depend on which
        other files happen to exist on disk. ``scripts/verify_audit_log.py``
        checks every week file in sequence and reports each file's result,
        which gets the same end-to-end coverage without that coupling.
        """
        week_file = self._log_dir / f"{week}.jsonl"
        if not week_file.exists():
            return ChainVerificationResult(week=week, ok=True, entries_checked=0, detail="no such log file")

        expected_prev: str | None = None
        checked = 0
        with open(week_file, encoding="utf-8") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    return ChainVerificationResult(
                        week=week, ok=False, entries_checked=checked,
                        first_break_line=lineno, detail="malformed JSON line",
                    )
                stored_hash = data.get("entry_hash") or ""
                if not stored_hash:
                    # Pre-SEC-23 entry (or a schema_version-1 line from an
                    # even older build) -- nothing to verify, and nothing
                    # for the *next* entry to chain against either.
                    expected_prev = None
                    checked += 1
                    continue
                stored_prev = data.get("prev_hash") or ""
                if expected_prev is not None and stored_prev != expected_prev:
                    return ChainVerificationResult(
                        week=week, ok=False, entries_checked=checked, first_break_line=lineno,
                        detail="prev_hash does not match the previous entry's hash -- an entry "
                               "may have been inserted, removed, or reordered",
                    )
                recomputed = self._recompute_hash_for_verification(data)
                if recomputed != stored_hash:
                    return ChainVerificationResult(
                        week=week, ok=False, entries_checked=checked, first_break_line=lineno,
                        detail="entry_hash does not match the recomputed HMAC -- this entry's "
                               "content may have been altered after it was recorded",
                    )
                expected_prev = stored_hash
                checked += 1
        return ChainVerificationResult(week=week, ok=True, entries_checked=checked, detail="chain intact")

    def _recompute_hash_for_verification(self, data: dict[str, Any]) -> str:
        return self._hash_canonical(data)

    def close(self) -> None:
        """Stop this logger's forwarder (if any) -- called once, from
        daemon_main.run_app()'s own shutdown path. A no-op when forwarding
        was never enabled."""
        if self._forwarder is not None:
            self._forwarder.stop()

    def export_week_to_excel(self, week: str) -> str | None:
        """Export one week's .jsonl to .xlsx, overwriting any existing file.

        Callers that only want to fill in weeks that have never been
        exported should use export_all_pending() instead.
        """
        try:
            import openpyxl
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            logger.warning("openpyxl not installed — skipping Excel audit export")
            return None

        week_file = self._log_dir / f"{week}.jsonl"
        if not week_file.exists():
            return None

        entries: list[AuditEntry] = []
        with open(week_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(AuditEntry(**json.loads(line)))
                    except Exception:
                        pass
        if not entries:
            return None

        output_path = str(self._log_dir / f"{week}.xlsx")
        wb = openpyxl.Workbook()

        # ── Main sheet ────────────────────────────────────────────────────
        ws = wb.active
        ws.title = "Decisions"

        HEADERS = [
            "Timestamp", "Week", "Connector", "Tool", "Human-Readable Name",
            "Summary", "Sender / Context", "Decision", "Auto-Accept Rule", "Latency (s)",
            "PII Detected", "PII Categories", "PII Match Details", "Claude's Reason (unverified)",
            "Delivery",
            # SEC-23: appended, not interleaved, so column indices existing
            # tooling/tests already rely on (Decision at 8, PII Detected at
            # 11, ...) stay stable.
            "Event ID", "Deployment ID", "Security Config Hash", "Integrity Hash",
        ]
        COL_WIDTHS = [22, 10, 12, 30, 22, 55, 30, 14, 22, 12, 12, 30, 55, 55, 16, 34, 34, 22, 22]

        hdr_font  = Font(bold=True, color="FFFFFF")
        hdr_fill  = PatternFill("solid", fgColor="2D4A6B")
        hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        decision_fills = {
            "approved":              PatternFill("solid", fgColor="E8F5E9"),
            "auto_accepted":         PatternFill("solid", fgColor="E3F2FD"),
            "accepted_via_accept_all": PatternFill("solid", fgColor="FFF3CD"),
            "accepted_via_temp_session": PatternFill("solid", fgColor="FFF3CD"),
            "rejected":              PatternFill("solid", fgColor="FFEBEE"),
            "denied_unattended":     PatternFill("solid", fgColor="FFD8A8"),
            "policy_check":          PatternFill("solid", fgColor="F1F3F5"),
            "rules_listed":          PatternFill("solid", fgColor="F1F3F5"),
            "org_config_startup":    PatternFill("solid", fgColor="F1F3F5"),
            "rule_changed_via_bridge_proposal":   PatternFill("solid", fgColor="FFF3CD"),
            "rule_removed_via_bridge_proposal":   PatternFill("solid", fgColor="FFF3CD"),
            "grant_changed_via_bridge_proposal":  PatternFill("solid", fgColor="FFF3CD"),
            "grant_removed_via_bridge_proposal":  PatternFill("solid", fgColor="FFF3CD"),
            "bridge_proposal_no_op": PatternFill("solid", fgColor="F1F3F5"),
            "error":                 PatternFill("solid", fgColor="FF6B6B"),
            "cancelled":             PatternFill("solid", fgColor="E9ECEF"),
            "approval_pending":      PatternFill("solid", fgColor="E7F0FF"),
            "expired":               PatternFill("solid", fgColor="FFD8A8"),
        }

        ws.append(HEADERS)
        for col, _ in enumerate(HEADERS, 1):
            c = ws.cell(row=1, column=col)
            c.font = hdr_font
            c.fill = hdr_fill
            c.alignment = hdr_align

        for entry in entries:
            ws.append([
                entry.timestamp, entry.week, entry.connector, entry.tool,
                entry.tool_name, _excel_literal(entry.summary), _excel_literal(entry.sender),
                entry.decision,
                entry.auto_accept_rule or "", round(entry.latency_seconds, 2),
                "Yes" if entry.pii_detected else "",
                "; ".join(entry.pii_categories), _excel_literal(entry.pii_match_details or ""),
                _excel_literal(entry.claude_reason or ""), entry.delivery or "",
                entry.event_id or "", entry.deployment_id or "",
                entry.security_config_hash or "", entry.entry_hash or "",
            ])
            fill = decision_fills.get(entry.decision, PatternFill())
            for col in range(1, len(HEADERS) + 1):
                ws.cell(row=ws.max_row, column=col).fill = fill

        for col, width in enumerate(COL_WIDTHS, 1):
            ws.column_dimensions[get_column_letter(col)].width = width

        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"

        # ── Summary sheet ─────────────────────────────────────────────────
        ws2 = wb.create_sheet("Summary")
        ws2.append(["Metric", "Value"])
        ws2.append(["Week", week])
        ws2.append(["Total decisions", len(entries)])
        counts = Counter(e.decision for e in entries)
        ws2.append(["Approved (manual)", counts.get("approved", 0)])
        ws2.append(["Auto-accepted", counts.get("auto_accepted", 0)])
        ws2.append(["Accepted via Always allow (new rule)", counts.get("accepted_via_accept_all", 0)])
        ws2.append(["Accepted (also armed temp-accept grace window)", counts.get("accepted_via_temp_session", 0)])
        ws2.append(["Rejected", counts.get("rejected", 0)])
        ws2.append(["Denied unattended (no human asked)", counts.get("denied_unattended", 0)])
        ws2.append(["Preflight checks (privacyfence_check_policy)", counts.get("policy_check", 0)])
        ws2.append(["PII flagged (any decision)", sum(1 for e in entries if e.pii_detected)])
        ws2.append([])
        ws2.append(["By connector", ""])
        for connector, cnt in sorted(Counter(e.connector for e in entries).items()):
            ws2.append([connector, cnt])
        category_counts = Counter(cat for e in entries for cat in e.pii_categories)
        if category_counts:
            ws2.append([])
            ws2.append(["By PII category (refinement trial)", ""])
            for category, cnt in sorted(category_counts.items()):
                ws2.append([category, cnt])
        ws2.column_dimensions["A"].width = 24
        ws2.column_dimensions["B"].width = 14

        wb.save(output_path)
        logger.info("Audit Excel exported: %s (%d entries)", output_path, len(entries))
        return output_path

    def export_all_pending(self) -> None:
        """Export any week that has .jsonl but no .xlsx."""
        for jsonl in sorted(self._log_dir.glob("*.jsonl")):
            week = jsonl.stem
            xlsx = self._log_dir / f"{week}.xlsx"
            if not xlsx.exists():
                self.export_week_to_excel(week)

    def recent_entries(self, limit: int = 20) -> list[AuditEntry]:
        """Most-recent-first entries for the settings window's "Recent
        decisions" list. Reads the current ISO week's .jsonl and, if that
        alone doesn't have `limit` entries, tops up from the previous week's
        file -- no need to scan every historical file for what's meant to be
        a short "what just happened" glance, not a full audit trail browser
        (that's what the Excel export is for)."""
        weeks = [current_week(), _previous_week(current_week())]
        entries: list[AuditEntry] = []
        for week in weeks:
            week_file = self._log_dir / f"{week}.jsonl"
            if not week_file.exists():
                continue
            week_entries: list[AuditEntry] = []
            with open(week_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        week_entries.append(AuditEntry(**json.loads(line)))
                    except Exception:
                        continue
            entries.extend(reversed(week_entries))
            if len(entries) >= limit:
                break
        return entries[:limit]

    def recent_matches(self, connector: str, tool: str, summary: str, *, week: str | None = None) -> int:
        """Count prior approved-like decisions (see APPROVED_LIKE_DECISIONS)
        for the same (connector, tool, summary) in one week's log --
        defaults to the current week. The request-fingerprint feature:
        "you've approved this exact request N times this week," so a
        reviewer can spot an unusually novel request versus a routine
        repeat at a glance.

        (connector, tool, summary) is a practical proxy for "the same
        request" -- AuditEntry carries neither an operation_key nor the
        full preview dict, and summary already names the specific resource
        for most tools (e.g. "Read email: Confidential Q3 numbers", 'Read
        "Budget.xlsx"'). A coarser or finer fingerprint can replace this
        later without changing the caller-facing count semantics.
        """
        week_file = self._log_dir / f"{week or current_week()}.jsonl"
        if not week_file.exists():
            return 0
        count = 0
        with open(week_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    data.get("connector") == connector
                    and data.get("tool") == tool
                    and data.get("summary") == summary
                    and data.get("decision") in APPROVED_LIKE_DECISIONS
                ):
                    count += 1
        return count


def _load_or_create_chain_key(path: Path) -> bytes:
    """SEC-23's hash-chain key: 32 random bytes, generated once per audit
    directory and reused for the life of that install (each principal's
    own ``logs/audit/`` gets its own key, same granularity as its own
    chain -- see AuditLogger.__init__).

    Honest threat model: this key lives right next to the .jsonl files it
    protects, at the same file permissions (0600, same directory). It does
    NOT defend against a party who can already read *and* write everything
    under this directory -- that party can read the key and recompute a
    consistent chain over a tampered file just as validly as this class
    can. What it DOES catch: accidental corruption; a party who gains
    write access to the .jsonl files specifically (e.g. via a bug in some
    other export/backup path) without also reading this key; and, in
    general, any edit made without going back through AuditLogger.record()
    itself. The real defense against a fully-privileged local
    administrator tampering with their own audit trail is a copy that
    leaves this trust boundary entirely -- see audit_forwarding.py's
    centralized forwarding, SEC-23's other half.
    """
    try:
        if path.exists():
            existing = path.read_bytes()
            if existing:
                return existing
    except OSError as exc:
        logger.warning("Could not read audit chain-integrity key at %s: %s", path, exc)
    key = secrets.token_bytes(32)
    try:
        atomic_write_bytes(path, key)
    except OSError as exc:
        logger.warning(
            "Could not persist audit chain-integrity key at %s: %s -- this process's chain will "
            "still work, but a restart will generate a new key and start a fresh chain segment",
            path, exc,
        )
    return key


def _load_chain_state(path: Path) -> str:
    """The last entry_hash this AuditLogger (or a previous process using
    the same log_dir) recorded, so a chain started before a daemon restart
    keeps extending rather than silently resetting to GENESIS_HASH every
    time the process restarts. Missing or unreadable state (a fresh audit
    directory, or one restored from a backup that didn't include the
    dotfile) starts a new chain segment at GENESIS_HASH -- not a security
    regression by itself, since verify_chain() treats a GENESIS_HASH
    (or absent-chain-fields) restart point as valid, just a break in
    provable continuity across that specific gap.
    """
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            last_hash = data.get("last_hash")
            if isinstance(last_hash, str) and last_hash:
                return last_hash
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read audit chain state at %s -- starting a new chain segment: %s", path, exc,
        )
    return GENESIS_HASH


def current_week() -> str:
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _previous_week(week: str) -> str:
    """One ISO week before `week` (e.g. "2026-W31" -> "2026-W30"), correctly
    rolling over a year boundary via datetime's own ISO calendar math rather
    than hand-rolled week-count arithmetic."""
    from datetime import timedelta

    year, week_num = week.split("-W")
    monday = datetime.fromisocalendar(int(year), int(week_num), 1)
    prev_monday = monday - timedelta(days=7)
    iso = prev_monday.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _fallback_log_dir() -> str:
    """Used only if get_audit_logger() is ever called before daemon_main.py's
    run_app() has called init_audit_logger() -- which shouldn't happen in
    practice, but this is the same last-resort fallback the original bare
    ``_INSTANCE`` singleton had, just principal-aware now (P6, docs/
    https-connector-refactor-plan.md §9.2): the local principal keeps the
    exact original hardcoded path, so an install that somehow only ever hit
    this fallback stays byte-identical; any other principal falls back to
    its own storage root instead of writing into the local principal's
    directory."""
    principal = current_principal()
    if principal.id == LOCAL_PRINCIPAL_ID:
        return os.path.join(os.path.expanduser("~"), ".privacyfence", "audit")
    return str(paths.user_dir(principal) / "logs" / "audit")


# PrincipalRegistry.get() already serializes construction per principal
# (see that class's own docstring on why it needs to be thread-safe), so
# the double-checked-locking dance the original bare singleton needed here
# is now the registry's job, not this module's.
_REGISTRY: PrincipalRegistry[AuditLogger] = PrincipalRegistry(lambda: AuditLogger(_fallback_log_dir()))


def get_audit_logger() -> AuditLogger:
    return _REGISTRY.get()


def init_audit_logger(
    log_dir: str,
    *,
    deployment_id: str = "",
    security_config_hash: str = "",
    forwarder: "AuditForwarder | None" = None,
) -> AuditLogger:
    return _REGISTRY.set(AuditLogger(
        log_dir, deployment_id=deployment_id, security_config_hash=security_config_hash,
        forwarder=forwarder,
    ))
