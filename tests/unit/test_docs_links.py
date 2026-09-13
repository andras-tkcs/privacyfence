"""Every relative Markdown link in this repo resolves -- file *and* heading anchor.

Three separate code-review rounds each found a different set of dead cross-references that a hand
audit had missed: ~300 citations to four deleted plan documents, nine README/`pii-detection-
keywords.md` anchors pointing at headings that had been renamed, and a single anchor that was one
hyphen short of the real slug. Each was fixed by hand, and the next round found another. This test
is the mechanism that was missing -- a broken link now fails a PR in under a second instead of
waiting for someone to read the doc and click.

Scope: relative links only. External `http(s)://` URLs are deliberately not fetched (no network in
this tier, and a third party's uptime is not this suite's business), and `mailto:` is skipped for
the same reason.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# `[text](target)`, with an optional `"title"` after the target. Deliberately not a Markdown
# parser: this only needs to find link targets, and the pattern that matters (a path, optionally
# followed by `#anchor`) is unambiguous enough that a parser would buy nothing.
_LINK = re.compile(r"\[(?:[^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "htmlcov"}

# GitHub renders `../../releases` and friends from a repo-root README as links to the repository's
# own tabs (releases, issues, ...) -- they are not filesystem paths and resolve above the repo
# root, so they are correct-as-written rather than broken.
_GITHUB_REPO_TABS = {"releases", "issues", "pulls", "wiki", "actions", "security", "tags"}


def _markdown_files() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.md")
        if not any(part in _SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
    )


def _slug(heading: str) -> str:
    """GitHub's heading-anchor slug.

    Lowercase, drop everything that is not a word character, space or hyphen, then turn each
    remaining space into a hyphen. The subtlety that produced a real bug: a character GitHub drops
    (`/`, `.`) leaves its *surrounding spaces* behind, so `--check / --record` collapses to four
    hyphens between the words, not three.
    """
    text = re.sub(r"`([^`]*)`", r"\1", heading)  # inline code renders as its contents
    text = text.replace("*", "")  # emphasis markers; `_` is NOT stripped -- GitHub keeps
    text = re.sub(r"[^\w\s-]", "", text)  # underscores in identifiers (qa_fixture_recorder.py)
    return text.strip().lower().replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if match:
            anchors.add(_slug(match.group(1)))
    return anchors


def _relative_links() -> list[tuple[Path, int, str]]:
    found = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in _LINK.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            found.append((path, text[: match.start()].count("\n") + 1, target))
    return found


_LINKS = _relative_links()


def test_the_scan_actually_found_links():
    """Guards the test itself: a regex or walk that silently matches nothing would make every
    assertion below vacuously pass, which is the failure mode a link checker can least afford."""
    assert len(_LINKS) > 50, f"only {len(_LINKS)} relative links found -- the scan is probably broken"


@pytest.mark.parametrize("path, line, target", _LINKS, ids=lambda v: str(v) if isinstance(v, str) else "")
def test_relative_link_resolves(path: Path, line: int, target: str):
    where = f"{path.relative_to(REPO_ROOT)}:{line}"
    file_part, _, anchor = target.partition("#")
    if not file_part:
        return

    resolved = (path.parent / file_part).resolve()
    if not resolved.is_relative_to(REPO_ROOT):
        # A GitHub repo-tab shortcut (`../../releases`), not a path into the tree.
        assert resolved.name in _GITHUB_REPO_TABS, f"{where}: link escapes the repo and is not a GitHub tab: {target}"
        return

    assert resolved.exists(), f"{where}: link target does not exist: {target}"

    if anchor and resolved.suffix == ".md":
        available = _anchors(resolved)
        assert anchor.lower() in available, (
            f"{where}: no heading in {resolved.relative_to(REPO_ROOT)} produces the anchor "
            f"#{anchor} (that file's headings slugify to, among others: "
            f"{sorted(a for a in available if a[:3] == anchor.lower()[:3]) or sorted(available)[:5]})"
        )
