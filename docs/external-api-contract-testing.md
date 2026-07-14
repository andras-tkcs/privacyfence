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

## Test accounts & isolation

The right answer differs per connector, because "sandbox" means something different depending on
whether the provider's OAuth grant is naturally scoped or account-wide:

| Connector | Sandbox available? | Recommendation |
|---|---|---|
| Salesforce | Yes — a Developer Edition org is a real, free, permanent sandbox, isolated from any production org by construction. | Use it directly, as already planned. |
| Jira / Confluence | Partial — the `PFQA` project/space convention (`qa-environment-setup.md`) scopes *where the tests act*, but Atlassian OAuth grants are site-wide, not project-scoped — a bug in a test could technically still read/touch something outside `PFQA` on the same site. | Acceptable to reuse the existing real-site-with-scoped-project pattern for now, since it's already the accepted design elsewhere in this repo and the blast radius in practice is low (tests only ever pass `PFQA`-prefixed keys). Worth revisiting if this site is ever shared with unrelated real work. |
| Slack | No natural sandbox — a bot/user token's read scopes (`conversations.list`, etc.) apply workspace-wide even if a test only *intends* to touch one channel. | Recommend a **dedicated Slack workspace** (free tier) for unattended contract runs, not a channel inside a real workspace. `connector-qa-testing.md`'s human-supervised channel-in-real-workspace approach is fine when a person is watching every popup; it's a bigger blast radius once nothing is supervising the run. |
| Google (Gmail/Drive/Calendar/Contacts/Tasks) | No natural sandbox — OAuth scopes are account-wide. | Recommend a **dedicated Google account** created solely for this. Cheap (free), and isolates any bug's blast radius from your real mailbox/Drive/calendar entirely. |
| Telegram | Hardest to isolate — `telethon` authenticates a real user session tied to a phone number, not a bot token, and "Saved Messages" is inherently personal. | Recommend a **second Telegram account on a spare/secondary number** dedicated to QA, if you have one available (e.g. a Google Voice number or a spare SIM). If that's not practical, treat Telegram as the one connector that stays in the manual `connector-qa-testing.md` process rather than being automated — the cost of getting this wrong (an automated job doing something to your real personal chat history) is disproportionate to the value of automating this one connector. |

The general principle: reuse a real account only where the provider itself gives you a scoping
boundary a bug can't easily cross (Salesforce's separate org, Jira/Confluence's project/space keys
being the only IDs the tests ever pass). Anywhere the OAuth grant is account-wide, an unattended
job (no human watching a popup, unlike `connector-qa-testing.md`) should run against a throwaway
account, not your real one.

## Where this runs, and credential storage

- **Runner**: GitHub Actions. The existing `tests.yml` job uses `macos-latest` because it needs
  real AppKit/PyObjC (`osascript` popups, the menu bar). None of that applies here — these tests
  only exercise the `*_client.py` HTTP/SDK layer — so both new workflows below can run on the
  cheaper, faster `ubuntu-latest`.
- **Credential storage**: GitHub Actions **encrypted secrets** (or an **Environment** with
  secrets scoped to it, which additionally supports requiring manual approval before a job that
  uses them runs). Secrets are encrypted at rest, injected only into the job's environment at
  runtime, and GitHub automatically masks any matching substring in logs. Store only long-lived
  **refresh tokens** (or, for Slack/Telegram, whatever their long-lived credential form is) plus
  `client_id`/`client_secret` where applicable — never a short-lived access token — and let each
  client's existing refresh logic (`ConfluenceClient._try_refresh` and the equivalent in
  `jira_client.py`) mint a fresh access token per run, exactly like the app does in normal use.
- **Fork-PR safety**: `CONTRIBUTING.md` explicitly invites fork-based PRs. GitHub's default
  behavior already protects against a malicious fork PR exfiltrating these secrets: a plain
  `pull_request`-triggered workflow run gets **no repo secrets and no write-scoped `GITHUB_TOKEN`**
  when the PR comes from a fork. This design relies on that default — every workflow below must
  use the plain `pull_request` trigger (never `pull_request_target`, which *does* grant secrets to
  fork PRs and is the well-known way this class of protection gets accidentally defeated). A
  fork-originated PR simply won't have the credentials available and the live-refresh job (Layer 2
  below) should no-op/skip rather than fail when it detects that, so external contributions still
  pass the network-free parts of CI normally.
- Because these are dedicated test accounts (see above), not your real identity, revocation is
  cheap and low-stakes if a token ever needs to be rotated — another reason to prefer throwaway
  accounts over scoping your real Slack/Google/Telegram credentials down.

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

### Layer 2 — Golden fixture capture, recorded on PRs that touch connector code

Layer 2 makes the *shape* of a real API response a checked-in, versioned artifact — the regression
baseline every future PR gets replayed against — and, per your adjustment, recording it is part of
PR CI itself rather than a separate weekly-only job. Recording it at the point a connector actually
changes (instead of on an unrelated weekly cadence) means the fixture update lands in the same PR
that's likely the reason the shape needed to change, with a reviewable diff right there.

A new script, `scripts/refresh_api_fixtures.py`, uses QA sandbox credentials to call each client's
read methods and dump the **raw** response (before `_parse_*` touches it) to
`tests/fixtures/live/<connector>/<method>.json` — e.g. `tests/fixtures/live/confluence/get_page.json`.
A new workflow, `external-api-fixture-refresh.yml`, runs it on `pull_request`, **path-filtered** to
only fire when a PR touches `src/privacyfence/*_client.py` or `src/privacyfence/connectors/**` —
untouched connectors, and PRs that don't touch connector code at all, never trigger a live call. Per
the fork-PR note above, this workflow gets no secrets on a fork PR and should detect that and skip
cleanly rather than fail.

The job re-runs the capture, diffs the result against the committed `tests/fixtures/live/` files,
and:
- if identical, passes silently — the live API still matches the checked-in baseline;
- if different, fails the check and pushes the refreshed fixture as a commit onto the PR branch (or,
  more conservatively to start, just posts the diff and lets the PR author commit it deliberately —
  see the sequencing question below) so the reviewer sees exactly what changed on the provider's
  side, in the same PR, right when it's relevant.

New unit tests (in the existing `tests/unit/test_<connector>_client.py` modules, alongside the
current hand-authored-fixture tests, e.g. a `TestLiveFixtureParsing` class) load the committed JSON
fixtures and feed them through the real `_parse_*` methods — exactly like today's tests, except the
fixture is real recorded data instead of one shaped by hand to match the code's assumptions. These
themselves stay in the normal, network-free, secret-free `pytest` job (`tests.yml`), so every PR —
including fork PRs with no credentials — still gets regression coverage against whatever the
fixture currently says, even though only non-fork, connector-touching PRs can refresh it.

This is deliberately a thin, dependency-free JSON capture/replay script rather than something like
`vcrpy`: it keeps the fixture format as plain, reviewable JSON (matching this repo's existing
`dict`-shaped fixtures), and avoids taking on a new dependency (even test-only) for something a
~20-line-per-connector capture shim covers — `vcrpy` can be revisited if that shim grows unwieldy in
practice.

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
  external-api-contract.yml      # new — weekly + workflow_dispatch, runs Layer 1
  external-api-fixture-refresh.yml  # new — pull_request, path-filtered to connector code, runs Layer 2
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
   the mechanism before automating capture. Still no CI/credential changes yet — this step just
   proves the replay half works against real recorded data.
3. **Test accounts** — set up the dedicated Google account and Slack workspace, and decide on
   Telegram (see [Test accounts & isolation](#test-accounts--isolation)). Provision the resulting
   credentials as GitHub Actions secrets/Environment.
4. **`scripts/refresh_api_fixtures.py` + `external-api-fixture-refresh.yml`** — automate capture and
   wire it to run on connector-touching PRs, per Layer 2's design above.
5. **Layer 1 + `external-api-contract.yml`** — add `tests/contract/`, the `contract` marker, and the
   scheduled workflow, reusing the same credentials step 3 provisioned.

Each step ships independent value and can land as its own PR. Steps 1–2 need nothing beyond what's
in this repo already; step 3 is the one that needs your input on account setup before step 4 can be
built.
