# Dev vs. live setup

Use separate state for source development and packaged/release testing so connector credentials, MCP discovery, configuration, and daemon processes do not interfere with each other.

## State locations

`src/privacyfence/paths.py` is authoritative.

- Source/unbundled development runs use repository-local development configuration, credentials, and logs.
- Bundled/release runs use the user's PrivacyFence state under `~/.privacyfence` (or the platform-equivalent user home path used by the implementation).
- MCP discovery/auth files such as `mcp_url` and `mcp_token` live under the user's PrivacyFence home so the Desktop shim can find the daemon independently of the source checkout.

Separate OS user accounts are a convenient way to isolate a development environment from an end-user/package environment, but they are not required if you deliberately separate ports/state and ensure only the intended daemon is active.

## Local web port

Local mode listens on the configured `web.port`, default `8765`. Only one process can bind the same address/port.

If two PrivacyFence daemons must run concurrently on the same machine, configure a different `web.port` for one environment. Otherwise stop the first daemon before starting the second.

## Development/source run

From the repository root, use:

```bash
./scripts/dev_start.sh
```

The script prepares/uses the project environment, builds the Desktop MCP shim, registers the development MCP entry, and runs the daemon in the foreground.

Use dedicated test/QA accounts for connector work. See [`qa-environment-setup.md`](qa-environment-setup.md) and [`connector-qa-testing.md`](connector-qa-testing.md).

## Packaged/release run

Use the artifact produced by the platform build path documented in [`platform-support.md`](platform-support.md). Treat that environment as an end-user install: do not rely on the source checkout/venv to make the packaged application work.

Install/configure the bundled MCP integration and connectors through the packaged application's normal settings/authentication surfaces.

## Switching environments

When switching between source and packaged testing:

1. stop the daemon that should no longer be active (unless the two environments intentionally use different ports);
2. start the intended environment's daemon/application;
3. verify the user's PrivacyFence discovery files point to that running instance;
4. ensure the MCP client is configured with the shim/package for the environment being tested;
5. restart the MCP client when its configuration has changed and it does not reload dynamically.

A port-bind failure usually means another daemon already owns the configured port. MCP authentication/connectivity failures can also result from stale discovery files or a client still using the other environment's shim/configuration.

## Release-candidate testing

Use [`release-testing.md`](release-testing.md) for human-only release checks and [`testing-policy.md`](testing-policy.md) for automated coverage. Test packaged behavior against the actual artifact, not against a source process as a substitute.
