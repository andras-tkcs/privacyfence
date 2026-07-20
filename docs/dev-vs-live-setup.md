# Dev vs. Live Setup (Two-Account Guide)

How to run a source/test build of PrivacyFence on one macOS account and a
real, GitHub-release build on another, without either one interfering with
the other.

## Why two accounts is enough

Everything PrivacyFence stores is scoped to `~/.privacyfence` on whichever
macOS account runs it: credentials, the IPC socket
(`~/.privacyfence/privacyfence.sock`, always this path regardless of
dev/bundled — see `src/privacyfence/ipc.py`), and the LaunchAgent. Two
separate accounts never see each other's daemon, socket, credentials, or
Claude MCP config — no extra isolation config needed.

One wrinkle: when running **from source** (unbundled), config/credentials/logs
live **inside the repo folder itself**, not `~/.privacyfence` (see
`src/privacyfence/paths.py`). Only a PyInstaller-bundled `.app` uses
`~/.privacyfence` for that. The IPC socket is the one exception that's
always under `~/.privacyfence`, bundled or not.

---

## Account 1: developer / test

Run from source — never install the DMG here.

`scripts/dev_start.sh` handles the repetitive part: it creates `.venv` and
`config/settings.yaml` if missing, builds the bridge from this checkout's
`bridge/` and (re-)registers it — via `claude mcp` if that CLI is on PATH,
otherwise by editing Claude Desktop's own config file directly — and starts
the daemon in the foreground. Ctrl-C stops the daemon and de-registers the
dev bridge again (prompting you to restart Claude Desktop when it went the
config-file route). Safe to re-run any time, e.g. after switching branches:

```bash
cd /Users/user1/Coding/privacyfence
./scripts/dev_start.sh
```

The one-time step it doesn't do for you: installing a (test) org config
bundle and authenticating connectors headlessly, before your first run:

```bash
mkdir -p org && cp org_config.json org/

privacyfence-app --gmail-oauth
privacyfence-app --drive-oauth
# ...etc for whichever connectors you're testing
```

For structured testing, follow [qa-environment-setup.md](qa-environment-setup.md)
once to create the `PFQA`-prefixed sandbox fixtures (Jira project, Drive
folder, Slack channels, etc.), then re-run
[connector-qa-testing.md](connector-qa-testing.md)'s Claude prompt any time
you want to smoke-test a change end to end.

---

## Account 2: real work (live)

Only ever install from a [GitHub Release](../../releases). No Python, no
git clone, no venv here — treat it as an end user would.

```bash
# after downloading PrivacyFence-<version>.dmg and dragging
# PrivacyFenceApp.app to /Applications:
xattr -cr /Applications/PrivacyFenceApp.app
cp com.privacyfence.app.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.privacyfence.app.plist
```

Then from the menu bar: install the real org config bundle,
**Authenticate…** each real connector, and double-click `PrivacyFence.mcpb`
from the mounted DMG to install the extension into Claude Desktop.

---

## Notes

- Switch accounts with Fast User Switching to test both side by side without
  logging out.
- If you ever build and try a DMG on the dev account for a sanity check, know
  it also writes to `~/.privacyfence` — fine as a one-off, but don't leave
  that daemon running alongside the source-mode daemon (the socket path
  collides).
- Rebuild the DMG (`bash scripts/build_dmg.sh`) to hand-test a release
  candidate on the live account before it's actually published to GitHub.
