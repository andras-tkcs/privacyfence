# Org-mode download delivery

Org mode cannot write files directly to an interactive user's local filesystem. Connector tools therefore return downloaded content either inline in the MCP result or through a short-lived staged download link served by the org-mode daemon.

## Delivery selection

`src/privacyfence/org_mode.py` owns the delivery policy.

The relevant defaults are:

- `inline_max_bytes = 8_000_000`
- `link_ttl_seconds = 300`
- disk staging enabled

If a payload is at or below the configured inline limit, PrivacyFence can return it inline. Larger payloads are staged and returned as a temporary authenticated download URL. Setting `inline_max_bytes` to `0` forces the staging path.

Configuration validation requires a non-negative inline limit and a positive link TTL.

## Staged downloads

Staged content is managed by `src/privacyfence/download_staging.py` and delivered through the web download route.

The staging path:

- encrypts staged content at rest;
- assigns an opaque, short-lived token;
- serves the content from `GET /downloads/{token}`;
- expires staged content according to the configured TTL;
- keeps download authorization separate from the connector's original provider credential.

## Local mode

Local mode can write a requested download to a user-selected/local destination because the daemon runs on the user's machine. The org-mode delivery rules above are specific to centralized deployments where the daemon and user filesystem are different machines.

## Preview and PII limits are separate

The delivery threshold is not the same as the pre-approval preview/PII scan limit. Connector tools may fetch or inspect only a bounded prefix for preview and privacy scanning while still delivering the complete approved file through the inline or staged path.

See [`file-type-support.md`](file-type-support.md) for preview/extraction behavior and [`security-and-compliance.md`](security-and-compliance.md) for the data-handling boundary.
