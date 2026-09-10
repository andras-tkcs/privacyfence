# Connector Live-Test CI Integration Plan

Setup and implementation plan for (1) acquiring dedicated test accounts for Google, Atlassian,
Salesforce, and Slack, (2) using them to automate what `docs/qa-environment-setup.md` and
`scripts/qa_fixture_recorder.py` currently require a human to run by hand, and (3) implementing
Phase 3.12 of `docs/security-remediation-plan.md` (TST-08 through TST-13).

**Decision this plan is built on** (see the "How this differs" framing below — flag, don't decide
silently, is this repo's own convention): `docs/testing-policy.md` §2 currently states, as a
permanent position, that no connector credential is ever provisioned to GitHub Actions or any
other cloud CI. That position stands **unchanged for GitHub-hosted runners** in this plan. What
changes is the addition of a **self-hosted runner** — a machine this project (not GitHub) owns and
controls — as a new, narrowly-scoped tier where the dedicated test-account credentials *do* live,
outside GitHub's shared infrastructure and outside fork-PR-triggered workflows entirely. Phase B
below covers exactly why this is safer than storing the same secrets as GitHub Actions
(GitHub-hosted) secrets, and Phase D updates `testing-policy.md` to describe the new tier
honestly rather than leaving the old "never" language standing next to code that contradicts it.

Nothing in `.github/workflows/tests.yml` (the GitHub-hosted, every-PR, every-fork merge gate)
changes its credential posture. The new work runs in a second, independent, scheduled workflow.

---

## How this differs from a straightforward reading of the request

1. **No credential reaches a GitHub-hosted runner, ever, including on `main`-only or
   `workflow_dispatch`-only workflows.** GitHub-hosted runners are ephemeral but shared
   infrastructure with a documented history of secret-exfiltration techniques (crafted
   `pull_request_target` misuse, malicious dependency scripts reading `$GITHUB_ENV`, compromised
   Actions in the supply chain). A self-hosted runner you provision, patch, and can look at the
   process list of is a materially smaller attack surface for the one thing that actually matters
   here: four sets of live, working OAuth credentials against real (if synthetic-content) SaaS
   accounts.
2. **Live connector calls run on a schedule, not on every PR.** Every PR already gets full
   regression coverage against the *recorded* fixtures (existing, unchanged, zero credentials —
   `TestLiveFixtureParsing` in `tests/unit/test_<connector>_client.py`). The live-account layer's
   job is narrower: catch provider API drift (a field renamed, a scope now required, an endpoint
   moved) before a human notices during the next manual PR-triggered `--check` run. A nightly or
   weekly schedule is exactly matched to that failure mode — provider APIs don't drift
   PR-to-PR — and it means a compromised runner has, at most, a fixed daily/weekly window of
   credential validity to exploit rather than a live credential sitting in every PR's job logs.
3. **The runner never merges anything by itself.** On drift it opens a PR with the redacted fixture
   diff and a human reviews it, exactly like the existing manual `--record` workflow in
   `testing-policy.md` §2.1 — this plan automates *detection*, not *judgment calls about what
   changed*.

If you'd rather run live checks on every PR (catches drift faster, costs more runner-minutes and
widens the exposure window on every green run) or skip the self-hosted runner and keep this
entirely manual (zero new infrastructure, but back to a human remembering to run `--check`), say so
— those are the two adjacent knobs this plan didn't pick unilaterally.

---

## Phase A — Acquire dedicated test accounts

Do this first; everything else depends on having real account credentials to configure. Use a
password manager entry (or your org's secrets vault) per account from the start — you'll paste
each credential into the self-hosted runner's local secret store in Phase B and nowhere else.

Budget summary (Phase A total, monthly, single-seat):

| Service | Plan | Cost | Why this tier |
|---|---|---|---|
| Google | Google Workspace Business Starter, 1 user | ~$7/user/month (varies by region/billing term) | Needed for `calendar_list_rooms` (TST coverage of the Workspace-admin room-directory path in `qa-environment-setup.md` §4) and Admin SDK access; a free consumer Gmail account works for every other connector if you'd rather skip this and accept that one gap |
| Slack | Developer Program sandbox (free) | $0 | See A.2 — [api.slack.com/developer-program](https://api.slack.com/developer-program) gives a free Enterprise Grid sandbox, a strictly better fit than a paid Pro workspace: Enterprise Grid has no 90-day history cutoff, and (per Slack's own May 2025 rate-limit change) an *internal, non-distributed* app built inside it still keeps full Tier 3 `conversations.history`/`conversations.replies` limits, same as `slack-setup.md`'s existing "never distribute" guidance already assumes |
| Atlassian | Free (Jira + Confluence Cloud, up to 10 users) | $0 | Free tier is sufficient for everything `atlassian-setup.md`/`qa-environment-setup.md` need — one small project/space, low request volume |
| Salesforce | Developer Edition org | $0 | Purpose-built free tier for exactly this; not a trial, doesn't expire |

Total: roughly **$7/month** (Google Workspace only) for full coverage including the Workspace
room-directory path; **$0/month** if a free consumer Google account is acceptable instead. A
payment method is still required on the Slack Developer Program account for identity verification
(§A.2) even though nothing is charged.

### A.1 Google — dedicated Workspace account

1. Go to [workspace.google.com](https://workspace.google.com/) → **Get started**.
2. Enter a business name (e.g. `PrivacyFence QA`) and business details — number of employees "Just
   you" is fine.
3. Choose whether you already have a domain or want to buy one through Google. **Recommended:** buy
   a cheap dedicated domain (e.g. `privacyfence-qa.dev`, ~$12–20/year) rather than using an existing
   organizational domain — this keeps the QA Workspace fully isolated from any production Google
   Workspace tenant, so a compromised runner credential can never touch real organizational data.
4. Create the admin account: e.g. `admin@privacyfence-qa.dev`. This becomes the account
   `google-cloud-setup.md` and `qa-environment-setup.md` are run against.
5. Verify domain ownership (Google walks you through a DNS TXT record if you bought the domain
   elsewhere) and complete billing setup — Business Starter tier, monthly billing.
6. Once the Workspace is live, sign in at [admin.google.com](https://admin.google.com/) as the
   admin user — this is the account that will authenticate every Google connector in PrivacyFence
   and that holds Directory Reader rights for the room-directory sync path.
7. Follow `docs/google-cloud-setup.md` **"For IT admins"** section in full, using this new
   Workspace account: create the Cloud project, enable the 8 listed APIs, configure the OAuth
   consent screen as **Internal** (this Workspace only), create the OAuth Desktop client, and run
   `scripts/build_org_bundle.py` to produce a QA-specific `org_config.json`. Keep this `org_config.json`
   separate from any production one — name it e.g. `org_config.qa.json`.
8. Authenticate PrivacyFence's Gmail/Drive/Calendar/Contacts/Tasks/Apps Script connectors against
   this account once, locally, per `qa-environment-setup.md`'s Prerequisites checklist.
9. (Optional, for `calendar_list_rooms` coverage) Follow `qa-environment-setup.md` §4's Workspace
   admin sub-steps: create a calendar resource in the Admin console, create a **second** Cloud
   project with Admin SDK API enabled, and run `scripts/sync_room_directory.py` once to populate
   `org_config.qa.json`'s `rooms` list.

### A.2 Slack — free Developer Program sandbox + app registration

Use the free **Slack Developer Program** rather than paying for a Pro-plan workspace — it provisions
a full Enterprise Grid environment at no cost, which is a better match for this project's needs than
a paid single-workspace Pro plan (no message-history cutoff, full API feature set), with the one
trade-off that the sandbox itself has a rolling expiry (see step 6).

1. Go to [api.slack.com/developer-program](https://api.slack.com/developer-program) and click
   **Join the Program**. Sign up with a dedicated email (e.g. an alias on the Google Workspace domain
   from A.1, `qa@privacyfence-qa.dev`) — don't reuse a personal or production Slack identity.
   Confirm via the activation email.
2. Signed in to your new developer account, go to **Sandboxes** → **Provision Sandbox**. You'll be
   asked for a payment method if the account isn't already on a paid plan — this is for identity
   verification only; provisioning and running a sandbox is not billed.
3. Fill in the sandbox details (an org/workspace name, e.g. `PrivacyFence QA`) and click
   **Provision Sandbox** again to confirm. This creates an Enterprise Grid org with one workspace —
   sign in to it via the time-limited PIN sent to your verified email (no SSO to configure).
4. Register the app inside this sandbox: go to [api.slack.com/apps](https://api.slack.com/apps)
   (signed in as the same account) → **Create New App** → **From scratch** → name it
   `PrivacyFence QA` → select the sandbox's workspace → **Create App**.
5. Follow `docs/slack-setup.md` **"For IT admins"** section exactly as written, in this new app:
   add every listed User Token Scope, **do not** click "Activate Public Distribution". This matters
   even inside a sandbox: Slack's own May 2025 rate-limit change exempts *internal, non-distributed*
   apps from the 1-request-per-minute/15-message cap on `conversations.history`/
   `conversations.replies` — the same property `slack-setup.md`'s existing warning already depends
   on, sandbox or not. Get the client id/secret for `build_org_bundle.py`.
6. Add the Slack client id/secret into the same `org_config.qa.json` from A.1 (`--merge`).
7. Authenticate PrivacyFence's Slack connector against this workspace once, locally.
8. Follow `qa-environment-setup.md` §3: create the approved channel, create and seed the
   `privacyfence-qa-control` channel with a `[QATEST]`-tagged thread.
9. **Sandbox renewal**: a provisioned sandbox is active for six months by default, and the org admin
   can extend its archive date another six months at a time before it lapses (**Sandboxes** →
   select the sandbox → **Extend**). Fold this into the same recurring reminder as Phase B.3's
   quarterly credential rotation — check the sandbox's expiry date at each rotation and extend it if
   it's within the next rotation window, so it never lapses out from under the recorded fixtures.

### A.3 Atlassian — free developer registration

1. Go to [id.atlassian.com](https://id.atlassian.com/signup) and create a new Atlassian account
   with a dedicated email (e.g. `qa@privacyfence-qa.dev`).
2. Go to [start.atlassian.com](https://start.atlassian.com/) (or your account's **Products** page)
   and create a new site — pick a subdomain, e.g. `privacyfence-qa.atlassian.net`. Select both
   **Jira Software** and **Confluence** when prompted for products (or add Confluence afterward
   from the site's **Manage apps**/product picker) — free tier covers up to 10 users, no card
   required to start.
3. Go to [developer.atlassian.com/console/myapps/](https://developer.atlassian.com/console/myapps/)
   signed in as the same account, and follow `docs/atlassian-setup.md` **"For IT admins"** section
   exactly: **Create → OAuth 2.0 integration**, configure the callback URL, add the classic Jira
   scopes and granular Confluence scopes listed there, and get the client id/secret.
4. Add the Atlassian client id/secret into `org_config.qa.json` (`--merge`).
5. Authenticate PrivacyFence's Jira and Confluence connectors against this site once, locally.
6. Follow `qa-environment-setup.md` §9–10: create the `PFQA` Jira project and `PFQA` Confluence
   space, each with one seed issue/page, plus a throwaway second project/space as the "different
   project, should still prompt" contrast case.

### A.4 Salesforce — free Developer Edition org

1. Go to
   [developer.salesforce.com/signup](https://developer.salesforce.com/signup) and sign up for a
   free Developer Edition org, using a dedicated email (e.g. `qa@privacyfence-qa.dev`) and a
   distinct username (Salesforce usernames are globally unique and email-shaped but need not match
   a real mailbox exactly, e.g. `qa@privacyfence-qa.dev.qa`).
2. Verify the account via the emailed confirmation link and set a password + security question.
3. Sign in at the org's login URL (from the confirmation email, something like
   `https://<random>-dev-ed.develop.my.salesforce.com`).
4. Follow `docs/salesforce-setup.md` **"For IT admins"** section exactly: **Setup → App Manager →
   New Connected App**, callback URL `http://localhost:53683/callback` (must match exactly), the two
   listed OAuth scopes, then wait the noted 2–10 minutes for the Connected App to activate, and get
   the consumer key/secret.
5. Add the Salesforce consumer key/secret into `org_config.qa.json` (`--merge`), with
   `--salesforce-login-url https://login.salesforce.com`.
6. Authenticate PrivacyFence's Salesforce connector against this org once, locally.
7. Follow `qa-environment-setup.md` §8: create 2–3 tagged sample Account records, create the
   `PrivacyFence QA Report`, wait for SOSL search indexing before relying on `salesforce_search`
   coverage.

### A.5 Phase A exit criteria

- [ ] Four independent test accounts exist, none sharing credentials with any production/personal
      account.
- [ ] A single `org_config.qa.json` exists with all four connectors' client id/secret merged in.
- [ ] `tests/fixtures/qa_environment.yaml` is filled in (copied from
      `tests/fixtures/qa_environment.yaml.example`) with every seed artifact id from A.1–A.4.
- [ ] `.venv/bin/python scripts/qa_fixture_recorder.py --check` (no args = all connectors) passes
      locally against these accounts, run from a developer machine, per `testing-policy.md` §2.1.

---

## Phase B — Provision the self-hosted runner

### B.1 Choose and provision the machine

A small, dedicated VM — not a workstation, not shared with anything else. Any cloud provider works;
sizing needs are minimal (1 vCPU / 2 GB RAM is plenty — this runs a scheduled Python script, not a
build farm).

1. Provision a small Linux VM (e.g. Ubuntu 24.04 LTS), reachable only for the setup steps below —
   no inbound ports need to stay open once the runner is registered (GitHub Actions self-hosted
   runners poll outbound over HTTPS; nothing needs to accept inbound connections).
2. Harden it minimally: OS auto-updates enabled, SSH key-only login, no other services running on
   it. Treat this machine as holding secrets equivalent in sensitivity to a production credential
   store, because it does.
3. Create a dedicated, unprivileged OS user (e.g. `pf-runner`) to run the GitHub Actions runner
   under — never root.

### B.2 Register it as a GitHub Actions runner, scoped narrowly

1. In the `privacyfence/privacyfence` repo (or, if you want it shared across multiple repos later,
   an organization-level runner group — start repo-scoped): **Settings → Actions → Runners → New
   self-hosted runner**.
2. Follow GitHub's generated download/config commands as the `pf-runner` OS user. When prompted for
   labels, add a distinctive one, e.g. `privacyfence-qa-live` — this is what the new workflow's
   `runs-on:` targets, so no other workflow in the repo can accidentally schedule work on this
   machine.
3. **Install it as a systemd service in ephemeral mode**, not the default persistent listening
   mode: `./config.sh ... --ephemeral --labels privacyfence-qa-live` followed by `./svc.sh install
   pf-runner && ./svc.sh start`. Ephemeral mode means the runner process — and everything it can see
   in its job workspace — exits after each job and a fresh process picks up the next one, so a job
   can't leave state behind for a later job to read (relevant if this repo's default-branch workflow
   file itself is ever compromised via a merged PR — see B.4).
4. **Repository setting**: Settings → Actions → General → "Fork pull request workflows" — confirm
   this is set to **not** run workflows from forks automatically (the repo default), and confirm no
   workflow file uses `pull_request_target` anywhere near this runner's label. This runner should be
   reachable only by workflows triggered by `schedule:` or `workflow_dispatch:` from a maintainer,
   never by any `pull_request` event from an untrusted head.

### B.3 Store credentials on the runner, not in GitHub

This is the core safety property of the self-hosted-runner approach: the four connector credentials
(OAuth token files under `credentials/`, plus `org_config.qa.json`) live **only as local files on
this VM**, never as GitHub Actions secrets, never transmitted to GitHub at all.

1. As the `pf-runner` user, clone a working copy of the repo (or let the runner's own job workspace
   do it — either way, this stays local to the VM).
2. Copy the OAuth token files produced by Phase A's local authentication steps into this VM's
   git-ignored `credentials/` directory (`scp` over SSH once, from whichever machine you ran Phase A
   on — the token files are the same regardless of which machine runs the check afterward).
3. Copy `org_config.qa.json` and the filled-in `tests/fixtures/qa_environment.yaml` onto the VM
   alongside it.
4. Lock down file permissions: `chmod 600` on every credential file, owned by `pf-runner` only.
5. **Do not** add any of these as GitHub Actions **secrets**. If a future maintainer is tempted to
   "just add it as a repo secret for convenience," that reintroduces exactly the exposure this
   design avoids — GitHub-hosted runners (including any other workflow with access to that secret)
   would then be able to read it. The runner-local file is the whole point.
6. Set a recurring calendar reminder (quarterly) to rotate every credential — re-authenticate each
   connector, replace the token files on the VM, and revoke the old OAuth grant in each provider's
   console (Google Cloud Console → OAuth consent screen; Slack app → OAuth & Permissions → Revoke;
   Atlassian → account → Connected apps; Salesforce → Setup → Connected Apps OAuth Usage).

### B.4 What this design does and doesn't protect against

- **Protects against**: a malicious PR (even from a maintainer's own fork-testing habits) ever
  seeing these credentials; a compromised third-party GitHub Action in the dependency chain reading
  them (nothing on `privacyfence-qa-live`'s job runs untrusted third-party Actions beyond what this
  plan's own workflow file pins); GitHub-side secret-scanning gaps (there's nothing to scan — the
  values never reach GitHub).
- **Does not protect against**: compromise of the VM itself (patch it, key-only SSH, no other
  workloads on it), or a maintainer merging a malicious change to the *workflow file itself* that
  then runs on `privacyfence-qa-live` on its next scheduled trigger — mitigate this the same way any
  CI/CD credential-holding pipeline does: require review on any change under
  `.github/workflows/connector-live-check.yml`, and keep the runner's job scope (see Phase C) as
  narrow as the script it's allowed to invoke.

---

## Phase C — The new workflow

### C.1 Workflow file

New file, `.github/workflows/connector-live-check.yml` — separate from `tests.yml` entirely, never
touched by a `pull_request` trigger:

```yaml
name: Connector live check

# Runs only on a schedule or by explicit maintainer trigger -- never on
# pull_request, never on push from a branch a non-maintainer controls.
# Targets the self-hosted runner provisioned in docs/connector-ci-integration-plan.md
# Phase B, which is the only place these connectors' live OAuth credentials
# ever exist. See docs/testing-policy.md's "Runner-local live tier" section.
on:
  schedule:
    - cron: '0 6 * * 1'   # weekly, Monday 06:00 UTC -- matched to how often
                           # a provider API actually changes shape, not to
                           # PR cadence; see the plan's rationale
  workflow_dispatch:

permissions:
  contents: write   # needed to open the drift PR (step below); nothing else

concurrency:
  group: connector-live-check
  cancel-in-progress: false   # never cancel a run mid-way through a live
                               # provider call sequence

jobs:
  live-check:
    runs-on: [self-hosted, privacyfence-qa-live]
    timeout-minutes: 20   # generous but bounded -- this should be a few
                           # minutes of read calls, not an open-ended job

    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0

      # No setup-python/setup-node steps here -- the self-hosted runner
      # keeps its own persistent venv (Phase B), rebuilt manually when
      # dependencies change, rather than a fresh install every run. A
      # GitHub-hosted job would need setup-python; this one deliberately
      # doesn't, since the point is minimal, auditable actions on a
      # credential-holding machine.
      - name: Run qa_fixture_recorder.py --check
        id: check
        run: |
          .venv/bin/python scripts/qa_fixture_recorder.py --check \
            --report-file /tmp/qa-check-report.md
        continue-on-error: true   # a drift finding is data, not a crash --
                                   # handled by the next step, not by failing
                                   # the job outright

      - name: Re-record on drift
        if: steps.check.outcome == 'failure'
        run: |
          .venv/bin/python scripts/qa_fixture_recorder.py --record \
            --report-file /tmp/qa-record-report.md

      # Opens a PR only when --record actually changed a fixture file --
      # a maintainer reviews the redacted diff exactly as the manual
      # process in testing-policy.md Sec 2.1 already describes, this just
      # removes the "someone has to remember to run it" step.
      - name: Open drift PR
        if: steps.check.outcome == 'failure'
        uses: peter-evans/create-pull-request@<pin-to-a-reviewed-sha>
        with:
          branch: chore/connector-live-fixture-drift
          title: "chore: connector live fixture drift detected"
          commit-message: "Update recorded fixtures after provider API drift"
          body-path: /tmp/qa-record-report.md
          add-paths: tests/fixtures/live/**

      - name: Upload report artifact
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: connector-live-check-report
          path: /tmp/qa-*-report.md
          retention-days: 90
```

Notes:

- `peter-evans/create-pull-request` needs pinning to a reviewed commit SHA, same convention as
  every other Action reference in this repo's existing workflows — resolve and pin it as part of
  implementing this file, don't merge with a floating tag.
- The job never fails loudly on drift by itself (`continue-on-error: true` on the check step) — a
  provider API shape changing is expected, occasional, real-world drift, not a CI outage. The
  **signal** is the opened PR, reviewed like any other.
- If `--record` itself fails (e.g. the account's OAuth token expired, the seed artifact was
  deleted), *that* should fail the job loudly — don't blanket-`continue-on-error` past a genuine
  environment problem. As written above, the `Re-record on drift` step has no `continue-on-error`,
  so it does fail the job in that case.
- No `pip install` step: dependencies live in the runner's own venv (Phase B.3), rebuilt manually
  (`pip install -e ".[test]"` as `pf-runner`, whenever `pyproject.toml`'s `test` extra changes) —
  keeping the credential-holding job's own steps as few and as auditable as possible.

### C.2 Optional follow-on: feed drift PRs through `tests.yml` normally

The opened PR (from `create-pull-request` above) is a completely ordinary PR against `main` —
`tests.yml` runs on it exactly as on any other PR, on the regular GitHub-hosted, credential-free
runner, replaying the newly-recorded fixtures through `TestLiveFixtureParsing`. No special-casing
needed; this is what makes the split safe — the live-credential job and the merge-gate job never
share a runner or a trigger.

---

## Phase D — Update `docs/testing-policy.md`

Rewrite §2's framing rather than leaving the absolute "never...or any other cloud CI" language
standing unchanged. Specifically:

1. Add a new subsection, "0. Runner-local live tier (scheduled, not per-PR)" before the existing
   §1, describing `connector-live-check.yml`: what it does, that it runs on a project-owned
   self-hosted runner and never a GitHub-hosted one, that credentials never become GitHub Actions
   secrets, and pointing at this document for the full setup.
2. In the existing "None of the 'No' rows require a credential..." closing paragraph, narrow the
   claim precisely: **GitHub-hosted** runners and **every `pull_request`-triggered** workflow still
   never see a credential — that part of the policy is unchanged and still absolute. The
   qualification is specifically "GitHub-hosted," not "GitHub Actions" broadly, since the new tier
   is technically a GitHub Actions workflow, just one that only ever executes on infrastructure this
   project controls.
3. Update the "Quick reference" table to add the new row:

   | Check | Runs in CI? | When |
   |---|---|---|
   | `qa_fixture_recorder.py --check` / `--record` (`connector-live-check.yml`) | Yes, but only on a project-owned self-hosted runner, never GitHub-hosted, never on `pull_request` | Weekly schedule + manual dispatch |

4. Cross-reference this document (`connector-ci-integration-plan.md`) from `testing-policy.md`'s
   §2.1, alongside the existing local-manual instructions (which remain valid and still work,
   unchanged, for anyone who wants to run a `--check` from their own laptop between scheduled runs).

---

## Phase E — Implement Phase 3.12 of the security remediation plan

3.12 (`tests/tst-08-through-13-remaining-test-depth`, TST-08–TST-13) covers six sub-items. Five of
six need **no live credentials at all** and run entirely in the existing GitHub-hosted `tests.yml`.
Only TST-08's fixture-*recording* step benefits from Phase A–D above (it stops being a manually-run
step and starts running on a schedule); TST-08's CI *guard* itself is a plain offline file check.

| Item | Scope | Where it runs | New credentials needed? |
|---|---|---|---|
| TST-08 | Record fixtures for the highest-risk read path per connector; add a CI guard that fails if a connector lacks one | Guard: `tests.yml`, GitHub-hosted, offline. Recording: `connector-live-check.yml` (Phase C) | No (guard) / handled by Phase A–C (recording) |
| TST-09 | One end-to-end deferred-approval-round-trip integration test | `tests.yml`, GitHub-hosted, in-process/mocked | No |
| TST-10 | Explicit cross-principal step-up-binding tests in `routes_security.py` | `tests.yml`, GitHub-hosted | No |
| TST-11 | Replace fixed `sleep`s with `Event.wait` + per-test timeout markers on timing-dependent tests | `tests.yml`, GitHub-hosted | No |
| TST-12 | `hypothesis` round-trip property tests on the four parsers | `tests.yml`, GitHub-hosted | No |
| TST-13 | Extend the parameterised-invariant pattern to the remaining systemic checks | `tests.yml`, GitHub-hosted | No |

### E.1 TST-08 — fixture presence guard

Current state: `tests/fixtures/live/` has a subdirectory for 10 of the 11 connectors (`calendar`,
`confluence`, `contacts`, `drive`, `gmail`, `jira`, `salesforce`, `slack`, `tasks`, `telegram`) —
**`apps_script` has none yet**. Record it as part of Phase A/C's first run (add `apps_script` to
`scripts/qa_fixture_recorder.py`'s `CONNECTOR_CHECKS` if it isn't already wired up, then `--record
apps_script` once the QA Apps Script project exists).

New guard, `scripts/check_fixture_coverage.py` (or a `test_fixture_coverage.py` in
`tests/unit/`, matching the repo's preference for tests over ad hoc scripts where either works):
enumerate every connector module in `src/privacyfence/connectors/`, assert
`tests/fixtures/live/<connector>/` exists and contains at least one non-empty `.json` file. Add as a
step in `tests.yml`'s existing `test` job, right after the Python test suite step — a missing
fixture fails the PR, same severity as any other merge-gate check.

### E.2 TST-09 — deferred-approval round-trip integration test

New `tests/integration/test_deferred_approval_round_trip.py`: drive a full cycle — a gated tool call
creates a pending approval, the approval sits deferred, a later web request approves it, and the
originally-blocked tool call resolves with the expected result — through the real `WebServer`/
`McpDispatcher`/`gate.py` stack, same posture as the existing `test_mcp_daemon_contract.py`
(real loopback socket, official `mcp` client, no external network).

### E.3 TST-10 — cross-principal step-up-binding tests

`tests/unit/web/test_routes_security.py` already has scaffolding (`_app(*, step_up=None,
sessions=None)`). Add cases proving a step-up credential registered/verified for principal A cannot
be used to satisfy a step-up requirement raised for principal B — the specific binding property the
review flags, not just "step-up works."

### E.4 TST-11 — replace fixed sleeps

Concrete scope, from the current `time.sleep(...)` calls in genuinely timing-dependent tests (not
the mock-server polling loops, which are a different pattern):

- `tests/unit/test_approvals.py` (6 call sites)
- `tests/unit/test_audit_forwarding.py` (3 call sites)
- `tests/unit/test_webauthn_stepup.py` (1 call site)
- `tests/unit/test_settings_controller.py`, `tests/unit/test_daemon_main.py`,
  `tests/unit/test_web_prompt.py`, `tests/unit/web/test_routes_settings.py` (each has one
  `time.sleep(interval)` in a reload-polling helper)
- `tests/unit/web/test_routes_approvals.py` (1 call site)

Replace each with a `threading.Event`/`asyncio.Event` the code under test can signal, `.wait(timeout=...)`
on the test side, and add a `pytest.mark.timeout(N)` (or rely on the global `pytest-timeout` config
already in `pyproject.toml`) so a hang fails fast instead of stalling. `tests/integration/
test_browser_smoke.py`, `test_shim_mcp_contract.py`, and `test_mcp_daemon_contract.py`'s
`time.sleep(0.05)`/`time.sleep(0.01)` calls are process-startup-settle waits around subprocess
launches, not signal-waits — leave those as-is unless they also prove flaky; converting a "give the
subprocess a moment to bind its socket" sleep into an `Event.wait` needs the subprocess itself to
signal readiness, which is a larger, separate change than TST-11's stated scope.

### E.5 TST-12 — hypothesis property tests

Add `hypothesis>=6.100` to `pyproject.toml`'s `test` extra. New
`tests/unit/test_parser_roundtrip_properties.py`: property tests over the four parsers the plan
names, focused on `html_to_text` → `markdown_to_html` round-tripping — generate arbitrary
(constrained to a realistic character/tag subset, not pure `st.text()`, to keep the search space
meaningful) HTML/Markdown fragments and assert the round-trip invariant holds, or that specific
known-safe transformations (e.g. the SEC-01 URL-scheme allowlist from Phase 0.1) are never violated
regardless of input shape.

### E.6 TST-13 — extend the parameterised-invariant pattern

Three systemic checks, each as a new parameterised test iterating every relevant tool/site rather
than one test per tool:

1. `reason` param present on every gated (`review`/`popup`) tool — introspect each connector's tool
   schema/registration and assert the parameter exists.
2. `pii_scan_text` passed by every `review`-gated tool — assert via the same call-capturing fixture
   pattern `coding-and-testing-guidelines.md` §2.5 already documents for gate-argument assertions.
3. All 11 token-writer call sites use the shared secure-write helper (from SEC-09, Phase 1.4) —
   grep-based or AST-based test asserting no call site writes a token file without going through
   `atomic_write_text`/`atomic_write_json`.

---

## Suggested execution order

```
Phase A (accounts)  ───────────────────────────▶ do first, ~2-3 hours across
                                                    four providers + waiting
                                                    on Salesforce's Connected
                                                    App activation delay

Phase B (runner)  ──────────────────────────────▶ can start in parallel with
                                                    Phase A; needs Phase A's
                                                    credentials to finish B.3

Phase C (workflow)  ─────────────────────────────▶ after A + B

Phase D (policy doc)  ───────────────────────────▶ same PR as Phase C

Phase E (3.12 itself)  ──────────────────────────▶ independent of A-D except
                                                     E.1's recording step;
                                                     E.2-E.6 can start
                                                     immediately, in parallel
                                                     with A-D
```

Phase E.2–E.6 has no dependency on Phase A–D at all — if you want visible progress on the security
remediation plan sooner, start there while account/runner provisioning is in flight.

## Risk register

| Risk | Mitigation |
|---|---|
| Self-hosted runner VM compromised | Dedicated, minimal, patched VM; ephemeral runner mode; quarterly credential rotation (B.3) |
| Malicious workflow-file change merged to `main` | Require review on `.github/workflows/connector-live-check.yml` changes specifically; scope job permissions to the minimum (`contents: write` only) |
| Provider ToS concerns around automated test traffic | All four providers (Google Workspace, Slack, Atlassian, Salesforce) explicitly offer developer/sandbox tiers meant for exactly this; weekly read-mostly traffic against synthetic `[QATEST]` data is well within normal developer use |
| Runner goes offline, scheduled job silently stops running | `schedule:` triggers on a dead runner just queue and eventually show as failed/stale in the Actions tab — add a `send_later`-style check-in or a separate lightweight "did the weekly job run" alert if this matters; not automated by this plan as written |
| Slack sandbox lapses (6-month expiry) unnoticed, breaking the live check | Fold the sandbox-expiry check into the same recurring reminder as credential rotation (A.2 step 9, B.3) rather than a separate cadence to track |
| Cost creep | Only Google Workspace is a recurring cost as scoped in Phase A (~$7/month) — Slack, Atlassian, and Salesforce are all free tiers; re-evaluate if a fifth connector (Telegram is already free/personal-account-based) or additional seats are ever needed |
