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

That tag push is what `.github/workflows/build.yml` **and** `.github/workflows/publish-pypi.yml`
both trigger on (`on: push: tags: ['v*']`) — the former builds and signs the DMG and attaches it to
a GitHub Release, marked prerelease iff the tag contains `a`, `b`, or `rc` (`update_checker.py`'s
beta channel reads exactly that flag); the latter builds the sdist/wheel from the same tag and
publishes them to PyPI. Nothing else anywhere needs editing or committing first. Between tags,
`__version__` is a `setuptools_scm`-synthesized dev version (`<next-version>.dev<n>+g<sha>`, e.g.
`4.0.1.dev3+gabc1234`) — see `update_checker.py`'s module docstring for exactly how that's compared
against real release tags.

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

### Publishing to PyPI

`publish-pypi.yml` builds the sdist/wheel and publishes to **TestPyPI first, then PyPI**, gated in
that order (`publish-pypi` job's `needs: publish-testpypi`) — a broken publish never reaches the
real index. It authenticates with neither project via a stored API token: both use PyPI's OIDC
**Trusted Publisher** mechanism (`pypa/gh-action-pypi-publish`, `permissions: id-token: write`),
so GitHub mints a short-lived token for the job and PyPI/TestPyPI trade it for a one-shot upload
credential themselves. There is no long-lived secret in this repo for either index.

Before the workflow can publish for the first time, register it as a Trusted Publisher on **both**
services — the project need not already exist there; both accept a "pending" publisher for a name
that isn't claimed yet, and claim it on the first successful publish. Do this once per service:

1. Sign in and go to `test.pypi.org/manage/account/publishing/` (repeat later, separately, on
   `pypi.org/manage/account/publishing/` — the two are unrelated accounts/registrations even if you
   use the same login for both).
2. Add a pending publisher with:
   - **PyPI Project Name**: `privacyfence`
   - **Owner**: `andras-tkcs`
   - **Repository name**: `privacyfence`
   - **Workflow name**: `publish-pypi.yml`
   - **Environment name**: `testpypi` (on TestPyPI) / `pypi` (on PyPI) — matches the `environment:`
     each job in `publish-pypi.yml` declares. Scoping the publisher to an environment means the
     minted OIDC token is only ever valid for that job, not any other job in this repo.

Optionally, also create matching GitHub Environments (repo **Settings → Environments**) named
`testpypi` and `pypi`. This isn't required for the OIDC exchange itself, but it's where you'd add a
**required reviewer** on the `pypi` environment if you want a manual go/no-go checkpoint between the
TestPyPI publish succeeding and the real PyPI publish running — the "test first" step the workflow
already enforces via job ordering, made into an explicit approval gate rather than just a rerun-only
safety net.

`workflow_dispatch` exists for rerunning by hand (e.g. after a transient failure) — point it at a
tagged commit. Run from an untagged commit and `setuptools_scm` produces a dev version with a local
segment (`+g<sha>`), which both indexes reject as an upload.

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
