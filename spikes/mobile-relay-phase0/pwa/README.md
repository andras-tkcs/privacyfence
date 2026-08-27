# Bare PWA (Phase 0)

Vanilla HTML/JS, no build step, no framework. Drives the relay's three
endpoints directly with `fetch`.

## Run it locally

```sh
# from this directory
python3 -m http.server 8080
```

Then open `http://localhost:8080/index.html` with a relay already running
(see `../relay/README.md` or the top-level spike README) and paste its URL
into the pairing form. To actually test the phone side from a real phone,
serve this over the same network (or the WireGuard tunnel once that's up)
and use the machine's LAN/tunnel IP instead of `localhost`.

## What's real here vs. what's a stand-in

- Pairing, long-poll wake, and the Approve/Deny round trip are real working
  code, browser-tested against the relay in this repo.
- The PII banner / content preview rendering (`renderPendingRequest` in
  `app.js`) is real, but the payload shape it renders is whatever the
  "daemon" side of a test happens to post -- Phase 1 is what makes a real
  daemon populate it from the actual gate content.
- `sw.js`'s `push` handler is not wired to anything yet (see its own
  comment) -- Phase 0 wakes via long-polling, which issue #55 explicitly
  allows as a fallback mode, not just a placeholder.
- There's no app icon (`manifest.json` has an empty `icons` array) and no
  offline caching -- "Add to Home Screen" on iOS will still work with a
  generic icon, which is enough to prove installability for a spike.
