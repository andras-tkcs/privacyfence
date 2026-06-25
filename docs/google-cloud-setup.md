# Google Cloud Console Setup

This guide walks through creating a Google Cloud project, configuring OAuth, and enabling the APIs that Loopline requires.

---

## 1. Create a new project

1. Go to [https://console.cloud.google.com/](https://console.cloud.google.com/) and sign in with the Google account you want Loopline to access.
2. Click the project selector at the top of the page → **New Project**.
3. Give it a name (e.g. `loopline`) and click **Create**.
4. Make sure the new project is selected in the project selector before continuing.

---

## 2. Enable required APIs

Open **APIs & Services → Library** and enable each of the following APIs one by one. Use the search box to find them.

| API name | Library search term | Used by |
|----------|--------------------|---------| 
| Gmail API | `Gmail API` | Gmail connector |
| Google Drive API | `Google Drive API` | Drive connector |
| Google People API | `People API` | Contacts connector |
| Google Calendar API | `Google Calendar API` | Calendar connector |
| Google Tasks API | `Tasks API` | Google Tasks connector |

For each:
1. Click the API in the search results.
2. Click **Enable**.

> **Note:** The People API covers Google Contacts. Do not confuse it with the older Contacts API, which is deprecated.

---

## 3. Configure the OAuth consent screen

Before you can create credentials, you need to set up the OAuth consent screen. This is what users see when Loopline asks for permission to access their Google account.

1. Go to **APIs & Services → OAuth consent screen**.
2. Choose **External** as the user type (even if you will only use it yourself) and click **Create**.
3. Fill in the required fields:
   - **App name:** `Loopline` (or any name you prefer)
   - **User support email:** your email address
   - **Developer contact information:** your email address
4. Click **Save and Continue**.
5. On the **Scopes** step, click **Save and Continue** — you do not need to add scopes here; they are requested at runtime.
6. On the **Test users** step, add the Google account(s) you will authorise Loopline with. Click **Add users**, enter the email address, and click **Save and Continue**.
7. Review the summary and click **Back to Dashboard**.

> **Why test users?** While the app is in *Testing* mode (the default for new projects), only explicitly listed test users can complete the OAuth flow. You do not need to submit the app for verification to use it yourself.

---

## 4. Create OAuth 2.0 credentials

1. Go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. Set **Application type** to **Desktop app**.
4. Give it a name (e.g. `Loopline Desktop`) and click **Create**.
5. In the confirmation dialog, click **Download JSON**.
6. Save the downloaded file as `client_secret.json`.

This file is what Loopline's setup wizard asks for. Keep it private — treat it like a password.

---

## 5. Import into Loopline

Launch **Loopline.app**. If the setup wizard is not open, click **Setup Wizard** in the floating window.

1. On the **Google OAuth** step, click **Import client_secret.json** and select the file you downloaded.
2. Follow the prompts to authorise Gmail, Drive, Calendar, and Contacts. Each service opens a browser window; sign in and click **Allow**.
3. Loopline stores the resulting tokens in `credentials/` and does not need the `client_secret.json` file again after initial setup.

---

## Troubleshooting

**"Access blocked: Loopline has not completed the Google verification process"**  
The app is in Testing mode. Make sure the Google account you are signing in with is listed as a test user (step 3.6 above).

**"This app isn't verified"**  
Click **Advanced → Go to Loopline (unsafe)** to proceed. This warning appears for any unverified OAuth app and is expected for personal use.

**"redirect_uri_mismatch"**  
Make sure you created credentials of type **Desktop app**, not Web application. Web app credentials require a registered redirect URI that Loopline does not use.

**Scopes not granted / 403 errors**  
If you see permission errors when Loopline tries to access a service, re-run the OAuth flow for that connector:

```bash
# From source install
loopline-app --gmail-oauth
loopline-app --drive-oauth
loopline-app --contacts-oauth
loopline-app --calendar-oauth
```

Or click **Re-authorize** next to the connector in the Loopline setup wizard.
