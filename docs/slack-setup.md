# Slack Setup

Loopline uses a **user token** (`xoxp-…`) to access Slack. This means Claude sees exactly what you see — every channel, DM, and private group you are a member of — with no bot to invite and no footprint visible to others.

---

## 1. Create a Slack app

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From scratch**.
3. Give it a name (e.g. `Loopline`) and select your workspace.
4. Click **Create App**.

---

## 2. Add user token scopes

1. In the left sidebar, go to **OAuth & Permissions**.
2. Scroll down to **Scopes → User Token Scopes** (not Bot Token Scopes).
3. Click **Add an OAuth Scope** and add each of the following:

| Scope | Purpose |
|-------|---------|
| `channels:read` | List public channels |
| `groups:read` | List private channels you're in |
| `im:read` | List your direct messages |
| `mpim:read` | List your group direct messages |
| `channels:history` | Read public channel messages |
| `groups:history` | Read private channel messages |
| `im:history` | Read direct message history |
| `mpim:history` | Read group DM history |
| `users:read` | Resolve user display names |
| `users:read.email` | Resolve user email addresses |
| `search:read` | Search messages across the workspace |
| `chat:write` | Send messages as you |
| `im:write` | Mark a DM as unread (`mark_unread` option on `slack_send_message`) |

> **Do not add Bot Token Scopes.** Only the User Token Scopes section is needed.

---

## 3. Install the app to your workspace

1. Scroll to the top of the **OAuth & Permissions** page.
2. Click **Install to Workspace**.
3. Review the permissions and click **Allow**.

After installation, copy the **User OAuth Token** (starts with `xoxp-`) shown at the top of the page.

---

## 4. Enter the token in Loopline

Launch **Loopline.app**. If the setup wizard is not open, click **Setup Wizard** in the floating window.

1. Navigate to the **Slack** step.
2. Paste the `xoxp-` token into the field.
3. Click **Next** to continue.

To configure manually, add the token to `config/settings.yaml`:

```yaml
slack:
  user_token: "xoxp-..."
```

---

## Troubleshooting

**`missing_scope` errors**
The scope was not added before installing. Add the missing scope under **OAuth & Permissions → User Token Scopes**, click **Reinstall to Workspace**, and paste the new token into Loopline.

**`not_in_channel` on history reads**
The token only sees channels you are a member of. Join the channel in Slack first.

**`invalid_auth` errors**
The token has been revoked. Reinstall the app and update the token in Loopline.
