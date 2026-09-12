-- docs/release-publishing-kpi-plan.md Phase 1. Deliberately narrow: only what the public KPI
-- (installer downloads by source/version/channel/OS/architecture/day) needs, and nothing that
-- could identify a visitor -- no IP, user ID, cookie ID, fingerprint, email, Cloudflare Ray ID,
-- or User-Agent in this table, ever (see the plan's Goal section).
--
-- One row per (day, channel, version, platform, architecture, artifact_kind); a download
-- increments its row's counter in place (see src/counters.ts) rather than inserting one row per
-- download, so the table stays small regardless of traffic volume.
CREATE TABLE download_counts (
    day           TEXT NOT NULL,
    channel       TEXT NOT NULL,
    version       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    architecture  TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, channel, version, platform, architecture, artifact_kind)
);
