"""Unit tests for privacyfence.resource_names.

Name resolution is purely cosmetic (the menu bar's display layer) -- these
tests focus on the caching/fallback contract: resolution never blocks or
fails loudly, a fresh resolve is cached for reuse, and a failed live lookup
falls back to the last-known name rather than losing it.
"""
from __future__ import annotations

from privacyfence import resource_grants as rg
from privacyfence import resource_names as rn


def _folder_type() -> rg.GrantResourceType:
    rt = rg.resource_type("drive", "folders")
    assert rt is not None
    return rt


class _FakeClient:
    def __init__(self, names: dict[str, str]):
        self._names = names
        self.calls = 0

    def get_file_metadata(self, resource_id: str):
        self.calls += 1
        from types import SimpleNamespace
        return SimpleNamespace(name=self._names.get(resource_id, ""))


class _FailingClient:
    def get_file_metadata(self, resource_id: str):
        raise RuntimeError("simulated API failure")


class TestResolve:
    def test_resolves_and_caches_a_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        client = _FakeClient({"F1": "Q3 Reports"})

        name = resolver.resolve(_folder_type(), "F1", client)

        assert name == "Q3 Reports"
        assert client.calls == 1

    def test_second_resolve_within_ttl_does_not_call_the_client_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        client = _FakeClient({"F1": "Q3 Reports"})

        resolver.resolve(_folder_type(), "F1", client)
        resolver.resolve(_folder_type(), "F1", client)

        assert client.calls == 1

    def test_no_client_and_no_cache_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()

        assert resolver.resolve(_folder_type(), "UNKNOWN", None) is None

    def test_no_client_falls_back_to_previously_cached_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        resolver.resolve(_folder_type(), "F1", _FakeClient({"F1": "Q3 Reports"}))

        # Later call with no live connection -- should still know the name.
        assert resolver.resolve(_folder_type(), "F1", None) == "Q3 Reports"

    def test_failing_client_falls_back_to_cached_name_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        resolver.resolve(_folder_type(), "F1", _FakeClient({"F1": "Q3 Reports"}))

        name = resolver.resolve(_folder_type(), "F1", _FailingClient())

        assert name == "Q3 Reports"

    def test_failing_client_with_no_prior_cache_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()

        assert resolver.resolve(_folder_type(), "F1", _FailingClient()) is None

    def test_empty_name_from_resolver_is_treated_as_unresolved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()

        assert resolver.resolve(_folder_type(), "MISSING", _FakeClient({})) is None

    def test_different_resource_ids_are_cached_independently(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        client = _FakeClient({"F1": "Alpha", "F2": "Beta"})

        assert resolver.resolve(_folder_type(), "F1", client) == "Alpha"
        assert resolver.resolve(_folder_type(), "F2", client) == "Beta"


class TestCachedName:
    def test_cached_name_before_any_resolve_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()

        assert resolver.cached_name(_folder_type(), "F1") is None

    def test_cached_name_reflects_a_prior_successful_resolve(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        resolver.resolve(_folder_type(), "F1", _FakeClient({"F1": "Q3 Reports"}))

        assert resolver.cached_name(_folder_type(), "F1") == "Q3 Reports"


class TestDiskPersistence:
    def test_name_survives_across_resolver_instances_via_disk_cache(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(rn, "_cache_file", lambda: cache_file)

        first = rn.ResourceNameResolver()
        first.resolve(_folder_type(), "F1", _FakeClient({"F1": "Q3 Reports"}))

        # Simulate a daemon restart: brand new instance, no live client yet.
        second = rn.ResourceNameResolver()
        assert second.cached_name(_folder_type(), "F1") == "Q3 Reports"

    def test_corrupt_disk_cache_is_ignored_not_raised(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json{{{")
        monkeypatch.setattr(rn, "_cache_file", lambda: cache_file)

        resolver = rn.ResourceNameResolver()  # must not raise

        assert resolver.cached_name(_folder_type(), "F1") is None

    def test_missing_disk_cache_file_is_treated_as_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "does-not-exist.json")

        resolver = rn.ResourceNameResolver()

        assert resolver.cached_name(_folder_type(), "F1") is None


class TestGetResolverSingleton:
    def test_returns_the_same_instance_across_calls(self, monkeypatch):
        monkeypatch.setattr(rn, "_INSTANCE", None)
        first = rn.get_resolver()
        second = rn.get_resolver()
        assert first is second
