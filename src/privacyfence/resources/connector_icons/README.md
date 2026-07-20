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

## Current sources

| File | Source |
| --- | --- |
| `gmail.png`, `drive.png`, `calendar.png` | Google's official Workspace branding CDN (`ssl.gstatic.com/images/branding/product/2x/<product>_2020q4_512dp.png`) — the flat mark, no background, matching Google's own brand-kit asset |
| `tasks.png`, `contacts.png` | The official Google Tasks / Google Contacts apps' own Play Store listing icons — not covered by the Workspace brand kit, so the app icon each is the closest first-party asset |
| `slack.png` | Slack's own App Store listing icon (publisher: Slack Technologies L.L.C.) |
| `jira.png`, `confluence.png` | Atlassian's own App Store listing icons for Jira Cloud / Confluence Cloud (publisher: Atlassian Pty Ltd) |
| `salesforce.png` | Salesforce's own App Store listing icon (publisher: salesforce.com) |
| `telegram.png` | Telegram's own App Store listing icon (publisher: Telegram FZ-LLC) |

All are square, ~512×512 (Google's three are 1024×1024), pulled directly from each company's own
CDN or app-store listing — not third-party icon aggregators.
