# Connector live-check runner setup

PrivacyFence runs real-provider contract checks on a project-controlled self-hosted GitHub Actions runner. The runner holds dedicated QA credentials locally; real connector credentials are not provisioned to GitHub-hosted runners.

The workflow definition is `.github/workflows/connector-live-check.yml`. This document covers the standing runner/account setup and recovery procedure.

## Runner requirements

Provision a dedicated Linux host/VM with:

- GitHub Actions self-hosted runner registered to this repository;
- runner label `privacyfence-test`;
- Python 3.11 or newer available as `python3`;
- normal build/runtime prerequisites required by PrivacyFence dependencies;
- outbound HTTPS access to the provider APIs and GitHub;
- a persistent runner user home directory for QA state.

Do not share this runner with untrusted repositories/jobs.

## Persistent QA state

The workflow expects its persistent state under `~/privacyfence` for the runner user. It copies that state into the ephemeral Actions checkout for each run.

Required paths:

```text
~/privacyfence/
├── credentials/
├── org/
│   └── org_config.json
└── tests/
    └── fixtures/
        └── qa_environment.yaml
```

`credentials/` contains the dedicated test-account connector grants/sessions. `org_config.json` is the QA organization configuration needed by the recorder/client setup. `qa_environment.yaml` contains non-secret identifiers/tags for the dedicated seeded QA resources.

Keep the directory readable only by the runner/service identity. Do not place these files in the repository checkout and do not commit them.

## QA accounts and seed data

Create/use the dedicated accounts and tagged seed resources described in [`qa-environment-setup.md`](qa-environment-setup.md). Do not point the live runner at personal or production data.

Authenticate each supported connector using the same PrivacyFence auth path the daemon uses, then copy/preserve the resulting credential files in the runner's persistent `~/privacyfence/credentials/` directory.

Telegram's application id/hash are repository/build-level app credentials provided through the workflow environment; the per-account Telegram session remains runner-local with the other connector credentials.

## Workflow behavior

On each scheduled/manual run the workflow:

1. checks out a clean ephemeral workspace;
2. creates a fresh virtual environment and installs the current checkout;
3. copies the persistent QA credentials/config/seed manifest into the checkout;
4. runs `scripts/qa_fixture_recorder.py --check`;
5. re-records fixtures when provider response drift is detected;
6. runs `scripts/qa_fixture_recorder.py --lifecycle` for supported write-capable providers;
7. opens/updates a fixture-drift PR when committed fixture files changed;
8. copies any refreshed OAuth credential files back from the checkout into the persistent `~/privacyfence/credentials/` (always, regardless of the outcome of the steps above);
9. uploads the run reports as artifacts.

Step 8 exists because some providers (Atlassian) rotate the refresh token on every use: the client already persists the refreshed token to disk when it refreshes, but that write only reaches the persistent store because of this step. Without it, every run after the first would refresh with an already-spent token and start failing with an authorization error — a token/grant rotated in step 6 or step 4 must survive the ephemeral checkout being discarded, or the very next run breaks. Google's refresh tokens aren't single-use, so this step is a no-op for calendar/tasks/contacts/drive/gmail; it matters for Jira/Confluence and any future OAuth-based connector whose provider rotates refresh tokens the same way.

The job runs on its configured schedule and through `workflow_dispatch`; it is not a pull-request job.

## Runner service

Run the self-hosted runner under a service manager so it is available for the scheduled workflow. Ensure the runner service uses the same OS user/home directory that owns `~/privacyfence`.

After runner replacement/re-registration, confirm the repository sees the `privacyfence-test` label and perform a manual workflow run before relying on the schedule.

## Rotating credentials

When a QA token/grant is revoked or expires:

1. authenticate the connector again using the dedicated QA account;
2. replace the corresponding file under `~/privacyfence/credentials/`;
3. preserve restrictive ownership/permissions;
4. run the workflow manually;
5. review any fixture drift PR before merging it.

Do not copy QA credentials into GitHub Actions secrets as a workaround.

## Troubleshooting

**Required QA file not found** — verify the runner service's home directory and the exact `~/privacyfence` tree above.

**Provider authorization failure** — refresh only the affected dedicated QA connector credential, then rerun.

**Atlassian (Jira/Confluence) `401`/`403 Forbidden` refreshing the token, especially right after a fresh reconnect** — the workflow copies any refreshed credential file back to `~/privacyfence/credentials/` after every run specifically so this doesn't happen (see "Workflow behavior" step 8); if it still does, confirm that write-back step actually ran (check the run's log) and that the runner user can write to `~/privacyfence/credentials/`. Atlassian rotates the refresh token on every use — reusing a stale one is a hard failure, not a retryable one, so this needs a fresh reconnect, not just a rerun.

**Seed resource not found/tag mismatch** — repair/recreate the dedicated resource and update `qa_environment.yaml` using [`qa-environment-setup.md`](qa-environment-setup.md).

**Runner never receives the job** — verify it is online, registered to the repository, and carries the `privacyfence-test` label.

**Install fails in the ephemeral venv** — reproduce `python3 -m venv` + `pip install -e ".[test]"` as the runner user and repair the host dependency/toolchain issue; do not keep a long-lived project venv as hidden state.

**`--lifecycle`'s Confluence row shows `n/a` under Cleanup** — expected, not a failure: deleting a page needs a `delete:page:confluence` scope this app deliberately never requests (see [`atlassian-setup.md`](atlassian-setup.md)); the QA space accumulates tagged pages that need occasional manual cleanup.

## Security boundary

The self-hosted runner is trusted infrastructure because it holds real QA connector grants. Restrict repository/job access accordingly, patch the host, monitor runner health, and rotate/revoke QA grants if the host is compromised.

The live runner supplements credential-free pull-request CI; it must not become a general-purpose execution environment for untrusted code.
