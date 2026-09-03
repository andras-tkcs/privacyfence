"""Cross-cutting P6 exit criterion (docs/https-connector-refactor-plan.md
§12: "Two principals isolated in tests; local mode byte-identical to
before"): every per-principal registry de-singletoned in this phase --
auto_accept, audit_log, pii_detector, privacy_filter, resource_names --
actually isolates two principals from each other, and the local principal's
own behavior is unchanged from calling the same accessor with no
principal_scope() at all.

Each module already has its own focused unit tests (test_auto_accept.py,
test_audit_log.py, test_pii_detector.py, test_privacy_filter.py,
test_resource_names.py) covering its own logic in depth; this file only
covers the one thing none of those, individually, can: that two different
principals never see each other's state.
"""
from __future__ import annotations

import json

import yaml

from privacyfence import audit_log, auto_accept, paths, pii_detector, privacy_filter, resource_names
from privacyfence.audit_log import AuditEntry, current_week
from privacyfence.principal import LOCAL_PRINCIPAL, Principal, current_principal, principal_scope
from privacyfence.resource_grants import resource_type


def _settings_path(tmp_path, name: str) -> str:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump({"auto_accept_rules": {}, "auto_accept_grants": {}}))
    return str(path)


class TestAutoAcceptIsolation:
    def test_two_principals_get_independent_evaluators_and_rules(self, tmp_path):
        alice, bob = Principal(id="alice"), Principal(id="bob")

        with principal_scope(alice):
            auto_accept.init_config_path(_settings_path(tmp_path, "alice"))
            auto_accept.add_auto_accept_rule("gmail.send", "always_to_domain", "example.com")
            alice_cfg = auto_accept.get_current_config()

        with principal_scope(bob):
            auto_accept.init_config_path(_settings_path(tmp_path, "bob"))
            bob_cfg = auto_accept.get_current_config()

        assert alice_cfg["auto_accept_rules"]  # alice's rule is there
        assert bob_cfg["auto_accept_rules"] == {}  # bob never saw it

    def test_two_principals_get_different_evaluator_instances(self):
        alice, bob = Principal(id="alice"), Principal(id="bob")
        with principal_scope(alice):
            alice_eval = auto_accept.get_auto_accept_evaluator()
        with principal_scope(bob):
            bob_eval = auto_accept.get_auto_accept_evaluator()
        assert alice_eval is not bob_eval

    def test_rules_changed_listeners_do_not_cross_principals(self):
        alice, bob = Principal(id="alice"), Principal(id="bob")
        fired = {"alice": 0, "bob": 0}

        with principal_scope(alice):
            auto_accept.add_rules_changed_listener(lambda: fired.__setitem__("alice", fired["alice"] + 1))
            auto_accept.reload_rules({})
        with principal_scope(bob):
            auto_accept.add_rules_changed_listener(lambda: fired.__setitem__("bob", fired["bob"] + 1))
            auto_accept.reload_rules({})
            auto_accept.reload_rules({})

        assert fired == {"alice": 1, "bob": 2}

    def test_local_mode_is_byte_identical_to_no_scope_at_all(self):
        # The whole point of default=LOCAL_PRINCIPAL (principal.py): calling
        # an accessor with no principal_scope() open must be indistinguishable
        # from calling it inside principal_scope(LOCAL_PRINCIPAL).
        outside = auto_accept.get_auto_accept_evaluator()
        with principal_scope(LOCAL_PRINCIPAL):
            inside = auto_accept.get_auto_accept_evaluator()
        assert outside is inside


class TestAuditLogIsolation:
    def test_two_principals_write_to_different_log_directories(self, tmp_path):
        alice, bob = Principal(id="alice"), Principal(id="bob")
        alice_dir = tmp_path / "alice-audit"
        bob_dir = tmp_path / "bob-audit"

        entry = AuditEntry(
            timestamp="2026-01-01T00:00:00+00:00", week=current_week(), request_id="r1",
            connector="gmail", tool="gmail_send_message", tool_name="", summary="s", sender="",
            decision="approved", auto_accept_rule="", latency_seconds=0.1,
        )

        with principal_scope(alice):
            audit_log.init_audit_logger(str(alice_dir))
            audit_log.get_audit_logger().record(entry)
        with principal_scope(bob):
            audit_log.init_audit_logger(str(bob_dir))

        alice_file = alice_dir / f"{current_week()}.jsonl"
        bob_file = bob_dir / f"{current_week()}.jsonl"
        assert alice_file.exists()
        assert not bob_file.exists()

    def test_two_principals_get_different_logger_instances(self):
        alice, bob = Principal(id="alice"), Principal(id="bob")
        with principal_scope(alice):
            alice_logger = audit_log.get_audit_logger()
        with principal_scope(bob):
            bob_logger = audit_log.get_audit_logger()
        assert alice_logger is not bob_logger


class TestPiiDetectorIsolation:
    def test_two_principals_get_independent_enabled_flags(self):
        alice, bob = Principal(id="alice"), Principal(id="bob")

        with principal_scope(alice):
            pii_detector.init_pii_detection(enabled=False)
        with principal_scope(bob):
            pii_detector.init_pii_detection(enabled=True)

        with principal_scope(alice):
            assert pii_detector.is_pii_detection_enabled() is False
        with principal_scope(bob):
            assert pii_detector.is_pii_detection_enabled() is True

    def test_two_principals_get_independent_disabled_categories(self):
        alice, bob = Principal(id="alice"), Principal(id="bob")

        with principal_scope(alice):
            pii_detector.init_pii_detection(enabled=True, detect_ip_addresses=False)
            alice_categories = pii_detector.detect_pii_categories("connect to 10.0.0.1 please")
        with principal_scope(bob):
            pii_detector.init_pii_detection(enabled=True, detect_ip_addresses=True)
            bob_categories = pii_detector.detect_pii_categories("connect to 10.0.0.1 please")

        assert "IP address" not in alice_categories
        assert "IP address" in bob_categories

    def test_local_mode_is_byte_identical_to_no_scope_at_all(self):
        pii_detector.init_pii_detection(enabled=False)
        outside = pii_detector.is_pii_detection_enabled()
        with principal_scope(LOCAL_PRINCIPAL):
            inside = pii_detector.is_pii_detection_enabled()
        assert outside is inside is False


class TestPrivacyFilterIsolation:
    def test_two_principals_get_independent_policies(self):
        alice, bob = Principal(id="alice"), Principal(id="bob")

        with principal_scope(alice):
            privacy_filter.init_privacy_filter({"privacy": {"default_policy": "block"}})
        with principal_scope(bob):
            privacy_filter.init_privacy_filter({"privacy": {"default_policy": "allow"}})

        with principal_scope(alice):
            assert privacy_filter.category_policy("privacy", "body") == "block"
            assert privacy_filter.apply_text("privacy", "body", "secret") == "[BLOCKED BY PRIVACY FILTER]"
        with principal_scope(bob):
            assert privacy_filter.category_policy("privacy", "body") == "allow"
            assert privacy_filter.apply_text("privacy", "body", "secret") == "secret"

    def test_local_mode_is_byte_identical_to_no_scope_at_all(self):
        privacy_filter.init_privacy_filter({"privacy": {"default_policy": "block"}})
        outside = privacy_filter.category_policy("privacy", "body")
        with principal_scope(LOCAL_PRINCIPAL):
            inside = privacy_filter.category_policy("privacy", "body")
        assert outside == inside == "block"


class _FakeClient:
    def get_file_metadata(self, resource_id: str):
        from types import SimpleNamespace
        return SimpleNamespace(name="Q3 Reports")


class TestResourceNamesIsolation:
    def test_two_principals_get_different_resolver_instances_and_cache_files(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        alice, bob = Principal(id="alice"), Principal(id="bob")

        rt = resource_type("drive", "folders")
        assert rt is not None

        with principal_scope(alice):
            alice_resolver = resource_names.get_resolver()
            alice_resolver.resolve(rt, "F1", client=_FakeClient())
        with principal_scope(bob):
            bob_resolver = resource_names.get_resolver()
            bob_cached = bob_resolver.cached_name(rt, "F1")

        assert alice_resolver is not bob_resolver
        assert bob_cached is None  # bob's own cache never saw alice's resolution

        alice_cache = json.loads((tmp_path / "users" / "alice" / "resource_name_cache.json").read_text())
        assert alice_cache  # alice's own on-disk cache got the write
        assert not (tmp_path / "users" / "bob" / "resource_name_cache.json").exists()


def test_default_principal_is_local_and_stable_across_calls():
    assert current_principal() == LOCAL_PRINCIPAL
    assert current_principal() == LOCAL_PRINCIPAL  # not a fresh object each time in some odd way
