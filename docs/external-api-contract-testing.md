# External API Contract Testing (Design Proposal — Local-First)

**Status: proposed, not yet implemented.** This revises an earlier version of this design that
ran live API calls from GitHub Actions using stored service credentials. That approach is
retired in favor of the one below: **no live third-party credential ever touches GitHub Actions,
any cloud CI, or any file that could be `git add`-ed by accident.** Only a developer's own machine
ever holds them, and the only thing that leaves that machine is already-recorded, already-synthetic
fixture data pulled from the fully isolated accounts in
[`qa-environment-setup.md`](qa-environment-setup.md).

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
   any secret store this repo doesn't fully control on a developer's own machine.** The recorder
   tool below runs locally, using the exact same OAuth token files `privacyfence-app --gmail-oauth`
   (etc.) already writes to a git-ignored `credentials/` directory — nothing new to invent, store,
   or rotate in the cloud.
2. **Every account the recorder touches is a dedicated, disposable QA identity**
   (`qa-environment-setup.md`), never your real accounts. This is a second, independent line of
   defense on top of principle 1 — even a mistake in how the recorder is run can only ever touch
   synthetic data.
3. **CI stays exactly as it is today**: fast, deterministic, secret-free, running on every PR
   including forks. It only ever *replays* fixture files already committed to the repo — it never
   makes a live call to anything.
4. **The recorded fixture is a durable regression-test asset**, not a throwaway debugging artifact
   — committed to git, reviewed like code, and reused by every future test run until someone
   deliberately re-records it.

## Part A — The local fixture recorder

### Where credentials live

`daemon_main.py` already resolves every connector's OAuth token to a fixed, git-ignored path —
`TOKEN_FILES` (`credentials/token.json`, `credentials/atlassian_token.json`, etc.), resolved against
`paths.data_dir()`, which in a from-source checkout is the repo root itself (`.gitignore` already
excludes `credentials/*`). This is the existing local-credential mechanism the whole app uses for
every developer running from source — the recorder reuses it as-is rather than inventing a second
credential format.

To keep QA credentials fully separate from whatever your normal dev checkout is authenticated
against, run the recorder from its **own dedicated git worktree**, per this repo's existing worktree
convention (`CLAUDE.md`):

```bash
git worktree add ~/Coding/worktrees/privacyfence-qa-recorder main
cd ~/Coding/worktrees/privacyfence-qa-recorder
```

That worktree's `credentials/` and `org/` directories are now used for nothing except the QA
identities from `qa-environment-setup.md`. Install `org_config_qa.json` there (built via
`scripts/build_org_bundle.py`, as described in that doc) and authenticate every connector headlessly
with the existing CLI flags:

```bash
privacyfence-app --gmail-oauth
privacyfence-app --drive-oauth
privacyfence-app --calendar-oauth
privacyfence-app --contacts-oauth
privacyfence-app --tasks-oauth
privacyfence-app --slack-oauth
privacyfence-app --salesforce-oauth
privacyfence-app --atlassian-oauth
privacyfence-app --telegram-setup
```

signing in as the corresponding QA identity in each browser/prompt. This step is entirely manual,
by design — it's the one point a human deliberately confirms which account is being authenticated.

### The tool

A new script, `scripts/qa_fixture_recorder.py`, reuses the same per-connector client-construction
logic `daemon_main.build_connectors()` already has (org config + token file → a real `*_client.py`
instance) rather than duplicating it, and calls a small, curated set of **read-only** methods per
connector against the `PFQA1`/`PFQA2`-style fixtures from `qa-environment-setup.md`:

```bash
python3 scripts/qa_fixture_recorder.py --check              # smoke test only, no files written
python3 scripts/qa_fixture_recorder.py --record confluence  # re-record one connector's fixtures
python3 scripts/qa_fixture_recorder.py --record all         # re-record everything
```

- `--check`: calls each connector's read methods, asserts non-empty results and no exceptions,
  prints a pass/fail summary. This is the local-only replacement for needing to spin up the full
  app + a Cowork session + click through popups just to know whether the client layer still talks
  to the provider correctly — a fast thing to run whenever you touch a `*_client.py`.
- `--record <connector>`: does the same calls, and additionally dumps the **raw** response (before
  `_parse_*` touches it) to `tests/fixtures/live/<connector>/<method>.json` in the worktree's own
  checkout — since the recorder runs inside a git worktree of this same repo, "record" and "commit"
  are the same ordinary git workflow as any other change; nothing needs to be copied between
  machines or repos.

### Guardrail against pointing at the wrong account

Because the whole design depends on these credentials always resolving to a QA-only identity, the
recorder refuses to run unless the live identity it just authenticated against matches a small,
**non-secret** manifest checked into the repo:

```yaml
# tests/fixtures/qa_environment.yaml — not secret, safe to commit; site/workspace
# names aren't credentials, and this is what keeps a mistake from being silent.
confluence_site: privacyfence-qa.atlassian.net
jira_site: privacyfence-qa.atlassian.net
slack_workspace: "PrivacyFence QA"
salesforce_instance_name_contains: "PrivacyFence QA"
google_account_hint: "<the QA Gmail address, so a human can eyeball it>"
```

Before the first call for a connector, the tool checks the client's own `check_connection()` result
(site URL, workspace name, instance URL, authorized email — all things the clients already return)
against this manifest and aborts loudly on a mismatch, rather than silently recording (or, worse,
in `--record` mode, silently overwriting a committed fixture with) a response from the wrong
account. This is a real safety check, not documentation — it's the thing that stops "I forgot which
worktree/checkout I had open" from becoming "I just recorded my real Confluence data into a
fixture file."

### Sanitization

Because every account behind these credentials is QA-only by construction
(`qa-environment-setup.md`), there's no real personal or organizational data to strip from a
recording in the first place — the guardrail above is what prevents that data from ever entering
the pipeline, which is a stronger property than "scrub it after the fact." Still worth one human
skim of the first recorded fixture per connector, mostly to confirm nothing unexpected (an
account-linkage hint, an internal id format) is in there, not because sensitive data is expected.

## Part B — Regression tests built on recorded fixtures

New tests in the existing `tests/unit/test_<connector>_client.py` modules — a `TestLiveFixtureParsing`
class alongside the current hand-authored-fixture tests — load the committed JSON files from
`tests/fixtures/live/<connector>/` and feed them through the real `_parse_*` methods:

```python
class TestLiveFixtureParsing:
    """Fixtures recorded from the isolated QA site by scripts/qa_fixture_recorder.py --
    real API shape, not hand-authored. Re-record via that script if this ever
    starts failing after a genuine Confluence API change."""

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

**Layer 3, folded in here rather than treated separately**: extend `tests/helpers.py` with a small
helper —

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
hand-authored "full" one is strictly better than the original version of this idea: it proves the
mapping is complete against the *actual* shape the provider returns, not against whatever shape a
human assumed when writing the fixture by hand.

## Optional: a credential-free staleness reminder

The one thing this design trades away, relative to the earlier live-CI version, is automatic
detection of drift between recordings — now bounded by how often a human remembers to run the
recorder, not by a weekly scheduled job. A cheap, **fully credential-free** mitigation: a scheduled
GitHub Actions workflow that holds no secrets at all and just checks how old the newest commit
touching `tests/fixtures/live/**` is, opening/updating a reminder issue if it's past some threshold
(e.g. 90 days). This adds zero credential-custody risk (it never talks to any external API) while
partially offsetting the latency trade-off — worth doing, but explicitly optional and separable from
everything above.

## File layout

```
tests/
  unit/                          # unchanged, gains TestLiveFixtureParsing per connector
  fixtures/
    qa_environment.yaml          # new — non-secret identity manifest, the recorder's guardrail
    live/                        # new — checked into git, the regression baseline
      confluence/
        get_page.json
        list_spaces.json
      jira/
        ...
scripts/
  qa_fixture_recorder.py         # new — run locally only, never in CI
.github/workflows/
  fixture-staleness-reminder.yml # new, optional — credential-free, just checks file age
  tests.yml                      # unchanged
```

Notably absent, compared to the earlier version of this design: any new GitHub Actions workflow
that holds service credentials, any `contract` pytest marker, any `tests/contract/` directory. All
of that lived in Layer 1 of the previous design, which is removed entirely.

## Rollout plan

1. **Layer 3 helper** (`assert_no_placeholder_fields`) — ship immediately, no dependency on
   anything else here; wire into Confluence's connector tests first.
2. **`qa-environment-setup.md`** — build the isolated accounts (separate doc, already rewritten).
3. **`scripts/qa_fixture_recorder.py`** (`--check` mode only, to start) — prove the tool can
   authenticate against the QA worktree and call every connector's read methods successfully,
   before it ever writes a fixture file.
4. **`--record` mode + the guardrail manifest** — start recording real fixtures, one connector at a
   time, each as its own reviewable PR.
5. **`TestLiveFixtureParsing` + Layer 3 wired to real fixtures** — as each connector's fixtures land,
   add its replay tests.
6. **Optional staleness reminder workflow** — whenever convenient; it's decoupled from everything
   else and adds no risk.

No step in this plan requires provisioning any credential to GitHub, CI, or any cloud service —
that's the entire point of the redesign.

---

## Evaluation

### Cybersecurity — credentials and real accounts

**What changed from the previous version of this design, and why it matters:**

The earlier design stored QA-account refresh tokens as GitHub Actions secrets and ran live API
calls from two new workflows (a scheduled one, and one triggered on connector-touching PRs). Even
with the fork-PR protections that design documented (secrets withheld from `pull_request` runs
originating from forks, avoiding `pull_request_target`), it still had a residual attack surface that
this local-only redesign removes entirely:

- **Supply-chain exposure during the live run itself.** The workflow installed this project's full
  dependency set (`atlassian-python-api`, `simple-salesforce`, `telethon`, `slack-sdk`,
  `google-api-python-client`, ...) into the *same* environment holding live QA credentials. A
  compromised transitive dependency, or a compromised third-party GitHub Action used anywhere in
  that workflow, would have had a live credential sitting right next to it — a real, if
  low-probability, exfiltration path that has nothing to do with getting the fork-PR trigger
  configuration right. Local-only credential custody eliminates this: the recorder's dependencies
  are the same ones already trusted to run on a developer's machine for every other purpose (the
  daemon itself, the test suite), and there is no cloud execution environment holding a live
  credential at all, ever.
- **A single point of cloud-side custody.** Even encrypted, GitHub Actions secrets are a shared
  resource governed by repo/org permission settings this project doesn't fully control end-to-end
  (GitHub's own infrastructure, anyone with admin access to the repo's secret settings). Removing
  them removes a whole category of "did we configure this correctly" risk — architectural absence
  of a secret is more robust than correctly-configured presence of one.
- **This wasn't the only mitigation, though — dedicated QA accounts (`qa-environment-setup.md`)
  independently bound the blast radius of any of the above**, and that design decision is unchanged
  and still load-bearing here: even in the worst case (a developer's own machine or worktree is
  compromised), only synthetic data in disposable, easily-revoked accounts is exposed — never a real
  mailbox, real Jira/Confluence site, real Slack workspace, or real Salesforce org.

**What still needs care under this design:**

- **Local machine hygiene** becomes the entire security boundary for these credentials — standard
  practice (disk encryption, screen lock, not a shared machine) is now doing real work, though this
  was already true for every real credential the app itself holds during normal development.
- **The guardrail manifest is the mechanism that turns "wrong worktree" into a loud failure instead
  of a silent bad recording** — it's a small piece of logic, but it's the one thing standing between
  a mistake and a fixture file (or, in the worst case if the discipline of "QA-only accounts" is ever
  relaxed later, real data) landing in git history. Worth treating its correctness as seriously as
  the gate itself, proportionally.
- **Telegram's phone-number binding remains the one place identity can't be fully synthetic** —
  covered in `qa-environment-setup.md` §7 with the recommendation to use a number you actually own
  long-term rather than a disposable one, specifically because a recycled number is a realistic
  account-takeover vector for a persistent, repeatedly-used session (unlike a one-time OTP).
- **This design says nothing new about `connector-qa-testing.md`'s own credential handling** — that
  process still runs against whatever accounts a human has authenticated in the PrivacyFence menu
  bar on their normal dev machine. Migrating it fully onto the same isolated QA identities this doc
  establishes is a natural follow-up, not something this proposal does on its own.

**Net assessment**: for a project whose entire purpose is guarding personal data, not putting a
live third-party credential in any cloud service — full stop, not "correctly scoped and monitored"
— is the more defensible default. The latency cost (below) is a reasonable trade for it.

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

**What this deliberately does *not* cover — stated plainly, not glossed over:**

- **Coverage is bounded by what the QA fixtures happen to contain.** A Confluence page with every
  field populated, a Jira issue with one specific custom field. Edge cases — permission-denied
  records, pagination boundaries, deleted/archived content, unusual unicode, rate-limit/5xx
  responses — aren't exercised unless someone deliberately seeds them into the QA environment too.
  The two-project/two-space setup in `qa-environment-setup.md` gives a contrast case, not edge-case
  breadth.
- **Nothing here exercises the gate, the popup UI, or the audit log.** `connector-qa-testing.md`
  remains the only thing that does, and remains necessary before releases — this proposal is
  explicitly scoped to the `*_client.py` layer only (see Non-goals, preserved from the earlier
  version of this doc).
- **Drift-detection latency is now "however often a human runs the recorder,"** worse than the
  previous design's weekly scheduled check. The optional staleness-reminder workflow above partially
  offsets this without reintroducing any credential risk, but it's an honest trade-off, not a wash —
  faster detection was the one real thing given up to get to zero cloud credential custody.
- **Telegram likely stays out of this system entirely** unless a long-term-owned secondary number is
  available (`qa-environment-setup.md` §7), meaning that connector keeps relying solely on
  `connector-qa-testing.md`'s manual process for the foreseeable future.

**Net assessment**: this is a real, durable improvement over today's mocked-only suite for the eight
connectors where a QA identity is practical, at the cost of slower (human-paced rather than
weekly-automatic) drift detection and continued reliance on the manual process for gate/UI coverage
and for Telegram specifically. Given the security trade-off above, that's the right call for this
project's risk profile — the previous design's faster detection wasn't worth the credential-custody
surface it required.
