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

```bash
cd /Users/user1/Coding/privacyfence
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp src/privacyfence/resources/settings.yaml.example config/settings.yaml
```

Install a (test) org config bundle and authenticate connectors headlessly:

```bash
mkdir -p ~/.privacyfence/org && cp org_config.json ~/.privacyfence/org/

privacyfence-app --gmail-oauth
privacyfence-app --drive-oauth
# ...etc for whichever connectors you're testing
```

Run the daemon in the foreground while iterating — easier to see logs, and
Ctrl-C/restart after code changes — instead of installing the LaunchAgent:

```bash
privacyfence-app
```

Point Claude Code at the venv's bridge binary (not the `.mcpb`):

```bash
claude mcp add privacyfence /Users/user1/Coding/privacyfence/.venv/bin/privacyfence-bridge
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
