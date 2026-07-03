# Slack Setup

PrivacyFence uses a **Slack user token** (`xoxp-…`) obtained via Slack's OAuth v2 browser flow. This means Claude sees exactly what you see — every channel, DM, and private group you are a member of — with no bot to invite and no footprint visible to others.

The Slack app itself is organization-level config: **one IT admin creates it once**, packages the client id/secret into PrivacyFence's organization config bundle, and distributes it. Individual users never see a token to copy/paste — they click **Authenticate…** in the menu bar and approve in their browser.

---

## For IT admins (once per organization)

### 1. Create a Slack app

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From scratch**.
3. Give it a name (e.g. `PrivacyFence`) and select your workspace.
4. Click **Create App**.

### 2. Add user token scopes

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
| `im:write` / `channels:write` / `groups:write` / `mpim:write` | Mark a conversation unread (`mark_unread` option on `slack_send_message`) |

> **Do not add Bot Token Scopes.** Only the User Token Scopes section is needed.

### 3. Set the redirect URL

Still on **OAuth & Permissions**, scroll to **Redirect URLs** and add:

```
http://127.0.0.1:53682/callback
```

This is PrivacyFence's loopback OAuth callback — it only listens during an active sign-in, on every user's own machine.

### 4. Get the client id and secret

Go to **Basic Information** in the left sidebar → **App Credentials**. Copy the **Client ID** and **Client Secret**.

> If your workspace requires admin approval for app installs, an admin will need to approve each user's first sign-in from Slack's side — this is a workspace policy setting, not something PrivacyFence controls.

### 5. Add it to the organization config bundle

```bash
python3 scripts/build_org_bundle.py \
  --slack-client-id 1234567890.1234567890 \
  --slack-client-secret abcdef0123456789abcdef0123456789 \
  -o org_config.json --merge
```

(Drop `--merge` if this is the first service you're adding to the bundle.) Distribute the resulting `org_config.json` to your users.

---

## For users

1. Get `org_config.json` from your IT team and install it via **Organization Config → Install/Update Organization Config…** in the PrivacyFence menu bar (if you haven't already for another service).
2. **Connectors → Slack → Authenticate…**. Your browser opens to Slack's consent screen — review the permissions and click **Allow**.
3. Quit and reopen PrivacyFence to activate the connector.

---

## Troubleshooting

**`missing_scope` errors** (IT admin)
A scope wasn't added before users signed in. Add the missing scope under **OAuth & Permissions → User Token Scopes**, then have each user click **Reconnect…** in the PrivacyFence menu bar to re-consent.

**`not_in_channel` on history reads**
The token only sees channels you are a member of. Join the channel in Slack first.

**`invalid_auth` errors**
The token has been revoked (e.g. you removed the app from your Slack account, or an admin uninstalled it). Click **Reconnect…** in the PrivacyFence menu bar.

**Browser doesn't return to PrivacyFence after clicking Allow**
Make sure the redirect URL in the Slack app's **OAuth & Permissions** page is exactly `http://127.0.0.1:53682/callback` (IT admin) — Slack requires an exact match.
