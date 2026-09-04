# Org Mode Setup Guide (Ubuntu Server, Caddy, Google Identity)

A from-scratch walkthrough for standing up PrivacyFence in **`org` mode** — one shared daemon on a
server, reachable over HTTPS, with people signing in as themselves (not the single implicit `local`
principal a desktop install uses) — on a fresh Ubuntu server, running as its own dedicated system
user, behind [Caddy](https://caddyserver.com/) for TLS, with **Google** as both the sign-in identity
provider and (optionally) the Gmail/Drive/Calendar/Contacts/Tasks connector.

This documents `org` mode as implemented through P8 ("per-user service authorization") of
[`https-connector-refactor-plan.md`](https-connector-refactor-plan.md), now on `main`. See §4
(operating modes), §9 (identity, authentication, multi-user state) and §10 (security analysis) of
that document for the design this guide is a concrete instance of.

> **This cannot be run end to end yet.** As of P8, `daemon_main.py`'s `run_app()` still
> unconditionally ends with `from .menu_bar import run_menu_bar`, and `menu_bar.py` does a bare
> `import rumps` (macOS AppKit) at module scope with no platform guard — so `privacyfence-app`
> crashes right after startup on Linux, in *any* mode, `org` included. This is documented today in
> [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md)'s "Linux" installation section and in the
> repo-root `privacyfence.service` unit's own header comment; the plan document's D4 ("same
> codebase, separate build target") tracks the real headless entrypoint that fixes it, scheduled for
> P7-P9, with the umbrella tracking issue linked from that unit file (#121).
>
> Everything below except **Step 7 (run PrivacyFence as a service)** works today and is safe to do
> ahead of time — DNS, the dedicated user, the Google OAuth clients, the org config bundle, and
> Caddy are all independent of that blocker. Step 7 is written for the moment the headless
> entrypoint ships; until then, starting the service will crash with `ModuleNotFoundError: No
> module named 'rumps'` (see Troubleshooting). Treat this guide as the checklist to already have
> done once that lands.

---

## Contents

- [What you're building](#what-youre-building)
- [Prerequisites](#prerequisites)
- [1. Point DNS at the server](#1-point-dns-at-the-server)
- [2. Create the dedicated `privacyfence` user](#2-create-the-dedicated-privacyfence-user)
- [3. Install PrivacyFence](#3-install-privacyfence)
- [4. Register PrivacyFence with Google](#4-register-privacyfence-with-google)
- [5. Build the organization config bundle](#5-build-the-organization-config-bundle)
- [6. Put Caddy in front of it](#6-put-caddy-in-front-of-it)
- [7. Run PrivacyFence as a service](#7-run-privacyfence-as-a-service)
- [8. First sign-in and connecting a service](#8-first-sign-in-and-connecting-a-service)
- [9. Connecting Claude](#9-connecting-claude)
- [10. Day-to-day admin](#10-day-to-day-admin)
- [Troubleshooting](#troubleshooting)

---

## What you're building

```
 Claude / browser                Caddy                    PrivacyFence
 (users, anywhere)               (this host)               (this host)

     https://pf.example.com  ──▶  :443, auto TLS   ──▶   127.0.0.1:8765
                                   reverse_proxy           (org mode, runs as
                                   adds X-Forwarded-*      the `privacyfence`
                                                            system user)
                                                                 │
                                                                 │ OIDC sign-in +
                                                                 │ OAuth (Gmail/Drive/…)
                                                                 ▼
                                                        accounts.google.com
```

- **Caddy** terminates TLS (it gets a Let's Encrypt certificate for you automatically) and reverse
  proxies plaintext HTTP to PrivacyFence, which binds only to `127.0.0.1` — nothing but Caddy can
  reach it directly.
- **PrivacyFence** runs `mode: org` (`org_config.json`). It is its own minimal OAuth 2.1
  authorization server for MCP clients (§9.4, decision B) and delegates *human* authentication to
  Google over OIDC — the same sign-in either a browser hitting `/login` or an MCP client's OAuth
  dance behind the scenes goes through, which is what makes "the browser session and the MCP token
  are the same identity" true (§9.4).
- **Google** plays two, separate roles here, each its own OAuth client (§9.3, §9.4) — don't conflate
  them:
  1. **Identity provider** — "Sign in with Google" for humans hitting `/login` or an MCP client's
     `/authorize` redirect. Required.
  2. **Connector** — Gmail/Drive/Calendar/Contacts/Tasks tool access, same as local-mode installs
     already have via [`google-cloud-setup.md`](google-cloud-setup.md). Optional, and *not* the
     same OAuth client as #1 — see [§4.2](#42-the-google-connector-client-optional).

---

## Prerequisites

- An Ubuntu Server install (22.04 or 24.04 LTS) you can `sudo` on, with ports 80 and 443 reachable
  from wherever your users are (Let's Encrypt's HTTP-01 challenge needs 80 reachable from the
  internet unless you switch Caddy to a DNS challenge — out of scope here).
- A domain or subdomain you control (e.g. `pf.example.com`) and access to its DNS.
- Python 3.11+ on the server (Ubuntu 22.04 ships 3.10 — see [Step 3](#3-install-privacyfence) for
  the `deadsnakes` PPA if you're on 22.04).
- A Google Cloud project you can create OAuth clients in. A Google Workspace organization lets you
  restrict sign-in to your own domain (**Internal** consent-screen user type); a plain Google
  account works too, but then access control has to happen at the OAuth consent screen (test-user
  allowlist, or submitting for verification) since PrivacyFence itself has no separate user
  allowlist yet — see [§4.1](#41-the-oidc-sign-in-client-required).
- Nothing PrivacyFence-specific installed anywhere yet.

---

## 1. Point DNS at the server

Create an `A` (and `AAAA`, if the server has IPv6) record for the hostname you'll run PrivacyFence
under, e.g.:

```
pf.example.com.   A      203.0.113.10
```

Everything below assumes `pf.example.com` — substitute your own hostname throughout. Wait for the
record to resolve (`dig +short pf.example.com`) before continuing to Caddy in Step 6, since Let's
Encrypt's challenge needs it live.

---

## 2. Create the dedicated `privacyfence` user

A system account with no login shell, so nothing can `ssh`/`su` into it directly — administration
happens via `sudo -u privacyfence`, which doesn't consult the shell field:

```bash
sudo adduser --system --group --home /home/privacyfence --shell /usr/sbin/nologin privacyfence
```

Run anything as that user for the rest of this guide with:

```bash
sudo -u privacyfence -H bash -c '<command>'
```

(`-H` sets `$HOME` to `/home/privacyfence`, which is what makes `~/.privacyfence` — this daemon's
config/credentials/logs root, see `paths.py`'s `data_dir()` — resolve where you expect.)

---

## 3. Install PrivacyFence

Ubuntu 22.04 ships Python 3.10; PrivacyFence needs 3.11+. If `python3 --version` already reports
3.11 or newer (24.04 does), skip straight to installing `pipx`.

```bash
# Only on 22.04 / anywhere python3 is older than 3.11:
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv

sudo apt install -y pipx
sudo -u privacyfence -H bash -c 'pipx ensurepath'
```

Then install PrivacyFence itself as the `privacyfence` user, into its own isolated environment
(`pipx` puts the console script at `~/.local/bin/privacyfence-app`, which is what the systemd unit
in [Step 7](#7-run-privacyfence-as-a-service) points at):

```bash
# Once org mode has shipped in a tagged release (see CLAUDE.md's "Releasing" section) --
# check `git tag` upstream against docs/https-connector-refactor-plan.md's own status line
# ("P8 ... have landed") to see whether the latest tag is past that point yet:
sudo -u privacyfence -H bash -c 'pipx install privacyfence --python python3.11'

# Until then -- org mode is on main but no tag past it has been cut yet, so install
# straight from main instead of PyPI:
sudo -u privacyfence -H bash -c \
  'pipx install "git+https://github.com/andras-tkcs/privacyfence.git@main" --python python3.11'
```

Switch to the plain `pipx install privacyfence` form (and re-run it to upgrade) once a version has
been tagged past org mode landing — nothing else in this guide changes.

Verify:

```bash
sudo -u privacyfence -H bash -c '~/.local/bin/privacyfence-app --help'
```

(This still works even before the headless-entrypoint fix — `--help` returns before `run_app()` ever
reaches the menu-bar import.)

---

## 4. Register PrivacyFence with Google

Do this in the [Google Cloud Console](https://console.cloud.google.com/), from your own workstation
— none of it touches the server. Use a dedicated project (e.g. `privacyfence-org`) or the same one
your organization already uses for [`google-cloud-setup.md`](google-cloud-setup.md)'s connector
setup; either is fine, they're independent OAuth clients either way.

### 4.1 The OIDC sign-in client (required)

This is what lets people sign in to PrivacyFence with their Google account — org mode's `idp`
section (§9.4).

1. **OAuth consent screen** (APIs & Services → OAuth consent screen):
   - **User type**: **Internal** if this is a Google Workspace organization (restricts sign-in to
     your own domain — the strongest access control available here, since PrivacyFence has no
     separate email allowlist of its own yet). Otherwise **External**, and see the note below.
   - App name: `PrivacyFence`. No scopes need adding here — `openid email profile` (what
     `org_identity.py` requests) are Google's default, non-sensitive scopes and need no
     verification, unlike the connector scopes in §4.2.
   - If you chose **External** and are still in **Testing** mode, add every user's Google account
     email under **Test users** — otherwise their sign-in attempt is rejected before it reaches
     PrivacyFence at all. Submit for verification instead if you expect this list to grow past a
     handful of people.
2. **Credentials** → **Create Credentials** → **OAuth client ID**:
   - **Application type**: **Web application**.
   - **Name**: `PrivacyFence sign-in` (anything — cosmetic).
   - **Authorized redirect URIs** — add exactly these two, substituting your own hostname:
     ```
     https://pf.example.com/oauth/idp/callback
     https://pf.example.com/oauth/idp/login-callback
     ```
     (`/oauth/idp/callback` is the authorization server's own callback from the IdP, on behalf of a
     pending MCP client login; `/oauth/idp/login-callback` is a browser's own `/login` — both go
     through the same `org_identity.py` code path, see `web/routes_org_identity.py`'s module
     docstring.)
   - Click **Create**, then copy the **Client ID** and **Client secret** shown — you'll pass these
     as `--idp-client-id`/`--idp-client-secret` in [Step 5](#5-build-the-organization-config-bundle).

Google's own OIDC issuer is fixed and well-known: `https://accounts.google.com` (its discovery
document lives at `https://accounts.google.com/.well-known/openid-configuration`, which is what
`org_identity.py`'s `discover_idp()` fetches at startup). You'll pass that as `--idp-issuer` — it is
**not** a URL you create, just Google's standing endpoint.

> **On `admin_group_claim`:** plain Google OIDC ID tokens carry no `groups` claim (that needs Google
> Workspace's separate Cloud Identity group-claim configuration, out of scope here), and nothing in
> PrivacyFence gates a feature on `is_admin` yet as of P8 — it's carried through
> (`Principal.is_admin`) for future use. Leave `--idp-admin-group-claim` unset for a Google IdP;
> everyone who can sign in is a plain, equally-privileged user.

### 4.2 The Google connector client (optional)

Only needed if you also want people to use the Gmail/Drive/Calendar/Contacts/Tasks connectors
through this deployment — separate from, and unrelated to, whether they can sign in at all.

Follow [`google-cloud-setup.md`](google-cloud-setup.md)'s "For IT admins" section for enabling the
required APIs and configuring the consent screen for these (sensitive) scopes — that part is
identical to a local-mode install. The one thing that's **different** for org mode, in that same
doc's step 4 ("Create OAuth 2.0 credentials"):

- **Application type: Web application, not Desktop app.** Local mode's loopback flow needs a
  Desktop-app client (any loopback port); org mode's server-redirect flow (§9.3) needs an explicit,
  registered HTTPS redirect URI, which only a Web application client can carry.
- **Authorized redirect URIs** — one per Google connector org mode currently wires (Apps Script
  isn't among them as of P8 — it stays local-mode-only):
  ```
  https://pf.example.com/oauth/callback/gmail
  https://pf.example.com/oauth/callback/drive
  https://pf.example.com/oauth/callback/calendar
  https://pf.example.com/oauth/callback/contacts
  https://pf.example.com/oauth/callback/tasks
  ```
- Download the client credentials as JSON (**Download JSON** on the credentials page) rather than
  copying id/secret by hand — `build_org_bundle.py`'s `--google-client-secret` flag (next step)
  reads that file directly, same as a local-mode bundle.

Other connectors' org-mode setup follows the same pattern (§9.3: "for [Slack, Salesforce and
Atlassian] this is a listener swap") — add `https://pf.example.com/oauth/callback/<service>` as one
more redirect URI on the app registration you'd otherwise create per
[`slack-setup.md`](slack-setup.md), [`salesforce-setup.md`](salesforce-setup.md) or
[`atlassian-setup.md`](atlassian-setup.md) (`<service>` is `slack`, `salesforce`, `jira` or
`confluence`). Not covered further here since the assumptions for this guide only called out Google.

---

## 5. Build the organization config bundle

Run this from a clone of the repository **on your own workstation** — `build_org_bundle.py` is
stdlib-only and needs no PrivacyFence install, per its own docstring, and doing it off-server keeps
the client secrets you're about to paste in off the machine until the finished, encrypted-in-transit
bundle is copied over.

```bash
git clone https://github.com/andras-tkcs/privacyfence
cd privacyfence   # main already carries org mode's --mode/--server-*/--idp-* flags

python3 scripts/build_org_bundle.py \
  --mode org \
  --server-issuer-url https://pf.example.com \
  --server-bind-host 127.0.0.1 \
  --server-port 8765 \
  --server-trusted-proxy 127.0.0.1 \
  --idp-issuer https://accounts.google.com \
  --idp-client-id <YOUR_IDP_CLIENT_ID>.apps.googleusercontent.com \
  --idp-client-secret <YOUR_IDP_CLIENT_SECRET> \
  --google-client-secret ~/Downloads/client_secret_<...>.json \
  -o org_config.json
```

Notes on the flags used:

- `--server-bind-host 127.0.0.1` — PrivacyFence listens only on loopback; Caddy (Step 6), running on
  the same host, is the only thing that can reach it. This is why there's no `--server-tls-cert`/
  `--server-tls-key` here — Caddy terminates TLS, PrivacyFence never sees a private key.
- `--server-trusted-proxy 127.0.0.1` — the one thing that makes it safe to trust
  `X-Forwarded-For`/`X-Forwarded-Proto` at all (§10.2: "honored only when an explicit
  `trusted_proxies` list is configured, never by default") — this must be the reverse proxy's own
  address, which is `127.0.0.1` here since Caddy and PrivacyFence share a host. Omit `--google-*` if
  you skipped §4.2.
- If you also built connector bundles for Slack/Salesforce/Atlassian, pass their flags too (see
  each's own setup doc) — `--merge` lets you add them incrementally without re-typing everything.

Copy the result to the server and lock it down (the script already `chmod 600`s it, but ownership
still needs fixing after the copy):

```bash
scp org_config.json youradminuser@pf.example.com:/tmp/org_config.json
ssh youradminuser@pf.example.com 'sudo -u privacyfence -H bash -c "
  mkdir -p ~/.privacyfence/org
  mv /tmp/org_config.json ~/.privacyfence/org/org_config.json
  chmod 600 ~/.privacyfence/org/org_config.json
"'
```

(`~/.privacyfence/org/org_config.json` is exactly where `daemon_main.py`'s `load_org_config()`
reads from — `org_dir() / "org_config.json"`, and `org_dir()` is `data_dir() / "org"`.)

---

## 6. Put Caddy in front of it

Install Caddy from its own official repository (Ubuntu's default repos carry an outdated build):

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

Replace `/etc/caddy/Caddyfile` with:

```caddyfile
pf.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

That's the entire config — Caddy's `reverse_proxy` sets `X-Forwarded-For`, `X-Forwarded-Proto` and
`X-Forwarded-Host` on every proxied request by default (which is what `--server-trusted-proxy
127.0.0.1` in Step 5 is there to trust), and Caddy obtains and renews a Let's Encrypt certificate for
`pf.example.com` automatically the first time it starts, using the DNS record from
[Step 1](#1-point-dns-at-the-server) to pass the HTTP-01 challenge.

```bash
sudo systemctl reload caddy
```

Open the firewall for HTTP/HTTPS (and make sure SSH stays open before you enable it):

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Port 8765 needs no firewall rule of its own — PrivacyFence only binds `127.0.0.1`, so it was never
reachable from outside the host in the first place; Caddy is the only path in.

---

## 7. Run PrivacyFence as a service

> **Blocked today** — see the callout at the top of this guide. The unit below is what you'll enable
> once the headless-entrypoint fix lands; until then, starting it will crash-loop on the `rumps`
> import (`Restart=on-failure` will keep retrying it every 5 seconds, uselessly — leave the unit
> disabled until then rather than enabling it to crash-loop in the background).

The repo ships `privacyfence.service`, a systemd **`--user`** unit mirroring the macOS LaunchAgent.
For a dedicated, non-interactive service account like this one, a plain **system** unit running as
that user is a better fit — it starts at boot with no `loginctl enable-linger` dance, and behaves
identically otherwise. Create `/etc/systemd/system/privacyfence.service`:

```ini
[Unit]
Description=PrivacyFence daemon (org mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=privacyfence
Group=privacyfence
WorkingDirectory=/home/privacyfence
Environment=HOME=/home/privacyfence
ExecStart=/home/privacyfence/.local/bin/privacyfence-app

# Mirrors privacyfence.service's own KeepAlive/SuccessfulExit=false: restart
# on a crash, not on a clean `systemctl stop`.
Restart=on-failure
RestartSec=5

# Mild sandboxing -- drop if it gets in the way while you're debugging.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/privacyfence

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now privacyfence
sudo systemctl status privacyfence
journalctl -u privacyfence -f
```

You should see a log line ending `Org mode active -- MCP-over-HTTP at https://pf.example.com/mcp
(OAuth 2.1, DCR at https://pf.example.com/register), IdP https://accounts.google.com` (from
`daemon_main.py`'s `_start_org_web_server`) once it's actually up.

`config/settings.yaml` needs no edits for this — the packaged default already ships
`web.mcp.enabled: true`, which is the one thing org mode's boot path (`_maybe_start_web_server`)
requires before it will start a server at all. It's created automatically from PrivacyFence's own
packaged template the first time the daemon runs successfully; if you want it in place before that
first successful run, seed it by hand:

```bash
sudo -u privacyfence -H bash -c '
  mkdir -p ~/.privacyfence/config
  ~/.local/pipx/venvs/privacyfence/bin/python3 - <<PY
import pathlib, shutil
import privacyfence
example = pathlib.Path(privacyfence.__file__).parent / "resources" / "settings.yaml.example"
shutil.copyfile(example, pathlib.Path.home() / ".privacyfence" / "config" / "settings.yaml")
PY
'
```

---

## 8. First sign-in and connecting a service

Once the daemon is actually running (Step 7):

1. Visit `https://pf.example.com/login` in a browser. You're redirected to Google; sign in and
   consent.
2. You land on `/connect` — the per-principal connections page (`web/routes_connect.py`). This is
   the org-mode equivalent of a local install's menu-bar **Connectors** page.
3. Click **Connect** next to Gmail/Drive/whichever connectors you registered in
   [§4.2](#42-the-google-connector-client-optional). Each one redirects to Google, asks for consent
   to that connector's specific scopes, and lands you back on `/connect` showing it connected.

Every principal's credentials land under their own `~/.privacyfence/users/<principal-id>/
credentials/` on the server (§9.2's per-principal storage layout) — nothing is shared between users,
and nothing another user's browser session can read.

---

## 9. Connecting Claude

Point any MCP client with native Streamable HTTP + OAuth 2.1 support at:

```
https://pf.example.com/mcp
```

Claude Code:

```bash
claude mcp add --transport http privacyfence https://pf.example.com/mcp
```

The first tool call triggers Claude's own OAuth flow: it dynamically registers itself
(`https://pf.example.com/register`, RFC 7591 DCR), opens a browser to
`https://pf.example.com/authorize`, which — since you're not already signed in — bounces through
`/login` → Google → back, and finally back to Claude with a token scoped to *that signed-in
principal*. No token file to copy, no shared secret — the same posture local mode's `/mcp`
already has, extended to real per-human identity (§10.3: MCP tokens and browser session cookies are
strictly separate audiences, checked in separate middleware, so neither can be replayed as the
other).

---

## 10. Day-to-day admin

- **Adding a user**: nothing to do on the PrivacyFence side. Anyone who can complete the Google
  sign-in (i.e., anyone your consent screen's Internal/test-user/verification posture from
  [§4.1](#41-the-oidc-sign-in-client-required) allows through) gets a `Principal` the first time
  they sign in — access control lives entirely at that layer today, not in a PrivacyFence-side
  allowlist.
- **Removing a user**: revoke their access at the IdP (remove them from the Workspace domain, the
  test-user list, or the relevant Google group) — they simply can't sign in again. Their
  `~/.privacyfence/users/<id>/` directory on the server is untouched by this; delete it by hand if
  you want their credentials and settings gone too.
- **Rotating a connector secret** (e.g. the Google connector client secret): rebuild the bundle with
  `--merge` so you don't have to re-specify the IdP section, and redeploy it exactly as in
  [Step 5](#5-build-the-organization-config-bundle) — existing users' own per-connector tokens are
  unaffected; only the org-wide client credentials change.
- **Logs**: `journalctl -u privacyfence -f` for the daemon; Caddy's own access/error logs are in
  `journalctl -u caddy -f` unless you've configured a separate log file in the Caddyfile.
- **Updating PrivacyFence**: re-run the `pipx install ... --force` form of whichever install command
  you used in [Step 3](#3-install-privacyfence), then `sudo systemctl restart privacyfence`.

---

## Troubleshooting

**`privacyfence-app` crashes right after "Startup complete, starting menu bar" with
`ModuleNotFoundError: No module named 'rumps'`**
This is the documented, current Linux blocker (see the callout at the top of this guide) — not
something a config change fixes. Track `docs/https-connector-refactor-plan.md`'s D4 and issue #121
for the headless entrypoint that resolves it.

**`sudo -iu privacyfence` says "This account is currently not available."**
Expected — the account's shell is `/usr/sbin/nologin` on purpose (Step 2). Use
`sudo -u privacyfence -H bash -c '<command>'` instead, which runs the given command directly and
never consults the shell field.

**Caddy shows "502 Bad Gateway"**
PrivacyFence isn't listening on `127.0.0.1:8765` — check `systemctl status privacyfence` and
`journalctl -u privacyfence` for why it isn't up (once Step 7 is actually runnable), and confirm
`org_config.json`'s `server.bind_host`/`server.port` match what the Caddyfile proxies to.

**Google shows "Error 400: redirect_uri_mismatch"**
The URI PrivacyFence built doesn't exactly match one registered on the OAuth client — check for
`http` vs `https`, a trailing slash, or a typo in the hostname. `/oauth/idp/callback` and
`/oauth/idp/login-callback` belong on the client from [§4.1](#41-the-oidc-sign-in-client-required);
`/oauth/callback/gmail` (etc.) belong on the separate client from
[§4.2](#42-the-google-connector-client-optional) — a redirect URI added to the wrong client also
shows as this error.

**Startup fails with `ValueError: org mode (org_config.json "mode": "org") requires an "idp"
section (issuer, client_id, client_secret)`**
`org_config.json` is missing or malformed — re-check the file landed at
`~/.privacyfence/org/org_config.json` (not `~/.privacyfence/org_config.json`) and that
`build_org_bundle.py` was actually given `--mode org` plus all three `--idp-*` flags — omitting any
one of them makes the script skip the `idp` section entirely (see [Step 5](#5-build-the-organization-config-bundle)).

**"Invalid Host header" (plain-text 400) instead of the sign-in page**
The Host header Caddy forwards doesn't match `server.issuer_url`'s hostname in `org_config.json` —
the allowlist that guards against this (`_HostAllowlistMiddleware`, §10.5) is built from
`issuer_url`, so a mismatch (wrong hostname, or a port suffix that shouldn't be there) rejects every
request. Re-check `--server-issuer-url` in Step 5 against the `pf.example.com` block in your
Caddyfile.

**"Access blocked: PrivacyFence has not completed the Google verification process" at sign-in**
Same cause as the identical message in [`google-cloud-setup.md`](google-cloud-setup.md)'s own
Troubleshooting section, but for the *sign-in* client this time: it's in Testing mode and the
account isn't on the test-user list (§4.1) — add it, or submit for verification once you expect more
than a handful of users.
