"""Checks GitHub Releases for a newer PrivacyFence version, once a day.

PrivacyFence ships as a single DMG (app + bridge, one version number) via GitHub Releases tagged
``v<major>.<minor>.<patch>[-<stage>[.<n>]]`` — no PyPI package exists to check against instead.
``stage`` is one of ``dev``/``alpha``/``beta``/``rc``; a bare ``v<major>.<minor>.<patch>`` tag is a
stable release. No pre-release tag has ever actually been cut yet — this module defines the scheme
so a future beta-testing program can start without any further changes here.

This is a "nice to have", not something any tool call depends on: a network failure, a GitHub API
hiccup, or a malformed response must never surface as more than a logged warning. Every public
function here either returns ``None``/a best-effort cached result on failure, or (for
``fetch_latest_release`` alone) raises :class:`UpdateCheckerError`, which every other function in
this module catches internally.

The on-disk cache also records which channel (stable vs. beta) produced it, so switching the
"Receive Beta Releases" setting on or off doesn't leave the user staring at a stale result from the
other channel for up to a day.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import requests

from . import __version__
from .paths import data_dir

logger = logging.getLogger(__name__)

GITHUB_RELEASES_LATEST_URL = "https://api.github.com/repos/andras-tkcs/privacyfence/releases/latest"
GITHUB_RELEASES_LIST_URL = "https://api.github.com/repos/andras-tkcs/privacyfence/releases?per_page=1"
REPO_RELEASES_URL_FALLBACK = "https://github.com/andras-tkcs/privacyfence/releases"
REQUEST_TIMEOUT_SECONDS = 10

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
REMIND_LATER_SECONDS = 4 * 60 * 60

# Pre-release stage -> sort rank. A missing suffix (stable) or an unrecognized one both fall
# through to STABLE_RANK, the highest rank -- a stable tag always outranks every pre-release of the
# same major.minor.patch, and an unrecognized suffix degrades safely instead of erroring.
_STAGE_RANK: dict[str, int] = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}
STABLE_RANK = 4

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z]+)\.?(\d+)?)?")


class UpdateCheckerError(Exception):
    """Raised only by fetch_latest_release() -- network, HTTP, or malformed-response problems."""


class UpdateCheckResult(NamedTuple):
    latest_version: str
    release_url: str
    is_beta: bool
    is_update_available: bool


def parse_version(raw: str) -> tuple[int, int, int, int, int] | None:
    """Parse a `major.minor.patch[-stage[.n]]` prefix out of a version/tag string, tolerating a
    leading 'v'. Returns (major, minor, patch, stage_rank, stage_num) -- directly tuple-comparable
    for correct precedence, e.g. 2.2.0-beta.1 < 2.2.0-rc.1 < 2.2.0. Returns None (never raises) if
    the string doesn't even match major.minor.patch."""
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    stage_name = (m.group(4) or "").lower()
    stage_num = int(m.group(5)) if m.group(5) else 0
    stage_rank = _STAGE_RANK.get(stage_name, STABLE_RANK)
    return (major, minor, patch, stage_rank, stage_num)


def is_newer(remote_version: str, local_version: str | None = None) -> bool:
    """True iff remote_version's parsed tuple is strictly greater than local_version's (module's
    own __version__ by default -- looked up at call time, not baked into the signature at import
    time, so it stays correct across version bumps and is monkeypatch-friendly in tests). A local
    '-dev' suffix ranks lowest, so a not-yet-tagged dev build never false-positives against its own
    upcoming release but does correctly report "newer" once any tagged release ships."""
    if local_version is None:
        local_version = __version__
    remote = parse_version(remote_version)
    local = parse_version(local_version)
    if remote is None or local is None:
        return False
    return remote > local


def _cache_file() -> Path:
    return data_dir() / "update_check_cache.json"


def _load_cache() -> dict[str, Any]:
    try:
        with open(_cache_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Could not load update check cache: %s", exc)
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        with open(_cache_file(), "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except Exception as exc:
        logger.warning("Could not save update check cache: %s", exc)


def fetch_latest_release(include_beta: bool = False) -> dict[str, Any]:
    """GET the GitHub releases endpoint (the single-object 'latest' endpoint for the stable
    channel, which already excludes pre-releases/drafts; the newest-first list endpoint for the
    beta channel, which includes pre-releases). Raises UpdateCheckerError on any network/HTTP/
    JSON-shape problem; never returns a partial dict."""
    url = GITHUB_RELEASES_LIST_URL if include_beta else GITHUB_RELEASES_LATEST_URL
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise UpdateCheckerError(f"Could not reach GitHub Releases: {exc}") from exc
    except ValueError as exc:
        raise UpdateCheckerError(f"Malformed response from GitHub Releases: {exc}") from exc

    if include_beta:
        if not isinstance(data, list) or not data:
            raise UpdateCheckerError("GitHub Releases list was empty")
        release = data[0]
    else:
        release = data

    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not tag:
        raise UpdateCheckerError("GitHub Releases response had no tag_name")
    return {
        "tag_name": tag,
        "html_url": release.get("html_url") or REPO_RELEASES_URL_FALLBACK,
        "prerelease": bool(release.get("prerelease", False)),
    }


def _channel(include_beta: bool) -> str:
    return "beta" if include_beta else "stable"


def _result_from_cache(cache: dict[str, Any]) -> UpdateCheckResult:
    latest_version = cache.get("latest_seen_version", "")
    available = is_newer(latest_version) and cache.get("skipped_version") != latest_version
    return UpdateCheckResult(
        latest_version=latest_version,
        release_url=cache.get("release_url") or REPO_RELEASES_URL_FALLBACK,
        is_beta=bool(cache.get("is_beta", False)),
        is_update_available=available,
    )


def check_for_update(force: bool = False, include_beta: bool = False) -> UpdateCheckResult | None:
    """The single entry point menu_bar.py calls, off the main thread. Reads the on-disk cache; if
    it matches the requested channel and was refreshed under 24h ago and `force` is False, returns
    a result derived from the cache with no network call. Otherwise calls fetch_latest_release(),
    updates the cache, and returns a fresh result. On fetch failure: falls back to a stale
    same-channel cache if one exists, else returns None -- never raises.
    """
    channel = _channel(include_beta)
    cache = _load_cache()
    same_channel = cache.get("channel") == channel
    last_checked_raw = cache.get("last_checked")
    cache_is_fresh = False
    if same_channel and last_checked_raw and not force:
        try:
            last_checked = datetime.fromisoformat(last_checked_raw)
            cache_is_fresh = (datetime.now(timezone.utc) - last_checked) < timedelta(seconds=CHECK_INTERVAL_SECONDS)
        except ValueError:
            cache_is_fresh = False

    if cache_is_fresh:
        return _result_from_cache(cache)

    try:
        release = fetch_latest_release(include_beta=include_beta)
    except UpdateCheckerError as exc:
        logger.warning("Update check failed: %s", exc)
        return _result_from_cache(cache) if same_channel and cache else None

    cache["last_checked"] = datetime.now(timezone.utc).isoformat()
    cache["channel"] = channel
    cache["latest_seen_version"] = release["tag_name"]
    cache["release_url"] = release["html_url"]
    cache["is_beta"] = release["prerelease"]
    _save_cache(cache)
    return _result_from_cache(cache)


def mark_skipped(version: str) -> None:
    """Persist that the user chose "Skip This Version" for `version` -- check_for_update() won't
    report is_update_available again for this exact version (a newer one released later still will)."""
    cache = _load_cache()
    cache["skipped_version"] = version
    _save_cache(cache)


def mark_remind_later() -> None:
    """Persist a short re-check delay (a few hours, not a full day) so "Remind Me Later" doesn't
    just reappear on the very next timer tick but also doesn't wait a full day."""
    cache = _load_cache()
    cache["remind_after"] = (datetime.now(timezone.utc) + timedelta(seconds=REMIND_LATER_SECONDS)).isoformat()
    _save_cache(cache)


def should_notify_now(cache: dict[str, Any] | None = None) -> bool:
    """True unless a "Remind Me Later" delay set by mark_remind_later() is still in effect."""
    cache = cache if cache is not None else _load_cache()
    remind_after_raw = cache.get("remind_after")
    if not remind_after_raw:
        return True
    try:
        remind_after = datetime.fromisoformat(remind_after_raw)
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= remind_after
