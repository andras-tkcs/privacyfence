# External API Contract Testing (Design Proposal — Local-First)

**Status: partially implemented** — see the [Rollout plan](#rollout-plan)'s status note for exactly
what's shipped vs. still design. No live third-party credential is ever provisioned to
GitHub Actions, any other cloud CI, or any secret store this repo doesn't fully control on a
developer's own machine — that part is unchanged from the previous revision. What changed: this
runs against your **real, already-authenticated accounts** (per
[`qa-environment-setup.md`](qa-environment-setup.md)), not dedicated throwaway ones. The isolation
that matters here is at the *content* level, not the account level — the recorder only ever reads
the specific synthetic artifacts that doc has you create, never a blanket "whatever's there," and
redacts real account-identity metadata before anything is written to disk.

## Problem

Per [`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md), every `tests/unit/`
module mocks the connector's `*_client.py` (or mocks the third-party SDK object one layer inside
it — see `test_confluence_client.py`'s `make_client()`, which swaps `ConfluenceClient._client` for
a `MagicMock`). That's the right call for testing *this codebase's* logic in isolation, but it has
a structural blind spot: every fixture in those tests is hand-authored to match what the parsing
code already expects. If a provider's API drifts — a field renamed, an endpoint removed, a response
shape changed — the fixture drifts right along with the code in the author's head, and the test
suite stays green while the real integration is broken.

This isn't hypothetical for this repo. Two examples already on record:

- **Confluence's entire page-content path was broken in production** while unit tests passed:
  `list_pages_in_space`, `get_page`, `get_page_by_title`, `create_page`, and `update_page` all
  called Confluence v1 REST endpoints Atlassian had removed (410 Gone) — see the migration note at
  [`confluence_client.py:38-43`](../src/privacyfence/confluence_client.py#L38-L43) and the "Example
  findings" section of
  [`connector-qa-testing.md`](connector-qa-testing.md#example-findings-from-the-2026-07-run). It
  was only caught by a human running a full manual QA pass against live accounts.
- **A popup preview field was silently blank for an unknown period**:
  `confluence_get_page_by_title` built the "Last modified" preview field from
  `getattr(page, "last_modified", "")`, but `ConfluencePage` has no `last_modified` attribute —
  only `updated` — so the field was always empty even though the README documented it as part of
  the popup. See the docstring at
  [`tests/unit/connectors/test_confluence_connector.py:1-11`](../tests/unit/connectors/test_confluence_connector.py#L1-L11).
  The regression test that now guards this constructs `ConfluencePage` directly via a `make_page()`
  helper — it never goes through `ConfluenceClient._parse_page_v2`, so it proves the connector
  handles a missing field gracefully, not that the field mapping from the *real* API is complete.

Both bugs are instances of the same gap: nothing in the suite exercises the path
**real API response → `_parse_*` → dataclass → popup `preview`/`details_text`** end to end.
`connector-qa-testing.md` already catches this class of bug — its own "Example findings" section is
where both were actually found — but it's a manual, human-in-the-loop process run before releases.
Drift is only discovered whenever someone next runs a full QA pass.

## Design principles

1. **No live third-party credential is ever provisioned to GitHub Actions, any other cloud CI, or
   anywhere but a developer's own machine.** The recorder tool below runs locally, using the exact
   same OAuth token files `privacyfence-app --gmail-oauth` (etc.) already write to a git-ignored
   `credentials/` directory — nothing new to invent, store, or rotate in the cloud.
2. **The recorder only ever touches content created specifically for QA** — the seed
   email/contact/issue/page/message from [`qa-environment-setup.md`](qa-environment-setup.md), found
   by its `[QATEST]`/`PFQA` tag, never a blanket "list everything and grab the first result." This
   is the actual isolation boundary now that real accounts are in play: not which account holds the
   data, but which specific, pre-labeled-synthetic item the recorder is allowed to read.
3. **Real account-identity metadata is redacted before anything touches disk.** Even a page/issue/
   email you create yourself carries your real account's identity in structural fields the API
   returns regardless of how synthetic the content is — author email, display name, account ID,
   organizer address. These get replaced with placeholder values before a fixture is written, every
   time, not just reviewed for after the fact.
4. **CI stays exactly as it is today**: fast, deterministic, secret-free, running on every PR
   including forks. It only ever *replays* fixture files already committed to the repo — it never
   makes a live call to anything.
5. **The recorded fixture is a durable regression-test asset**, not a throwaway debugging artifact —
   committed to git, reviewed like code, and reused by every future test run until someone
   deliberately re-records it.

## Part A — The local fixture recorder

### Where credentials live

`daemon_main.py` already resolves every connector's OAuth token to a fixed, git-ignored path —
`TOKEN_FILES` (`credentials/token.json`, `credentials/atlassian_token.json`, etc.), resolved against
`paths.data_dir()`, which in a from-source checkout is the repo root itself (`.gitignore` already
excludes `credentials/*`). This is the existing local-credential mechanism the app already uses for
every developer running from source — the recorder reuses it as-is: whatever accounts you've already
authenticated from the menu bar per `qa-environment-setup.md` are what it uses. No separate
credential store, no separate accounts.

### The tool

`scripts/qa_fixture_recorder.py` builds each connector's real `*_client.py` the same way
`daemon_main.py` does — `load_org_config()` + `TOKEN_FILES` — but constructs the bare client
directly rather than a full gated `Connector`, since it only ever calls a small, curated set of
**targeted, read-only** methods per connector: always by the specific ID/key of a
`qa-environment-setup.md` seed artifact (read from the non-secret manifest below), never a blanket
list/search call kept for its own sake. Implemented today: **every connector** — Confluence, Jira,
Salesforce, Gmail, Drive, Calendar, Contacts, Tasks, Slack, and Telegram. Five capture mechanisms
cover them:

- `RawCapture` — Confluence and Jira funnel every client call through the same
  `self._request(fn, *a, **kw)` choke point (same author, same Atlassian OAuth grant); works for
  both unmodified.
- `RawCaptureCall` — Salesforce has an analogous but differently-shaped choke point, `_call(fn)` (a
  single `fn(state)` callable, not `fn(*args, **kwargs)`); a genuinely different wrapper, not a
  copy of `RawCapture` with the connector name swapped.
- `RawCaptureExecute` — Gmail, Drive, Calendar, Contacts, and Tasks have **no** PrivacyFence-level
  choke point at all; every method calls `googleapiclient`'s chained builder inline
  (`service.users().messages().get(...).execute()`), building a fresh `HttpRequest` every time. This
  one instead patches `HttpRequest.execute` itself, at the class level, for the scope of a `with`
  block — a bigger lever than instance-level monkeypatching, but safe here since the recorder is
  single-threaded and sequential. Genuinely verified, not assumed: a MagicMock service double (the
  pattern every existing Google-connector client test in this repo uses) never constructs a real
  `HttpRequest` and so can't exercise this class at all — `tests/unit/test_qa_fixture_recorder.py`
  instead builds a real, offline `HttpRequest` (`googleapiclient.discovery.build(...,
  static_discovery=True)` needs no network or credentials) with a fake httplib2 transport, and
  confirms the patched method is what actually runs. One mechanism, five connectors — proven once
  for Gmail, then reused unmodified for Drive/Calendar/Contacts/Tasks (each still needed its own
  `check_<connector>()`, seed artifact, and — Gmail and Contacts specifically — its own reasoning
  about what redaction should or shouldn't do, see below).
- `RawCaptureApiCall` — Slack has yet another shape: no PrivacyFence-level choke point, but
  `slack_sdk`'s own `WebClient.api_call(api_method, ...)` **is** one, underneath every
  `conversations_*`/`users_*` wrapper method. Different from the other three in one important way:
  a single `SlackClient` read can trigger more than one `api_call()` — `get_thread_replies()` calls
  `conversations.replies` for the messages, then its own parsing step
  (`_resolve_user_name()`/`get_user_info()`) calls `users.info` once per distinct message author not
  already cached. A capture that only kept the most recent result would silently end up holding
  `users.info`'s response instead of `conversations.replies`', so this one keys captures by Slack API
  method name (`{"conversations.replies": {...}, "users.info": {...}}`) and the caller picks the one
  it actually wants.
- `RawCaptureTelethon` — Telegram is the most different of all: its client (`telegram_client.py`) is
  fully `async` (Telethon is natively asyncio-based), and its raw responses are typed TL objects
  (`Message`/`Dialog`/`User`/...), not JSON dicts the way every REST-backed connector's response is —
  there's no HTTP response body to begin with, since Telethon speaks MTProto directly. Two
  consequences, handled separately: (1) `check_telegram()` itself is a plain synchronous function
  (matching every other entry in `CONNECTOR_CHECKS`), but internally just calls `asyncio.run()` on an
  `async` implementation — the rest of the recorder (`run()`/`main()`) never has to know Telegram is
  different at all. (2) The capture class wraps a single bound *async* method on the raw
  `telethon.TelegramClient` instance (`"get_messages"`) the same instance-level way
  `RawCapture`/`RawCaptureCall` do, then converts the captured TL object to a plain JSON-serializable
  form via a small recursive helper, `_telethon_to_jsonable()` — needed because `TLObject.to_dict()`
  (Telethon's own generated code) resolves most nesting but still leaves `datetime`/`bytes` leaf
  values un-JSON-serializable, and Telethon's *non*-generated wrapper classes (`custom.Dialog`, not
  used by this recorder but worth naming the trap) don't recursively resolve nested TL objects via
  `to_dict()` at all.

```bash
python3 scripts/qa_fixture_recorder.py --check                          # every implemented connector
python3 scripts/qa_fixture_recorder.py --check confluence jira          # just these two
python3 scripts/qa_fixture_recorder.py --record salesforce              # re-record its fixtures
python3 scripts/qa_fixture_recorder.py --check --report-file r.md # also save the report to a file
```

- `--check`: calls each connector's read methods against its seed artifact, asserts non-empty
  results and no exceptions, prints a pass/fail summary. This is the local-only replacement for
  needing to spin up the full app + a Cowork session + click through popups just to know whether the
  client layer still talks to the provider correctly.
- `--record [connector ...]`: does the same targeted calls, redacts identity fields (below), and
  dumps the result to `tests/fixtures/live/<connector>/<method>.json`, ready to review and commit
  like any other change. With no connector names given, both modes run every connector
  `CONNECTOR_CHECKS` currently implements — there's no separate `all` keyword.

For the handful of *list*-shaped calls worth recording too (proving the list-envelope shape parses,
not just a single-item get), the recorder filters the raw response down to **only the entries whose
tag matches the seed artifact** before writing anything to disk — e.g. for
`gmail_list_messages`/`jira_search_issues`/`confluence_search`, keep only the result(s) whose
subject/summary/title contains `[QATEST]`, drop everything else from the recorded file. A real
inbox/site returning real results as part of proving the tool *works* is fine and unavoidable; a
real inbox/site's results ending up *committed to git* is not, and this filter is what keeps the two
separate.

### Identity-field redaction

Before any fixture is written, the recorder replaces the *value* of known identity-carrying field
**names** with fixed placeholders, recursively through the whole response — `authorId`/`accountId`/
`ownerId`/`createdById`/`lastModifiedById` → `"qa-placeholder-account-id"`, `email`/`emailAddress`/...
→ `qa-placeholder@example.com`, `displayName`/`publicName`/... → `"QA Placeholder"`. Deliberately
**key**-based, not a pattern match against values: a bare `name` key is left alone, since it's just
as often a space's or a record's legitimate display name as it is a person's — matching on value
shape (anything email-*shaped*) would have false-positived on that and other ordinary content. This
runs unconditionally, not as an optional review step, because it has to hold even when a human
forgets to check: the whole point is that a fixture destined for a public git history never carries
the real account owner's real email or name, even though the *content* around it (the page title,
the issue summary) was already synthetic to begin with.

A **separate** whole-object rule (`_REDACT_WHOLE_OBJECT_KEYS`) exists for keys whose entire nested
value is a person, not just one field of it — Salesforce's `Owner`/`CreatedBy`/`LastModifiedBy`
relationship lookups return a nested `{Id, Name, Email, ...}` object rather than a flat `*Id`
string, and that nested `Name` would otherwise slip straight past the deliberately-narrow bare-`name`
exclusion above. This was a real gap, not a hypothetical one: mocking a realistic Salesforce
response with a nested `Owner` object during Salesforce's own implementation surfaced it before any
fixture was ever recorded — see the [Rollout plan](#rollout-plan)'s status note for exactly what
that mocked response looked like. It's the reason this section says "review the redacted output"
isn't optional busywork.

A **third**, connector-specific pass exists for Gmail alone: `redact_gmail_message()`. Gmail's raw
message shape buries sender/recipient identity inside `payload.headers` — a *list* of `{"name":
"From", "value": "..."}` objects, where the interesting key is always the generic `"value"`, never
a distinctively-named field. Neither the key-based rule above nor the whole-object rule can see
this at all (there's no `From`/`To` *key* anywhere in the structure to match against). Found the
same way as the Salesforce gap — by actually building a realistic offline response and checking the
redacted output, not by assuming the generic pass would cover it — and worth calling out explicitly
because a future connector is likely to have its own version of this same shape (identity value
addressed indirectly through a sibling key, not the field name itself), which no amount of growing
the generic key lists will ever catch.

A **fourth** connector-specific pass, `redact_slack_messages()`, exists for Slack: its raw message
shape identifies the author through a single generic `"user"` (or `"bot_id"`) key — not the kind of
distinctively-named field (`authorId`, `emailAddress`) the shared key lists above are built around,
so adding `"user"` to them outright would risk over-redacting an unrelated `"user"` key on some
other connector's raw shape instead of catching a real gap. Also covers the same identity field
where it recurs one level down — a reply's `edited.user`, a `reactions[].users` list, a
`files[].user` — the same kind of nested-shape gap Salesforce's `Owner`/`CreatedBy`/`LastModifiedBy`
turned out to have.

A **fifth** pass, `redact_telegram_messages()`, exists for Telegram: its raw `Message.to_dict()`
shape identifies the chat/sender through numeric `Peer` sub-objects — `{"_": "PeerUser", "user_id":
123456789}` nested under `peer_id`/`from_id`/`saved_peer_id` — again a generic key one level down,
not a distinctively-named top-level field. Especially relevant here since the only chat this
recorder ever targets is Saved Messages, where `peer_id` is always *your own* real numeric account
id, not some other party's — the exact same "even synthetic content still says a real account
touched it" concern as everywhere else, just with an `int` in place of an email/name string.

This is a genuinely different, and larger, concern than it looks: content-level synthetic data
(covered by `qa-environment-setup.md`) and identity-level metadata redaction (covered here) are two
separate problems, and only content being synthetic does **not** imply identity metadata is safe —
an API response for a page you wrote yourself still says *you* wrote it, in your real name and real
email, regardless of what the page says.

### Guardrail against recording the wrong thing

Before writing a fixture, the recorder confirms the object it just fetched actually matches the
expected seed artifact — its ID/key came from a small, checked-in manifest
(`tests/fixtures/qa_environment.yaml`, not secret, just IDs/keys/tags) and its title/summary
contains the `[QATEST]` tag `qa-environment-setup.md` has you put there. If either check fails
(wrong ID resolved, tag missing — e.g. because the seed artifact was renamed or deleted), that one
fixture is refused and reported as a failing row (with a note naming what didn't match) — the run
keeps going for every other connector/method, but nothing gets written to disk for the one that
failed the check, instead of silently recording whatever it happened to fetch. Combined with
the redaction step above, this is what makes "real accounts, not dedicated ones" a safe design:
every write to disk is gated on "is this the specific synthetic thing I expected" before it's gated
on "did I strip the identity fields," not relying on either check alone.

## Part B — Regression tests built on recorded fixtures

New tests in the existing `tests/unit/test_<connector>_client.py` modules — a `TestLiveFixtureParsing`
class alongside the current hand-authored-fixture tests — load the committed JSON files from
`tests/fixtures/live/<connector>/` and feed them through the real `_parse_*` methods:

```python
class TestLiveFixtureParsing:
    """Fixtures recorded from a real, tagged QA seed artifact by
    scripts/qa_fixture_recorder.py -- real API shape, not hand-authored, with
    identity fields redacted. Re-record via that script if this ever starts
    failing after a genuine Confluence API change."""

    def test_get_page_fixture_still_parses(self, client):
        raw = json.loads((FIXTURES_DIR / "confluence" / "get_page.json").read_text())
        page = client._parse_page_v2(raw, include_body=True)
        assert page.title and page.author and page.updated and page.space_key
```

These run in the normal, network-free, secret-free `pytest` job — every PR, including PRs from
forks with no credentials at all, gets regression coverage against whatever the fixture currently
says. When a provider changes something, re-recording (Part A, run locally, by a human, at whatever
cadence they choose) updates the fixture in an ordinary commit with an ordinary diff; if the parser
doesn't handle the new shape, this test fails immediately in that same PR, with the JSON diff right
there to explain why.

**Field-completeness, folded in here rather than treated separately**: extend `tests/helpers.py`
with a small helper —

```python
def assert_no_placeholder_fields(preview: dict, placeholders=("", "(unknown)", None)) -> None:
    """Assert a gated_call preview dict has no fallback/placeholder value.
    Catches a _parse_* field mapping silently degrading to a default (see the
    confluence last_modified bug in test_confluence_connector.py) without
    needing to already know the bug exists.
    """
    blank = {k: v for k, v in preview.items() if v in placeholders}
    assert not blank, f"Preview fields fell back to a placeholder: {blank}"
```

— and, for each connector, run the **recorded** fixture (not a hand-authored one) through the real
`_parse_*` method, then through the real connector method that builds the popup preview, then check
it with `assert_no_placeholder_fields`. Using the real recorded fixture here instead of a
hand-authored "full" one is strictly better: it proves the mapping is complete against the *actual*
shape the provider returns, not against whatever shape a human assumed when writing the fixture by
hand — and since identity fields were already redacted to fixed, non-empty placeholders (not blank
strings), this check still works correctly on a redacted fixture without a false "field is empty"
result.

## Optional: a credential-free staleness reminder

The one thing this design trades away, relative to a hypothetical always-on live check, is automatic
detection of drift between recordings — bounded by how often a human remembers to re-run the
recorder. A cheap, **fully credential-free** mitigation: a scheduled GitHub Actions workflow that
holds no secrets at all and just checks how old the newest commit touching `tests/fixtures/live/**`
is, opening/updating a reminder issue past some threshold (e.g. 90 days). Zero credential-custody
risk (it never talks to any external API), optional and separable from everything above.

## File layout

```
tests/
  unit/                          # unchanged, gains TestLiveFixtureParsing per connector
  fixtures/
    qa_environment.yaml          # new — non-secret manifest of seed-artifact IDs/keys/tags
    live/                        # new — checked into git, the regression baseline
      confluence/
        get_page.json
        search.json              # list-shaped, filtered to [QATEST]-tagged results only
      jira/
        ...
scripts/
  qa_fixture_recorder.py         # new — run locally only, never in CI
.github/workflows/
  fixture-staleness-reminder.yml # new, optional — credential-free, just checks file age
  tests.yml                      # unchanged
```

## Rollout plan

1. **Field-completeness helper** (`assert_no_placeholder_fields`) — ship immediately, no dependency
   on anything else here; wire into Confluence's connector tests first.
2. **`qa-environment-setup.md` seed artifacts** — create the tagged synthetic content per connector,
   added as a step alongside that connector's existing fixtures (e.g. Confluence §10, Jira §9)
   rather than as a separate doc — only Confluence and Jira have this today.
3. **`scripts/qa_fixture_recorder.py`** (`--check` mode only, to start) — prove the tool can
   authenticate and call every connector's targeted read methods successfully, before it ever writes
   a fixture file.
4. **Redaction + guardrail + `--record` mode** — these three ship together, not separately; recording
   without them defeats the design. Start recording real fixtures, one connector at a time, each as
   its own reviewable PR — review the redacted output specifically for anything identity-shaped that
   the redaction list missed, since that list is necessarily connector-specific and easy to
   under-cover on the first pass.
5. **`TestLiveFixtureParsing` + field-completeness wired to real fixtures** — as each connector's
   fixtures land, add its replay tests.
6. **Optional staleness reminder workflow** — whenever convenient; it's decoupled from everything
   else and adds no risk.

**Status**: 1, 2, and 5 are done for **every connector** — Confluence, Jira, Salesforce, Gmail,
Drive, Calendar, Contacts, Tasks, Slack, and Telegram (`assert_no_placeholder_fields`
in `tests/helpers.py`; `TestFieldCompleteness` in Confluence/Jira/Salesforce's connector test
modules specifically — the three whose popup preview fields were already the target of a real,
historical field-mapping bug; the rest (Drive/Tasks are auto-approved with no popup to check;
Gmail/Calendar/Contacts/Slack/Telegram's equivalent coverage is still just `TestLiveFixtureParsing`,
not yet extended to `TestFieldCompleteness`) don't have it yet;
`TestLiveFixtureParsing` in each client's test module, skipped until a real fixture exists for
each — Telegram's version replays through a small reconstructed stand-in object rather than the raw
dict directly, since `_parse_message()` reads Telethon object attributes, not dict keys; see the
comment above `_message_from_fixture()` in `test_telegram_client.py`). 3 and 4 shipped together
rather than staged, since the guardrail and redaction logic weren't separable in practice from the
recording code path itself — `scripts/qa_fixture_recorder.py` implements `--check`/`--record` for
all ten now, with `CONNECTOR_CHECKS` fully populated. Verified against realistic mocked/offline API
responses for each, including the guardrail correctly refusing an untagged/stale-ID/mismatched-name
fetch, **and** — as a permanent regression suite now, not just one-off manual runs —
`tests/unit/test_qa_fixture_recorder.py`, which is also where `RawCaptureExecute` gets its offline
`HttpRequest` proof and `RawCaptureTelethon` gets its real-`to_dict()` proof (see above). Four real
redaction gaps were found and fixed via that verification, not after the fact: Salesforce's
whole-object `Owner`/`CreatedBy`/`LastModifiedBy` case, Gmail's `payload.headers`-list case, Slack's
`user`/`bot_id`-as-generic-key case, and Telegram's `peer_id`/`from_id`-as-nested-`Peer`-object case
(see "Identity-field redaction" above for all four) — and one deliberate *non*-redaction decision
was made the same careful way: Contacts' recorded fixture is left unredacted on purpose, since a
contact's own name/email/phone are the content under test, not someone else's identity. No real
fixture has been recorded for any of the ten yet (each needs a real, authenticated account and a
seed artifact per `qa-environment-setup.md` §1/§2/§3/§4/§5/§6/§7/§8/§9/§10 — something this can't be
done from a sandboxed environment); `tests/fixtures/qa_environment.yaml` ships with all ten
connectors' placeholder values, ready to fill in. Step 6 hasn't started.

No step in this plan requires provisioning any credential to GitHub, CI, or any cloud service.

---

## Local checks before opening a PR

This is the day-to-day answer to "what do I run before creating a PR" once this system exists —
folded into [`coding-and-testing-guidelines.md` §2.7](coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo)'s
Definition of Done as the authoritative checklist; this section is the worked example behind that
one bullet.

**Worked example**: you fixed a parsing bug in `confluence_client.py`.

1. Run the normal suite first — unchanged by any of this:
   ```bash
   pytest -v --cov=src/privacyfence --cov-report=term-missing
   ```
2. Because this PR touches a `*_client.py` file, also run the recorder in `--check` mode, scoped to
   the connector you touched (fast — a handful of targeted calls against the `PFQA`-tagged seed
   artifacts from `qa-environment-setup.md`, not a full re-record):
   ```bash
   python3 scripts/qa_fixture_recorder.py --check confluence
   ```
   This is the exact class of check that would have caught both bugs in the [Problem](#problem)
   section before they shipped — it calls the real API and confirms every field the popup path
   needs (`title`, `author`, `updated`, `space_key`, ...) actually comes back non-empty.
3. Two outcomes:
   - **Passes, and the live shape hasn't changed**: nothing else to do. `--check` never writes a
     file, so there's nothing new to commit from this step.
   - **Fails, or your fix was specifically in response to the provider's shape changing**: run
     `python3 scripts/qa_fixture_recorder.py --record confluence`, inspect the diff under
     `tests/fixtures/live/confluence/*.json` — it should be a small, meaningful shape change, with
     identity fields already redacted to placeholders (if anything in the diff looks like a real
     email or name, the redaction list needs a fix *before* you commit, not after) — then commit
     the updated fixtures alongside your code fix, in the same PR.
4. Paste the check's report (below) into the PR description.

### When to run this

Only when the PR touches `src/privacyfence/*_client.py` or `src/privacyfence/connectors/**` — not
every PR. A docs change, a `gate.py` change, a `menu_bar.py` change has no reason to make a live
call. `--check` with no argument runs every connector; `--check <connector>` scopes it to just the
one(s) you touched, which is both faster and keeps the report focused on what's actually relevant
for a reviewer to look at.

### The report

`--check` and `--record` both print the same small, deterministic Markdown table to stdout, and
accept `--report-file <path>` to also save it — structural results only (pass/fail per method,
which fields were present, fixture age), never message/page/issue *content*, for the same reason a
recorded fixture never carries it:

```markdown
## PrivacyFence local QA check — 2026-07-14T10:32Z

Command: `qa_fixture_recorder.py --check confluence`

| Connector  | Method     | Seed artifact      | Result  | Notes                                        |
|------------|------------|---------------------|---------|-----------------------------------------------|
| confluence | list_spaces| —                   | ✅ pass | 4 spaces returned                             |
| confluence | get_page   | `PFQA` seed page    | ✅ pass | title, author, updated, space_key all present |
| confluence | search     | `[QATEST]` tag      | ✅ pass | 1 matching result                             |

Fixture freshness: tests/fixtures/live/confluence/*.json last recorded 2026-06-02 (42 days ago).
```

Paste this directly into the PR description under a `## Local QA check` heading (wrap it in a
`<details>` block if you'd rather keep it collapsed by default) — that's the "attach to the PR"
mechanism. No new GitHub infrastructure, no CI job holding credentials, just a copy-paste block a
reviewer can read without re-running anything themselves or needing access to the QA accounts at
all. Don't commit the report file itself to the repo — it's a point-in-time artifact of your local
run, not a durable asset the way the fixtures it's reporting on are.

---

## Evaluation

### Cybersecurity — credentials and real accounts

**What's unchanged from the credential-custody question, regardless of which accounts are used:**
no live third-party credential is ever provisioned to GitHub Actions, any other cloud CI, or any
secret store outside a developer's own machine. That removes the supply-chain exposure a
live-CI design would have (this project's full dependency tree — `atlassian-python-api`,
`simple-salesforce`, `telethon`, etc. — running in the same environment as a live credential, on
every scheduled or PR-triggered run) and the "did we configure the fork-PR protection correctly"
question entirely, by never having a secret in that environment to begin with.

**What changed by using real accounts instead of dedicated throwaway ones, and what that actually
costs:** the account-level blast-radius argument from the earlier revision of this doc (a leaked QA
credential can only expose synthetic data because the *account* is disposable) no longer applies —
a leaked credential for your real Gmail/Jira/Slack/Salesforce/Telegram account is exactly as
sensitive as it always was, recorder or not. Three things carry the actual weight instead:

- **Content-level isolation** (`qa-environment-setup.md`): every artifact the recorder or the manual
  QA process touches is one you deliberately created with synthetic content, found by an explicit
  `[QATEST]`/`PFQA` tag — never "whatever's there." This bounds what *could* leak through this
  system specifically to content that was never sensitive in the first place, without needing the
  account itself to be disposable.
- **Identity-field redaction**: content being synthetic does not make an API response's structural
  identity fields (author email, account ID, display name) synthetic — those are still your real
  identity, on a real account, in every recording. This is a genuinely separate control from content
  isolation and the design above treats it as mandatory, not optional, specifically because a
  fixture file is headed for a git history that (per `CONTRIBUTING.md`'s fork-based contribution
  model) is effectively public.
- **The recorder's own credential exposure is now identical to the app's normal exposure** — the
  same token files, the same local machine, the same trust boundary as running PrivacyFence itself
  day to day. No new credential-custody surface was introduced; the recorder is just another local
  process reading the same files the daemon already reads.

**Net assessment**: this is a smaller, more targeted set of guarantees than the previous
dedicated-account revision offered, and correctly so — it protects the actual thing at risk (real
account credentials staying only ever local, and specific real content never leaking through this
particular pipeline) without asking you to maintain a second identity per connector for a benefit
you didn't need. The redaction step is the one piece that's new *because* real accounts are in
play, and it's worth treating as load-bearing, not an afterthought — it's the difference between
"this fixture is safe to commit" and "this fixture has my name and email in it."

### Test coverage

**What this buys, concretely:**

- Closes the exact gap `connector-qa-testing.md`'s own findings document: a real API response shape
  (Confluence's v1→v2 migration) or a real field-mapping mistake (`last_modified`) now has an
  automated, git-diffable check, instead of depending entirely on a human noticing during a manual
  QA pass.
- Fixtures become a durable, versioned regression asset — once Confluence's actual v2 shape is
  recorded, `TestLiveFixtureParsing` protects that shape from silent regressions in `_parse_page_v2`
  forever after, independent of whether anyone re-records again soon.
- Costs nothing in CI speed, determinism, or fork-PR compatibility — every PR still runs the exact
  same fast, network-free, 100%-pass-required suite it does today.

**What this deliberately does *not* cover:**

- **Coverage is bounded by the seed artifacts, deliberately narrow ones.** A single Confluence page,
  a single Jira issue, one Gmail thread. Edge cases — permission-denied records, pagination
  boundaries, deleted/archived content, unusual unicode, rate-limit/5xx responses — aren't exercised
  unless someone deliberately creates a seed artifact for that case too. This was true under the
  dedicated-account design as well; it doesn't change here.
- **Nothing here exercises the gate, the popup UI, or the audit log.** `connector-qa-testing.md`
  remains the only thing that does, and remains necessary before releases.
- **Targeting reads at a single tagged artifact, instead of grabbing "the first result of a list
  call," is deliberately narrower than what a live-CI or dedicated-account design could have
  recorded** (e.g. a full, real `list_pages_in_space` response) — that's the direct cost of the
  content-isolation principle above: broader recording would mean broader real-data exposure in the
  committed fixture. The list-shaped recordings described in Part A (filtered to `[QATEST]`-tagged
  entries only) are the compromise — they prove the envelope shape parses without capturing anything
  beyond the one artifact that's already safe to record.
- **Drift-detection latency is "however often a human runs the recorder,"** not automatic. The
  optional staleness-reminder workflow partially offsets this without any credential risk.
- **Telegram and the `trusted_sender_domain` Gmail rule are the two places a real, unlabeled
  artifact's existence (not content) will still matter once those connectors get recorder
  support** — `qa-environment-setup.md` §1 already notes there's no synthetic substitute for "a
  domain you actually receive mail from"; the same constraint applies to Telegram (§7), since
  `telethon` has no bot-token equivalent for reading a real chat's own history. The rule for the
  recorder is the same one this doc uses everywhere else: confirm behavior/shape, never persist
  content.

**Net assessment**: a real, durable improvement over today's mocked-only suite, narrower in what it
records than a dedicated-account design would allow, in exchange for not requiring a second identity
per connector. That trade is the right one given the actual goal — keep testing your real accounts
safely — rather than solving an account-isolation problem that wasn't the one being asked for.
