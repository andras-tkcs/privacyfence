# Connector icons

Drop real per-service brand icons here to have them render top-left in the approval dialog,
alongside the "PrivacyFence" kicker (see `approval_window.py`'s `_connector_icon_path()`).

Expected filenames — one PNG per connector, named to match the identifier `gate.py`'s
`gated_call(connector=...)` already passes through (`menu_bar.py`'s `ALL_CONNECTORS`):

- `gmail.png`
- `drive.png`
- `contacts.png`
- `calendar.png`
- `tasks.png`
- `slack.png`
- `jira.png`
- `confluence.png`
- `salesforce.png`
- `telegram.png`

Until a given file exists, `_connector_icon_path()` returns `None` and the dialog renders exactly
as it does today for that connector — no icon, no reserved layout space, never an error.

These are real trademarked logos, not something to fabricate. Source them from each service's own
official brand/press kit (most publish explicit guidelines for showing their logo to indicate an
integration — Google's Identity brand guidelines, Slack's, Atlassian's for Jira/Confluence,
Salesforce's, and Telegram's all do). Follow each one's usage rules (minimum size, clear space,
don't recolor/distort) rather than editing the asset to fit.
