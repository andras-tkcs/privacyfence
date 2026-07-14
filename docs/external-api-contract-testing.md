# External API Contract Testing (Design Proposal)

**Status: proposed, not yet implemented.** This document designs a testing layer this repo
doesn't have yet. See [Rollout plan](#rollout-plan) for what could ship first without any new
infrastructure.

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
  The regression test that now guards this (`test_last_modified_placeholder_when_missing`,
  same file) constructs `ConfluencePage` directly via a `make_page()` helper — it never goes
  through `ConfluenceClient._parse_page_v2`, so it proves the connector handles a missing field
  gracefully, not that the field mapping from the *real* API is complete.

Both bugs are instances of the same gap: nothing in the suite exercises the path
**real API response → `_parse_*` → dataclass → popup `preview`/`details_text`** end to end. Every
`*_client.py` has this same shape (a thin wrapper around `atlassian-python-api` / `simple-salesforce`
/ `google-api-python-client` / `slack-sdk` / `telethon`, with hand-rolled `_parse_*` methods turning
raw dicts into dataclasses), so the risk isn't Confluence-specific — it's structural to how every
connector is built.

`connector-qa-testing.md` already exists to catch exactly this class of bug, and it works — its own
"Example findings" section is where both bugs above were actually found. But it's a manual,
human-in-the-loop process run before releases: a person pastes a prompt into Claude Cowork, watches
dozens of native popups, and clicks through them one at a time. That's the right tool for exercising
the *gate* and the end-user experience, but it means API drift is only discovered whenever someone
next runs a full QA pass — potentially long after a provider shipped the breaking change.

## Goals

- Catch "the provider's API moved out from under us" and "a field the popup needs came back empty"
  automatically, closer to when the drift actually happens, without requiring a human QA pass.
- Keep the existing unit suite exactly as fast, deterministic, and secret-free as it is today —
  none of this should touch `pytest -v --cov=...`'s current 100%-pass-required PR gate.
- Reuse the `PFQA`-prefixed QA sandbox fixtures already documented in
  [`qa-environment-setup.md`](qa-environment-setup.md) rather than inventing a second set.
- Make drift *reviewable*: when a provider's response shape changes, a human should see a diff in a
  PR, not just a red build.

## Non-goals

- Replacing `connector-qa-testing.md`. That doc exercises the gate, auto-accept rules, the audit
  log, and the actual native popup UI — none of which this proposal touches. This is narrowly about
  the `*_client.py` layer: does the real API still return what the parsing code assumes.
- Testing write operations against live accounts on a schedule. `create_page`, `send_message`, etc.
  create real artifacts in real (albeit QA) accounts; running those unattended on a cron is a
  cleanup and rate-limit headache disproportionate to the value here. Layer 1 below is read-only.
- 100% field/endpoint coverage on day one. Start with the connectors most exposed to unofficial or
  actively-changing APIs (Confluence and Jira via `atlassian-python-api`, Salesforce via
  `simple-salesforce` — both already have a real breakage on record) before extending to the
  Google APIs, which are more heavily versioned and have historically drifted less.

## Design: three layers

### Layer 1 — Live contract tests (scheduled, not PR-gated)

A new `tests/contract/` directory, one module per connector (`test_confluence_contract.py`, etc.),
marked with a new `@pytest.mark.contract` marker registered in `pyproject.toml`. These construct
the real `*_client.py` classes — no mocking — using credentials for the same QA sandbox accounts
`qa-environment-setup.md` already sets up, and call real read-only methods against the known
`PFQA`-prefixed fixtures:

```python
@pytest.mark.contract
class TestConfluenceLiveContract:
    def test_get_page_returns_all_popup_fields(self, confluence_qa_client, pfqa_space_key):
        pages = confluence_qa_client.list_pages_in_space(pfqa_space_key, max_results=1)
        assert pages, "PFQA space has no pages — QA fixture is missing, not a code bug"
        page = confluence_qa_client.get_page(pages[0].id)
        assert page.title
        assert page.author        # feeds preview["Author"]
        assert page.updated       # feeds preview["Last modified"] — see the bug this would catch
        assert page.space_key
```

Because `testpaths = ["tests"]` in `pyproject.toml` would otherwise pick these up in the normal
run, the existing `tests.yml` job needs one change: `pytest -v --cov=... -m "not contract"`. A new
workflow (`external-api-contract.yml`) runs `pytest -m contract` on a weekly `schedule` plus
`workflow_dispatch`, using GitHub Actions secrets for the QA sandbox's stored **refresh tokens**
(not access tokens — those expire in hours). Every OAuth-backed client here already has its own
refresh-and-retry logic (`ConfluenceClient._try_refresh`, and the equivalent in `jira_client.py`),
so the workflow only needs to seed `client_id`/`client_secret`/`refresh_token` once; each run
mints its own short-lived access token the same way the app does in normal use.

A failure here doesn't block any PR — it's a scheduled job, and GitHub already emails
watchers/`CODEOWNERS` when a scheduled workflow run fails, which is enough signal to start with. A
dedicated "auto-file an issue on failure" step can be added later if that turns out to be too easy
to miss (see [Open questions](#open-questions)).

### Layer 2 — Golden fixture replay (runs in normal PR CI, no live network)

Layer 1 catches drift, but only weekly, and only for a human to notice. Layer 2 makes the *shape*
of a real API response a checked-in, versioned artifact, so drift shows up as an ordinary,
reviewable file diff instead of something someone has to go looking for.

A new script, `scripts/refresh_api_fixtures.py`, uses the same QA sandbox credentials as Layer 1 to
call each client's read methods and dump the **raw** response (before `_parse_*` touches it) to
`tests/fixtures/live/<connector>/<method>.json` — e.g.
`tests/fixtures/live/confluence/get_page.json`. This is a manual/scheduled operation (run by the
same weekly workflow, or by hand), not something every test run does. Since the raw response for
these read-only QA fixtures already contains nothing more sensitive than `PFQA` test content, no
separate scrubbing pass should be needed beyond confirming that by hand once per connector when the
script is written — call this out explicitly in review rather than assuming it.

New unit tests (in the existing `tests/unit/test_<connector>_client.py` modules, alongside the
current hand-authored-fixture tests, e.g. a `TestLiveFixtureParsing` class) load these recorded
JSON blobs and feed them through the real `_parse_*` methods — exactly like today's tests, except
the fixture is real recorded data instead of a fixture shaped by hand to match the code's
assumptions. These run in the normal, network-free, secret-free `pytest` job, so they gate PRs like
everything else and don't add flakiness or latency to CI.

When a provider changes something, the next scheduled fixture refresh captures the new shape; if
the parser doesn't handle it, this layer's replay test fails immediately in the PR that updates the
fixture — with a plain JSON diff showing exactly what changed, not just a live-API failure someone
has to go reproduce by hand. This is deliberately a thin, dependency-free JSON capture/replay
script rather than something like `vcrpy`: it keeps the fixture format as plain, reviewable JSON
(matching this repo's existing `dict`-shaped fixtures), and avoids taking on a new dependency (even
test-only) for something a ~20-line-per-connector capture shim covers — `vcrpy` can be revisited if
that shim grows unwieldy in practice.

### Layer 3 — Field-completeness structural check (no infrastructure needed — ship this first)

Independent of Layers 1–2, and requiring no secrets, schedule, or new CI job, is a narrower fix for
the *specific* class of bug the `last_modified` regression was: a `_parse_*` method (or the
connector code building `preview`/`details_text` from its output) silently falling back to an
empty/placeholder value because of a wrong attribute or key path.

Add a shared helper to `tests/helpers.py`:

```python
def assert_no_placeholder_fields(preview: dict, placeholders=("", "(unknown)", None)) -> None:
    """Assert a gated_call preview dict has no fallback/placeholder value, for a
    fixture that's supposed to be fully populated. Catches a `_parse_*` field
    mapping silently degrading to a default (see the confluence `last_modified`
    bug in tests/unit/connectors/test_confluence_connector.py) without needing
    to already know the bug exists.
    """
    blank = {k: v for k, v in preview.items() if v in placeholders}
    assert not blank, f"Preview fields fell back to a placeholder: {blank}"
```

Then, for each connector, extend the existing "full page"/"full record" fixture already present in
each `test_<connector>_client.py` (e.g. `TestParsePageV2::test_full_page_without_body` in
`test_confluence_client.py`) so the **same raw dict** is reused end to end: parsed by the real
`_parse_*` method, then run through the real connector method that builds the popup preview
(instead of the connector test's current pattern of constructing the dataclass directly via a
`make_page()`-style helper, which bypasses parsing entirely), then checked with
`assert_no_placeholder_fields`. This is the piece that would have failed on the `last_modified` bug
immediately, without a human needing to notice the popup was missing a field first.

This layer is cheap enough to fold into the existing connector-test checklist in
[`coding-and-testing-guidelines.md` §2.6](coding-and-testing-guidelines.md#26-new-connector-checklist)
as a fifth bullet once written, rather than treated as a separate opt-in system.

## File layout

```
tests/
  unit/                          # unchanged
  contract/                      # new — Layer 1, marked @pytest.mark.contract
    conftest.py                  # QA sandbox client fixtures, credential loading
    test_confluence_contract.py
    test_jira_contract.py
    test_salesforce_contract.py
    ...
  fixtures/
    live/                        # new — Layer 2, checked into git
      confluence/
        get_page.json
        list_spaces.json
      jira/
        ...
scripts/
  refresh_api_fixtures.py        # new — regenerates tests/fixtures/live/
.github/workflows/
  external-api-contract.yml      # new — weekly + workflow_dispatch, runs Layer 1 (and Layer 2 refresh)
  tests.yml                      # existing — one-line change: add -m "not contract"
```

## Rollout plan

Layer 3 has no dependency on the other two and finds a real, already-documented bug class on its
own — start there:

1. **Layer 3** — add `assert_no_placeholder_fields` to `tests/helpers.py`, wire it into
   Confluence's connector tests first (the connector with the on-record bug), then the
   new-connector checklist for everyone else. No CI or credential changes.
2. **Layer 2 skeleton** — add the `tests/fixtures/live/` directory and one hand-seeded fixture per
   connector (captured once, manually, from a real QA sandbox call) plus its replay test, to prove
   the mechanism before automating capture.
3. **`scripts/refresh_api_fixtures.py`** — automate what step 2 did by hand, for all connectors.
4. **Layer 1 + the scheduled workflow** — add `tests/contract/`, the `contract` marker, and
   `external-api-contract.yml`, wired to call `refresh_api_fixtures.py` as part of the same run so
   fixture refresh and live-contract-check stay in lockstep.

Each step ships independent value and can land as its own PR; nothing in steps 1–3 requires
provisioning CI secrets.

## Open questions

- **Credential provisioning**: who owns the QA sandbox's stored refresh tokens as GitHub Actions
  secrets, and what's the rotation/revocation story if this repo is forked (`CONTRIBUTING.md`
  already anticipates forks)? Needs an answer before step 4, not before step 1.
- **Failure signal**: is a scheduled-workflow-failure email enough, or does this need an
  auto-filed/updated tracking issue (as `connector-qa-testing.md`'s own findings section suggests
  manual QA runs already produce)? Start with the plain email; revisit if drift goes unnoticed in
  practice.
- **Fixture sanitization**: Layer 2's raw JSON dumps come from QA-only, already-fake data, but this
  should still get one explicit human review pass per connector when its capture shim is first
  written, rather than assumed safe by default.
- **Google connectors' priority**: `google-api-python-client` is officially versioned and has not
  produced a breakage like Confluence's, so it's reasonable to sequence Gmail/Drive/Calendar/
  Contacts/Tasks after Confluence/Jira/Salesforce rather than in parallel — but this is a judgment
  call worth revisiting once Layers 1–2 exist for the higher-risk connectors and the marginal cost
  of extending them is clearer.
