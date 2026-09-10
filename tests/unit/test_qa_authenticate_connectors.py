"""Tests for scripts/qa_authenticate_connectors.py's pure logic --
resolve_steps() (the --only group/connector expansion) and
install_org_config() (the backup-then-copy behavior). Nothing here invokes
run_step(): that spawns a real privacyfence-app OAuth flow and opens a
browser, which is exactly what qa_fixture_recorder.py's own module
docstring says never belongs in an automated test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import qa_authenticate_connectors as qa_auth  # noqa: E402


def test_resolve_steps_default_is_full_phase_a_order():
    steps = qa_auth.resolve_steps(None)
    assert [s.connector for s in steps] == [
        "gmail", "drive", "calendar", "contacts", "tasks", "apps_script",
        "slack", "atlassian", "salesforce",
    ]


def test_resolve_steps_group_name_expands_to_its_connectors():
    steps = qa_auth.resolve_steps(["google"])
    assert [s.connector for s in steps] == [
        "gmail", "drive", "calendar", "contacts", "tasks", "apps_script",
    ]


def test_resolve_steps_individual_connector_name():
    steps = qa_auth.resolve_steps(["slack", "salesforce"])
    assert [s.connector for s in steps] == ["slack", "salesforce"]


def test_resolve_steps_preserves_phase_a_order_regardless_of_only_order():
    steps = qa_auth.resolve_steps(["salesforce", "google"])
    assert [s.connector for s in steps] == [
        "gmail", "drive", "calendar", "contacts", "tasks", "apps_script", "salesforce",
    ]


def test_resolve_steps_mixed_group_and_connector_dedupes():
    steps = qa_auth.resolve_steps(["google", "gmail"])
    assert [s.connector for s in steps] == [
        "gmail", "drive", "calendar", "contacts", "tasks", "apps_script",
    ]


def test_resolve_steps_rejects_unknown_name():
    with pytest.raises(ValueError, match="telegram"):
        qa_auth.resolve_steps(["telegram"])


def test_telegram_is_never_in_the_default_step_list():
    assert "telegram" not in qa_auth.CONNECTOR_NAMES
    assert all(s.connector != "telegram" for s in qa_auth.STEPS)


def test_install_org_config_copies_bundle_into_place(tmp_path, monkeypatch):
    target = tmp_path / "org" / "org_config.json"
    monkeypatch.setattr(qa_auth, "DEFAULT_ORG_CONFIG_PATH", target)
    source = tmp_path / "org_config.qa.json"
    source.write_text('{"google": {}}', encoding="utf-8")

    qa_auth.install_org_config(source)

    assert target.read_text(encoding="utf-8") == '{"google": {}}'
    assert not list(target.parent.glob("org_config.json.bak.*"))


def test_install_org_config_backs_up_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "org" / "org_config.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"google": {"old": true}}', encoding="utf-8")
    monkeypatch.setattr(qa_auth, "DEFAULT_ORG_CONFIG_PATH", target)
    source = tmp_path / "org_config.qa.json"
    source.write_text('{"google": {"new": true}}', encoding="utf-8")

    qa_auth.install_org_config(source)

    assert target.read_text(encoding="utf-8") == '{"google": {"new": true}}'
    backups = list(target.parent.glob("org_config.json.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"google": {"old": true}}'


def test_install_org_config_missing_source_raises(tmp_path, monkeypatch):
    target = tmp_path / "org" / "org_config.json"
    monkeypatch.setattr(qa_auth, "DEFAULT_ORG_CONFIG_PATH", target)

    with pytest.raises(FileNotFoundError):
        qa_auth.install_org_config(tmp_path / "does_not_exist.json")
