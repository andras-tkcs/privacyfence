"""Unit tests for privacyfence.update_checker.

This is a background "nice to have" check, not something any tool call depends on -- the one
invariant every test class here ultimately protects is that a network failure, a malformed GitHub
response, or a corrupt cache file must never raise out of check_for_update(); the worst outcome is
returning a stale or None result.
"""
from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest
import requests
from freezegun import freeze_time

from privacyfence import update_checker as uc


@pytest.fixture(autouse=True)
def _fixed_local_version(monkeypatch):
    # check_for_update()/is_newer() default to comparing against the real,
    # currently-checked-out __version__ -- fix it to a known-old baseline so
    # these tests don't silently start failing/passing differently as the
    # repo's own version gets bumped over time. Tests that care about a
    # specific local version pass local_version explicitly to is_newer().
    monkeypatch.setattr(uc, "__version__", "1.0.0")


class TestParseVersion:
    def test_plain_stable_tag(self):
        assert uc.parse_version("2.1.0") == (2, 1, 0, uc.STABLE_RANK, 0, math.inf)

    def test_v_prefixed_stable_tag(self):
        assert uc.parse_version("v2.1.0") == (2, 1, 0, uc.STABLE_RANK, 0, math.inf)

    def test_alpha_suffix(self):
        assert uc.parse_version("2.1.0a3") == (2, 1, 0, 1, 3, math.inf)

    def test_beta_suffix_with_number(self):
        assert uc.parse_version("v2.1.0b2") == (2, 1, 0, 2, 2, math.inf)

    def test_rc_suffix_with_number(self):
        assert uc.parse_version("v2.1.0rc1") == (2, 1, 0, 3, 1, math.inf)

    def test_between_tags_dev_build_guessing_next_patch(self):
        # setuptools_scm's own shape for "N commits past the last tag" -- see this module's
        # docstring. No <stage><n> component: it's guessing at a plain next stable patch release.
        assert uc.parse_version("2.1.1.dev3+gabc1234") == (2, 1, 1, uc.STABLE_RANK, 0, 3)

    def test_between_tags_dev_build_guessing_next_prerelease(self):
        assert uc.parse_version("2.1.0rc2.dev1+gabc1234") == (2, 1, 0, 3, 2, 1)

    def test_dirty_worktree_suffix_ignored(self):
        assert uc.parse_version("2.1.1.dev3+gabc1234.d20260904") == (2, 1, 1, uc.STABLE_RANK, 0, 3)

    def test_unrecognized_suffix_returns_none(self):
        # The old dashed/spelled-out scheme ("-nightly", "-beta.1", ...) no longer matches at all --
        # every real tag or __version__ this module ever sees now comes from setuptools_scm or a
        # human tagging in this exact scheme, so "unparseable" is no longer conflated with "stable".
        assert uc.parse_version("v2.1.0-nightly") is None

    def test_garbage_returns_none(self):
        assert uc.parse_version("not-a-version") is None

    def test_stage_rank_ordering(self):
        assert (
            uc.parse_version("2.1.0a1")
            < uc.parse_version("2.1.0b1")
            < uc.parse_version("2.1.0rc1")
            < uc.parse_version("2.1.0")
        )

    def test_dev_build_ranks_below_the_release_it_is_guessing_at(self):
        assert uc.parse_version("2.1.0rc2.dev1+gabc1234") < uc.parse_version("2.1.0rc2")


class TestIsNewer:
    def test_remote_newer_stable(self):
        assert uc.is_newer("v2.2.0", local_version="2.1.0") is True

    def test_remote_equal(self):
        assert uc.is_newer("v2.1.0", local_version="2.1.0") is False

    def test_remote_older(self):
        assert uc.is_newer("v2.0.0", local_version="2.1.0") is False

    def test_local_dev_build_vs_the_release_it_is_guessing_at_is_older(self):
        assert uc.is_newer("2.1.1", local_version="2.1.1.dev3+gabc1234") is True

    def test_local_dev_build_still_newer_than_older_stable_release(self):
        assert uc.is_newer("v2.1.0", local_version="2.1.1.dev3+gabc1234") is False

    def test_unparseable_remote_returns_false(self):
        assert uc.is_newer("garbage", local_version="2.1.0") is False

    def test_unparseable_local_returns_false(self):
        assert uc.is_newer("2.1.0", local_version="garbage") is False

    def test_beta_newer_than_earlier_beta(self):
        assert uc.is_newer("v2.2.0b2", local_version="2.2.0b1") is True

    def test_stable_newer_than_rc_of_same_number(self):
        assert uc.is_newer("v2.2.0", local_version="2.2.0rc1") is True

    def test_rc_newer_than_beta_of_same_number(self):
        assert uc.is_newer("v2.2.0rc1", local_version="2.2.0b1") is True

    def test_local_beta_not_newer_than_same_remote_beta(self):
        assert uc.is_newer("v2.2.0b1", local_version="2.2.0b1") is False

    def test_local_beta_older_than_remote_stable_of_same_number(self):
        assert uc.is_newer("v2.2.0", local_version="2.2.0b1") is True

    def test_local_beta_older_than_remote_rc_of_same_number(self):
        assert uc.is_newer("v2.2.0rc1", local_version="2.2.0b1") is True


class TestFetchLatestRelease:
    def test_stable_success_hits_latest_endpoint(self, monkeypatch):
        captured = {}

        def fake_get(url, **kw):
            captured["url"] = url
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "tag_name": "v2.2.0",
                "html_url": "https://github.com/andras-tkcs/privacyfence/releases/tag/v2.2.0",
                "prerelease": False,
            }
            return response

        monkeypatch.setattr("requests.get", fake_get)

        release = uc.fetch_latest_release(include_beta=False)

        assert captured["url"] == uc.GITHUB_RELEASES_LATEST_URL
        assert release == {
            "tag_name": "v2.2.0",
            "html_url": "https://github.com/andras-tkcs/privacyfence/releases/tag/v2.2.0",
            "prerelease": False,
        }

    def test_beta_success_hits_list_endpoint_and_takes_first_element(self, monkeypatch):
        captured = {}

        def fake_get(url, **kw):
            captured["url"] = url
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = [
                {"tag_name": "v2.2.0b1", "html_url": "https://x/tag/v2.2.0b1", "prerelease": True}
            ]
            return response

        monkeypatch.setattr("requests.get", fake_get)

        release = uc.fetch_latest_release(include_beta=True)

        assert captured["url"] == uc.GITHUB_RELEASES_LIST_URL
        assert release["tag_name"] == "v2.2.0b1"
        assert release["prerelease"] is True

    def test_beta_empty_list_raises(self, monkeypatch):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = []
        monkeypatch.setattr("requests.get", lambda *a, **kw: response)

        with pytest.raises(uc.UpdateCheckerError, match="empty"):
            uc.fetch_latest_release(include_beta=True)

    def test_connection_error_raises_update_checker_error(self, monkeypatch):
        def raise_it(*a, **kw):
            raise requests.RequestException("network error")

        monkeypatch.setattr("requests.get", raise_it)

        with pytest.raises(uc.UpdateCheckerError, match="network error"):
            uc.fetch_latest_release()

    def test_http_error_status_raises_update_checker_error(self, monkeypatch):
        response = MagicMock()
        response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
        monkeypatch.setattr("requests.get", lambda *a, **kw: response)

        with pytest.raises(uc.UpdateCheckerError, match="404"):
            uc.fetch_latest_release()

    def test_malformed_json_raises_update_checker_error(self, monkeypatch):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("no JSON object could be decoded")
        monkeypatch.setattr("requests.get", lambda *a, **kw: response)

        with pytest.raises(uc.UpdateCheckerError, match="Malformed"):
            uc.fetch_latest_release()

    def test_missing_tag_name_raises_update_checker_error(self, monkeypatch):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"html_url": "https://x"}
        monkeypatch.setattr("requests.get", lambda *a, **kw: response)

        with pytest.raises(uc.UpdateCheckerError, match="tag_name"):
            uc.fetch_latest_release()


def _fake_release(tag="v2.2.0", prerelease=False):
    return {"tag_name": tag, "html_url": f"https://x/tag/{tag}", "prerelease": prerelease}


class TestCheckForUpdate:
    def test_no_cache_fetches_and_reports_available(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release())

        result = uc.check_for_update()

        assert result is not None
        assert result.latest_version == "v2.2.0"
        assert result.is_update_available is True

    def test_fresh_same_channel_cache_skips_network_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")

        def raise_if_called(include_beta=False):
            raise AssertionError("fetch_latest_release should not be called")

        with freeze_time("2026-07-29 12:00:00") as frozen:
            monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release())
            uc.check_for_update()

            frozen.tick(delta=3600)  # 1h later -- still under the 24h gate
            monkeypatch.setattr(uc, "fetch_latest_release", raise_if_called)
            result = uc.check_for_update()

        assert result is not None
        assert result.latest_version == "v2.2.0"

    def test_stale_cache_triggers_a_new_fetch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")

        with freeze_time("2026-07-29 12:00:00") as frozen:
            monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.2.0"))
            uc.check_for_update()

            frozen.tick(delta=uc.CHECK_INTERVAL_SECONDS + 1)
            monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.3.0"))
            result = uc.check_for_update()

        assert result.latest_version == "v2.3.0"

    def test_force_bypasses_the_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")
        calls = []

        def fake_fetch(include_beta=False):
            calls.append(1)
            return _fake_release()

        monkeypatch.setattr(uc, "fetch_latest_release", fake_fetch)
        uc.check_for_update()
        uc.check_for_update(force=True)

        assert len(calls) == 2

    def test_channel_mismatch_bypasses_the_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.2.0"))
        uc.check_for_update(include_beta=False)

        calls = []

        def fake_fetch_beta(include_beta=False):
            calls.append(include_beta)
            return _fake_release("v2.3.0b1", prerelease=True)

        monkeypatch.setattr(uc, "fetch_latest_release", fake_fetch_beta)
        result = uc.check_for_update(include_beta=True)

        assert calls == [True]
        assert result.latest_version == "v2.3.0b1"

    def test_fetch_failure_with_no_cache_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")

        def raise_it(include_beta=False):
            raise uc.UpdateCheckerError("network down")

        monkeypatch.setattr(uc, "fetch_latest_release", raise_it)

        assert uc.check_for_update() is None

    def test_fetch_failure_with_stale_same_channel_cache_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")

        with freeze_time("2026-07-29 12:00:00") as frozen:
            monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.2.0"))
            uc.check_for_update()

            frozen.tick(delta=uc.CHECK_INTERVAL_SECONDS + 1)

            def raise_it(include_beta=False):
                raise uc.UpdateCheckerError("network down")

            monkeypatch.setattr(uc, "fetch_latest_release", raise_it)
            result = uc.check_for_update()

        assert result is not None
        assert result.latest_version == "v2.2.0"

    def test_fetch_failure_with_stale_other_channel_cache_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.2.0"))
        uc.check_for_update(include_beta=False)

        def raise_it(include_beta=False):
            raise uc.UpdateCheckerError("network down")

        monkeypatch.setattr(uc, "fetch_latest_release", raise_it)

        assert uc.check_for_update(include_beta=True) is None

    def test_corrupt_last_checked_timestamp_is_treated_as_not_fresh(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(
            '{"channel": "stable", "last_checked": "not-a-timestamp", '
            '"latest_seen_version": "v2.1.0"}'
        )
        monkeypatch.setattr(uc, "_cache_file", lambda: cache_file)
        monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.2.0"))

        result = uc.check_for_update()  # must not raise despite the bad timestamp

        assert result.latest_version == "v2.2.0"


class TestDiskPersistence:
    def test_corrupt_cache_is_ignored_not_raised(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json{{{")
        monkeypatch.setattr(uc, "_cache_file", lambda: cache_file)

        assert uc._load_cache() == {}

    def test_save_failure_is_logged_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "no-such-dir" / "cache.json")

        uc._save_cache({"foo": "bar"})  # parent dir doesn't exist -- must not raise

    def test_missing_cache_file_treated_as_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "does-not-exist.json")

        assert uc._load_cache() == {}

    def test_state_survives_across_calls(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(uc, "_cache_file", lambda: cache_file)
        monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.2.0"))

        uc.check_for_update()

        assert uc._load_cache()["latest_seen_version"] == "v2.2.0"


class TestMarkSkippedAndRemindLater:
    def test_skipped_version_reports_not_available(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.2.0"))
        uc.check_for_update()

        uc.mark_skipped("v2.2.0")
        result = uc.check_for_update(force=True)

        assert result.is_update_available is False

    def test_newer_version_after_skip_still_reports_available(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.2.0"))
        uc.check_for_update()
        uc.mark_skipped("v2.2.0")

        monkeypatch.setattr(uc, "fetch_latest_release", lambda include_beta=False: _fake_release("v2.3.0"))
        result = uc.check_for_update(force=True)

        assert result.is_update_available is True

    def test_remind_later_sets_a_future_timestamp_respected_by_should_notify_now(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")

        with freeze_time("2026-07-29 12:00:00") as frozen:
            uc.mark_remind_later()
            assert uc.should_notify_now() is False

            frozen.tick(delta=uc.REMIND_LATER_SECONDS + 1)
            assert uc.should_notify_now() is True

    def test_should_notify_now_true_with_no_remind_after_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")

        assert uc.should_notify_now() is True

    def test_should_notify_now_true_with_corrupt_remind_after(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "_cache_file", lambda: tmp_path / "cache.json")

        assert uc.should_notify_now({"remind_after": "not-a-timestamp"}) is True
