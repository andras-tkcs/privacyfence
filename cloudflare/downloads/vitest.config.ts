import { defineConfig } from "vitest/config";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-pool-workers";

export default defineConfig({
  plugins: [
    cloudflareTest(async () => ({
      wrangler: { configPath: "./wrangler.toml" },
      miniflare: {
        // Exposed to test/setup.ts via `env.TEST_MIGRATIONS` (see test/env.d.ts) -- this Worker
        // itself never sees this binding, only the test runner does. Path is relative to `vitest
        // run`'s own working directory (this package's root), matching wrangler.toml's own
        // `migrations_dir`.
        bindings: { TEST_MIGRATIONS: await readD1Migrations("./migrations") },
      },
    })),
  ],
  test: {
    setupFiles: ["./test/setup.ts"],
  },
});
