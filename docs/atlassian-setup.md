# Atlassian Setup (Jira & Confluence)

PrivacyFence connects to **Jira Cloud** and **Confluence Cloud** via Atlassian's OAuth 2.0 (3LO). One OAuth grant covers both products — a user authenticates once and both connectors work.

> **Cloud only.** PrivacyFence supports Atlassian Cloud (`.atlassian.net` domains) only, not Jira/Confluence Data Center or Server.

The OAuth app is organization-level config: **one IT admin creates it once**, packages the client id/secret into PrivacyFence's organization config bundle, and distributes it. Individual users just click **Authenticate…** in PrivacyFence Settings — no API tokens to generate or paste.

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

Jira and Confluence need **different scope types** here — this isn't a typo:

- **Jira API** — add these as **classic** scopes: `read:jira-work`, `write:jira-work`, `read:jira-user`. Jira's endpoints work fine with classic scopes, and Atlassian's own guidance is to prefer classic for Jira where available.
- **Confluence API** — add these as **granular** scopes: `read:space:confluence`, `read:page:confluence`, `write:page:confluence`, `read:content:confluence`, `read:content-details:confluence`, `write:content:confluence`, `read:attachment:confluence`. Confluence Cloud's newer v2 API (which PrivacyFence uses to list spaces) only accepts granular-scoped tokens — a classic-scoped token 401s on those endpoints ("scope does not match") even with `read:confluence-space.summary` granted. `read:content-details:confluence` gates CQL/text search and content-detail reads; `write:content:confluence` is needed for page creation alongside `write:page:confluence`; `read:attachment:confluence` gates both listing a page's attachments (`confluence_list_attachments`) and downloading one (`confluence_download_attachment`) — Confluence's legacy attachment download link (what the v2 attachments list itself returns) is browser-session-only and 401s for an OAuth 3LO token regardless of scope, so PrivacyFence uses the dedicated download-redirect endpoint that scope actually covers. (Note: `search:confluence` is a **classic**-only scope name and won't appear in the granular picker — don't look for it there.)

Classic and granular are independent scope namespaces per product, so mixing classic Jira scopes with granular Confluence scopes in the same app/token is fine — Atlassian tracks them separately (visible as separate entries in the token's `accessible-resources` response). Don't switch Jira's scopes to granular "for consistency" — that breaks Jira with the same "scope does not match" 401, since Jira's classic-to-granular scope names aren't a reliable 1:1 mapping.

You won't find `offline_access` (needed so PrivacyFence can refresh the token without asking users to sign in again) anywhere in the Permissions picker — it isn't tied to a product API, so the console never lists it as a checkbox. PrivacyFence's code adds it directly to the `scope` parameter of the authorization request, so there's nothing to configure here for it.

> **Changing scopes on an app your team already uses?** Everyone needs to click **Reconnect…** on Jira or Confluence in PrivacyFence Settings afterward — existing tokens keep whatever scopes they were issued with until re-authenticated.

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

### 6. Org mode needs a *second*, dedicated app

Unlike Slack/Salesforce/Google, an Atlassian OAuth 2.0 (3LO) app accepts only **one** registered
Callback URL, full stop — there's no "add one more line" option here. That one URL is already spoken
for by the loopback callback in step 2 (`http://127.0.0.1:53684/callback`, local desktop installs),
so an [`org` mode](org-mode-setup-guide.md) deployment
(`https://your-server-hostname/oauth/callback/atlassian` — see `web/routes_connect.py`'s
`_GRANT_KEY`, which sends the *same* callback URL for both Jira and Confluence since they're one
underlying grant) needs an **app of its own**:

1. Repeat steps 1–4 above to create a second app (e.g. `PrivacyFence (org)`), with its Callback URL
   set to `https://your-server-hostname/oauth/callback/atlassian` instead.
2. Build a **separate** `org_config.json` for the server from this second app's client id/secret —
   don't `--merge` it into the same bundle you hand out to local desktop users, since that bundle's
   one `atlassian` section can only ever carry one app's credentials, and the server's own bundle
   also needs the `--mode org`/`--server-*`/`--idp-*` flags from
   [`org-mode-setup-guide.md` §5](org-mode-setup-guide.md#5-build-the-organization-config-bundle)
   that a local-install bundle doesn't carry.

If you're only ever running `org` mode (no local desktop installs), you don't need two apps or two
bundles — just point step 2's Callback URL at the org-mode one from the start.

---

## For users

1. Get `org_config.json` from your IT team and install it via **Organization Config…** in PrivacyFence Settings (if you haven't already for another service — if a config is already installed, click **Update…** in the status prompt).
2. **Connectors → Jira → Authenticate…** (or **Confluence** — either one triggers the same sign-in and activates both). Your browser opens to Atlassian's consent screen — sign in and click **Accept**.
3. If your account has access to more than one Atlassian site, PrivacyFence asks you to pick one.
4. Quit and reopen PrivacyFence to activate the connectors.

---

## Troubleshooting

**"The app's callback URL is invalid" during sign-in** (IT admin)
For a local desktop install, the Callback URL in the Atlassian app must be exactly
`http://127.0.0.1:53684/callback` — not `http://localhost:53684/callback`. Atlassian matches the
redirect URI as a literal string, and PrivacyFence's loopback server always sends `127.0.0.1`. For an
[`org` mode](org-mode-setup-guide.md) deployment it must instead be exactly
`https://your-server-hostname/oauth/callback/atlassian` (see [§6](#6-org-mode-needs-a-second-dedicated-app))
— an Atlassian app can only have one Callback URL, so it's one or the other, never both, on a given
app.

**"401 Unauthorized" right after authenticating** (IT admin)
Double-check the **Callback URL** matches the deployment mode this app is for (see the previous
entry), and that both the Jira API and Confluence API scopes were added under **Permissions**.

**Confluence connects but space/page calls fail with 401 ("scope does not match")** (IT admin)
The Confluence scopes were added as **classic** scopes instead of **granular**. Confluence's v2 API (used for space listing) rejects classic-scoped tokens outright — re-add the scopes listed above using the granular picker, then have users **Reconnect…** to get a token with the new scopes.

**Jira fails with 401 ("scope does not match") after re-authenticating** (IT admin)
Jira's scopes were added as **granular** instead of **classic** — or the OAuth app's `scope` request was changed to send granular Jira scope names. Jira needs classic scopes (`read:jira-work`, `write:jira-work`, `read:jira-user`); granular Jira scope names don't map cleanly and reliably 401 even when scopes look "equivalent." Switch Jira back to classic in **Permissions**, then **Reconnect…**.

**`confluence_list_attachments`/`confluence_download_attachment` fail with 401** (IT admin)
Either the `read:attachment:confluence` scope wasn't added to the OAuth app yet (see step 3 above), or it was, but existing users haven't clicked **Reconnect…** on Confluence yet — a token issued before this scope existed keeps whatever scopes it was issued with. Add the scope if missing, then have affected users **Reconnect…**.

**"403 Forbidden" on specific projects or spaces**
Your Atlassian account does not have access to that project or space. Check your Jira/Confluence permissions in the Atlassian admin console.

**Wrong Atlassian site connected**
Click **Reconnect…** on Jira or Confluence in PrivacyFence Settings to sign in again and pick a different site.

**Token expired mid-session**
PrivacyFence refreshes the token automatically in the background. If it still fails, click **Reconnect…** in PrivacyFence Settings.
