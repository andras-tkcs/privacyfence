# Relay skeleton (Phase 0)

Stdlib-only Python HTTP server (`ThreadingHTTPServer`) implementing the
mailbox API described in the module docstring of `relay_server.py`. No new
dependency needed beyond what `privacyfence` already requires.

## Run it

```sh
python3 relay_server.py --host 127.0.0.1 --port 8765
```

For a real deployment, bind `--host` to the relay's WireGuard interface
address (`10.55.0.1` in `../wireguard/relay.conf.template`) instead of
`127.0.0.1`, so it's reachable only over the tunnel -- never bind it to a
public interface directly.

## API

| Method | Path                       | Caller | Purpose                                             |
|--------|----------------------------|--------|------------------------------------------------------|
| POST   | `/pair`                    | either | Mint a mailbox ID + token (stand-in for Phase 2's real pairing UX) |
| POST   | `/mailbox/{id}`            | daemon | Post a new pending request (`request_id`, `payload`, optional `ttl_seconds`) |
| GET    | `/mailbox/{id}`            | phone  | Long-poll for the pending request (`?wait=<seconds>`) |
| POST   | `/mailbox/{id}/decision`   | phone  | Submit `{request_id, decision}` (`"approved"`/`"denied"`) |
| GET    | `/mailbox/{id}/decision`   | daemon | Long-poll for the decision on a `request_id` |

All requests except `/pair` require `?token=<mailbox token>`; a bad or
missing token is a flat 403 that doesn't distinguish "wrong token" from "no
such mailbox."

## Tests

```sh
pip install pytest requests
pytest ../tests/test_roundtrip.py -v
```

See that file's docstring and the module docstring at the top of
`relay_server.py` for the fail-closed and idempotent-decision invariants
these tests exist to pin down.
