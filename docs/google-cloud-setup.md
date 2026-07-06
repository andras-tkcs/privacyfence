# Google Cloud Console Setup

This guide walks through creating a Google Cloud project, configuring OAuth, and enabling the APIs that PrivacyFence's Gmail, Drive, Calendar, Contacts, and Tasks connectors require.

Google is organization-level config: **one IT admin does this once**, packages the result into PrivacyFence's organization config bundle, and distributes it. Individual users never touch the Google Cloud Console — they just click **Authenticate…** in the menu bar and sign in with their browser.

---

## For IT admins (once per organization)

### 1. Create a new project

1. Go to [https://console.cloud.google.com/](https://console.cloud.google.com/) and sign in.
2. Click the project selector at the top of the page → **New Project**.
3. Give it a name (e.g. `privacyfence`) and click **Create**.
4. Make sure the new project is selected in the project selector before continuing.

### 2. Enable required APIs

Open **APIs & Services → Library** and enable each of the following APIs one by one. Use the search box to find them.

| API name | Library search term | Used by |
|----------|--------------------|---------| 
| Gmail API | `Gmail API` | Gmail connector |
| Google Drive API | `Google Drive API` | Drive connector |
| Google Docs API | `Google Docs API` | Drive connector (`drive_write_doc_content`) |
| Google Sheets API | `Google Sheets API` | Drive connector (`drive_sheets_*`) |
| Google People API | `People API` | Contacts connector |
| Google Calendar API | `Google Calendar API` | Calendar connector |
| Google Tasks API | `Tasks API` | Google Tasks connector |

For each: click the API in the search results, then click **Enable**.

> **Note:** The People API covers Google Contacts. Do not confuse it with the older Contacts API, which is deprecated.

> **Note:** The Sheets API doesn't need its own OAuth scope or consent-screen entry — it accepts the same `drive` scope already granted, so users don't re-authenticate. It still has to be individually **enabled** in this project's API Library like every other API here; if it's left disabled, `drive_sheets_*` calls fail with an `accessNotConfigured` / "API has not been used in project ... before or it is disabled" error even though the user's OAuth token is otherwise valid.

### 3. Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**.
2. Choose **Internal** if you're on Google Workspace (so only your organization's accounts can authorize), or **External** for a personal Google account. Click **Create**.
3. Fill in the required fields:
   - **App name:** `PrivacyFence` (or any name you prefer)
   - **User support email:** your email address
   - **Developer contact information:** your email address
4. Click **Save and Continue**.
5. On the **Scopes** step, click **Save and Continue** — you do not need to add scopes here; they are requested at runtime.
6. If you chose **External**, add every user who will use PrivacyFence as a **Test user** on that step (or submit the app for verification if you have many users — see Google's docs on OAuth verification).
7. Review the summary and click **Back to Dashboard**.

### 4. Create OAuth 2.0 credentials

1. Go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. Set **Application type** to **Desktop app**.
4. Give it a name (e.g. `PrivacyFence Desktop`) and click **Create**.
5. In the confirmation dialog, click **Download JSON**. This is your `client_secret.json` — keep it private, treat it like a password.

### 5. Add it to the organization config bundle

From the PrivacyFence repo (or anywhere with Python 3 installed — the script has no dependencies):

```bash
python3 scripts/build_org_bundle.py \
  --org-name "Your Company" \
  --google-client-secret /path/to/client_secret.json \
  -o org_config.json
```

Run it again with `--merge` if you're adding Google to a bundle that already has other services configured. Distribute the resulting `org_config.json` to your users (email, a shared drive, MDM — whatever your organization already uses to distribute internal tools).

---

## For users

1. Get `org_config.json` from your IT team.
2. In the PrivacyFence menu bar: **Organization Config → Install/Update Organization Config…**, and select the file.
3. For each Google connector you want (Gmail, Drive, Calendar, Contacts, Tasks): **Connectors → \<service\> → Authenticate…**. Your browser opens to Google's sign-in page — sign in and click **Allow**.
4. Quit and reopen PrivacyFence to activate the connector.

---

## Troubleshooting

**"Access blocked: PrivacyFence has not completed the Google verification process"** (IT admin)
The app is in Testing mode. Make sure the Google account signing in is listed as a test user (step 3.6 above), or submit the app for Google's verification if you have many users.

**"This app isn't verified"**
Click **Advanced → Go to PrivacyFence (unsafe)** to proceed. This warning appears for any unverified OAuth app and is expected until the org's app is verified by Google.

**"redirect_uri_mismatch"** (IT admin)
Make sure you created credentials of type **Desktop app**, not Web application — Desktop app clients accept any loopback redirect port, which is what PrivacyFence's OAuth flow uses.

**Scopes not granted / 403 errors** (user)
Click **Reconnect…** next to the connector in the PrivacyFence menu bar to re-run the OAuth flow. From source, you can also run `privacyfence-app --gmail-oauth` (or `--drive-oauth` / `--contacts-oauth` / `--calendar-oauth` / `--tasks-oauth`).
