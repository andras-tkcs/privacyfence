# Telegram Setup

PrivacyFence uses **Telethon** (the MTProto user client) to access Telegram. This means Claude sees exactly what you see — every chat, group, and channel you are a member of — as your personal account, not a bot.

Telegram is the one deliberate exception to PrivacyFence's "browser OAuth everywhere" rule: MTProto has no browser-redirect OAuth equivalent for full user-session access, so signing in still means entering your phone number and a verification code (and your two-step-verification password, if you have one set). That per-user step happens through native PrivacyFence menu bar prompts instead of a Terminal window — you're never asked for a password to type into a text field you can't verify.

Unlike Google/Slack/Salesforce/Atlassian, the Telegram **application** (`api_id` / `api_hash`) is not organization-level config. Telegram has no concept of an "organization" the way OAuth providers do — `api_id`/`api_hash` just identify the PrivacyFence app itself, the same for every user. They're baked into the official PrivacyFence release build; there's nothing for an IT admin to register or distribute, and no Organization Config step for Telegram.

---

## For users

1. **Connectors → Telegram → Authenticate…** in the PrivacyFence menu bar.
2. Enter your phone number (with country code, e.g. `+1 555 000 0000`) when prompted.
3. Telegram sends a confirmation code to your Telegram app (or by SMS) — enter it when prompted.
4. If your account has two-step verification enabled, you'll be asked for your password too.
5. PrivacyFence saves a session file locally and does not ask again until it expires or is revoked.

---

## For maintainers (building PrivacyFence from source or cutting a release)

The release build reads `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from the environment at package time and bakes them into a git-ignored module — see `scripts/build_dmg.sh` and `src/privacyfence/app_credentials.py`. In CI (`.github/workflows/build.yml`), these come from the `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` repository secrets.

To register your own application (e.g. for a fork, or local development):

1. Go to [https://my.telegram.org/apps](https://my.telegram.org/apps) and sign in with a phone number.
2. Click **Create new application**.
3. Fill in the required fields:
   - **App title:** `PrivacyFence` (or any name you prefer)
   - **Short name:** `privacyfence` (lowercase, no spaces)
   - **Platform:** Desktop
   - **Description:** optional
4. Click **Create application**.
5. Copy the **App api_id** (a number) and **App api_hash** (a hex string) shown on the page.

> **Keep these secret.** This repo is public — never commit real values into source, `.env` files, or test fixtures. Anyone with the `api_id`/`api_hash` can build an app that impersonates a Telegram client, and abusive use of a shared `api_id` can get it rate-limited or flagged by Telegram, affecting every user of that build.

For a local dev build, export them before running the daemon:

```bash
export PRIVACYFENCE_TELEGRAM_API_ID=12345678
export PRIVACYFENCE_TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
```

---

## Troubleshooting

**"Telegram app credentials are missing from this build"**
You're running a build without `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` baked in (or set as env vars for a dev build). If you downloaded the official DMG, this shouldn't happen — please file an issue.

**"Session is not authorized" on startup**
The session file is missing or expired. Click **Reconnect…** on Telegram in the PrivacyFence menu bar (or run `privacyfence-app --telegram-setup` from source).

**"Two-step verification required"**
Enter your Telegram cloud password when the PrivacyFence menu bar prompts for it during sign-in.

**"PHONE_NUMBER_BANNED" or "AUTH_KEY_UNREGISTERED"**
Telegram has invalidated your session. Delete `credentials/telegram.session` — under `~/.privacyfence/`
for a bundled install, or the repo root if running from source (see
[dev-vs-live-setup.md](dev-vs-live-setup.md)) — and click **Authenticate…** again.

**Verification code arrives in the Telegram app, not by SMS**
This is expected for accounts that already have the Telegram app installed. Open Telegram on your phone or desktop and look for the code in the official **Telegram** system message.
