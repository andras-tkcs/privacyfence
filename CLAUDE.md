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
both trigger on (`on: push: tags: ['v*']`) — the former builds and signs the DMG/Windows installer/
`.deb` (running each one's own packaged-artifact smoke test, see "Packaged-artifact release gating"
below) and generates the SBOMs, the latter builds the sdist/wheel from the same tag. The two
workflows trigger independently but no longer publish independently: `publish-pypi.yml`'s
`wait_for_build` job blocks every one of its own publish steps on `build.yml`'s run for that same
commit actually succeeding — see "Packaged-artifact release gating" below. Every one of those
artifacts always uploads to the private Cloudflare R2 release archive (see "Cloudflare R2 release
archive" below);
whether it *also* reaches a public GitHub Release / PyPI/TestPyPI depends on the tag's channel
(`a`/`b`/`rc` suffix, or none for stable — same PEP 440 short-form scheme `update_checker.py`'s
beta channel already ranks by): only a stable tag's DMG/SBOMs get attached to a public GitHub
Release and only a stable tag's sdist/wheel reach PyPI/TestPyPI; a pre-release tag still gets a
GitHub Release entry (marked prerelease, so `update_checker.py`'s beta channel — which reads
exactly that flag — keeps working), just with no files attached to it. Nothing else anywhere needs
editing or committing first. Between tags,
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

### Packaged-artifact release gating

`docs/automated-test-strategy-plan.md` Phase 6.4: every published DMG/installer/`.deb` is started
and exercised, automatically, before it (or anything else from the same tag) actually ships.

Within `build.yml`, this needs no cross-workflow trickery — each of the `build` (macOS),
`build-windows`, and `build-deb` jobs runs its own packaged-artifact test
(`tests/integration/test_macos_packaged_smoke.py`, `test_windows_packaged_smoke.py`,
`test_deb_packaged_lifecycle.py` — all `pytest.mark.packaged`) as an ordinary step, right after
that job builds its own artifact and before that same job's own R2-upload/GitHub-Release-attach
steps. An ordinary failed step stops the job there, so a broken DMG/installer/`.deb` never reaches
its own upload steps — no `needs:` needed for this part, since it's all sequencing within one job.

Getting the same guarantee into `publish-pypi.yml` is the part that actually needs wiring: that
workflow's sdist/wheel has no packaged-artifact test of its own to gate on, but a broken macOS/
Windows/Linux build should still block *its* publish too — a release isn't good just because the
one artifact this workflow happens to build is fine. GitHub Actions has no `needs:` across separate
workflow files, so `publish-pypi.yml`'s `wait_for_build` job (its own first job, gating
`publish-testpypi`/`publish-pypi`/`publish-r2`) polls the REST API for `build.yml`'s own run against
the exact same commit and fails loudly, without publishing anything, unless that run completed
successfully. See that job's own comment for the reasoning, including how a `workflow_dispatch`
rerun is gated the same way (keyed on commit SHA, not run recency, so a rerun after a fixed and
re-run `build.yml` finds the new result immediately).

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
   - **Owner**: `privacyfence`
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

**Only a stable tag reaches PyPI/TestPyPI.** `publish-testpypi` (and, transitively, `publish-pypi`,
which depends on it) is gated on `needs.build.outputs.channel == 'stable'` — a pre-release tag
(`a`/`b`/`rc` suffix) still builds the sdist/wheel, but the `build` job's `publish-r2` sibling is
the only place it's published; see below.

### Cloudflare R2 release archive

Every tag push — stable and pre-release alike — additionally uploads that release's artifacts to
Cloudflare R2 (bucket `privacyfence-releases`), laid out as `releases/<channel>/<version>/...`:
the DMG and the two org-config build scripts (`build.yml`'s `build` job), both SBOMs (`build.yml`'s
`sbom` job), and the sdist/wheel (`publish-pypi.yml`'s `publish-r2` job). `<version>` is the
resolved `major.minor.patch[a|b|rc<n>]` string (never the `v`-prefixed tag itself); `<channel>` is
`stable`, `alpha`, `beta`, or `rc`, derived from that suffix — see `scripts/r2_release.py`, which
every one of those upload steps calls (`channel` subcommand to resolve the directory, `upload` to
actually push files). That script is also where the R2 credential wiring lives now; it replaced
`.github/workflows/r2-smoke-test.yml`, a one-off workflow that proved the GitHub Actions → R2
credentials/endpoint plumbing worked and was deleted once `r2_release.py` existed to reuse it.

R2 is the one archive that has *everything*, regardless of what's also public elsewhere — stable
artifacts land here too, even though they're also on PyPI/GitHub Releases. The bucket itself is
left at Cloudflare R2's default (private — no public bucket policy or custom domain configured by
anything in this repo), which is deliberate: alpha/beta (and, per the same gate, rc) need
restricted access, and a private bucket is the only channel-agnostic way to guarantee that without
duplicating the per-channel logic into a bucket-policy layer too. Concretely, this means:

- **Stable**: reaches PyPI/TestPyPI (see above) and gets a public GitHub Release with the DMG,
  org-config scripts, and SBOMs attached, exactly as before — R2 is an additional private mirror,
  not stable's only distribution point.
- **Alpha / beta / rc**: never reach PyPI/TestPyPI, and their GitHub Release entry (still created,
  marked prerelease, so `update_checker.py`'s beta channel — which reads exactly that flag off the
  releases list — keeps working) carries no file attachments. The actual DMG/SBOMs/sdist/wheel
  exist only in the private R2 bucket.

Required secrets/vars (Settings → Secrets and variables → Actions), same names
`r2-smoke-test.yml` used:

- `CF_R2_ACCESS_KEY_ID` / `CF_R2_SECRET_ACCESS_KEY` (secrets) — an R2 API token scoped to the
  `privacyfence-releases` bucket.
- `CF_R2_ENDPOINT` (repo/environment variable) — the bucket's S3-compatible endpoint URL.

**Not yet decided: how an authorized alpha/beta tester actually gets a file out of the private
bucket.** Nothing in this repo automates that today (no presigned-URL script, no Cloudflare Access
policy) — `scripts/r2_release.py` only ever pushes files in. Whoever sets up the beta-testing
program should pick one (a maintainer-run script that mints short-lived presigned URLs, or
Cloudflare Access/Zero Trust gating allow-listed tester emails in front of the bucket) and document
it here alongside this section.

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
