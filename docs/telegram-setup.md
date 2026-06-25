# Telegram Setup

Loopline uses **Telethon** (the MTProto user client) to access Telegram. This means Claude sees exactly what you see — every chat, group, and channel you are a member of — as your personal account, not a bot.

---

## 1. Create a Telegram application

1. Go to [https://my.telegram.org/apps](https://my.telegram.org/apps) and sign in with your Telegram phone number.
2. Click **Create new application**.
3. Fill in the required fields:
   - **App title:** `Loopline` (or any name you prefer)
   - **Short name:** `loopline` (lowercase, no spaces)
   - **Platform:** Desktop
   - **Description:** optional
4. Click **Create application**.
5. Copy the **App api_id** (a number) and **App api_hash** (a hex string) shown on the page.

> **Keep these secret.** Anyone with your `api_id` and `api_hash` can build an app that acts as your account.

---

## 2. Enter the credentials in Loopline

Launch **Loopline.app**. If the setup wizard is not open, click **Setup Wizard** in the floating window.

1. Navigate to the **Telegram** step.
2. Enter your **API ID** and **API Hash**.
3. Click **Authorize**. A browser window opens asking for your phone number.
4. Enter your phone number (with country code, e.g. `+1 555 000 0000`).
5. Telegram sends a confirmation code to your Telegram app (or by SMS). Enter it when prompted.
6. If your account has two-step verification enabled, enter your password as well.
7. Once authorized, Loopline saves a session file to `credentials/telegram.session` and does not need the code again.

To configure manually, add the following to `config/settings.yaml`:

```yaml
telegram:
  api_id: 12345678
  api_hash: "0123456789abcdef0123456789abcdef"
  session_file: "credentials/telegram.session"
```

Then run the interactive authorization flow from source:

```bash
loopline-app --telegram-setup
```

---

## Troubleshooting

**"Session is not authorized" on startup**  
The session file is missing or expired. Re-run the authorization step in the setup wizard (or `loopline-app --telegram-setup` from source).

**"Two-step verification required"**  
Enter your Telegram cloud password when prompted during the authorization flow.

**"PHONE_NUMBER_BANNED" or "AUTH_KEY_UNREGISTERED"**  
Telegram has invalidated your session. Delete `credentials/telegram.session` and re-authorize.

**Authorization code arrives in Telegram app, not by SMS**  
This is expected for accounts that already have the Telegram app installed. Open Telegram on your phone or desktop and look for the code in the official **Telegram** system message.
