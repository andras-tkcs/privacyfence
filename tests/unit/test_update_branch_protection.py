"""Tests for scripts/update_branch_protection.py.

Imported by file path (importlib) rather than as a package -- same pattern as
tests/unit/test_r2_release.py, since scripts/ isn't part of the installed `privacyfence`
distribution. Every GitHub API call is mocked; these tests never make a real network request and
never require GITHUB_TOKEN to be set for anything other than the "missing token" case.

The fixtures below are shaped like real `GET /repos/{owner}/{repo}/rulesets/{id}` responses,
including the read-only fields (`id`, `_links`, `created_at`, ...) the update call rejects, so the
round-trip test actually proves those are stripped rather than assuming it.
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
    response.json.return_value = json_body if json_body is not None else {}
    if status_code >= 400:
        response.raise_for_status.side_effect = ubp.requests.HTTPError(f"{status_code} error")
    else:
        response.raise_for_status.return_value = None
    return response


def _ruleset(*, contexts=("test",), strict=True, include=("refs/heads/main",), enforcement="active", rid=1):
    """A ruleset detail body, with the sibling rules a real one carries so the round-trip test can
    prove they survive an update."""
    return {
        "id": rid,
        "name": "main",
        "target": "branch",
        "enforcement": enforcement,
        "node_id": "RRS_readonly",
        "source": "privacyfence/privacyfence",
        "source_type": "Repository",
        "created_at": "2026-09-09T14:15:15.915Z",
        "updated_at": "2026-09-13T05:15:39.575Z",
        "_links": {"self": {"href": "https://api.github.com/..."}},
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": list(include), "exclude": []}},
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": strict,
                    "do_not_enforce_on_create": True,
                    "required_status_checks": [
                        {"context": c, "integration_id": 15368} for c in contexts
                    ],
                },
            },
            {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
            {"type": "deletion"},
            {"type": "non_fast_forward"},
        ],
    }


def _wire(monkeypatch, *, listing, detail):
    """Point `requests.get` at a two-call sequence: the rulesets listing, then a detail fetch."""

    def _get(url, **kwargs):
        if url.endswith("/rulesets"):
            return _fake_response(json_body=listing)
        return _fake_response(json_body=detail)

    monkeypatch.setattr(ubp.requests, "get", _get)


class TestFindBranchRuleset:
    def test_returns_the_active_branch_ruleset_for_the_branch(self, monkeypatch):
        _wire(monkeypatch, listing=[{"id": 1, "target": "branch", "enforcement": "active"}], detail=_ruleset())
        found = ubp.find_branch_ruleset("privacyfence", "privacyfence", "main")
        assert found is not None and found["name"] == "main"

    def test_matches_the_default_branch_placeholder(self, monkeypatch):
        """A ruleset created via the UI's "Include default branch" stores ~DEFAULT_BRANCH, never
        the literal ref -- matching only `refs/heads/<branch>` would miss the common case."""
        _wire(
            monkeypatch,
            listing=[{"id": 1, "target": "branch", "enforcement": "active"}],
            detail=_ruleset(include=("~DEFAULT_BRANCH",)),
        )
        assert ubp.find_branch_ruleset("privacyfence", "privacyfence", "main") is not None

    def test_skips_a_disabled_ruleset(self, monkeypatch):
        monkeypatch.setattr(
            ubp.requests,
            "get",
            lambda url, **kw: _fake_response(json_body=[{"id": 1, "target": "branch", "enforcement": "disabled"}]),
        )
        assert ubp.find_branch_ruleset("privacyfence", "privacyfence", "main") is None

    def test_skips_a_ruleset_targeting_another_branch(self, monkeypatch):
        _wire(
            monkeypatch,
            listing=[{"id": 1, "target": "branch", "enforcement": "active"}],
            detail=_ruleset(include=("refs/heads/release",)),
        )
        assert ubp.find_branch_ruleset("privacyfence", "privacyfence", "main") is None

    def test_ignores_non_branch_targets(self, monkeypatch):
        monkeypatch.setattr(
            ubp.requests,
            "get",
            lambda url, **kw: _fake_response(json_body=[{"id": 9, "target": "tag", "enforcement": "active"}]),
        )
        assert ubp.find_branch_ruleset("privacyfence", "privacyfence", "main") is None

    def test_raises_on_error_status(self, monkeypatch):
        monkeypatch.setattr(ubp.requests, "get", lambda *a, **kw: _fake_response(status_code=500))
        with pytest.raises(ubp.requests.HTTPError):
            ubp.find_branch_ruleset("privacyfence", "privacyfence", "main")


class TestGetCurrent:
    def test_returns_none_when_no_ruleset_governs_the_branch(self, monkeypatch):
        monkeypatch.setattr(ubp.requests, "get", lambda *a, **kw: _fake_response(json_body=[]))
        assert ubp.get_current("privacyfence", "privacyfence", "main") is None

    def test_extracts_contexts_and_strict(self, monkeypatch):
        _wire(
            monkeypatch,
            listing=[{"id": 1, "target": "branch", "enforcement": "active"}],
            detail=_ruleset(contexts=("test", "static-analysis"), strict=False),
        )
        state = ubp.get_current("privacyfence", "privacyfence", "main")
        assert state["contexts"] == ["test", "static-analysis"]
        assert state["strict"] is False

    def test_ruleset_without_a_required_checks_rule_reads_as_empty(self, monkeypatch):
        """Distinct from "no ruleset at all": here there IS something to update, so `apply` should
        be able to add the rule rather than bailing out."""
        detail = _ruleset()
        detail["rules"] = [r for r in detail["rules"] if r["type"] != "required_status_checks"]
        _wire(monkeypatch, listing=[{"id": 1, "target": "branch", "enforcement": "active"}], detail=detail)
        state = ubp.get_current("privacyfence", "privacyfence", "main")
        assert state["contexts"] == []
        assert state["ruleset"]["name"] == "main"


class TestApplyChecks:
    def _captured_put(self, monkeypatch):
        captured = {}

        def _put(url, **kwargs):
            captured["url"] = url
            captured["body"] = kwargs["json"]
            return _fake_response()

        monkeypatch.setattr(ubp.requests, "put", _put)
        return captured

    def test_replaces_contexts_and_keeps_sibling_rules(self, monkeypatch):
        captured = self._captured_put(monkeypatch)
        ubp.apply_checks("o", "r", _ruleset(contexts=("old",)), ["test", "static-analysis"], strict=True)
        rules = {r["type"]: r for r in captured["body"]["rules"]}
        assert [c["context"] for c in rules["required_status_checks"]["parameters"]["required_status_checks"]] == [
            "static-analysis",
            "test",
        ]
        # The rules this script does not manage must survive the round trip untouched.
        assert set(rules) == {"required_status_checks", "pull_request", "deletion", "non_fast_forward"}
        assert rules["pull_request"]["parameters"]["required_approving_review_count"] == 0

    def test_strips_read_only_fields_from_the_update_body(self, monkeypatch):
        captured = self._captured_put(monkeypatch)
        ubp.apply_checks("o", "r", _ruleset(), ["test"], strict=True)
        assert not (ubp._READ_ONLY_RULESET_FIELDS & set(captured["body"]))
        # ...while the writable identity fields the PUT requires are still present.
        assert captured["body"]["name"] == "main"
        assert captured["body"]["target"] == "branch"
        assert captured["body"]["enforcement"] == "active"
        assert captured["body"]["conditions"]["ref_name"]["include"] == ["refs/heads/main"]

    def test_preserves_integration_id_for_known_contexts_only(self, monkeypatch):
        captured = self._captured_put(monkeypatch)
        ubp.apply_checks("o", "r", _ruleset(contexts=("test",)), ["test", "brand-new"], strict=True)
        checks = {
            c["context"]: c
            for c in captured["body"]["rules"][-1]["parameters"]["required_status_checks"]
        }
        assert checks["test"]["integration_id"] == 15368
        assert "integration_id" not in checks["brand-new"]

    def test_puts_to_the_ruleset_id(self, monkeypatch):
        captured = self._captured_put(monkeypatch)
        ubp.apply_checks("o", "r", _ruleset(rid=22647766), ["test"], strict=True)
        assert captured["url"].endswith("/repos/o/r/rulesets/22647766")

    def test_carries_strict_through(self, monkeypatch):
        captured = self._captured_put(monkeypatch)
        ubp.apply_checks("o", "r", _ruleset(strict=True), ["test"], strict=False)
        params = captured["body"]["rules"][-1]["parameters"]
        assert params["strict_required_status_checks_policy"] is False

    def test_raises_on_error_status(self, monkeypatch):
        monkeypatch.setattr(ubp.requests, "put", lambda *a, **kw: _fake_response(status_code=422))
        with pytest.raises(ubp.requests.HTTPError):
            ubp.apply_checks("o", "r", _ruleset(), ["test"], strict=True)


class TestMainShow:
    def test_reports_missing_checks(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ubp, "get_current", lambda *a, **kw: {"contexts": ["test"], "strict": True, "ruleset": _ruleset()}
        )
        assert ubp.main(["show"]) == 0
        out = capsys.readouterr().out
        assert "missing (would be added by `apply`):" in out
        assert "org-mode-smoke" in out

    def test_reports_already_in_sync(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ubp,
            "get_current",
            lambda *a, **kw: {"contexts": ubp.REQUIRED_STATUS_CHECKS, "strict": True, "ruleset": _ruleset()},
        )
        assert ubp.main(["show"]) == 0
        assert "already in sync" in capsys.readouterr().out

    def test_names_the_ruleset_it_read(self, monkeypatch, capsys):
        """The whole point of R7: make it impossible to mistake which resource was consulted."""
        monkeypatch.setattr(
            ubp,
            "get_current",
            lambda *a, **kw: {"contexts": [], "strict": True, "ruleset": _ruleset(rid=22647766)},
        )
        ubp.main(["show"])
        assert "ruleset: 'main' (id 22647766)" in capsys.readouterr().out

    def test_reports_extra_checks_not_in_target(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ubp,
            "get_current",
            lambda *a, **kw: {
                "contexts": [*ubp.REQUIRED_STATUS_CHECKS, "retired-job"],
                "strict": True,
                "ruleset": _ruleset(),
            },
        )
        assert ubp.main(["show"]) == 0
        assert "retired-job" in capsys.readouterr().out

    def test_no_ruleset_exits_nonzero_with_an_actionable_message(self, monkeypatch, capsys):
        monkeypatch.setattr(ubp, "get_current", lambda *a, **kw: None)
        assert ubp.main(["show"]) == 1
        err = capsys.readouterr().err
        assert "No active branch ruleset" in err
        # Names the classic-protection trap explicitly, since that is what made this a live bug.
        assert "classic branch protection" in err


class TestMainApply:
    def test_dry_run_does_not_call_apply_checks(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ubp, "get_current", lambda *a, **kw: {"contexts": ["test"], "strict": True, "ruleset": _ruleset()}
        )

        def _fail_if_called(*a, **kw):
            raise AssertionError("apply_checks should not be called in --dry-run mode")

        monkeypatch.setattr(ubp, "apply_checks", _fail_if_called)
        assert ubp.main(["apply", "--dry-run"]) == 0
        assert "(dry run -- not applied)" in capsys.readouterr().out

    def test_already_in_sync_skips_apply_call(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ubp,
            "get_current",
            lambda *a, **kw: {"contexts": ubp.REQUIRED_STATUS_CHECKS, "strict": True, "ruleset": _ruleset()},
        )

        def _fail_if_called(*a, **kw):
            raise AssertionError("apply_checks should not be called when already in sync")

        monkeypatch.setattr(ubp, "apply_checks", _fail_if_called)
        assert ubp.main(["apply"]) == 0
        assert "nothing to do" in capsys.readouterr().out

    def test_applies_target_list_and_preserves_strict(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ubp, "get_current", lambda *a, **kw: {"contexts": ["test"], "strict": False, "ruleset": _ruleset()}
        )
        seen = {}

        def _capture(owner, repo, ruleset, checks, *, strict):
            seen.update(ruleset=ruleset, checks=checks, strict=strict)

        monkeypatch.setattr(ubp, "apply_checks", _capture)
        assert ubp.main(["apply"]) == 0
        assert seen["checks"] == sorted(ubp.REQUIRED_STATUS_CHECKS)
        assert seen["strict"] is False
        assert seen["ruleset"]["name"] == "main"
        assert "applied." in capsys.readouterr().out

    def test_no_ruleset_exits_nonzero_without_applying(self, monkeypatch):
        monkeypatch.setattr(ubp, "get_current", lambda *a, **kw: None)

        def _fail_if_called(*a, **kw):
            raise AssertionError("apply_checks must not run when there is no ruleset to update")

        monkeypatch.setattr(ubp, "apply_checks", _fail_if_called)
        assert ubp.main(["apply"]) == 1


class TestHeaders:
    def test_missing_token_is_a_clear_failure(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(SystemExit, match="GITHUB_TOKEN is required"):
            ubp._headers()


class TestRequiredChecksList:
    def test_matches_the_per_pr_gating_jobs_in_tests_yml(self):
        """A rename in tests.yml that doesn't reach this list silently stops gating that job --
        the exact drift this script exists to prevent, so it is asserted rather than trusted."""
        workflow = (_SCRIPT_PATH.parents[1] / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for job in ("test", "platform-windows", "platform-macos", "static-analysis", "org-mode-smoke"):
            assert f"\n  {job}:" in workflow, f"{job} is required but no longer a job in tests.yml"
        assert "python-version: ['3.11', '3.12']" in workflow, (
            "the test-python-compat matrix changed -- update the two 'Test (Python X, core suite)' entries"
        )
