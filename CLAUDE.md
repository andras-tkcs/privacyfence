# CLAUDE.md

Process notes for working on PrivacyFence with Claude Code. For code/test conventions see
[`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md); for contribution
process (forking, issues, license) see [`CONTRIBUTING.md`](CONTRIBUTING.md). This file covers the
parts of the workflow that live only in git history, not in a doc — release mechanics and branch
hygiene.

## Version bumps

The version string lives in exactly two places, always bumped together in their own commit with no
other changes:

- `pyproject.toml` (`project.version`)
- `src/privacyfence/__init__.py` (`__version__`)

Commit message format: `Bump to vX.Y.Z` or `Bump to vX.Y.Z: <short summary of what shipped>`.

**Only bump when a branch is actually about to be released to `main`.** Because branches are
developed in parallel (see worktrees below), bumping the version early on a feature branch risks
colliding with another branch's bump landing first — two branches both claiming the same next
version. If a bump commit's branch ends up merging after another release already took that version
number, revert the bump (see `d929510`, "Revert version bump — will release together with other
pending CRs") and let the version get bumped once, at actual release time, not per-branch.

## Branching & PRs

- Branch names are `<type>/<kebab-case-description>` — `feat/`, `feature/`, `fix/`, `chore/`,
  `tests/` are all in use; `fix/` for bug fixes, `feat/`/`feature/` for new functionality.
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
