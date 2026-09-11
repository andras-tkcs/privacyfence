# Connector Live-Check: Account & Runner Setup

Setup instructions for the two pieces of infrastructure `.github/workflows/connector-live-check.yml`
needs: dedicated test accounts for Google, Atlassian, Salesforce, and Slack (Phase A), and the
self-hosted runner those accounts' credentials live on (Phase B). See
[`testing-policy.md` §0](testing-policy.md#0-runner-local-live-tier-scheduled-not-per-pr) for what
the workflow actually does and when it runs; this document is only about standing the
infrastructure up (or rebuilding it — a new runner VM, rotated accounts, a lapsed Slack sandbox).

This replaces `connector-ci-integration-plan.md`, which planned this work before it existed. That
plan's Phase C (the workflow file), Phase D (the `testing-policy.md` update), and Phase E (the
`security-remediation-plan.md` Phase 3.12 test items) all shipped and needed no standing
documentation of their own once done — see `.github/workflows/connector-live-check.yml` itself for
the current, authoritative workflow definition rather than a copy here that would just go stale.
Phase A and Phase B, below, are different: they describe real-world account/infrastructure setup
that isn't "done" in the same sense — it's provisioned once and then maintained (credential
rotation, a lapsed sandbox, a replacement VM), so it stays here as a live reference. Phase B is
rewritten from the original plan's version to reflect what the actual runner needed once someone
went to build it — the original plan got two things wrong (documented in Troubleshooting below)
that only showed up when running it for real: it recommended `--ephemeral` runner mode, which is
incompatible with a systemd-managed always-listening runner, and it assumed all of the runner's
credentials, code, and venv would persist together as one long-lived directory, which turned out to
fight `actions/checkout`'s own cleaning behavior more than it helped.

---

## Phase A — Acquire dedicated test accounts

Do this first; everything else depends on having real account credentials to configure. Use a
password manager entry (or your org's secrets vault) per account from the start — you'll copy each
credential onto the self-hosted runner in Phase B and nowhere else.

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
   `scripts/build_org_bundle.py` to produce a QA-specific `org_config.json`. Keep this working copy
   separate from any production one while you assemble it — name it e.g. `org_config.qa.json`; it
   gets installed as plain `org_config.json` on the runner in Phase B.3.
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
trade-off that the sandbox itself has a rolling expiry (see step 9).

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

- [x] Four independent test accounts exist, none sharing credentials with any production/personal
      account.
- [x] A single `org_config.qa.json` exists with all four connectors' client id/secret merged in.
- [x] `tests/fixtures/qa_environment.yaml` is filled in (copied from
      `tests/fixtures/qa_environment.yaml.example`) with every seed artifact id from A.1–A.4.
- [ ] `.venv/bin/python scripts/qa_fixture_recorder.py --check` (no args = all connectors) passes
      locally against these accounts, run from a developer machine, per `testing-policy.md` §2.1.

All four are now provisioned on the production runner (`~/privacyfence/credentials/`,
`~/privacyfence/org/org_config.json`, `~/privacyfence/tests/fixtures/qa_environment.yaml` — see
Phase B.3's layout). The last item — an actual passing `--check` against real connector data,
rather than just the infrastructure running cleanly — is confirmed by the next
`connector-live-check.yml` run once Phase B.3's copy step includes `qa_environment.yaml` (see the
workflow file).

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
4. Confirm `python3 -m venv` actually works for that user (`python3 -m venv /tmp/venv-test && rm -rf
   /tmp/venv-test`). On a fresh Ubuntu/Debian box this can fail with "ensurepip is not available" —
   install whichever `pythonX.Y-venv` apt package matches the box's `python3 --version` (e.g. `sudo
   apt install python3.14-venv`) if so. The workflow builds a fresh venv on every run (Phase C — see
   the workflow file), so this has to work before the first run, not just once.

### B.2 Register it as a GitHub Actions runner, scoped narrowly

1. In the `privacyfence/privacyfence` repo (or, if you want it shared across multiple repos later,
   an organization-level runner group — start repo-scoped, and confirm it under **this repo's**
   Settings → Actions → Runners page specifically, not only an org-level list): **Settings → Actions
   → Runners → New self-hosted runner**.
2. Follow GitHub's generated download/config commands as the `pf-runner` OS user. When prompted for
   labels, add a distinctive one, e.g. `privacyfence-test` — this is what the workflow's `runs-on:`
   targets, so no other workflow in the repo can accidentally schedule work on this machine.
3. **Install it as a systemd service in the default (non-ephemeral) persistent mode**: `./config.sh
   --url https://github.com/privacyfence/privacyfence --token <TOKEN> --labels privacyfence-test
   --unattended` (no `--ephemeral`), followed by `sudo ./svc.sh install && sudo ./svc.sh start`. **Do
   not pass `--ephemeral`** — see Troubleshooting below for what actually happens if you do (it isn't
   subtle: the runner disappears from GitHub's runner list after its first job).
4. **Repository setting**: Settings → Actions → General → "Fork pull request workflows" — confirm
   this is set to **not** run workflows from forks automatically (the repo default), and confirm no
   workflow file uses `pull_request_target` anywhere near this runner's label. This runner should be
   reachable only by workflows triggered by `schedule:` or `workflow_dispatch:` from a maintainer,
   never by any `pull_request` event from an untrusted head.
5. **Allowed-actions setting**, if this repo restricts which Actions can run (Settings → Actions →
   General → Actions permissions → "Allow select actions and reusable workflows"): add
   `peter-evans/create-pull-request@*` to the allowlist alongside whatever's already there. The
   pinned commit SHA in the workflow file itself is what actually gets run; the allowlist entry only
   grants permission for that action to execute at all.

### B.3 Store credentials on the runner, not in GitHub

This is the core safety property of the self-hosted-runner approach: the connector credentials
(OAuth token files, plus `org_config.json`) live **only as local files on this VM**, never as GitHub
Actions secrets, never transmitted to GitHub at all. `tests/fixtures/qa_environment.yaml` lives
alongside them for a different reason (see below) — it isn't a credential, but it's still
runner-local state that has to survive an ephemeral, every-run-wiped checkout.

The workflow (see `.github/workflows/connector-live-check.yml`'s `QA_SECRETS_DIR`) expects exactly
this layout at `~/privacyfence` (i.e. `/home/pf-runner/privacyfence` for the `pf-runner` user):

```
~/privacyfence/
├── credentials/            # the OAuth token files Phase A's local auth steps produced
│   ├── token.json          # (gmail)
│   ├── drive_token.json
│   ├── calendar_token.json
│   ├── contacts_token.json
│   ├── tasks_token.json
│   ├── apps_script_token.json
│   ├── atlassian_token.json    # shared by Jira + Confluence
│   └── slack_token.json
│       # (no salesforce/telegram token file yet as of this writing -- see A.5)
├── org/
│   └── org_config.json     # the merged org_config.qa.json from Phase A, installed under its
│                            # real filename -- see paths.py's org_dir() for why it must be
│                            # exactly this name
└── tests/
    └── fixtures/
        └── qa_environment.yaml   # the filled-in seed-artifact manifest from Phase A.5
```

This is deliberately **not** a git clone, and deliberately **not** where the workflow's own code or
venv live — those are rebuilt fresh in the ephemeral Actions job workspace on every run (see the
workflow file); only this runner-local state needs to survive between runs, so only this lives here.

1. Copy the OAuth token files produced by Phase A's local authentication steps into
   `~/privacyfence/credentials/` (`scp` over SSH once, from whichever machine you ran Phase A on —
   the token files are the same regardless of which machine runs the check afterward).
2. Copy the finished `org_config.qa.json` to `~/privacyfence/org/org_config.json` (note the rename).
3. Copy the filled-in `tests/fixtures/qa_environment.yaml` from Phase A.5 to
   `~/privacyfence/tests/fixtures/qa_environment.yaml`.
4. Lock down file permissions: `chmod 600` on every credential file, owned by `pf-runner` only.
   (`qa_environment.yaml` isn't a credential and doesn't need the same lockdown, but there's no harm
   in treating it the same way.)
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
  them (nothing on `privacyfence-test`'s job runs untrusted third-party Actions beyond what the
  workflow file itself pins); GitHub-side secret-scanning gaps (there's nothing to scan — the values
  never reach GitHub); a job leaving stray credential copies behind for a later job to find (the
  Actions job workspace is fully ephemeral — fresh checkout, fresh venv, credentials copied in fresh
  every run — so there's nothing runner-local inside it for the next run's ordinary `clean: true`
  checkout to protect).
- **Does not protect against**: compromise of the VM itself (patch it, key-only SSH, no other
  workloads on it), or a maintainer merging a malicious change to the *workflow file itself* that
  then runs on `privacyfence-test` on its next scheduled trigger — mitigate this the same way any
  CI/CD credential-holding pipeline does: require review on any change under
  `.github/workflows/connector-live-check.yml`, and keep the runner's job scope as narrow as the
  script it's allowed to invoke.

### B.5 Troubleshooting

Real failure modes hit standing this up, in the order they tend to surface:

- **`The action <owner>/<name>@<sha> is not allowed in <repo> because all actions must be...`** — the
  repo's allowed-actions allowlist (Settings → Actions → General → Actions permissions) doesn't cover
  `peter-evans/create-pull-request`. Add `peter-evans/create-pull-request@*` to it (see B.2.5).
- **The dispatched run just sits `queued` forever, never picked up.** Check, in order: (1) the
  runner is registered under **this repo's** Settings → Actions → Runners page, not only an
  org-level runner group without this repo in its access list; (2) its labels are exactly
  `self-hosted` + `privacyfence-test` (both, exact spelling); (3) the systemd service is actually
  running (`sudo systemctl status actions.runner.*`) — a green "Idle" dot in GitHub's UI is
  heartbeat-based and can lag behind the process actually being alive.
- **The runner disappears from GitHub's runner list entirely after exactly one job runs**, and
  `journalctl -u actions.runner.*` shows `An error occurred: Not configured. Run config.(sh/cmd) to
  configure the runner.` — it was registered with `--ephemeral`. That mode fully deregisters and
  wipes its own local config after one job, which is fundamentally incompatible with a
  systemd-managed, always-listening runner (there's no isolation/scaling benefit to it here — that's
  what ephemeral mode is actually for). Fix: `sudo ./svc.sh stop && sudo ./svc.sh uninstall`, get a
  fresh registration token, `./config.sh ... --unattended` **without** `--ephemeral`, reinstall the
  service. Confirm it stays listed as Idle/green after its *next* job completes, not just its first.
- **`.venv/bin/python: No such file or directory` (exit 127) despite the venv-build step reporting
  success on a previous run** — a stale design assumed a persistent venv survives between runs in the
  Actions job workspace; it doesn't, because `actions/checkout`'s default `clean: true` wipes
  untracked files (including `.venv`) before every checkout. The shipped workflow avoids this
  entirely by building the venv fresh every run instead of relying on one surviving — if this
  resurfaces, something reintroduced a persistent-venv assumption into the workflow file.
- **`python3.X: command not found`** — don't hardcode an exact Python minor version in the workflow;
  different runner VMs land on whatever their distro or manual setup gave them (3.12, 3.13, 3.14, ...).
  The shipped workflow uses plain `python3`, matching `pyproject.toml`'s `>=3.11` floor rather than
  guessing a specific version.
- **`python3 -m venv .venv` fails with "ensurepip is not available"** — the matching `pythonX.Y-venv`
  apt package isn't installed (see B.1.4).
- **`error: manifest not found at .../tests/fixtures/qa_environment.yaml`** — this is
  `qa_fixture_recorder.py`'s own expected, correct behavior when Phase A.5 hasn't been finished yet
  (e.g. rebuilding this setup from scratch), not an infrastructure problem. The workflow already
  copies a real manifest in from `QA_SECRETS_DIR` once one exists (Phase B.3.3) — fill in
  `tests/fixtures/qa_environment.yaml` (Phase A.5) and place it there; there's nothing to fix in the
  workflow itself for this one.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Self-hosted runner VM compromised | Dedicated, minimal, patched VM; quarterly credential rotation (B.3.6) |
| Malicious workflow-file change merged to `main` | Require review on `.github/workflows/connector-live-check.yml` changes specifically; scope job permissions to the minimum (`contents: write` + `pull-requests: write`) |
| Provider ToS concerns around automated test traffic | All four providers (Google Workspace, Slack, Atlassian, Salesforce) explicitly offer developer/sandbox tiers meant for exactly this; weekly read-mostly traffic against synthetic `[QATEST]` data is well within normal developer use |
| Runner goes offline, scheduled job silently stops running | `schedule:` triggers on a dead runner just queue and eventually show as failed/stale in the Actions tab — add a check-in or a separate lightweight "did the weekly job run" alert if this matters; not automated today |
| Slack sandbox lapses (6-month expiry) unnoticed, breaking the live check | Fold the sandbox-expiry check into the same recurring reminder as credential rotation (A.2.9, B.3.6) rather than a separate cadence to track |
| Cost creep | Only Google Workspace is a recurring cost as scoped in Phase A (~$7/month) — Slack, Atlassian, and Salesforce are all free tiers; re-evaluate if a fifth connector (Telegram is already free/personal-account-based) or additional seats are ever needed |
