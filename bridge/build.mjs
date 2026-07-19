// Bundles bridge/src/index.ts and every dependency (the MCP SDK, zod, …)
// into a single dependency-free dist/bridge.js. This is what lets the .mcpb
// ship without a node_modules/ directory: Claude Desktop's own Node runtime
// executes the one bundled file, so nothing Python- or npm-framework-shaped
// needs to be embedded in the extension. See docs/mcp-bridge-nodejs-migration.md.
import { build } from "esbuild";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));

// BRIDGE_VERSION is normally injected by scripts/build_mcpb.sh from
// pyproject.toml's [project.version] — see docs/mcp-bridge-nodejs-migration.md
// §9 on why this is generated, not hand-maintained in package.json. Falls
// back to package.json's version for plain `npm run build` during local dev.
const pkg = JSON.parse(readFileSync(path.join(here, "package.json"), "utf8"));
const version = process.env.BRIDGE_VERSION ?? pkg.version;

await build({
  entryPoints: [path.join(here, "src/index.ts")],
  outfile: path.join(here, "dist/bridge.js"),
  bundle: true,
  platform: "node",
  target: "node20",
  format: "esm",
  banner: { js: "#!/usr/bin/env node" },
  define: { "process.env.BRIDGE_VERSION": JSON.stringify(version) },
  sourcemap: true,
  logLevel: "info",
});
