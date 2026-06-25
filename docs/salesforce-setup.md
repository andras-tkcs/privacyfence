# Salesforce Setup

Loopline connects to Salesforce using either **username + password + security token** (standard login) or a **connected app access token** (OAuth). The username/password flow is simpler for personal use.

---

## Option A — Username and password (recommended for personal use)

### 1. Find your Salesforce instance URL

Your instance URL is the hostname you use to log in, e.g. `https://mycompany.my.salesforce.com`. You can also find it in **Setup → Company Settings → My Domain**.

### 2. Get your security token

Salesforce requires a security token in addition to your password when logging in from an untrusted IP address.

1. In Salesforce, click your avatar in the top-right corner → **Settings**.
2. In the left sidebar, go to **My Personal Information → Reset My Security Token**.
3. Click **Reset Security Token**. Salesforce emails the new token to your registered email address.

> **Note:** Resetting the token invalidates the old one. Any other integration using the old token will need to be updated.

### 3. Enter credentials in Loopline

Launch **Loopline.app**. If the setup wizard is not open, click **Setup Wizard** in the floating window.

1. Navigate to the **Salesforce** step.
2. Enter your **Instance URL**, **Email / Username**, **Password**, and **Security Token**.
3. Click **Next** to continue.

To configure manually, add the following to `config/settings.yaml`:

```yaml
salesforce:
  instance_url: "https://yourcompany.my.salesforce.com"
  username: "you@yourcompany.com"
  password: "your-password"
  security_token: "your-security-token"
```

---

## Option B — Access token (Connected App / OAuth)

If your org uses IP restrictions or you prefer OAuth, you can supply a session access token directly.

1. Obtain a token using your preferred OAuth flow (e.g. via the Salesforce CLI: `sf org display --target-org <alias> --json | jq .result.accessToken`).
2. In `config/settings.yaml`, use:

```yaml
salesforce:
  instance_url: "https://yourcompany.my.salesforce.com"
  access_token: "00D..."
```

> **Note:** Access tokens expire. For long-running use, the username/password flow is more practical.

---

## Troubleshooting

**"INVALID_LOGIN: Invalid username, password, security token; or user locked out"**  
Double-check your username, password, and security token. If you recently reset your password, the security token is also reset — check your email for a new one.

**"LOGIN_MUST_USE_SECURITY_TOKEN"**  
Your org requires a security token. See step 2 above to obtain one.

**"REQUEST_LIMIT_EXCEEDED" or API limit errors**  
Salesforce enforces a daily API call limit per org. Reduce query frequency or switch to a Salesforce org with a higher limit.

**Sandbox vs. production**  
If you are connecting to a sandbox, use the sandbox login URL (e.g. `https://yourcompany--sandbox.sandbox.my.salesforce.com`). The setup is otherwise identical.
