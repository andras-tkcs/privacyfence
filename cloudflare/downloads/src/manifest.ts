/**
 * Release manifest types and R2 lookups. Schema matches
 * docs/release-publishing-kpi-plan.md Phase 2 exactly (schema `1`) so this Worker needs no
 * changes when Phase 2's `scripts/r2_release.py finalize` starts writing real manifests --
 * Phase 1 only ever reads hand-written fixtures of the same shape (see test/fixtures.ts).
 */
import { channelForVersion, type Channel } from "./channel.js";

export interface ManifestArtifact {
  id: string;
  kind: string;
  platform: string;
  architecture: string;
  filename: string;
  key: string;
  size: number;
  sha256: string;
}

export interface Manifest {
  schema: number;
  version: string;
  channel: Channel;
  published_at: string;
  artifacts: ManifestArtifact[];
}

export interface LatestPointer {
  version: string;
  manifest: string;
}

export function latestPointerKey(channel: Channel): string {
  return `releases/${channel}/latest.json`;
}

export function manifestKey(channel: Channel, version: string): string {
  return `releases/${channel}/${version}/manifest.json`;
}

async function getJson<T>(bucket: R2Bucket, key: string): Promise<T | null> {
  const object = await bucket.get(key);
  if (!object) return null;
  return object.json<T>();
}

/** Resolves a channel's `latest.json` pointer to its manifest. Null if nothing is published yet. */
export async function resolveLatestManifest(bucket: R2Bucket, channel: Channel): Promise<Manifest | null> {
  const pointer = await getJson<LatestPointer>(bucket, latestPointerKey(channel));
  if (!pointer) return null;
  return getJson<Manifest>(bucket, pointer.manifest);
}

/**
 * Resolves a specific version's manifest directly (no `latest.json` involved -- an older
 * version stays downloadable by exact version even after a newer one becomes `latest`).
 * Throws the same way `channelForVersion` does for a version string that was never tagged.
 */
export async function resolveVersionManifest(
  bucket: R2Bucket,
  version: string,
): Promise<{ channel: Channel; manifest: Manifest } | null> {
  const channel = channelForVersion(version);
  const manifest = await getJson<Manifest>(bucket, manifestKey(channel, version));
  if (!manifest) return null;
  return { channel, manifest };
}

export function findArtifact(manifest: Manifest, artifactId: string): ManifestArtifact | undefined {
  return manifest.artifacts.find((artifact) => artifact.id === artifactId);
}
