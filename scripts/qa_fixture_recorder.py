#!/usr/bin/env python3
"""Local-only QA connector smoke-check and fixture recorder.

Run this on a developer's own machine only -- never in CI, never with any
credential provisioned to GitHub Actions or any other cloud service. It
reuses the exact OAuth token files ``privacyfence-app --<connector>-oauth``
already writes to the git-ignored ``credentials/`` directory
(``daemon_main.TOKEN_FILES``), and talks to your real, already-authenticated
accounts -- the same ones set up per ``docs/qa-environment-setup.md``.

Two modes:

    qa_fixture_recorder.py --check [connector ...]
        Calls each connector's read methods against its tagged seed
        artifact, asserts non-empty/expected results, prints a report.
        Never writes a fixture file. Safe to run any time, as often as
        you like.

    qa_fixture_recorder.py --record [connector ...]
        Does the same targeted calls, redacts real account-identity
        fields (author email, account id, display name -- see
        ``docs/external-api-contract-testing.md``), and writes the result
        to tests/fixtures/live/<connector>/<method>.json.

Both modes accept ``--report-file PATH`` to also save the printed report,
ready to paste into a PR description (see
``docs/external-api-contract-testing.md#local-checks-before-opening-a-pr``).

Only reads from ``tests/fixtures/qa_environment.yaml`` -- a small, non-secret
manifest of your seed artifacts' IDs/keys/tags (see
``docs/qa-environment-setup.md``) -- ever decide *which* real object this
script is allowed to touch. If a fetched object's title/summary doesn't
carry the ``[QATEST]`` tag that manifest expects, recording is refused for
that item rather than silently capturing whatever was fetched.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from privacyfence import daemon_main  # noqa: E402
from privacyfence.confluence_client import ConfluenceClient, ConfluenceClientError  # noqa: E402
from privacyfence.jira_client import JiraClient, JiraClientError  # noqa: E402
from privacyfence.salesforce_client import SalesforceClient, SalesforceClientError  # noqa: E402
from privacyfence.gmail_client import GmailClient, GmailClientError  # noqa: E402
from privacyfence.drive_client import DriveClient, DriveClientError  # noqa: E402
from privacyfence.calendar_client import CalendarClient, CalendarClientError  # noqa: E402
from privacyfence.contacts_client import ContactsClient, ContactsClientError  # noqa: E402
from privacyfence.tasks_client import TasksClient, TasksClientError  # noqa: E402
from privacyfence.slack_client import SlackClient, SlackClientError  # noqa: E402
from privacyfence.slack_client import load_token_file as load_slack_token  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "live"
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "qa_environment.yaml"

QATEST_TAG = "[QATEST]"

# ---------------------------------------------------------------------------- #
# Identity-field redaction -- runs unconditionally on every recording, never
# optional. Content being synthetic (docs/qa-environment-setup.md) does not
# make an API response's structural identity fields synthetic too: a page
# you wrote yourself still says *you* wrote it, in your real account id and
# real name, regardless of what the page says. See
# docs/external-api-contract-testing.md's "Identity-field redaction".
# ---------------------------------------------------------------------------- #

_REDACT_ACCOUNT_ID_KEYS = {
    "authorid", "accountid", "author_id", "account_id", "ownerid",
    "createdbyid", "lastmodifiedbyid",  # Salesforce's actual flat field names
}
_REDACT_EMAIL_KEYS = {"email", "emailaddress", "authoremail", "accountemail"}
# Deliberately narrow -- a bare "name" key is legitimate, non-identity
# content just as often as it's a person's name (a space's name, a page
# title field, a Salesforce record's Name field), so it's excluded here on
# purpose. Only qualified, unambiguously-identity name fields are redacted.
_REDACT_NAME_KEYS = {"displayname", "publicname", "authorname", "accountname"}
# Keys whose *entire* nested value is a person, regardless of what
# sub-fields it has -- Salesforce relationship lookups (Owner, CreatedBy,
# LastModifiedBy) can return a nested {Id, Name, Email, ...} object rather
# than a flat *Id field, and a bare "Name" sub-key inside one of these
# would otherwise slip past _REDACT_NAME_KEYS's deliberately narrow list.
# Found by actually testing redact() against a realistic nested shape
# before shipping Salesforce support, not assumed safe.
_REDACT_WHOLE_OBJECT_KEYS = {"owner", "createdby", "lastmodifiedby"}

_REDACTED_ACCOUNT_ID = "qa-placeholder-account-id"
_REDACTED_EMAIL = "qa-placeholder@example.com"
_REDACTED_NAME = "QA Placeholder"
_REDACTED_WHOLE_OBJECT = {"Id": _REDACTED_ACCOUNT_ID, "Name": _REDACTED_NAME}


def redact(value: Any) -> Any:
    """Recursively replace identity-carrying field values with fixed,
    non-empty placeholders (never blank strings -- a blank would be
    indistinguishable from the exact bug assert_no_placeholder_fields
    exists to catch)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_l = k.lower()
            if key_l in _REDACT_WHOLE_OBJECT_KEYS and isinstance(v, dict):
                out[k] = dict(_REDACTED_WHOLE_OBJECT)
            elif key_l in _REDACT_ACCOUNT_ID_KEYS:
                out[k] = _REDACTED_ACCOUNT_ID
            elif key_l in _REDACT_EMAIL_KEYS:
                out[k] = _REDACTED_EMAIL
            elif key_l in _REDACT_NAME_KEYS:
                out[k] = _REDACTED_NAME
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


# Gmail's raw message shape buries sender/recipient identity inside
# payload.headers -- a *list* of {"name": "From", "value": "..."} objects,
# where the interesting key is always the generic "value", never a
# distinctively-named field. redact()'s key-based matching structurally
# cannot see this -- found before shipping Gmail support (the same way the
# Salesforce Owner/CreatedBy gap was), not discovered after a fixture was
# already committed. Applied as a connector-specific pass, before the
# generic redact() runs, since the QA seed thread is sent to yourself, so
# both From and To carry your real address.
_SENSITIVE_GMAIL_HEADERS = {"from", "to", "cc", "bcc", "reply-to", "sender", "delivered-to", "return-path"}


def redact_gmail_message(raw: dict[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(raw)
    headers = ((raw.get("payload") or {}).get("headers")) or []
    for header in headers:
        if isinstance(header, dict) and str(header.get("name", "")).lower() in _SENSITIVE_GMAIL_HEADERS:
            header["value"] = _REDACTED_EMAIL
    return raw


# Slack's raw message shape identifies the author via a single generic
# "user" (or "bot_id") key -- not a distinctively-named field the way
# Confluence's authorId/Salesforce's OwnerId are, so adding it to the
# shared _REDACT_ACCOUNT_ID_KEYS would risk over-redacting an unrelated
# "user" key on some other connector's raw shape. Applied as a
# connector-specific pass, before the generic redact() runs, the same way
# Gmail's headers-list shape is handled.
_REDACTED_SLACK_USER_ID = "U00QAPLACEHOLDER"


def redact_slack_messages(raw: dict[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(raw)
    for message in raw.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        if message.get("user"):
            message["user"] = _REDACTED_SLACK_USER_ID
        if message.get("bot_id"):
            message["bot_id"] = _REDACTED_SLACK_USER_ID
        edited = message.get("edited")
        if isinstance(edited, dict) and edited.get("user"):
            edited["user"] = _REDACTED_SLACK_USER_ID
        for reaction in message.get("reactions", []) or []:
            if isinstance(reaction, dict) and reaction.get("users"):
                reaction["users"] = [_REDACTED_SLACK_USER_ID for _ in reaction["users"]]
        for slack_file in message.get("files", []) or []:
            if isinstance(slack_file, dict) and slack_file.get("user"):
                slack_file["user"] = _REDACTED_SLACK_USER_ID
    return raw


# ---------------------------------------------------------------------------- #
# Report
# ---------------------------------------------------------------------------- #

class CheckResult:
    def __init__(
        self, connector: str, method: str, seed_artifact: str, ok: bool, note: str,
        raw: Any = None, fixture_relpath: str = "",
    ) -> None:
        self.connector = connector
        self.method = method
        self.seed_artifact = seed_artifact
        self.ok = ok
        self.note = note
        self.raw = raw
        self.fixture_relpath = fixture_relpath


def _fixture_freshness_lines(connectors: list[str]) -> list[str]:
    lines = []
    for connector in connectors:
        conn_dir = FIXTURES_DIR / connector
        if not conn_dir.exists() or not any(conn_dir.glob("*.json")):
            lines.append(f"Fixture freshness: tests/fixtures/live/{connector}/ has no recorded fixtures yet.")
            continue
        newest = max((f.stat().st_mtime for f in conn_dir.glob("*.json")), default=0)
        age_days = (datetime.datetime.now().timestamp() - newest) / 86400
        lines.append(
            f"Fixture freshness: tests/fixtures/live/{connector}/*.json last recorded "
            f"{datetime.datetime.fromtimestamp(newest):%Y-%m-%d} ({age_days:.0f} days ago)."
        )
    return lines


def render_report(command: str, results: list[CheckResult]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    lines = [f"## PrivacyFence local QA check — {now}", "", f"Command: `{command}`", ""]
    lines.append("| Connector | Method | Seed artifact | Result | Notes |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        mark = "✅ pass" if r.ok else "❌ fail"
        lines.append(f"| {r.connector} | {r.method} | {r.seed_artifact} | {mark} | {r.note} |")
    lines.append("")
    connectors = sorted({r.connector for r in results})
    lines.extend(_fixture_freshness_lines(connectors))
    return "\n".join(lines)


# ---------------------------------------------------------------------------- #
# Manifest
# ---------------------------------------------------------------------------- #

def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        print(
            f"error: manifest not found at {MANIFEST_PATH}\n"
            "Work through docs/qa-environment-setup.md first, then fill in your seed artifacts'\n"
            "IDs/keys in that file.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------- #
# Raw-response capture -- lets --record dump the response exactly as the
# provider returned it, before ConfluenceClient._parse_page_v2 (etc.) ever
# touches it, without this script needing to know any endpoint path itself.
# Works for any client that funnels every call through a single
# ``self._request(fn, *a, **kw)`` choke point, as ConfluenceClient and
# JiraClient both do.
# ---------------------------------------------------------------------------- #

class RawCapture:
    def __init__(self, client: Any) -> None:
        self._client = client
        self.captured: Any = None

    def __enter__(self) -> "RawCapture":
        self._original = self._client._request

        def wrapped(fn: Callable, *args: Any, **kwargs: Any) -> Any:
            result = self._original(fn, *args, **kwargs)
            self.captured = copy.deepcopy(result)
            return result

        self._client._request = wrapped
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client._request = self._original


class RawCaptureCall:
    """Variant of RawCapture for a client whose choke point takes a single
    ``fn(state)`` callable instead of ``fn(*args, **kwargs)`` --
    SalesforceClient._call(fn) is the one example today (``fn`` closes over
    whatever arguments it needs and receives the built ``sf`` client as its
    only parameter). ``_call`` itself doesn't transform the result at all
    (unlike ConfluenceClient/JiraClient's ``_request``, which is also just a
    passthrough) -- the parsing happens one level up, in the calling method
    -- so what's captured here is exactly the same raw shape that method
    then parses.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.captured: Any = None

    def __enter__(self) -> "RawCaptureCall":
        self._original = self._client._call

        def wrapped(fn: Callable) -> Any:
            result = self._original(fn)
            self.captured = copy.deepcopy(result)
            return result

        self._client._call = wrapped
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client._call = self._original


class RawCaptureExecute:
    """Variant for the Google-API connectors (Gmail/Drive/Calendar/Contacts/
    Tasks), none of which have any PrivacyFence-level choke point at all --
    every method calls googleapiclient's chained-builder pattern
    (``service.users().messages().get(...).execute()``) inline, with the
    ``.execute()`` call built fresh, on a new ``googleapiclient.http.
    HttpRequest`` instance, every time. There is no per-client method to
    monkeypatch the way ConfluenceClient/JiraClient/SalesforceClient have --
    so this patches ``HttpRequest.execute`` itself, at the class level, for
    the scope of the ``with`` block only.

    This is a real, verified mechanism, not a guess: offline (no network, no
    credentials), ``googleapiclient.discovery.build(..., static_discovery=
    True)`` returns a real ``HttpRequest`` from a real chained call, and
    ``HttpRequest.execute()`` genuinely routes through this patched method
    when called normally (no special args needed at the call site) -- see
    ``tests/unit/test_qa_fixture_recorder.py`` for that exact offline proof,
    built specifically because a MagicMock-based service double (the
    existing pattern in test_gmail_client.py etc.) never touches
    ``HttpRequest`` at all and so can't verify this class.

    Patching a third-party class method process-wide is a bigger lever than
    RawCapture/RawCaptureCall's instance-level monkeypatching, but the
    recorder is single-threaded and sequential (one connector, one call, at
    a time), so there's no concurrent caller to interfere with, and the
    patch is removed the moment the ``with`` block exits either way.
    """

    def __init__(self) -> None:
        self.captured: Any = None

    def __enter__(self) -> "RawCaptureExecute":
        from googleapiclient.http import HttpRequest

        self._HttpRequest = HttpRequest
        self._original = HttpRequest.execute
        outer = self

        def wrapped(self_req: Any, http: Any = None, num_retries: int = 0) -> Any:
            result = outer._original(self_req, http=http, num_retries=num_retries)
            outer.captured = copy.deepcopy(result)
            return result

        HttpRequest.execute = wrapped
        return self

    def __exit__(self, *exc: Any) -> None:
        self._HttpRequest.execute = self._original


class RawCaptureApiCall:
    """Variant for SlackClient, whose choke point is slack_sdk's own
    ``WebClient.api_call(api_method, ...)`` -- every ``conversations_*``/
    ``users_*`` method on the SDK is a thin wrapper that calls this with a
    literal Slack API method name (e.g. ``"conversations.replies"``) and
    returns a ``SlackResponse`` whose ``.data`` is the raw dict.

    Unlike RawCapture/RawCaptureCall, a single SlackClient read can trigger
    more than one ``api_call()`` under the hood: ``get_thread_replies()``
    calls ``conversations.replies`` for the messages, then -- inside its own
    parsing step, ``_resolve_user_name()``/``get_user_info()`` -- calls
    ``users.info`` once per distinct message author not already cached. A
    capture that only kept the most recent result would end up holding
    ``users.info``'s response instead of ``conversations.replies``', so this
    keeps one per Slack API method name and lets the caller pick.
    """

    def __init__(self, slack_client: "SlackClient") -> None:
        self._web_client = slack_client._client
        self.captured: dict[str, Any] = {}

    def __enter__(self) -> "RawCaptureApiCall":
        self._original = self._web_client.api_call

        def wrapped(api_method: str, *args: Any, **kwargs: Any) -> Any:
            result = self._original(api_method, *args, **kwargs)
            self.captured[api_method] = copy.deepcopy(result.data)
            return result

        self._web_client.api_call = wrapped
        return self

    def __exit__(self, *exc: Any) -> None:
        self._web_client.api_call = self._original


# ---------------------------------------------------------------------------- #
# Atlassian (Jira + Confluence) -- one OAuth grant, one token file, shared by
# both clients exactly as daemon_main.py shares it. Both funnel every call
# through a single self._request(fn, *a, **kw) choke point, which is what
# RawCapture relies on -- a connector without that shape (most of the rest;
# see CONNECTOR_CHECKS below) needs its own capture mechanism, not just a
# new check_<connector>() function.
# ---------------------------------------------------------------------------- #

def _load_atlassian_config() -> tuple[dict[str, Any], str]:
    org_config = daemon_main.load_org_config()
    atlassian_org = org_config.get("atlassian") or {}
    if not atlassian_org.get("client_id"):
        raise SystemExit("Atlassian organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["atlassian"])
    token = daemon_main.load_atlassian_token(token_path)
    return {**atlassian_org, **(token or {})}, token_path


def _build_confluence_client() -> ConfluenceClient:
    config, token_path = _load_atlassian_config()
    return ConfluenceClient(config=config, token_file=token_path)


def _build_jira_client() -> JiraClient:
    config, token_path = _load_atlassian_config()
    return JiraClient(config=config, token_file=token_path)


def check_confluence(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    cfg = manifest.get("confluence") or {}
    space_key = cfg.get("space_key", "PFQA")
    seed_page_id = cfg.get("seed_page_id", "")
    seed_page_title = cfg.get("seed_page_title", f"PrivacyFence QA seed page {QATEST_TAG}")

    results: list[CheckResult] = []
    client = _build_confluence_client()

    # list_spaces -- only ever recorded filtered to the one QA space, never
    # a full real list of every space on the site.
    try:
        with RawCapture(client) as cap:
            spaces = client.list_spaces(max_results=250)
        match = next((s for s in spaces if s.key == space_key), None)
        ok = match is not None
        note = "found" if ok else f"space key {space_key!r} not in list_spaces() result"
        raw = None
        if record and ok and isinstance(cap.captured, dict):
            filtered = dict(cap.captured)
            filtered["results"] = [r for r in (cap.captured.get("results") or []) if r.get("key") == space_key]
            raw = redact(filtered)
        results.append(CheckResult("confluence", "list_spaces", space_key, ok, note, raw, "list_spaces.json"))
    except ConfluenceClientError as exc:
        results.append(CheckResult("confluence", "list_spaces", space_key, False, str(exc)))

    # get_page -- targeted at the seed page's id if the manifest has it,
    # otherwise resolved once by title.
    try:
        with RawCapture(client) as cap:
            page = client.get_page(seed_page_id) if seed_page_id else client.get_page_by_title(space_key, seed_page_title)
        tagged = QATEST_TAG in page.title
        complete = bool(page.title and page.author and page.updated and page.space_key)
        ok = tagged and complete
        if not tagged:
            note = f"fetched page title {page.title!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing popup field(s) -- title/author/updated/space_key not all present"
        else:
            note = "title, author, updated, space_key all present"
        raw = redact(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(CheckResult("confluence", "get_page", seed_page_title, ok, note, raw, "get_page.json"))
    except ConfluenceClientError as exc:
        results.append(CheckResult("confluence", "get_page", seed_page_title, False, str(exc)))

    return results


def check_jira(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    cfg = manifest.get("jira") or {}
    project_key = cfg.get("project_key", "PFQA")
    seed_issue_key = cfg.get("seed_issue_key", "")
    seed_issue_summary = cfg.get("seed_issue_summary", f"PrivacyFence QA seed issue {QATEST_TAG}")

    results: list[CheckResult] = []
    client = _build_jira_client()

    # list_projects -- JiraClient.list_projects() returns a plain list, not
    # Confluence's {"results": [...]} envelope; only ever recorded filtered
    # to the one QA project, never every real project on the site.
    try:
        with RawCapture(client) as cap:
            projects = client.list_projects(max_results=250)
        match = next((p for p in projects if p.key == project_key), None)
        ok = match is not None
        note = "found" if ok else f"project key {project_key!r} not in list_projects() result"
        raw = None
        if record and ok and isinstance(cap.captured, list):
            raw = redact([p for p in cap.captured if p.get("key") == project_key])
        results.append(CheckResult("jira", "list_projects", project_key, ok, note, raw, "list_projects.json"))
    except JiraClientError as exc:
        results.append(CheckResult("jira", "list_projects", project_key, False, str(exc)))

    # get_issue -- targeted at the seed issue's key if the manifest has it,
    # otherwise resolved once via a JQL search scoped to project + summary.
    try:
        if seed_issue_key:
            with RawCapture(client) as cap:
                issue = client.get_issue(seed_issue_key)
        else:
            found = client.search_issues(
                f'project = {project_key} AND summary ~ "{seed_issue_summary}"', max_results=1,
            )
            if not found:
                raise JiraClientError(
                    f"no issue found matching summary {seed_issue_summary!r} in {project_key!r}"
                )
            with RawCapture(client) as cap:
                issue = client.get_issue(found[0].key)
        tagged = QATEST_TAG in issue.summary
        # assignee is legitimately "(unassigned)"/empty on a fresh seed issue
        # -- not part of the completeness check, unlike Confluence's author,
        # which the client always populates from a real accountId.
        complete = bool(issue.key and issue.summary and issue.status)
        ok = tagged and complete
        if not tagged:
            note = f"fetched issue summary {issue.summary!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing popup field(s) -- key/summary/status not all present"
        else:
            note = "key, summary, status all present"
        raw = redact(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(CheckResult("jira", "get_issue", seed_issue_summary, ok, note, raw, "get_issue.json"))
    except JiraClientError as exc:
        results.append(CheckResult("jira", "get_issue", seed_issue_summary, False, str(exc)))

    return results


def _build_salesforce_client() -> SalesforceClient:
    org_config = daemon_main.load_org_config()
    sf_org = org_config.get("salesforce") or {}
    if not sf_org.get("consumer_key"):
        raise SystemExit("Salesforce organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["salesforce"])
    token = daemon_main.load_salesforce_token(token_path)
    config = {**sf_org, **token}
    return SalesforceClient(config=config, token_file=token_path)


def check_salesforce(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    cfg = manifest.get("salesforce") or {}
    report_id = cfg.get("report_id", "")
    report_name = cfg.get("report_name", "PrivacyFence QA Report")
    object_type = cfg.get("object_type", "Account")
    seed_record_id = cfg.get("seed_record_id", "")
    # No separate seed artifact here -- qa-environment-setup.md §8 already
    # has you create sample records tagged [QATEST]; the recorder just
    # targets one of those instead of adding a new one.
    seed_record_name = cfg.get("seed_record_name", f"PrivacyFence QA — Acme Test Co {QATEST_TAG}")

    results: list[CheckResult] = []
    client = _build_salesforce_client()

    # list_reports -- only ever recorded filtered to the one QA report,
    # never every real report accessible to the account.
    try:
        with RawCaptureCall(client) as cap:
            reports = client.list_reports()
        if report_id:
            match = next((r for r in reports if r.id == report_id), None)
        else:
            match = next((r for r in reports if r.name == report_name), None)
        ok = match is not None
        note = "found" if ok else f"report {report_id or report_name!r} not in list_reports() result"
        raw = None
        if record and ok and isinstance(cap.captured, dict):
            filtered = dict(cap.captured)
            filtered["records"] = [
                r for r in (cap.captured.get("records") or []) if r.get("Id") == match.id
            ]
            raw = redact(filtered)
        results.append(
            CheckResult("salesforce", "list_reports", report_id or report_name, ok, note, raw, "list_reports.json")
        )
    except SalesforceClientError as exc:
        results.append(CheckResult("salesforce", "list_reports", report_id or report_name, False, str(exc)))

    # get_record -- targeted at the seed record's id if the manifest has
    # it, otherwise resolved once via search() by its tagged Name.
    try:
        if seed_record_id:
            with RawCaptureCall(client) as cap:
                rec = client.get_record(object_type, seed_record_id)
        else:
            found = client.search(seed_record_name, object_types=object_type)
            if not found:
                raise SalesforceClientError(
                    f"no {object_type} record found matching name {seed_record_name!r}"
                )
            with RawCaptureCall(client) as cap:
                rec = client.get_record(object_type, found[0].id)
        name_field = str(rec.fields.get("Name") or rec.fields.get("name") or "")
        tagged = QATEST_TAG in name_field
        complete = bool(rec.id and name_field)
        ok = tagged and complete
        if not tagged:
            note = f"fetched record name {name_field!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing popup field(s) -- id/Name not both present"
        else:
            note = "id, Name present"
        raw = redact(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(
            CheckResult("salesforce", "get_record", seed_record_name, ok, note, raw, "get_record.json")
        )
    except SalesforceClientError as exc:
        results.append(CheckResult("salesforce", "get_record", seed_record_name, False, str(exc)))

    return results


def _build_gmail_client() -> GmailClient:
    org_config = daemon_main.load_org_config()
    client_config = daemon_main._google_client_config(org_config)
    if not client_config:
        raise SystemExit("Google organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["gmail"])
    return GmailClient(client_config=client_config, token_file=token_path)


def check_gmail(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    cfg = manifest.get("gmail") or {}
    seed_message_id = cfg.get("seed_message_id", "")

    # Unlike Confluence/Jira/Salesforce, Gmail has no cheap by-title resolve
    # fallback: GmailClient.list_messages() itself makes one .execute() call
    # per result on top of the list call (fetching each stub's metadata),
    # so "resolve then get" here would mean multiple uncontrolled calls
    # instead of one targeted one. seed_message_id is required.
    if not seed_message_id:
        return [CheckResult(
            "gmail", "get_message", "(unconfigured)", False,
            "gmail.seed_message_id is not set in tests/fixtures/qa_environment.yaml -- "
            "Gmail has no by-subject resolve fallback, fill this in from the seed thread "
            "in qa-environment-setup.md §1",
        )]

    results: list[CheckResult] = []
    client = _build_gmail_client()

    try:
        with RawCaptureExecute() as cap:
            message = client.get_message(seed_message_id)
        tagged = QATEST_TAG in message.subject
        complete = bool(message.sender and message.date and message.subject)
        ok = tagged and complete
        if not tagged:
            note = f"fetched message subject {message.subject!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing popup field(s) -- sender/date/subject not all present"
        else:
            note = "sender, date, subject all present"
        raw = None
        if record and ok and isinstance(cap.captured, dict):
            raw = redact(redact_gmail_message(cap.captured))
        results.append(CheckResult("gmail", "get_message", seed_message_id, ok, note, raw, "get_message.json"))
    except GmailClientError as exc:
        results.append(CheckResult("gmail", "get_message", seed_message_id, False, str(exc)))

    return results


def _build_drive_client() -> DriveClient:
    org_config = daemon_main.load_org_config()
    client_config = daemon_main._google_client_config(org_config)
    if not client_config:
        raise SystemExit("Google organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["drive"])
    return DriveClient(client_config=client_config, token_file=token_path)


def check_drive(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    # drive_get_file_metadata is auto-approved (no gate/preview -- see
    # connectors/drive.py), unlike every other connector's targeted read
    # here, so this only proves the raw-response -> DriveFile mapping
    # stays correct (contract drift), not popup-preview completeness. No
    # new seed artifact needed: targets the QA Sandbox folder
    # qa-environment-setup.md §2 already has you create, identified by its
    # exact name rather than a [QATEST] body tag (a folder has no body,
    # and this one is already unique/durable by construction).
    cfg = manifest.get("drive") or {}
    folder_name = cfg.get("folder_name", "PrivacyFence QA Sandbox")
    folder_id = cfg.get("folder_id", "")

    results: list[CheckResult] = []
    client = _build_drive_client()

    try:
        if folder_id:
            with RawCaptureExecute() as cap:
                file = client.get_file_metadata(folder_id)
        else:
            matches = client.list_files(
                f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                max_results=1,
            )
            if not matches:
                raise DriveClientError(f"no folder found matching name {folder_name!r}")
            with RawCaptureExecute() as cap:
                file = client.get_file_metadata(matches[0].id)
        tagged = file.name == folder_name
        complete = bool(file.id and file.name)
        ok = tagged and complete
        if not tagged:
            note = f"fetched file name {file.name!r} does not match expected {folder_name!r} -- refusing to record"
        elif not complete:
            note = "missing field(s) -- id/name not both present"
        else:
            note = "id, name present"
        raw = redact(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(
            CheckResult("drive", "get_file_metadata", folder_name, ok, note, raw, "get_file_metadata.json")
        )
    except DriveClientError as exc:
        results.append(CheckResult("drive", "get_file_metadata", folder_name, False, str(exc)))

    return results


def _build_calendar_client() -> CalendarClient:
    org_config = daemon_main.load_org_config()
    client_config = daemon_main._google_client_config(org_config)
    if not client_config:
        raise SystemExit("Google organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["calendar"])
    return CalendarClient(client_config=client_config, token_file=token_path)


def check_calendar(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    cfg = manifest.get("calendar") or {}
    calendar_id = cfg.get("calendar_id", "primary")
    seed_event_id = cfg.get("seed_event_id", "")
    seed_event_title = cfg.get("seed_event_title", f"PrivacyFence QA seed event {QATEST_TAG}")

    results: list[CheckResult] = []
    client = _build_calendar_client()

    try:
        if seed_event_id:
            with RawCaptureExecute() as cap:
                event = client.get_event(calendar_id, seed_event_id)
        else:
            # list_events is a single .execute() call (unlike Gmail's
            # list_messages), so a resolve-then-get fallback is safe here.
            found = client.list_events(calendar_id, max_results=1, query=seed_event_title)
            if not found:
                raise CalendarClientError(f"no event found matching query {seed_event_title!r}")
            with RawCaptureExecute() as cap:
                event = client.get_event(calendar_id, found[0].id)
        tagged = QATEST_TAG in event.title
        complete = bool(event.title and event.start_time and event.organizer_email)
        ok = tagged and complete
        if not tagged:
            note = f"fetched event title {event.title!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing popup field(s) -- title/start_time/organizer_email not all present"
        else:
            note = "title, start_time, organizer_email all present"
        raw = redact(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(CheckResult("calendar", "get_event", seed_event_title, ok, note, raw, "get_event.json"))
    except CalendarClientError as exc:
        results.append(CheckResult("calendar", "get_event", seed_event_title, False, str(exc)))

    return results


def _build_contacts_client() -> ContactsClient:
    org_config = daemon_main.load_org_config()
    client_config = daemon_main._google_client_config(org_config)
    if not client_config:
        raise SystemExit("Google organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["contacts"])
    return ContactsClient(client_config=client_config, token_file=token_path)


def check_contacts(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    # A contact's name/email/phone *are* the content under test here, not
    # someone else's identity leaking into an otherwise-synthetic page/
    # issue/event -- there's no separate "real author touched this" split
    # the way every other connector has. The usual redact() pass is
    # deliberately NOT applied: doing so would scrub the very field
    # mapping this check exists to verify (a real displayName/value would
    # come back as a placeholder either way, masking a genuine parsing
    # bug). See qa-environment-setup.md §5 and this repo's
    # external-api-contract-testing.md "Identity-field redaction" section.
    cfg = manifest.get("contacts") or {}
    seed_contact_resource_name = cfg.get("seed_contact_resource_name", "")
    seed_contact_display_name = cfg.get("seed_contact_display_name", f"PrivacyFence QA Test Contact {QATEST_TAG}")

    results: list[CheckResult] = []
    client = _build_contacts_client()

    try:
        if seed_contact_resource_name:
            with RawCaptureExecute() as cap:
                contact = client.get_contact(seed_contact_resource_name)
        else:
            found = client.search_contacts(seed_contact_display_name, max_results=1, source="personal")
            if not found:
                raise ContactsClientError(f"no contact found matching name {seed_contact_display_name!r}")
            with RawCaptureExecute() as cap:
                contact = client.get_contact(found[0].resource_name)
        tagged = QATEST_TAG in contact.display_name
        complete = bool(contact.resource_name and contact.display_name)
        ok = tagged and complete
        if not tagged:
            note = f"fetched contact name {contact.display_name!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing field(s) -- resource_name/display_name not both present"
        else:
            note = "resource_name, display_name present"
        raw = copy.deepcopy(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(
            CheckResult("contacts", "get_contact", seed_contact_display_name, ok, note, raw, "get_contact.json")
        )
    except ContactsClientError as exc:
        results.append(CheckResult("contacts", "get_contact", seed_contact_display_name, False, str(exc)))

    return results


def _build_tasks_client() -> TasksClient:
    org_config = daemon_main.load_org_config()
    client_config = daemon_main._google_client_config(org_config)
    if not client_config:
        raise SystemExit("Google organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["tasks"])
    return TasksClient(client_config=client_config, token_file=token_path)


def check_tasks(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    # Task carries no identity field at all (id/title/notes/due/status/
    # completed/updated/position/parent -- see tasks_client.py), so this
    # is the one connector where the generic redact() is a safe no-op
    # rather than a deliberate choice either way.
    cfg = manifest.get("tasks") or {}
    task_list_id = cfg.get("task_list_id", "")
    seed_task_id = cfg.get("seed_task_id", "")

    # Unlike Confluence/Jira/Salesforce/Calendar/Contacts, tasks_client.py
    # has no search-by-title method to reuse for a resolve fallback --
    # only list_tasks, an uncontrolled full-list fetch. Both ids required.
    if not task_list_id or not seed_task_id:
        return [CheckResult(
            "tasks", "get_task", "(unconfigured)", False,
            "tasks.task_list_id and tasks.seed_task_id must both be set in "
            "tests/fixtures/qa_environment.yaml -- Tasks has no by-title resolve fallback",
        )]

    results: list[CheckResult] = []
    client = _build_tasks_client()

    try:
        with RawCaptureExecute() as cap:
            task = client.get_task(task_list_id, seed_task_id)
        tagged = QATEST_TAG in task.title
        complete = bool(task.id and task.title)
        ok = tagged and complete
        if not tagged:
            note = f"fetched task title {task.title!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing field(s) -- id/title not both present"
        else:
            note = "id, title present"
        raw = redact(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(CheckResult("tasks", "get_task", seed_task_id, ok, note, raw, "get_task.json"))
    except TasksClientError as exc:
        results.append(CheckResult("tasks", "get_task", seed_task_id, False, str(exc)))

    return results


def _build_slack_client() -> SlackClient:
    org_config = daemon_main.load_org_config()
    slack_org = org_config.get("slack") or {}
    if not slack_org.get("client_id"):
        raise SystemExit("Slack organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["slack"])
    token = load_slack_token(token_path)
    return SlackClient(token.get("access_token", ""))


def check_slack(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    # No new seed artifact needed -- qa-environment-setup.md §3 already has
    # you create the privacyfence-qa-control channel with a durable,
    # [QATEST]-tagged seed message and threaded reply (it exists precisely
    # so it does *not* match the approved-channel grant); the recorder just
    # targets that thread via get_thread_replies instead of adding another.
    cfg = manifest.get("slack") or {}
    channel_name = cfg.get("channel_name", "privacyfence-qa-control")
    channel_id = cfg.get("channel_id", "")
    seed_thread_ts = cfg.get("seed_thread_ts", "")

    results: list[CheckResult] = []
    client = _build_slack_client()
    target = f"#{channel_name}"

    try:
        if not channel_id:
            found_channel = next(
                (c for c in client.list_channels(max_results=1000) if c.name == channel_name), None
            )
            if found_channel is None:
                raise SlackClientError(f"no channel found matching name {channel_name!r}")
            channel_id = found_channel.id

        if not seed_thread_ts:
            # Single call, not a fan-out -- get_channel_history() is a
            # cheap resolve, unlike Gmail's list_messages().
            history = client.get_channel_history(channel_id, limit=200)
            found_message = next((m for m in history if QATEST_TAG in (m.text or "")), None)
            if found_message is None:
                raise SlackClientError(f"no message carrying {QATEST_TAG} found in {target} history")
            seed_thread_ts = found_message.id

        with RawCaptureApiCall(client) as cap:
            replies = client.get_thread_replies(channel_id, seed_thread_ts)
        tagged = bool(replies) and QATEST_TAG in (replies[0].text or "")
        # channel_name is deliberately not part of this gate -- unlike
        # title/summary on the other connectors, a missing channel_name has
        # a graceful fallback in the popup preview (connectors/slack.py's
        # _channel_display falls back to the raw channel id), so it isn't
        # the kind of silent field-mapping bug this check exists to catch.
        complete = bool(replies) and bool(replies[0].text and replies[0].user_id)
        ok = tagged and complete
        if not tagged:
            note = f"thread starter does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing popup field(s) -- text/user_id not both present"
        else:
            note = "text, user_id present"
        raw = None
        if record and ok:
            captured = cap.captured.get("conversations.replies")
            if isinstance(captured, dict):
                raw = redact(redact_slack_messages(captured))
        results.append(CheckResult("slack", "get_thread_replies", target, ok, note, raw, "get_thread_replies.json"))
    except SlackClientError as exc:
        results.append(CheckResult("slack", "get_thread_replies", target, False, str(exc)))

    return results


CONNECTOR_CHECKS: dict[str, Callable[[bool, dict[str, Any]], list[CheckResult]]] = {
    "confluence": check_confluence,
    "jira": check_jira,
    "salesforce": check_salesforce,
    "gmail": check_gmail,
    "drive": check_drive,
    "calendar": check_calendar,
    "contacts": check_contacts,
    "tasks": check_tasks,
    "slack": check_slack,
}


# ---------------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------------- #

def run(mode: str, connectors: list[str], report_file: str | None) -> int:
    manifest = load_manifest()
    requested = connectors or sorted(CONNECTOR_CHECKS)
    record = mode == "record"

    all_results: list[CheckResult] = []
    for name in requested:
        check_fn = CONNECTOR_CHECKS.get(name)
        if check_fn is None:
            print(f"'{name}' has no recorder implementation yet -- see scripts/qa_fixture_recorder.py "
                  "CONNECTOR_CHECKS and docs/external-api-contract-testing.md's Part A.", file=sys.stderr)
            continue
        all_results.extend(check_fn(record, manifest))

    if record:
        for r in all_results:
            if r.raw is None:
                continue
            out_dir = FIXTURES_DIR / r.connector
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / r.fixture_relpath).write_text(json.dumps(r.raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    command = f"qa_fixture_recorder.py --{mode} {' '.join(requested)}"
    report = render_report(command, all_results)
    print(report)
    if report_file:
        Path(report_file).write_text(report + "\n", encoding="utf-8")

    return 0 if all(r.ok for r in all_results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--check", action="store_true", help="Smoke-check only; never writes a fixture.")
    mode_group.add_argument("--record", action="store_true", help="Check and (re-)record fixtures.")
    parser.add_argument("connectors", nargs="*", help="Connector name(s), e.g. confluence. Default: all implemented.")
    parser.add_argument("--report-file", help="Also save the printed report to this path.")
    args = parser.parse_args()

    mode = "record" if args.record else "check"
    return run(mode, args.connectors, args.report_file)


if __name__ == "__main__":
    raise SystemExit(main())
