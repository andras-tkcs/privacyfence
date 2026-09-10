# Google Cloud Console Setup

This guide walks through creating a Google Cloud project, configuring OAuth, and enabling the APIs that PrivacyFence's Gmail, Drive, Calendar, Contacts, Tasks, and Apps Script connectors require. If your organization also does Workspace room/resource booking, there's a second, separate project involved — see "Room directory sync" below.

Google is organization-level config: **one IT admin does this once**, packages the result into PrivacyFence's organization config bundle, and distributes it. Individual users never touch the Google Cloud Console — they just click **Authenticate…** in PrivacyFence Settings and sign in with their browser.

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
| Google Docs API | `Google Docs API` | Drive connector (`drive_write_doc_content`, `drive_docs_edit_content`, `drive_docs_format_content`) |
| Google Sheets API | `Google Sheets API` | Drive connector (`drive_sheets_*`) |
| Google People API | `People API` | Contacts connector |
| Google Calendar API | `Google Calendar API` | Calendar connector |
| Google Tasks API | `Tasks API` | Google Tasks connector |
| Apps Script API | `Apps Script API` | Apps Script connector (`apps_script_get_content`, `apps_script_write_content`, `apps_script_get_execution_log`) |

For each: click the API in the search results, then click **Enable**.

> **Note:** The People API covers Google Contacts. Do not confuse it with the older Contacts API, which is deprecated.

> **Note:** The Sheets API doesn't need its own OAuth scope or consent-screen entry — it accepts the same `drive` scope already granted, so users don't re-authenticate. It still has to be individually **enabled** in this project's API Library like every other API here; if it's left disabled, `drive_sheets_*` calls fail with an `accessNotConfigured` / "API has not been used in project ... before or it is disabled" error even though the user's OAuth token is otherwise valid.

> **Note:** This project deliberately never requests Admin SDK / Workspace-directory scopes. `calendar_list_rooms` (room/resource booking) is served from a static room directory synced separately — see "Room directory sync" below — precisely so that the OAuth client every employee authorizes day to day can never read the Workspace directory.

> **Note:** The Apps Script connector also requests a narrow `drive.metadata.readonly` scope (name/id/timestamps only, never file content) purely to list a user's own standalone script projects — the Apps Script API itself has no "list my projects" endpoint. It runs no script code: there is deliberately no `run`/execute tool. See `apps_script_client.py`'s module docstring.

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

   > **Deploying [`org` mode](org-mode-setup-guide.md) instead of (or in addition to) local desktop
   > installs?** Org mode's server-side redirect flow needs a **Web application** client, not a
   > Desktop app one, with explicit HTTPS redirect URIs registered — a different client id/secret
   > from the Desktop app client above (the two can coexist; local desktop installs and the org-mode
   > server just use different credentials for the same Google project). See
   > [`org-mode-setup-guide.md` §4.2](org-mode-setup-guide.md#42-the-google-connector-client-optional)
   > for the exact steps.

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

## Room directory sync (optional, separate Google Cloud project)

Skip this whole section if your organization doesn't do Workspace room/resource booking —
every other Calendar tool works fine without it.

`calendar_list_rooms` doesn't call Google live. It reads a static room directory (name, email,
building, floor, capacity) that IT syncs into `org_config.json` ahead of time with
`scripts/sync_room_directory.py`. That script needs `admin.directory.resource.calendar.readonly`,
a Workspace-admin-level scope — and it deliberately runs against **a second Google Cloud
project**, separate from the one above, so the OAuth client every employee authorizes for
Gmail/Drive/Calendar/Contacts/Tasks never carries that scope. A leaked or over-shared per-user
token then simply can't read your Workspace directory, no matter what.

1. Create a **second** project the same way as step 1 above (e.g. `privacyfence-room-sync`).
2. **APIs & Services → Library** → enable **Admin SDK API** only.
3. **APIs & Services → OAuth consent screen** → same as step 3 above, but there's no need to add
   test users beyond whoever on your IT team will actually run the sync.
4. **APIs & Services → Credentials** → **+ Create Credentials → OAuth client ID** → **Desktop app**
   → **Download JSON**. This is a *second*, separate `client_secret.json` — keep it at least as
   private as the first one, and never add it to `org_config.json` or hand it to end users.
5. Run the sync, signed in with an account that holds the Workspace **Directory Reader** role (or
   super admin). Like `build_org_bundle.py`, this script doesn't need a full PrivacyFence install
   — copy it out of the repo if you like — just the same three Google client libraries PrivacyFence
   itself depends on:
   ```bash
   pip install google-auth google-auth-oauthlib google-api-python-client
   python3 scripts/sync_room_directory.py \
     --admin-client-secret /path/to/room_sync_client_secret.json \
     --org-config org_config.json
   ```
   This merges a `rooms` snapshot into the existing bundle without touching its other sections.
   Re-run it whenever your organization's rooms change; `--token-file` (default
   `.room_sync_token.json`) caches the sync's own token so you don't have to re-consent every time
   — keep that file private too, for the same reason as the client secret.
6. Redistribute the updated `org_config.json` exactly as in step 5 above. The `rooms` data itself
   is plain metadata, not a credential, so it's fine for every user's install to have it.

---

## For users

**Local desktop install:**

1. Get `org_config.json` from your IT team.
2. In PrivacyFence Settings: **Organization Config…**, and select the file.
3. For each Google connector you want (Gmail, Drive, Calendar, Contacts, Tasks, Apps Script): **Connectors → \<service\> → Authenticate…**. Your browser opens to Google's sign-in page — sign in and click **Allow**.
4. Quit and reopen PrivacyFence to activate the connector.

**[`org` mode](org-mode-setup-guide.md) deployment** (a server your IT team runs, not a desktop
install — ask them which applies to you; Apps Script isn't offered here, it stays local-mode-only,
see [`org-mode-setup-guide.md` §4.2](org-mode-setup-guide.md#42-the-google-connector-client-optional)):

1. Visit `https://your-server-hostname/login` and sign in with whatever identity provider your
   organization's server uses for sign-in (org mode's IdP is a separate, independent choice from
   which connectors it wires up — see [`org-mode-setup-guide.md`
   §4.1](org-mode-setup-guide.md#41-the-oidc-sign-in-client-required) — it's commonly Google too, but
   doesn't have to be). Either way, there's no separate "install a config file" step like local mode's.
2. On the `/connect` page, click **Connect** next to each Google connector you want (Gmail, Drive,
   Calendar, Contacts, Tasks). Each redirects to Google, asks for consent to that connector's specific
   scopes, and lands you back on `/connect` showing it connected.
3. Nothing to quit/reopen, since there's no local app. See [`org-mode-setup-guide.md`
   §8](org-mode-setup-guide.md#8-first-sign-in-and-connecting-a-service).

---

## Troubleshooting

**"Access blocked: PrivacyFence has not completed the Google verification process"** (IT admin)
The app is in Testing mode. Make sure the Google account signing in is listed as a test user (step 3.6 above), or submit the app for Google's verification if you have many users.

**"This app isn't verified"**
Click **Advanced → Go to PrivacyFence (unsafe)** to proceed. This warning appears for any unverified OAuth app and is expected until the org's app is verified by Google.

**"redirect_uri_mismatch"** (IT admin)
For a local desktop install, make sure you created credentials of type **Desktop app**, not Web
application — Desktop app clients accept any loopback redirect port, which is what PrivacyFence's
OAuth flow uses. For an [`org` mode](org-mode-setup-guide.md) deployment it's the other way around:
the connector client must be a **Web application** client with the exact
`https://your-server-hostname/oauth/callback/<service>` redirect URIs registered — see
[`org-mode-setup-guide.md` §4.2](org-mode-setup-guide.md#42-the-google-connector-client-optional).
These are two different, unrelated OAuth clients even against the same Google Cloud project — check
you're editing the one this install actually uses.

**Scopes not granted / 403 errors** (user)
Click **Reconnect…** next to the connector in PrivacyFence Settings to re-run the OAuth flow. From source, you can also run `privacyfence-app --gmail-oauth` (or `--drive-oauth` / `--contacts-oauth` / `--calendar-oauth` / `--tasks-oauth` / `--apps-script-oauth`).

**`calendar_list_rooms` comes back empty** (user)
This just means IT hasn't run `scripts/sync_room_directory.py` yet, or hasn't redistributed the
result — it's not an error. Ask IT to run the sync (see "Room directory sync" above) and send you
the refreshed `org_config.json`.

**`sync_room_directory.py` fails with "Room directory listing requires Google Workspace admin access"** (IT admin)
The Google account you signed in with when running the script isn't a Workspace admin and doesn't
hold the **Directory Reader** role. Re-run the script signed in as an account that does — this is
enforced by Google, not by PrivacyFence.
