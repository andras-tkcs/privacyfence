# Org-mode download delivery

Org mode cannot write files directly to an interactive user's local filesystem. Connector tools therefore return downloaded content either inline in the MCP result or through a short-lived staged download link served by the org-mode daemon.

## Delivery selection

`src/privacyfence/org_mode.py` owns the delivery policy.

The relevant defaults are:

- `inline_max_bytes = 8_000_000`
- `link_ttl_seconds = 300`
- `allow_disk_staging = true`

If a payload is at or below the configured inline limit, PrivacyFence can return it inline. Larger payloads are staged and returned as a temporary authenticated download URL. Setting `inline_max_bytes` to `0` forces the staging path.

Setting `allow_disk_staging` to `false` disables the staging path itself: a payload too large to return inline is refused outright rather than ever being written to disk.

Configuration validation requires a non-negative inline limit and a positive link TTL.

## Staged downloads

Staged content is managed by `src/privacyfence/download_staging.py` and delivered through the web download route.

The staging path:

- encrypts staged content at rest;
- assigns an opaque, short-lived token;
- serves the content from `GET /downloads/{token}`;
- enforces the configured TTL: a token past its expiry is never claimable, whatever else has happened;
- keeps download authorization separate from the connector's original provider credential;
- returns an identical 404 for a missing, expired, or wrong-principal token, so the response never discloses which case applies.

Expiry itself is enforced opportunistically, not by a background reaper: `DownloadStagingStore` sweeps expired entries (removing both the registry entry and the on-disk ciphertext) only at the top of `stage()` and `claim()`, mirroring the same pattern `PendingApprovalRegistry` already uses for approvals. Claim-time authorization is unaffected — the sweep runs before every claim check, so an expired token is always rejected. What can lag is disk cleanup: a staged file nobody ever claims, on a principal whose staging store sees no further activity, keeps its encrypted ciphertext on disk until *something* stages or claims again for that principal (or the daemon restarts).

At-rest encryption protects a staged file against recovery from disk, a backup, or forensic imaging of the storage medium. It does not protect against compromise of the live daemon process itself: content necessarily exists in plaintext in memory for the brief window between decrypting it and streaming it to the requester.

## Local mode

Local mode can write a requested download to a user-selected/local destination because the daemon runs on the user's machine. The org-mode delivery rules above are specific to centralized deployments where the daemon and user filesystem are different machines.

## Preview and PII limits are separate

The delivery threshold is not the same as the pre-approval preview/PII scan limit. Connector tools may fetch or inspect only a bounded prefix for preview and privacy scanning while still delivering the complete approved file through the inline or staged path.

See [`file-type-support.md`](file-type-support.md) for preview/extraction behavior and [`security-and-compliance.md`](security-and-compliance.md) for the data-handling boundary.
