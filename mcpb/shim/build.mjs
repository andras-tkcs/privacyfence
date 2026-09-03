// Bundles mcpb/shim/src/index.ts and every dependency (the MCP SDK and its
// own transitive deps) into a single dependency-free dist/shim.js -- the
// same reason bridge/build.mjs does this for the bridge: the .mcpb ships
// without a node_modules/ directory, and Claude Desktop supplies the Node
// runtime that executes this one bundled file (server.type = "node" in
// mcpb/manifest.json.tmpl).
import { build } from "esbuild";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

await build({
  entryPoints: [path.join(here, "src/index.ts")],
  outfile: path.join(here, "dist/shim.js"),
  bundle: true,
  platform: "node",
  target: "node20",
  format: "esm",
  banner: { js: "#!/usr/bin/env node" },
  sourcemap: true,
  logLevel: "info",
});
