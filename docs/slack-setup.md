# Slack App Setup

Loopline needs two separate Slack tokens because Slack splits permissions between bot tokens and user tokens:

| Token | Prefix | Used for |
|-------|--------|----------|
| Bot token | `xoxb-` | Reading channels, message history, thread replies, resolving users |
| User token | `xoxp-` | Searching messages (`search:read`), sending messages as you (`chat:write`) |

Both tokens come from the same Slack app. The user token is optional — if you only need to read channels and history you can leave it blank.

---

## 1. Create a Slack app

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From scratch**.
3. Give it a name (e.g. `Loopline`) and select the workspace you want to connect.
4. Click **Create App**.

---

## 2. Configure bot token scopes

1. In the left sidebar, go to **OAuth & Permissions**.
2. Scroll down to **Scopes → Bot Token Scopes**.
3. Click **Add an OAuth Scope** and add each of the following:

| Scope | Purpose |
|-------|---------|
| `channels:read` | List public channels |
| `groups:read` | List private channels the bot is in |
| `channels:history` | Read public channel messages |
| `groups:history` | Read private channel messages |
| `users:read` | Resolve user display names |
| `users:read.email` | Resolve user email addresses |

---

## 3. Configure user token scopes

Still on the **OAuth & Permissions** page:

1. Scroll down to **Scopes → User Token Scopes**.
2. Click **Add an OAuth Scope** and add:

| Scope | Purpose |
|-------|---------|
| `search:read` | Search messages across the workspace |
| `chat:write` | Send messages as you |

---

## 4. Install the app to your workspace

1. Scroll back to the top of the **OAuth & Permissions** page.
2. Click **Install to Workspace** (or **Reinstall** if you already installed it).
3. Review the permissions and click **Allow**.

After installation you will see two tokens on the same page:

- **Bot User OAuth Token** — starts with `xoxb-`
- **User OAuth Token** — starts with `xoxp-`

Copy both tokens.

---

## 5. Add the bot to channels (optional)

The bot token can only read channels it has been invited to. For each private channel or DM group you want Loopline to access, invite the bot:

```
/invite @Loopline
```

Public channels are visible without an invitation.

---

## 6. Enter the tokens in Loopline

Launch **Loopline.app**. If the setup wizard is not open, click **Setup Wizard** in the floating window.

1. Navigate to the **Slack** step.
2. Paste the `xoxb-` token into the **Bot token** field.
3. Paste the `xoxp-` token into the **User token** field.
4. Click **Next** to continue.

To configure manually, add both tokens to `config/settings.yaml`:

```yaml
slack:
  bot_token: "xoxb-..."
  user_token: "xoxp-..."
```

---

## Troubleshooting

**`not_in_channel` errors**  
The bot has not been invited to the channel. Run `/invite @Loopline` in the channel.

**`not_allowed_token_type` on search or send**  
These operations require the user token. Make sure you have added `search:read` and `chat:write` to **User Token Scopes** (not Bot Token Scopes) and re-installed the app.

**`missing_scope` errors**  
The scope was not added before installing. Add the missing scope under **OAuth & Permissions → Scopes**, then click **Reinstall to Workspace** and update the token in Loopline.

**`invalid_auth` errors**  
The token has been revoked or rotated. Re-install the app to get fresh tokens.
