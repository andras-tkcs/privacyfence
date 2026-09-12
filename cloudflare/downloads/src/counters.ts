/**
 * D1 download counting. See migrations/0001_download_counts.sql for the table this writes to,
 * and index.ts's `shouldCount()` for the rule deciding which requests get here at all.
 */
import type { Channel } from "./channel.js";

export interface DownloadEvent {
  channel: Channel;
  version: string;
  platform: string;
  architecture: string;
  artifactKind: string;
}

/**
 * Increments today's counter for `event`. Best-effort: a D1 failure is logged and swallowed
 * rather than thrown, since this must never affect the byte stream the caller has already
 * started sending back to the downloader (docs/release-publishing-kpi-plan.md Phase 1).
 * Callers should invoke this via `ExecutionContext#waitUntil()` so it doesn't add latency to the
 * download response either.
 */
export async function recordDownload(db: D1Database, event: DownloadEvent, now: Date = new Date()): Promise<void> {
  const day = now.toISOString().slice(0, 10);
  try {
    await db
      .prepare(
        `INSERT INTO download_counts (day, channel, version, platform, architecture, artifact_kind, count)
         VALUES (?, ?, ?, ?, ?, ?, 1)
         ON CONFLICT (day, channel, version, platform, architecture, artifact_kind)
         DO UPDATE SET count = count + 1`,
      )
      .bind(day, event.channel, event.version, event.platform, event.architecture, event.artifactKind)
      .run();
  } catch (err) {
    console.error("privacyfence-downloads: failed to record download counter", err);
  }
}

export interface DownloadStats {
  total: number;
  by_channel: Record<string, number>;
  by_platform: { platform: string; architecture: string; count: number }[];
}

/** Powers `GET /api/stats/downloads`. Throws on a real D1 error -- the caller decides how to
 * surface that (see index.ts's `handleStats`) rather than papering over it with a fake zero. */
export async function queryStats(db: D1Database): Promise<DownloadStats> {
  const [totalRow, byChannel, byPlatform] = await Promise.all([
    db.prepare(`SELECT COALESCE(SUM(count), 0) AS total FROM download_counts`).first<{ total: number }>(),
    db
      .prepare(`SELECT channel, SUM(count) AS count FROM download_counts GROUP BY channel ORDER BY channel`)
      .all<{ channel: string; count: number }>(),
    db
      .prepare(
        `SELECT platform, architecture, SUM(count) AS count FROM download_counts
         GROUP BY platform, architecture ORDER BY platform, architecture`,
      )
      .all<{ platform: string; architecture: string; count: number }>(),
  ]);

  return {
    total: totalRow?.total ?? 0,
    by_channel: Object.fromEntries((byChannel.results ?? []).map((row) => [row.channel, row.count])),
    by_platform: (byPlatform.results ?? []).map((row) => ({
      platform: row.platform,
      architecture: row.architecture,
      count: row.count,
    })),
  };
}
