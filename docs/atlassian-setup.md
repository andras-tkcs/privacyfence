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
   http://127.0.0.1:53684/callback
   ```
   Atlassian requires an exact string match — `localhost` will not match PrivacyFence's actual redirect URI even though it resolves to the same address.

### 3. Add permissions (scopes)

PrivacyFence requests **granular** scopes, not classic ones. This matters for Confluence in particular: Confluence Cloud's newer v2 API (which PrivacyFence uses to list spaces) only accepts granular-scoped tokens — a classic-scoped token gets a 401 on those endpoints even with `read:confluence-space.summary` granted.

In the left sidebar, go to **Permissions**, switch the scope picker to **granular** scopes, and add both:
- **Jira API** — `read:user:jira`, `read:project:jira`, `read:issue:jira`, `read:comment:jira`, `write:issue:jira`, `write:comment:jira`
- **Confluence API** — `read:space:confluence`, `read:page:confluence`, `write:page:confluence`, `read:content:confluence`

You won't find `offline_access` (needed so PrivacyFence can refresh the token without asking users to sign in again) anywhere in the Permissions picker — it isn't tied to a product API, so the console never lists it as a checkbox. PrivacyFence's code adds it directly to the `scope` parameter of the authorization request, so there's nothing to configure here for it.

The exact set of scope names in Atlassian's console can drift over time; if a scope listed above doesn't appear, search the picker for the closest match to the operation it covers (view issues, view projects, view/create pages, etc.) rather than falling back to the classic API group.

> **Changing scopes on an app your team already uses?** Everyone needs to click **Reconnect…** on Jira or Confluence in the PrivacyFence menu bar afterward — existing tokens keep whatever scopes they were issued with until re-authenticated.

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

**"The app's callback URL is invalid" during sign-in** (IT admin)
The Callback URL in the Atlassian app must be exactly `http://127.0.0.1:53684/callback` — not `http://localhost:53684/callback`. Atlassian matches the redirect URI as a literal string, and PrivacyFence's loopback server always sends `127.0.0.1`.

**"401 Unauthorized" right after authenticating** (IT admin)
Double-check the **Callback URL** is exactly `http://127.0.0.1:53684/callback`, and that both the Jira API and Confluence API scopes were added under **Permissions**.

**Confluence connects but space/page calls fail with 401** (IT admin)
The Confluence scopes were added as **classic** scopes instead of **granular**. Confluence's v2 API (used for space listing) rejects classic-scoped tokens outright — re-add the scopes listed above using the granular picker, then have users **Reconnect…** to get a token with the new scopes.

**"403 Forbidden" on specific projects or spaces**
Your Atlassian account does not have access to that project or space. Check your Jira/Confluence permissions in the Atlassian admin console.

**Wrong Atlassian site connected**
Click **Reconnect…** on Jira or Confluence in the PrivacyFence menu bar to sign in again and pick a different site.

**Token expired mid-session**
PrivacyFence refreshes the token automatically in the background. If it still fails, click **Reconnect…** in the menu bar.
