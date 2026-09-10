# Salesforce Setup

PrivacyFence connects to Salesforce via OAuth 2.0 (the Web Server Flow), through a **Connected App**. No username, password, or security token is ever entered into PrivacyFence — users sign in through Salesforce's own login page in their browser.

The Connected App is organization-level config: **one IT admin creates it once**, packages the consumer key/secret into PrivacyFence's organization config bundle, and distributes it. Individual users just click **Authenticate…** in PrivacyFence Settings.

---

## For IT admins (once per organization)

### 1. Create a Connected App

1. In Salesforce, go to **Setup → App Manager → New Connected App**.
2. Fill in **Connected App Name** (e.g. `PrivacyFence`) and **Contact Email**.
3. Check **Enable OAuth Settings**.
4. Set **Callback URL** to:
   ```
   http://localhost:53683/callback
   ```
   Salesforce requires callback URLs to use HTTPS, with one exception: `http://`
   is allowed for testing when the host is literally `localhost`. PrivacyFence's
   redirect URI for Salesforce always uses `localhost` (not `127.0.0.1`, unlike
   PrivacyFence's other OAuth connectors) specifically so it qualifies for this
   exception — enter it exactly as `http://localhost:53683/callback` or
   Salesforce's console will reject the value.

   > **Deploying [`org` mode](org-mode-setup-guide.md) instead of (or in addition to) local desktop
   > installs?** Salesforce's Callback URL field accepts more than one URL — enter each on its own
   > line. Add a second line with `https://your-server-hostname/oauth/callback/salesforce` (org
   > mode's server-side redirect, `web/routes_connect.py`), substituting your own hostname. Both can
   > coexist in the same Connected App; the same consumer key/secret you build below work for either
   > deployment. See [`org-mode-setup-guide.md`
   > §4.2](org-mode-setup-guide.md#42-the-google-connector-client-optional) for the general pattern
   > this follows.
5. Under **Selected OAuth Scopes**, add:
   - `Manage user data via APIs (api)`
   - `Perform requests at any time (refresh_token, offline_access)`
6. Save.

> **New Connected Apps can take 2–10 minutes to become active.** If sign-in fails immediately after creating the app, wait a few minutes and try again.

### 2. Get the consumer key and secret

1. Open the Connected App you just created (**Setup → App Manager → \[your app\] → View**).
2. Click **Manage Consumer Details** (you may need to verify your identity again).
3. Copy the **Consumer Key** and **Consumer Secret**.

### 3. Add it to the organization config bundle

```bash
python3 scripts/build_org_bundle.py \
  --salesforce-consumer-key 3MVG9... \
  --salesforce-consumer-secret abcdef0123456789 \
  --salesforce-login-url https://login.salesforce.com \
  -o org_config.json --merge
```

Use `--salesforce-login-url https://test.salesforce.com` if your users authenticate against a sandbox instead of production. Distribute the resulting `org_config.json` to your users.

---

## For users

**Local desktop install:**

1. Get `org_config.json` from your IT team and install it via **Organization Config…** in PrivacyFence Settings (if you haven't already for another service — if a config is already installed, click **Update…** in the status prompt).
2. **Connectors → Salesforce → Authenticate…**. Your browser opens to Salesforce's login page — sign in and click **Allow**.
3. Quit and reopen PrivacyFence to activate the connector.

**[`org` mode](org-mode-setup-guide.md) deployment** (a server your IT team runs, not a desktop
install — ask them which applies to you):

1. Visit `https://your-server-hostname/login` and sign in with your organization identity provider.
2. On the `/connect` page, click **Connect** next to Salesforce. Your browser opens to Salesforce's
   login page — sign in and click **Allow**.
3. You land back on `/connect` showing Salesforce as connected — nothing to quit/reopen, since
   there's no local app. See [`org-mode-setup-guide.md`
   §8](org-mode-setup-guide.md#8-first-sign-in-and-connecting-a-service).

Your access token is refreshed automatically in the background as needed — no re-entering credentials.

---

## Troubleshooting

**"redirect_uri_mismatch" or "invalid client credentials"** (IT admin)
The Connected App's **Callback URL** must include `http://localhost:53683/callback` exactly (local desktop installs), and, for an [`org` mode](org-mode-setup-guide.md) deployment, `https://your-server-hostname/oauth/callback/salesforce` on its own line too. Also double-check the Consumer Key/Secret went into the bundle correctly.

**Salesforce won't let me save an `http://` Callback URL** (IT admin)
Salesforce requires callback URLs to use HTTPS except when the host is exactly `localhost` — `http://127.0.0.1:...` or any other host will be rejected. Use `http://localhost:53683/callback` exactly as shown above.

**Sign-in fails right after creating the Connected App**
New Connected Apps take a few minutes to propagate — wait and retry.

**"REQUEST_LIMIT_EXCEEDED" or API limit errors**
Salesforce enforces a daily API call limit per org. Reduce query frequency or switch to a Salesforce org with a higher limit.

**Sandbox vs. production**
If your org authenticates against a sandbox, make sure the bundle was built with `--salesforce-login-url https://test.salesforce.com` (IT admin) — the sign-in flow is otherwise identical.

**Token expired mid-session**
PrivacyFence retries once with a refreshed token automatically. If it still fails, click **Reconnect…** in PrivacyFence Settings to sign in again.
