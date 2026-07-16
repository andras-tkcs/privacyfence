# Testing Policy

What runs where, and when. This repo has three tiers of testing, only the first of which runs in
GitHub Actions — the other two need a real macOS machine, real screen, and/or real authenticated
accounts, none of which CI has or should have. See
[`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) for how to *write* tests;
this document is about which ones run automatically versus which ones a human has to run.

## 1. Automated suite — every PR, in CI

`.github/workflows/tests.yml` runs on every push to `main` and every pull request:

```bash
pytest -v --cov=src/privacyfence --cov-report=term-missing
```

on a `macos-latest` runner (this app depends on real AppKit/PyObjC behavior, so it can't run on
Linux CI). A 100% pass rate is required to merge; the coverage report is informational only —
nothing gates on a specific percentage.

This tier is fully self-contained: no network calls to Gmail/Slack/Jira/etc., no credentials, no
manual steps. It includes:

- Every module under `tests/unit/`, one test module per `src/privacyfence/` module.
- Each connector's `TestLiveFixtureParsing` class (in `tests/unit/test_<connector>_client.py`),
  which replays a **previously recorded** fixture from `tests/fixtures/live/<connector>/` through
  the real `_parse_*` method — still fully offline, since it's reading a committed JSON file, not
  making a live API call. See [§2.1](#21-qa_fixture_recorderpy---check---record) below for how those
  fixtures get recorded in the first place. A connector with no recorded fixture yet has its
  `TestLiveFixtureParsing` tests skip (not fail) with a message pointing at the recorder.
- `tests/unit/test_qa_fixture_recorder.py` — unit tests for the recorder script itself
  (`scripts/qa_fixture_recorder.py`), exercised against mocked/offline API responses. This is
  different from actually running the recorder: these tests prove the recorder's own logic
  (redaction, capture mechanisms, the tag guardrail) is correct without touching any real account.
- `tests/unit/test_approval_window.py` — builds the real AppKit view tree for every popup shape and
  asserts on its content (buttons, PII tint/banner, summary rows, details text), without ever
  calling the real modal loop (`runApproval_()`/`NSApplication.runModalForWindow_()`). See
  [§2.2](#22-qa_popup_smokepy) for the one thing this construction-only coverage doesn't reach.

## 2. Local-only checks — run manually before opening/updating a relevant PR, never in CI

Two scripts exist specifically because some failure classes can't be caught by a fully-mocked,
fully-offline suite. Both are excluded from CI on purpose — one needs real, authenticated
third-party accounts; the other needs a real screen and a real click — and both print the same
kind of small, deterministic Markdown report meant to be pasted into the PR description so a
reviewer doesn't have to re-run anything or have access to the same accounts/hardware themselves.

### 2.1 `qa_fixture_recorder.py --check` / `--record`

Every `tests/unit/test_<connector>_client.py` module mocks the connector's `*_client.py` (or the
third-party SDK object one layer inside it), which is correct for testing this codebase's own
parsing logic in isolation — but it has a structural blind spot: a hand-authored mock fixture can
drift out of sync with what the real provider API actually returns (a field renamed, an endpoint
removed, a response shape changed) while the mocked test suite stays green. `scripts/
qa_fixture_recorder.py` closes that gap by calling the real, targeted read methods against a real,
already-authenticated account.

**Never run in CI.** It reuses the exact OAuth token files `privacyfence-app --<connector>-oauth`
writes to the git-ignored `credentials/` directory, and only ever targets one specific,
`[QATEST]`-tagged seed artifact per connector — set up once per environment via
[`qa-environment-setup.md`](qa-environment-setup.md), resolved through the non-secret, git-ignored
manifest `tests/fixtures/qa_environment.yaml` (see
[`qa_environment.yaml.example`](../tests/fixtures/qa_environment.yaml.example) for the template). No
credential is ever provisioned to GitHub Actions or any other cloud service to make this possible.

Two modes:

- `--check [connector ...]` — calls each connector's read methods against its seed artifact,
  asserts non-empty/expected results, prints a report. Never writes a file. Safe to run any time.
- `--record [connector ...]` — the same calls, plus identity-field redaction (author email, account
  id, display name, ...) and structural de-identification (opaque resource ids, decorative URLs —
  neither of which any test actually depends on the specific value of), then writes the result to
  `tests/fixtures/live/<connector>/<method>.json`.

**When to run this**: only when a PR touches `src/privacyfence/*_client.py` or
`src/privacyfence/connectors/**` — not every PR. Scope it to the connector(s) touched:

```bash
python3 scripts/qa_fixture_recorder.py --check confluence
```

- **Passes, live shape unchanged**: nothing else to do. `--check` never writes a file.
- **Fails, or the fix was specifically in response to a provider shape change**: run
  `--record <connector>`, inspect the diff under `tests/fixtures/live/<connector>/*.json` — it
  should be a small, meaningful shape change, with identity fields already redacted to placeholders
  (if anything in the diff looks like a real email, name, or account-specific id, the redaction
  logic needs a fix before committing, not after) — then commit the updated fixtures alongside the
  code fix, in the same PR.
- Paste the printed report (or the file from `--report-file <path>`) into the PR description under
  a `## Local QA check` heading.

### 2.2 `qa_popup_smoke.py`

`test_approval_window.py` covers popup *content* construction on every PR, but deliberately leaves
one thing untested: whether the real modal loop actually blocks and a real click actually reaches
it (e.g. a modal loop wired to the wrong window, or a button whose target/action never fires) —
exactly the class of failure construction-only tests can't catch.

**Never run in CI.** It requires macOS, real AppKit, and Accessibility permission granted to
whatever process runs it (it drives a real click via `System Events`), and pops real, visible
windows on-screen for a couple of seconds each — run it locally, not headless.

**When to run this**: whenever `approval_window.py`'s modal-loop plumbing changes (not every popup
content change — those are covered by `test_approval_window.py` on every PR).

```bash
python3 scripts/qa_popup_smoke.py
```

Paste the printed report into the PR description under a `## Popup smoke check` heading, same
convention as §2.1.

## 3. Full manual QA pass — before a release, not per-PR

[`connector-qa-testing.md`](connector-qa-testing.md) drives every tool through a live Claude
Cowork/Desktop session connected to the real `privacyfence` daemon, against real accounts, watching
what actually prompts. This is the only thing that exercises the gate, the popup UI, and the audit
log end to end — none of tiers 1 or 2 do. Run it before a release, or after any change to
`gate.py`/`auto_accept.py`/`resource_grants.py`/menu-bar auto-accept UI broadly, not on every PR.

## Quick reference

| Check | Runs in CI? | When |
|---|---|---|
| `pytest` (full suite) | Yes, every PR | Always — this is the merge gate |
| `qa_fixture_recorder.py --check` | No | PR touches a `*_client.py`/`connectors/**` file |
| `qa_popup_smoke.py` | No | PR touches `approval_window.py`'s modal-loop plumbing |
| `connector-qa-testing.md`'s live Cowork pass | No | Before a release, or a broad gate/auto-accept change |

None of the "No" rows require a credential, secret, or macOS Accessibility permission to ever be
granted to GitHub Actions or any other cloud CI — they exist specifically because that's not
something this project is willing to do, not as a stopgap until it is.
