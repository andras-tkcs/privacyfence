# Atlassian Setup (Jira & Confluence)

Loopline connects to **Jira Cloud** and **Confluence Cloud** using a single Atlassian API token. One token covers both products — you only need to set this up once.

> **Cloud only.** Loopline supports Atlassian Cloud (`.atlassian.net` domains) only, not Jira/Confluence Data Center or Server.

---

## 1. Find your Atlassian Cloud URL

Your cloud URL is the base domain for your Atlassian organisation, e.g. `https://yourcompany.atlassian.net`. You can find it in the address bar when you open Jira or Confluence in a browser.

---

## 2. Create an API token

1. Go to [https://id.atlassian.com/manage/api-tokens](https://id.atlassian.com/manage/api-tokens) and sign in with your Atlassian account.
2. Click **Create API token**.
3. Give it a label (e.g. `Loopline`) and click **Create**.
4. Click **Copy** to copy the token. **Save it now** — it is only shown once.

> **This token has the same permissions as your Atlassian account.** Keep it secret and treat it like a password. You can revoke it at any time from the same page.

---

## 3. Enter credentials in Loopline

Launch **Loopline.app**. If the setup wizard is not open, click **Setup Wizard** in the floating window.

1. Navigate to the **Atlassian** step.
2. Enter your **Cloud URL** (e.g. `https://yourcompany.atlassian.net`), **Email address**, and **API Token**.
3. Click **Next** to continue.

To configure manually, add the following to `config/settings.yaml`:

```yaml
jira:
  cloud_url: "https://yourcompany.atlassian.net"
  email: "you@yourcompany.com"
  api_token: "your-api-token"

confluence:
  cloud_url: "https://yourcompany.atlassian.net"
  email: "you@yourcompany.com"
  api_token: "your-api-token"
```

Both sections use the same values. If you only want one of the two connectors, you can omit the other block.

---

## Troubleshooting

**"401 Unauthorized" or "Basic auth with passwords is deprecated"**  
Make sure you are using an **API token**, not your Atlassian account password. Passwords are no longer accepted for API access.

**"403 Forbidden" on specific projects or spaces**  
Your Atlassian account does not have access to that project or space. Check your Jira/Confluence permissions in the Atlassian admin console.

**"Cloud URL not found" or connection errors**  
Verify the URL is exactly `https://yourcompany.atlassian.net` with no trailing slash. Personal accounts use `https://yourcompany.atlassian.net`; check the address bar in your browser.

**Token revoked or expired**  
API tokens do not expire on their own, but they can be revoked. Generate a new token at [https://id.atlassian.com/manage/api-tokens](https://id.atlassian.com/manage/api-tokens) and update `config/settings.yaml`.
