// Ambient binding types for this Worker, matching wrangler.toml's [[r2_buckets]]/[[d1_databases]]
// blocks exactly. Committed by hand rather than generated, since the two bindings are fixed and
// documented in docs/release-publishing-kpi-plan.md's Prerequisites section; regenerate with
// `npx wrangler types` instead if a binding is ever added or renamed.

interface Env {
  RELEASES: R2Bucket;
  DB: D1Database;
}

// `cloudflare:test`'s `env` export (see test/env.d.ts) and Workers' own `WorkerEntrypoint`/
// `DurableObject` base classes default their `Env` type parameter to this -- see
// @cloudflare/workers-types' `declare namespace Cloudflare { interface Env {} }`.
declare namespace Cloudflare {
  interface Env extends globalThis.Env {}
}
