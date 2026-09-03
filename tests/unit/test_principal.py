"""Tests for principal.py: Principal identity, principal_scope, and the
PrincipalRegistry de-singleton-ing helper (P6, docs/
https-connector-refactor-plan.md §9.1-§9.2).
"""
from __future__ import annotations

import threading

import pytest

from privacyfence.principal import (
    LOCAL_PRINCIPAL,
    LOCAL_PRINCIPAL_ID,
    Principal,
    PrincipalRegistry,
    current_principal,
    principal_scope,
)


class TestCurrentPrincipal:
    def test_defaults_to_local_principal_with_no_scope_entered(self):
        assert current_principal() == LOCAL_PRINCIPAL
        assert current_principal().id == LOCAL_PRINCIPAL_ID

    def test_scope_changes_current_principal_for_its_duration(self):
        alice = Principal(id="alice")
        assert current_principal() != alice
        with principal_scope(alice):
            assert current_principal() == alice
        assert current_principal() == LOCAL_PRINCIPAL

    def test_scope_restores_previous_principal_on_exception(self):
        alice = Principal(id="alice")
        with pytest.raises(RuntimeError):
            with principal_scope(alice):
                assert current_principal() == alice
                raise RuntimeError("boom")
        assert current_principal() == LOCAL_PRINCIPAL

    def test_scopes_nest(self):
        alice = Principal(id="alice")
        bob = Principal(id="bob")
        with principal_scope(alice):
            assert current_principal() == alice
            with principal_scope(bob):
                assert current_principal() == bob
            assert current_principal() == alice
        assert current_principal() == LOCAL_PRINCIPAL


class TestPrincipal:
    def test_email_and_display_name_default_to_empty(self):
        p = Principal(id="alice")
        assert p.email == ""
        assert p.display_name == ""

    def test_frozen(self):
        p = Principal(id="alice")
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            p.id = "bob"  # type: ignore[misc]

    def test_equality_is_by_value(self):
        assert Principal(id="alice") == Principal(id="alice")
        assert Principal(id="alice") != Principal(id="bob")


class TestPrincipalRegistry:
    def test_builds_lazily_via_factory(self):
        built = []

        def factory():
            built.append(1)
            return object()

        registry: PrincipalRegistry[object] = PrincipalRegistry(factory)
        assert built == []
        registry.get()
        assert built == [1]

    def test_returns_the_same_instance_for_the_same_principal(self):
        registry: PrincipalRegistry[object] = PrincipalRegistry(object)
        first = registry.get()
        second = registry.get()
        assert first is second

    def test_two_principals_get_two_different_instances(self):
        registry: PrincipalRegistry[object] = PrincipalRegistry(object)
        alice = Principal(id="alice")
        bob = Principal(id="bob")

        with principal_scope(alice):
            alice_instance = registry.get()
        with principal_scope(bob):
            bob_instance = registry.get()

        assert alice_instance is not bob_instance

    def test_a_principal_gets_the_same_instance_across_separate_scopes(self):
        registry: PrincipalRegistry[object] = PrincipalRegistry(object)
        alice = Principal(id="alice")

        with principal_scope(alice):
            first = registry.get()
        with principal_scope(alice):
            second = registry.get()

        assert first is second

    def test_set_installs_an_explicit_instance_for_the_current_principal(self):
        registry: PrincipalRegistry[object] = PrincipalRegistry(object)
        explicit = object()
        registry.set(explicit)
        assert registry.get() is explicit

    def test_set_does_not_affect_other_principals(self):
        registry: PrincipalRegistry[object] = PrincipalRegistry(object)
        alice = Principal(id="alice")
        bob = Principal(id="bob")

        with principal_scope(alice):
            registry.set(object())
            alice_instance = registry.get()
        with principal_scope(bob):
            bob_instance = registry.get()

        assert alice_instance is not bob_instance

    def test_reset_clears_every_principal(self):
        registry: PrincipalRegistry[object] = PrincipalRegistry(object)
        alice = Principal(id="alice")

        with principal_scope(alice):
            before = registry.get()
        registry.reset()
        with principal_scope(alice):
            after = registry.get()

        assert before is not after

    def test_discard_evicts_only_the_named_principal(self):
        registry: PrincipalRegistry[object] = PrincipalRegistry(object)
        alice = Principal(id="alice")
        bob = Principal(id="bob")

        with principal_scope(alice):
            alice_before = registry.get()
        with principal_scope(bob):
            bob_before = registry.get()

        registry.discard("alice")

        with principal_scope(alice):
            alice_after = registry.get()
        with principal_scope(bob):
            bob_after = registry.get()

        assert alice_after is not alice_before
        assert bob_after is bob_before

    def test_discard_defaults_to_the_current_principal(self):
        registry: PrincipalRegistry[object] = PrincipalRegistry(object)
        alice = Principal(id="alice")

        with principal_scope(alice):
            before = registry.get()
            registry.discard()
            after = registry.get()

        assert before is not after

    def test_factory_can_see_which_principal_it_is_building_for(self):
        seen = []

        def factory():
            seen.append(current_principal().id)
            return object()

        registry: PrincipalRegistry[object] = PrincipalRegistry(factory)
        with principal_scope(Principal(id="alice")):
            registry.get()

        assert seen == ["alice"]

    def test_concurrent_get_from_multiple_threads_builds_exactly_once(self):
        build_count = 0
        build_lock = threading.Lock()

        def slow_factory():
            nonlocal build_count
            with build_lock:
                build_count += 1
            return object()

        registry: PrincipalRegistry[object] = PrincipalRegistry(slow_factory)
        results: list[object] = []
        results_lock = threading.Lock()

        def worker():
            instance = registry.get()
            with results_lock:
                results.append(instance)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert build_count == 1
        assert len({id(r) for r in results}) == 1
