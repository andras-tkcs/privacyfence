# CLAUDE.md

Process notes for working on PrivacyFence with Claude Code. For code/test conventions see
[`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md); for contribution
process (forking, issues, license) see [`CONTRIBUTING.md`](CONTRIBUTING.md). This file covers the
parts of the workflow that live only in git history, not in a doc — release mechanics and branch
hygiene.

## Releasing

There is no version string in the source tree and no version-bump commit. `pyproject.toml` declares
`dynamic = ["version"]`; the real version is derived from git tags by `setuptools_scm`
(`[tool.setuptools_scm]` in `pyproject.toml`), and `src/privacyfence/__init__.py` reads it back at
import time via `importlib.metadata.version("privacyfence")`. This replaced the old two-file
hand-bumped scheme (`pyproject.toml`'s `project.version` + `__init__.py`'s `__version__`, kept in
sync by a dedicated `Bump to vX.Y.Z` commit) specifically to avoid that scheme's failure mode:
parallel branches (see worktrees below) both claiming the same next version, one bump commit landing
after another release already took that number (see `d929510`, "Revert version bump — will release
together with other pending CRs", from back when that was still how it worked).

**Cutting a release is a tag, not a commit.** Once `main` is at the commit you want to release, tag
it and push the tag:

```
git tag v4.0.0            # stable
git tag v4.0.0a13          # pre-release: a=alpha, b=beta, rc=release-candidate (PEP 440 short form)
git push origin <tag>
```

That tag push is what `.github/workflows/build.yml` triggers on (`on: push: tags: ['v*']`) — it
builds and signs the DMG and attaches it to a GitHub Release, marked prerelease iff the tag contains
`a`, `b`, or `rc` (`update_checker.py`'s beta channel reads exactly that flag). Nothing else
anywhere needs editing or committing first. Between tags, `__version__` is a `setuptools_scm`-
synthesized dev version (`<next-version>.dev<n>+g<sha>`, e.g. `4.0.1.dev3+gabc1234`) — see
`update_checker.py`'s module docstring for exactly how that's compared against real release tags.

A checkout needs its full tag history for this to resolve correctly — a shallow clone (or a tarball
with no `.git/` at all) falls back to `[tool.setuptools_scm]`'s `fallback_version`, a placeholder
that's never a real shipped version. `.github/workflows/tests.yml` and `build.yml` both pass
`fetch-depth: 0` to `actions/checkout` for exactly this reason; do the same in any new workflow that
installs this package. `scripts/build_dmg.sh` and `scripts/build_mcpb.sh` both read the resolved
version back via `importlib.metadata.version("privacyfence")`, so they require the package to
already be `pip install -e .`d (both scripts' own prerequisites say so) — same as
`PrivacyFenceApp.spec`'s `VERSION` and `src/privacyfence/__init__.py`'s `__version__` itself.

`mcpb/shim/package.json`'s `version` field is **not** tied to any of this — leave it as
`0.0.0-dev`. The shim carries no protocol version of its own to keep in sync with the daemon's (it
has no tool-schema knowledge at all — see `mcpb/shim/src/index.ts`'s module docstring), so unlike
the original bridge it replaced (retired at P5, see `docs/https-connector-refactor-plan.md`),
there's nothing here for the real version to be injected into at build time. `scripts/
build_mcpb.sh` reads the real version only to stamp the `.mcpb` manifest itself
(`mcpb/manifest.json.tmpl`'s `__VERSION__`), not anything inside the bundled `shim.js`.

## Branching & PRs

- Branch names are `<type>/<kebab-case-description>`. Standard types: `feature/` for new
  functionality, `fix/` for bug fixes, `chore/` for non-functional maintenance, `tests/` for
  test-only changes. Use `feature/`, not `feat/` — a few early branches used `feat/` before this
  was settled; that prefix is retired, don't reintroduce it on new branches.
- `main` is protected — all changes land via PR (`CONTRIBUTING.md`). PRs merge with a real merge
  commit (`Merge pull request #N from <fork>/<branch>`), not squash — keep that in mind when writing
  commit messages on a feature branch, since they survive into `main`'s history individually.
- Definition of done for a PR is the checklist in
  [`docs/coding-and-testing-guidelines.md` §2.7](docs/coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo).

## Parallel sessions & worktrees

The user regularly runs multiple Claude Code sessions on this repo at once, each on a different
task/branch. To avoid one session's checkout state (branch switches, uncommitted edits) interfering
with another's:

- Start new work in its own `git worktree` under `~/Coding/worktrees/`, not by switching branches
  in whichever checkout happens to be open. Naming convention already in use:
  `~/Coding/worktrees/privacyfence-<short-branch-slug>` (e.g. `privacyfence-fix-tasks-ssl`).
- Don't reuse an existing worktree for an unrelated task — one worktree per active branch/task.
