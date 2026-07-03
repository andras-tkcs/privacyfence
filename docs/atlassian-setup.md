# Atlassian Setup (Jira & Confluence)

PrivacyFence connects to **Jira Cloud** and **Confluence Cloud** via Atlassian's OAuth 2.0 (3LO). One OAuth grant covers both products — a user authenticates once and both connectors work.

> **Cloud only.** PrivacyFence supports Atlassian Cloud (`.atlassian.net` domains) only, not Jira/Confluence Data Center or Server.

The OAuth app is organization-level config: **one IT admin creates it once**, packages the client id/secret into PrivacyFence's organization config bundle, and distributes it. Individual users just click **Authenticate…** in the menu bar — no API tokens to generate or paste.

---

## For IT admins (once per organization)

### 1. Create an OAuth 2.0 app

1. Go to [https://developer.atlassian.com/console/myapps/](https://developer.atlassian.com/console/myapps/) and sign in.
2. Click **Create → OAuth 2.0 integration**.
3. Give it a name (e.g. `PrivacyFence`) and click **Create**.

### 2. Configure authorization

1. In the left sidebar, go to **Authorization**.
2. Next to **OAuth 2.0 (3LO)**, click **Add**/**Configure**.
3. Set the **Callback URL** to:
   ```
   http://localhost:53684/callback
   ```

### 3. Add permissions (scopes)

In the left sidebar, go to **Permissions** and add both:
- **Jira API** — with scopes `read:jira-work`, `write:jira-work`, `read:jira-user`
- **Confluence API** — with scopes `read:confluence-content.all`, `write:confluence-content`, `read:confluence-space.summary`

Also make sure `offline_access` is granted (needed so PrivacyFence can refresh the token without asking users to sign in again) — Atlassian includes it automatically once you request the scopes above through the classic scopes picker; if you're using granular scopes, add `offline_access` explicitly under **Permissions → User identity API** or the equivalent section shown in the console.

### 4. Get the client id and secret

In the left sidebar, go to **Settings**. Copy the **Client ID** and **Secret**.

### 5. Add it to the organization config bundle

```bash
python3 scripts/build_org_bundle.py \
  --atlassian-client-id abcdef01234567890 \
  --atlassian-client-secret abcdef0123456789abcdef0123456789 \
  -o org_config.json --merge
```

Distribute the resulting `org_config.json` to your users.

---

## For users

1. Get `org_config.json` from your IT team and install it via **Organization Config → Install/Update Organization Config…** in the PrivacyFence menu bar (if you haven't already for another service).
2. **Connectors → Jira → Authenticate…** (or **Confluence** — either one triggers the same sign-in and activates both). Your browser opens to Atlassian's consent screen — sign in and click **Accept**.
3. If your account has access to more than one Atlassian site, PrivacyFence asks you to pick one.
4. Quit and reopen PrivacyFence to activate the connectors.

---

## Troubleshooting

**"401 Unauthorized" right after authenticating** (IT admin)
Double-check the **Callback URL** is exactly `http://localhost:53684/callback`, and that both the Jira API and Confluence API scopes were added under **Permissions**.

**"403 Forbidden" on specific projects or spaces**
Your Atlassian account does not have access to that project or space. Check your Jira/Confluence permissions in the Atlassian admin console.

**Wrong Atlassian site connected**
Click **Reconnect…** on Jira or Confluence in the PrivacyFence menu bar to sign in again and pick a different site.

**Token expired mid-session**
PrivacyFence refreshes the token automatically in the background. If it still fails, click **Reconnect…** in the menu bar.
