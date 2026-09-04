# WebAuthn link-open check (§10.6)

Step-by-step procedure for the check
[`https-connector-refactor-plan.md`](https-connector-refactor-plan.md) §10.6 names as a **blocking
entry condition for P9** (step-up auth, D7 — see that doc's §12 phase table):

> Does a platform-authenticator biometric prompt (Face ID / Touch ID / Android fingerprint / Windows
> Hello) actually appear when the approval link is opened from *inside a real Claude conversation*,
> on Desktop, iOS, and Android?

§10.6 settles the general platform facts (Chrome Custom Tabs and `SFSafariViewController`/
`ASWebAuthenticationSession` support platform WebAuthn fully; a bare embedded Android `WebView` does
not) but leaves one thing open: which of those components each of Claude's own apps actually uses
for an in-chat link is app-specific, undocumented, and can change between app versions. This doc
closes that gap with a real test tool — `scripts/webauthn_link_open_check.py` — and this checklist.

**Do this before P9 is scheduled, not during it.** Re-run it whenever Claude's Desktop/iOS/Android
apps take a major update, or a new client surface is added, since the thing being tested is
Claude-app behavior, not anything in this repo.

**Time**: roughly ten minutes to run once Part A is done. Part A is a one-time setup, maybe 15
minutes on a machine that has never done this before.

---

## What the tool actually checks, and what you still have to watch for yourself

The page at `scripts/webauthn_link_open_check.py` is a minimal, throwaway WebAuthn relying party —
the same idea as pointing a browser at `webauthn.io`, except self-hosted so the exact
`navigator.credentials` call shape matches what D7 decided on (`authenticatorAttachment: "platform"`,
`userVerification: "required"`), not a generic demo that would also accept a security key.

Once you run a registration or a sign-in ceremony on it, the page shows you three facts it can
verify cryptographically, server-side:

- **`user_verified`** — was the UV flag actually set in `authenticatorData`, not just presence
  (§10.6: "verify user verification, not just the signature")
- **`attachment`** — did the browser report `platform` (Face ID/Touch ID/fingerprint/Hello) or
  `cross-platform` (a security key)?
- **`backed_up`** — is this credential synced (iCloud Keychain / Google Password Manager), which
  weakens "this credential lives only on this device" (§10.6's synced-passkey callout)?

**What it cannot see, and asks you directly**: whether the OS actually showed *you* a biometric
sheet on that screen just now. That's the one judgment call only a human with the real device can
make, and it's the actual point of this whole exercise — everything else is confirming your eyes
weren't fooled by a UI that quietly no-ops.

---

## Part A — One-time setup on a fresh Ubuntu VM

Assumes a fresh Ubuntu 22.04/24.04 LTS install (server or desktop, doesn't matter — you only need a
shell and outbound internet access). Minimal specs are plenty: 1 vCPU / 1 GB RAM. **No inbound port
needs to be opened and no public IP is required** — the tunnel in Part A.3 makes an outbound-only
connection, which is exactly why this is the fast path even on a VM behind NAT/a security group with
nothing open.

### A.1 System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version   # need 3.9+ for the webauthn package; 3.11+ matches this repo's own baseline
```

Ubuntu 22.04 ships Python 3.10, 24.04 ships 3.12 — either is fine for this standalone script (it
doesn't import anything from `privacyfence` itself, so it isn't bound to the project's `>=3.11`
baseline the way the rest of the repo is).

### A.2 Get the script and install its one dependency

You only need the single file `scripts/webauthn_link_open_check.py` — either clone the whole repo
(simplest) or copy just that file to the VM.

```bash
git clone --depth 1 https://github.com/andras-tkcs/privacyfence.git
cd privacyfence

python3 -m venv .venv-webauthn-check
source .venv-webauthn-check/bin/activate
pip install --upgrade pip
pip install "webauthn>=2.0,<3.0"
```

(If you only copied the one file instead of cloning, do the same three `venv`/`pip` steps in
whatever directory you put it, and adjust the path in the commands below.)

### A.3 Install `cloudflared` (the tunnel)

WebAuthn requires a *secure context with a registrable-domain RP ID* (§10.6: "the RP-ID rule
constrains D1") — `localhost` qualifies, a bare IP address does not, and a real mobile browser
tapping a link from a chat app obviously isn't going to reach your VM's `localhost`. A Cloudflare
Quick Tunnel is the fastest way to get a real `https://` hostname pointed at a process on this VM,
with **no account, no signup, and no DNS to configure** — perfect for a one-off ten-minute check.

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared
cloudflared --version
```

Setup is done. Everything from here on is Part B, repeatable any time you need to re-run the check.

---

## Part B — Running the check (~10 minutes)

### B.1 Start the test server (terminal 1)

```bash
cd privacyfence
source .venv-webauthn-check/bin/activate
python3 scripts/webauthn_link_open_check.py
```

Leave this running. It prints `Serving the WebAuthn link-open check on http://127.0.0.1:8000` and
logs each request. State is in-memory only — nothing to clean up later beyond `Ctrl+C`.

### B.2 Start the tunnel (terminal 2, same VM)

```bash
cloudflared tunnel --url http://localhost:8000
```

Wait for a line like:

```
+--------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://random-two-words-more-words.trycloudflare.com                                |
+--------------------------------------------------------------------------------------+
```

Copy that `https://…trycloudflare.com` URL — that's the link you'll paste into Claude conversations
below. It's ephemeral: it dies the moment you `Ctrl+C` this terminal, and a fresh run gets a new one.

### B.3 Sanity-check it yourself first (optional but recommended)

Open the tunnel URL in your own desktop browser once, before testing any real device. Fill in a
label, click **1 — Register a passkey here**, approve with whatever authenticator your desktop
offers, then **2 — Verify with that passkey**. You should see a green "Server-verified facts" banner
with `user_verified: true`. If this baseline doesn't work, nothing downstream will either — fix it
here first (usually a firewall/browser issue, not a §10.6 one).

Click **Reset all test data on this server** afterwards so the log you're about to build only
contains the real per-platform results.

### B.4 Test each real Claude surface

Repeat this block once for **Claude Desktop**, once for **Claude iOS**, once for **Claude Android** —
and, as a contrast case worth having, once more for the *same link* opened in that platform's
**ordinary system browser** (not through Claude at all), so you have a known-good baseline to compare
Claude's in-chat behavior against.

1. Open (or reuse) a real conversation in the app being tested.
2. Paste the tunnel URL as plain text into the chat and send it.
3. **Tap/click the link exactly as Claude renders it** — this is the whole point: which component
   *Claude's own UI* hands the link to, not a copy-paste into a separate browser tab.
4. On the page that opens:
   - Fill in the label field, e.g. `iPhone · Claude iOS · in-chat link`.
   - Click **1 — Register a passkey here**. Watch the screen for a Face ID / Touch ID / fingerprint
     / Windows Hello sheet.
   - When the "Server-verified facts" banner appears, answer the **Yes/No/Unsure** prompt about
     whether you actually saw that OS prompt.
   - Click **2 — Verify with that passkey** and repeat the observation/answer.
5. Note the banner's three server-verified facts (`user_verified`, `attachment`, `backed_up`) for
   this platform — the running **Session log** table on the page keeps all of them visible at once
   as you go through each surface, so you don't need to write anything down separately.

You do **not** need to restart the server or tunnel between platforms — register a differently
labeled credential for each one and keep going.

### B.5 Read the result

**Pass**, for a given platform: green banner, `user_verified: true`, `attachment: platform`, *and*
you personally answered "yes" to seeing the OS prompt. All four have to hold — a green banner with
"no" on the human question means the ceremony itself was silently satisfied by something that didn't
actually put a biometric check in front of the person holding the phone, which is worse than an
outright failure.

**Fail**, and exactly the failure mode §10.6 exists to catch, looks like one of:

- A big red "WebAuthn is not available in this browser at all" banner immediately on page load — the
  in-chat link opened in a bare embedded `WebView` with no platform-authenticator UI at all.
- The ceremony completes but `user_verified: false`, or you answered "no" to the human question —
  something satisfied the request without a real biometric check.
- `attachment: cross-platform` when you expected a platform prompt.

If any platform fails: **that surface is not ready for P9.** Don't schedule step-up auth against it
until Claude's own app is confirmed (in a later app release, or by routing that specific link through
the system browser instead) to actually open the link somewhere platform WebAuthn works.

### B.6 Record it

Click **Copy report** (or `curl https://<tunnel-host>/api/report`) to get a small Markdown table like:

```markdown
## WebAuthn link-open check (§10.6)

| Time (UTC) | Device / app | Action | User-verified | Attachment | Backed up | Saw OS prompt? |
|---|---|---|---|---|---|---|
| 14:02:11 UTC | iPhone · Claude iOS · in-chat link | register | True | platform | False | yes |
| 14:02:41 UTC | iPhone · Claude iOS · in-chat link | login | True | platform | False | yes |
| 14:05:03 UTC | Pixel · Claude Android · in-chat link | register | True | platform | False | yes |
| ... | | | | | | |
```

Paste that — plus a one-line note on which platforms passed/failed — into whatever is tracking P9's
entry conditions (the PR or issue that schedules it, or directly into
`https-connector-refactor-plan.md` §12's "What P0 found" section if you're updating the plan itself).

### B.6a If "2 — Verify with that passkey" is grayed out after a successful registration

Seen in practice inside Claude's iOS in-chat browser: registration completes (green banner, prompt
seen), but the **Verify** button stays disabled. This is not WebAuthn refusing anything — it means
that specific in-app browser reloaded or rebuilt the tab around the native Face ID hand-off, which
wipes the page's in-memory JS state even though the credential was already saved server-side. The
tool re-derives the button's enabled state from the server on every load, so this should self-heal
within a second; if it doesn't, pull down to refresh (or re-tap the same link) and it will pick the
credential back up without making you register again. Worth noting as its own observation about that
surface either way — an app whose embedded browser tears down page state around a native modal is a
rougher experience than one that doesn't, independent of whether WebAuthn itself worked.

### B.6b Third-party passkey providers (1Password, Bitwarden, …) add their own hand-off wrinkle

If a third-party password manager is set as the device's default passkey/AutoFill provider (iOS:
Settings → Passwords → AutoFill Passwords and Passkeys), it — not iCloud Keychain/Google Password
Manager — fields the `navigator.credentials.create()`/`.get()` call. The ceremony still reports
`platform` attachment and `user_verified: true` (the provider does its own Face ID/Touch ID/biometric
check via the OS before releasing the passkey), so this doesn't change the pass/fail verdict by
itself.

What's been observed in practice: registering inside Claude's iOS in-chat browser with 1Password as
the provider, control didn't cleanly return from the 1Password app back into the page afterward —
the tab appeared stuck until reloaded (recoverable, thanks to §B.6a's fix). That's worth recording as
its own note, separate from the platform/UV result: a third-party provider hand-off hanging inside
Claude's embedded browser is a reliability finding about *that browser*, distinct from whether the
underlying WebAuthn ceremony itself works.

To isolate whether a hang like this is provider-specific or a broader "any external-app round trip
confuses this embedded browser" issue, temporarily set AutoFill's default provider back to Apple
Passwords (same Settings path) and repeat register/verify once more for comparison.

### B.7 Clean up

`Ctrl+C` both terminals. The tunnel URL stops resolving immediately; the server's in-memory state
(credentials, log) is gone with the process. Nothing was installed system-wide except `cloudflared`
and a throwaway venv — safe to leave the VM as-is and just re-run B.1–B.2 next time, or discard the
VM entirely.

---

## Security notes

- **Never leave this running on a stable hostname or for longer than the check takes.** It has no
  authentication of its own — anyone who gets the tunnel URL while it's live can register/verify
  passkeys against it. A Quick Tunnel's random, ephemeral hostname and short lifetime are part of the
  design here, not an inconvenience to work around with a persistent one.
- The script derives its WebAuthn RP ID and origin from the incoming request's `Host`/
  `X-Forwarded-Proto` headers rather than a fixed config value (see `_origin_and_rp_id`'s docstring
  in the script) — deliberate for this throwaway, single-operator tool, and explicitly not a pattern
  to reuse for a real relying party.
- Nothing sensitive is collected. No real PrivacyFence account, data, or credential is touched — only
  the fact that a passkey ceremony happened, plus the flags WebAuthn itself reports.

## See also

- [`https-connector-refactor-plan.md`](https-connector-refactor-plan.md) §10.6 — the design
  discussion and decision (D7) this check exists to de-risk.
- [`testing-policy.md`](testing-policy.md) §2 — how this fits alongside the repo's other
  local-only, human-run checks (`qa_popup_smoke.py`, `qa_web_smoke.py`).
