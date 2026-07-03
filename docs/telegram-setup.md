# Telegram Setup

PrivacyFence uses **Telethon** (the MTProto user client) to access Telegram. This means Claude sees exactly what you see — every chat, group, and channel you are a member of — as your personal account, not a bot.

Telegram is the one deliberate exception to PrivacyFence's "browser OAuth everywhere" rule: MTProto has no browser-redirect OAuth equivalent for full user-session access, so signing in still means entering your phone number and a verification code (and your two-step-verification password, if you have one set). That per-user step now happens through native PrivacyFence menu bar prompts instead of a Terminal window — you're never asked for a password to type into a text field you can't verify.

The Telegram **application** (`api_id` / `api_hash`) is still organization-level config: **one IT admin registers it once**, packages it into PrivacyFence's organization config bundle, and distributes it. Individual users only ever do the phone/code sign-in.

---

## For IT admins (once per organization)

### 1. Register a Telegram application

1. Go to [https://my.telegram.org/apps](https://my.telegram.org/apps) and sign in with a phone number.
2. Click **Create new application**.
3. Fill in the required fields:
   - **App title:** `PrivacyFence` (or any name you prefer)
   - **Short name:** `privacyfence` (lowercase, no spaces)
   - **Platform:** Desktop
   - **Description:** optional
4. Click **Create application**.
5. Copy the **App api_id** (a number) and **App api_hash** (a hex string) shown on the page.

> **Keep these secret.** Anyone with the `api_id`/`api_hash` can build an app that impersonates a Telegram client — treat the bundle file itself with the same care you'd give any credential.

### 2. Add it to the organization config bundle

```bash
python3 scripts/build_org_bundle.py \
  --telegram-api-id 12345678 \
  --telegram-api-hash 0123456789abcdef0123456789abcdef \
  -o org_config.json --merge
```

Distribute the resulting `org_config.json` to your users.

---

## For users

1. Get `org_config.json` from your IT team and install it via **Organization Config → Install/Update Organization Config…** in the PrivacyFence menu bar (if you haven't already for another service).
2. **Connectors → Telegram → Authenticate…**.
3. Enter your phone number (with country code, e.g. `+1 555 000 0000`) when prompted.
4. Telegram sends a confirmation code to your Telegram app (or by SMS) — enter it when prompted.
5. If your account has two-step verification enabled, you'll be asked for your password too.
6. PrivacyFence saves a session file locally and does not ask again until it expires or is revoked.

---

## Troubleshooting

**"Session is not authorized" on startup**
The session file is missing or expired. Click **Reconnect…** on Telegram in the PrivacyFence menu bar (or run `privacyfence-app --telegram-setup` from source).

**"Two-step verification required"**
Enter your Telegram cloud password when the PrivacyFence menu bar prompts for it during sign-in.

**"PHONE_NUMBER_BANNED" or "AUTH_KEY_UNREGISTERED"**
Telegram has invalidated your session. Delete `~/.privacyfence/credentials/telegram.session` and click **Authenticate…** again.

**Verification code arrives in the Telegram app, not by SMS**
This is expected for accounts that already have the Telegram app installed. Open Telegram on your phone or desktop and look for the code in the official **Telegram** system message.
