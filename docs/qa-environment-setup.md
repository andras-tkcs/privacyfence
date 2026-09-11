# QA environment setup

Use dedicated synthetic QA accounts/resources for live connector testing. The goal is repeatable provider coverage without touching personal or production data.

## Principles

- Use dedicated test accounts/tenants/workspaces where the provider supports them.
- Seed only synthetic content.
- Tag/name seeded resources consistently with `[QATEST]` so scripts can verify they are operating on QA data.
- Store only non-secret resource identifiers/keys in `tests/fixtures/qa_environment.yaml`.
- Keep OAuth tokens, sessions, private keys, and other credentials in the git-ignored PrivacyFence credential locations; never commit them.

## Seed manifest

`scripts/qa_fixture_recorder.py` reads `tests/fixtures/qa_environment.yaml` to find the dedicated resources it is allowed to inspect. Keep this manifest stable so repeated runs target the same known objects.

The manifest may contain resource ids, project keys, channel names, labels, or equivalent provider locators. It must not contain access tokens or real user secrets.

## Provider setup

Create representative resources for the connectors exercised by the live-check workflow, for example:

- Google: test messages/files/events/contacts/tasks and any Apps Script project used by connector QA;
- Slack: dedicated test workspace/channels/messages;
- Atlassian: dedicated Jira project/issues and Confluence space/pages;
- Salesforce: dedicated sandbox/test records;
- Telegram: dedicated test chat/session where applicable.

Use the connector-specific setup guides for OAuth/app configuration and PrivacyFence's normal authentication commands/settings to create the local credential files.

## Fixture recording

With credentials and the seed manifest configured, run:

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --check
```

To refresh committed redacted response fixtures after verified provider drift:

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --record
```

For supported write-capable providers:

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --lifecycle
```

The recorder redacts identity-bearing fields and de-identifies structural ids/URLs before writing fixtures. Review fixture diffs before committing them anyway; no real account identity, tenant URL, token, or private content should enter the repository.

## Self-hosted runner

The scheduled live check uses the same dedicated QA resources. Provision its persistent credentials and seed manifest according to [`connector-live-check-setup.md`](connector-live-check-setup.md).

## Maintenance

When a provider deletes/renames a QA resource, recreate it with the same synthetic intent and update `qa_environment.yaml`. When credentials expire, reauthorize only the dedicated QA account and replace the credential file.

Do not solve drift by pointing tests at arbitrary live data. The seed set should remain small, explicit, synthetic, and independently reviewable.
