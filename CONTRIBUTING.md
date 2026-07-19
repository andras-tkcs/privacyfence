# Contributing to PrivacyFence

## Forking

You're welcome to fork PrivacyFence and build on it. If you publish a fork or
derivative project, a brief note to the author (privacyfence@tkcs.name or
open a GitHub issue) is appreciated — not required, just courteous. See `NOTICE`
for details.

## Pull Requests

All changes to `main` go through pull requests. Direct pushes are blocked.

1. Fork the repo and create a feature branch off `main`.
2. Keep PRs focused — one logical change per PR.
3. Describe *why* the change is needed, not just what it does.
4. PRs require review and approval from the maintainer before merging.

## Issues

Use GitHub Issues for bug reports and feature requests. Include:
- macOS version and Python version
- Steps to reproduce (for bugs)
- What connector is involved, if relevant

## Code Style

- `src/privacyfence/` (the daemon): Python 3.11+, standard library preferred over new dependencies
- `bridge/` (the MCP bridge): TypeScript/Node — see
  [`docs/mcp-bridge-nodejs-migration.md`](docs/mcp-bridge-nodejs-migration.md) for why it's a
  separate language from the daemon
- No comments unless the *why* is non-obvious
- Match the surrounding code's style

## License

By submitting a pull request you agree that your contribution is licensed under
the Apache License 2.0, the same license as this project.
