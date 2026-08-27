# WireGuard tunnel skeleton (Phase 0)

Plain pinned-key WireGuard, direct connection -- the choice made in
[issue #55](https://github.com/andras-tkcs/privacyfence/issues/55#issuecomment-5436316171).
No Headscale/Tailscale, no rendezvous-hop variant: the relay has a
port-forwarded UDP port, so peers dial it directly.

This directory holds the config skeleton, not a working deployment --
standing up the actual throwaway box is a manual, per-deployment step (see
below). Nothing here talks to the `relay/` mailbox server yet at the network
layer; in a real deployment the relay server binds to its WireGuard
interface address (`10.55.0.1` in the templates) rather than a public
address, so the tunnel is what makes it reachable at all.

## 1. Generate keys

```sh
python3 generate_keys.py relay mac-daemon phone-1 --out ./keys
```

Produces `<name>.privatekey` / `<name>.publickey` pairs, byte-for-byte
compatible with `wg genkey`/`wg pubkey` output (WireGuard keys are just raw
X25519 keys, base64-encoded -- see the script's docstring). Only ship
`.publickey` files between machines; `.privatekey` files never leave the box
they were generated on.

## 2. Provision the throwaway relay box

Any always-on machine the user/org physically controls -- a spare
Raspberry Pi is the reference case in the issue. Requirements:
- A public IP (or a router that can port-forward to it) with UDP 51820
  forwarded.
- `wireguard-tools` installed (`apt install wireguard-tools` on Debian/Raspberry Pi OS).

```sh
sudo cp relay.conf.template /etc/wireguard/wg0.conf
# edit /etc/wireguard/wg0.conf: paste in relay.privatekey, and one [Peer]
# block per client with its .publickey and a unique AllowedIPs /32.
sudo systemctl enable --now wg-quick@wg0
```

## 3. Configure each client (Mac daemon host, phone)

```sh
sudo cp client.conf.template ./mac-daemon.conf
# edit: paste in mac-daemon.privatekey, the relay's .publickey, and the
# relay's public host:port.
sudo wg-quick up ./mac-daemon.conf
```

For a phone, import the equivalent config into a WireGuard client app (iOS
has an official WireGuard app) -- either by hand-editing the same template
or, once Phase 2's QR-code pairing UX exists, generating it programmatically.
Phase 0 doesn't attempt that automation; it's exercised by hand.

## 4. Verify

```sh
ping 10.55.0.1   # from a client, once its tunnel is up -- reaches the relay
```

Once this succeeds, the `relay/` mailbox server can be bound to `10.55.0.1`
instead of `127.0.0.1`, and `pwa/` served from a client machine on the tunnel
can reach it exactly as it would over loopback in the local dev flow
described in the top-level README.

## What Phase 0 deliberately leaves out

- **Automated provisioning.** Phase 4 ("move the relay onto dedicated
  hardware, its own network segment") is the phase that owns turning this
  into a repeatable deployment. Phase 0 is a spike: hand-edited configs on a
  single throwaway box are the point, not a gap.
- **Key rotation / revocation UX.** Phase 2's job.
- **Network segmentation (VLAN, scoped egress).** Also explicitly a later
  hardening step per the issue ("treat it like a DMZ host").
