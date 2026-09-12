"""Tests for scripts/update_branch_protection.py.

Imported by file path (importlib) rather than as a package -- same pattern as
tests/unit/test_r2_release.py, since scripts/ isn't part of the installed `privacyfence`
distribution. Every GitHub API call is mocked; these tests never make a real network request and
never require GITHUB_TOKEN to be set for anything other than the "missing token" case.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "update_branch_protection.py"
_spec = importlib.util.spec_from_file_location("update_branch_protection", _SCRIPT_PATH)
ubp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ubp)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-tests")


def _fake_response(*, status_code=200, json_body=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = ubp.requests.HTTPError(f"{status_code} error")
    else:
        response.raise_for_status.return_value = None
    return response


class TestGetCurrent:
    def test_returns_none_on_404(self, monkeypatch):
        monkeypatch.setattr(ubp.requests, "get", lambda *a, **kw: _fake_response(status_code=404))
        assert ubp.get_current("privacyfence", "privacyfence", "main") is None

    def test_returns_parsed_body_on_success(self, monkeypatch):
        body = {"contexts": ["test"], "strict": True}
        monkeypatch.setattr(ubp.requests, "get", lambda *a, **kw: _fake_response(json_body=body))
        assert ubp.get_current("privacyfence", "privacyfence", "main") == body

    def test_raises_on_other_error_status(self, monkeypatch):
        monkeypatch.setattr(ubp.requests, "get", lambda *a, **kw: _fake_response(status_code=500))
        with pytest.raises(ubp.requests.HTTPError):
            ubp.get_current("privacyfence", "privacyfence", "main")


class TestMainShow:
    def test_reports_missing_checks(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ubp, "get_current", lambda *a, **kw: {"contexts": ["test"], "strict": True}
        )
        exit_code = ubp.main(["show"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "missing (would be added by `apply`):" in out
        assert "platform-windows" in out

    def test_reports_already_in_sync(self, monkeypatch, capsys):
        monkeypatch.setattr(ubp, "get_current", lambda *a, **kw: {"contexts": ubp.REQUIRED_STATUS_CHECKS, "strict": True})
        exit_code = ubp.main(["show"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "already in sync" in out

    def test_reports_extra_checks_not_in_target(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ubp,
            "get_current",
            lambda *a, **kw: {"contexts": [*ubp.REQUIRED_STATUS_CHECKS, "some-retired-job"], "strict": True},
        )
        exit_code = ubp.main(["show"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "extra (not in this script's target list -- review before removing):" in out
        assert "some-retired-job" in out

    def test_handles_no_existing_protection(self, monkeypatch, capsys):
        monkeypatch.setattr(ubp, "get_current", lambda *a, **kw: None)
        exit_code = ubp.main(["show"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "current: []" in out


class TestMainApply:
    def test_dry_run_does_not_call_apply_checks(self, monkeypatch, capsys):
        monkeypatch.setattr(ubp, "get_current", lambda *a, **kw: {"contexts": ["test"], "strict": True})

        def _fail_if_called(*a, **kw):
            raise AssertionError("apply_checks should not be called in --dry-run mode")

        monkeypatch.setattr(ubp, "apply_checks", _fail_if_called)
        exit_code = ubp.main(["apply", "--dry-run"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "(dry run -- not applied)" in out

    def test_already_in_sync_skips_apply_call(self, monkeypatch, capsys):
        monkeypatch.setattr(ubp, "get_current", lambda *a, **kw: {"contexts": ubp.REQUIRED_STATUS_CHECKS, "strict": True})

        def _fail_if_called(*a, **kw):
            raise AssertionError("apply_checks should not be called when already in sync")

        monkeypatch.setattr(ubp, "apply_checks", _fail_if_called)
        exit_code = ubp.main(["apply"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "already in sync -- nothing to do" in out

    def test_applies_target_list_and_preserves_strict(self, monkeypatch, capsys):
        monkeypatch.setattr(ubp, "get_current", lambda *a, **kw: {"contexts": ["test"], "strict": False})
        calls = []
        monkeypatch.setattr(
            ubp,
            "apply_checks",
            lambda owner, repo, branch, checks, *, strict: calls.append((owner, repo, branch, checks, strict)),
        )
        exit_code = ubp.main(["apply"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "applied. new required status checks:" in out
        assert len(calls) == 1
        _owner, _repo, _branch, checks, strict = calls[0]
        assert checks == sorted(ubp.REQUIRED_STATUS_CHECKS)
        assert strict is False  # preserved from the existing protection rule, not overridden

    def test_applies_default_strict_true_when_no_existing_protection(self, monkeypatch):
        monkeypatch.setattr(ubp, "get_current", lambda *a, **kw: None)
        calls = []
        monkeypatch.setattr(
            ubp,
            "apply_checks",
            lambda owner, repo, branch, checks, *, strict: calls.append(strict),
        )
        ubp.main(["apply"])
        assert calls == [True]


class TestApplyChecksRequestShape:
    def test_patches_required_status_checks_subresource_only(self, monkeypatch):
        captured = {}

        def _fake_patch(url, headers, json, timeout):
            captured["url"] = url
            captured["json"] = json
            return _fake_response(json_body={})

        monkeypatch.setattr(ubp.requests, "patch", _fake_patch)
        ubp.apply_checks("privacyfence", "privacyfence", "main", ["b", "a"], strict=True)

        assert captured["url"] == "https://api.github.com/repos/privacyfence/privacyfence/branches/main/protection/required_status_checks"
        assert captured["json"] == {"strict": True, "contexts": ["a", "b"]}


def test_missing_token_raises_clear_error(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="GITHUB_TOKEN is required"):
        ubp._headers()
