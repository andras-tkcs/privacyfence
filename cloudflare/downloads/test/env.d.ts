import type { D1Migration } from "@cloudflare/vitest-pool-workers";

// `cloudflare:test`'s `env` export is typed as `Cloudflare.Env` (see ../worker-configuration.d.ts
// for the RELEASES/DB half of that); this adds the test-only migrations binding vitest.config.ts
// injects, so `env.TEST_MIGRATIONS` in test/setup.ts type-checks without an `as` cast.
declare global {
  namespace Cloudflare {
    interface Env {
      TEST_MIGRATIONS: D1Migration[];
    }
  }
}
