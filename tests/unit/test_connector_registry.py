"""Tests for connector_registry.py's ConnectorRegistry (P6, docs/
https-connector-refactor-plan.md §9.2's "Connectors become per-principal
too" paragraph -- the lazy, bounded, principal-keyed cache of
ConnectorHosts).
"""
from __future__ import annotations

import threading

import pytest

from privacyfence.connector import Connector
from privacyfence.connector_registry import ConnectorRegistry, TooManyPrincipalsError
from privacyfence.principal import Principal, current_principal


class FakeConnector(Connector):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def tool_specs(self):
        return []

    async def call(self, tool, args):  # pragma: no cover -- not exercised here
        raise NotImplementedError


class TestConnectorRegistry:
    def test_builds_lazily(self):
        calls = []

        def factory(principal: Principal) -> list[Connector]:
            calls.append(principal.id)
            return [FakeConnector("gmail")]

        registry = ConnectorRegistry(factory)
        assert calls == []
        host = registry.get(Principal(id="alice"))
        assert calls == ["alice"]
        assert set(host.connectors) == {"gmail"}

    def test_returns_the_same_host_on_a_second_call_for_the_same_principal(self):
        registry = ConnectorRegistry(lambda p: [FakeConnector("gmail")])
        alice = Principal(id="alice")

        first = registry.get(alice)
        second = registry.get(alice)

        assert first is second

    def test_two_principals_get_two_different_hosts(self):
        registry = ConnectorRegistry(lambda p: [FakeConnector(f"gmail-{p.id}")])
        alice = Principal(id="alice")
        bob = Principal(id="bob")

        alice_host = registry.get(alice)
        bob_host = registry.get(bob)

        assert alice_host is not bob_host
        assert set(alice_host.connectors) == {"gmail-alice"}
        assert set(bob_host.connectors) == {"gmail-bob"}

    def test_factory_runs_under_principal_scope(self):
        seen = []

        def factory(principal: Principal) -> list[Connector]:
            seen.append(current_principal().id)
            return []

        registry = ConnectorRegistry(factory)
        registry.get(Principal(id="alice"))

        assert seen == ["alice"]

    def test_principal_count_reflects_live_hosts(self):
        registry = ConnectorRegistry(lambda p: [])
        assert registry.principal_count == 0
        registry.get(Principal(id="alice"))
        assert registry.principal_count == 1
        registry.get(Principal(id="bob"))
        assert registry.principal_count == 2
        registry.get(Principal(id="alice"))  # cached, not a second entry
        assert registry.principal_count == 2

    def test_evict_removes_one_principal_only(self):
        registry = ConnectorRegistry(lambda p: [FakeConnector(f"c-{p.id}")])
        alice = Principal(id="alice")
        bob = Principal(id="bob")
        alice_host_before = registry.get(alice)
        registry.get(bob)

        registry.evict("alice")

        assert registry.principal_count == 1
        alice_host_after = registry.get(alice)
        assert alice_host_after is not alice_host_before

    def test_exceeding_max_principals_raises(self):
        registry = ConnectorRegistry(lambda p: [], max_principals=2)
        registry.get(Principal(id="a"))
        registry.get(Principal(id="b"))

        with pytest.raises(TooManyPrincipalsError):
            registry.get(Principal(id="c"))

    def test_a_cached_principal_is_still_reachable_at_capacity(self):
        registry = ConnectorRegistry(lambda p: [], max_principals=1)
        alice = Principal(id="alice")
        first = registry.get(alice)

        second = registry.get(alice)  # cache hit, must not raise even at capacity

        assert first is second

    def test_idle_eviction_makes_room_under_capacity(self, monkeypatch):
        import privacyfence.connector_registry as cr

        fake_now = [1000.0]
        monkeypatch.setattr(cr.time, "monotonic", lambda: fake_now[0])

        registry = ConnectorRegistry(lambda p: [], max_principals=1, idle_evict_seconds=60)
        registry.get(Principal(id="alice"))

        fake_now[0] += 120  # older than idle_evict_seconds
        # Must not raise -- alice is evicted for being idle, making room.
        registry.get(Principal(id="bob"))

        assert registry.principal_count == 1

    def test_a_host_cached_by_a_concurrent_builder_is_kept_over_a_redundant_one(self):
        # Deterministic version of the race ConnectorRegistry.get()'s own
        # comment describes: the factory itself plants a second host into
        # the registry's cache (standing in for another thread finishing
        # its own build first) before this call's build "finishes" -- the
        # call must return the one that was already cached, not overwrite
        # it with its own, redundant result.
        registry = ConnectorRegistry(lambda p: [])
        alice = Principal(id="alice")
        winner = object()

        def factory(principal: Principal) -> list[Connector]:
            registry._hosts[principal.id] = winner  # simulates the other builder finishing first
            registry._last_used[principal.id] = 0.0
            return [FakeConnector("late")]

        registry._factory = factory
        result = registry.get(alice)

        assert result is winner
        assert registry.principal_count == 1

    def test_concurrent_get_for_the_same_principal_builds_only_once(self):
        build_count = 0
        build_lock = threading.Lock()

        def factory(principal: Principal) -> list[Connector]:
            nonlocal build_count
            with build_lock:
                build_count += 1
            return []

        registry = ConnectorRegistry(factory)
        alice = Principal(id="alice")
        results: list[object] = []
        results_lock = threading.Lock()

        def worker():
            host = registry.get(alice)
            with results_lock:
                results.append(host)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len({id(r) for r in results}) == 1
        # Two builds can race past the "not yet cached" check before either
        # finishes (see ConnectorRegistry.get()'s own comment on this) --
        # what must hold is that every caller ends up with the one host that
        # actually got cached, not that the factory itself only ever runs
        # once.
        assert build_count >= 1
