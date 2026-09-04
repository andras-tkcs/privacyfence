"""Unit tests for privacyfence.approvals.PendingApprovalRegistry -- the
deferred-approval protocol's domain object (docs/https-connector-refactor-
plan.md §5-§6). See that module's own docstring for the two-layer
answer()/finalize() design these tests exercise directly, without gate.py's
own orchestration in the way.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from privacyfence.approvals import (
    PendingApprovalRegistry,
    TooManyPendingApprovalsError,
    canonical_key,
)
from privacyfence.principal import Principal, principal_scope


def make_registry(**overrides) -> PendingApprovalRegistry:
    kwargs = dict(hold_window=1.0, pending_ttl=1.0, ledger_ttl=1.0, max_pending=10)
    kwargs.update(overrides)
    return PendingApprovalRegistry(**kwargs)


class TestCanonicalKey:
    def test_same_args_produce_the_same_key_regardless_of_order(self):
        a = canonical_key("gmail", "gmail_get_message", {"id": "1", "x": "y"})
        b = canonical_key("gmail", "gmail_get_message", {"x": "y", "id": "1"})
        assert a == b

    def test_different_args_produce_different_keys(self):
        a = canonical_key("gmail", "gmail_get_message", {"id": "1"})
        b = canonical_key("gmail", "gmail_get_message", {"id": "2"})
        assert a != b

    def test_none_args_is_the_same_as_empty_dict(self):
        assert canonical_key("gmail", "gmail_get_message", None) == canonical_key(
            "gmail", "gmail_get_message", {}
        )


class TestRegisterOrCoalesce:
    def test_first_registration_creates_a_new_approval(self):
        registry = make_registry()
        approval, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        assert created is True
        assert approval.kind == "card"
        assert approval.dedupe_key == "k1"

    def test_second_call_with_the_same_key_coalesces_onto_the_first(self):
        registry = make_registry()
        first, created1 = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        second, created2 = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r2",
        )
        assert created2 is False
        assert second is first
        assert len(registry.list_pending()) == 1

    def test_a_different_key_gets_its_own_approval(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        second, created2 = registry.register_or_coalesce(
            dedupe_key="k2", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r2",
        )
        assert created2 is True
        assert second is not first
        assert len(registry.list_pending()) == 2

    def test_a_finalized_approval_does_not_block_a_fresh_registration_for_the_same_key(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        registry.finalize(first.id, "deny")
        second, created2 = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r2",
        )
        assert created2 is True
        assert second is not first

    def test_cap_is_enforced_for_genuinely_new_keys(self):
        registry = make_registry(max_pending=2)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.register_or_coalesce(
            dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
        )
        with pytest.raises(TooManyPendingApprovalsError):
            registry.register_or_coalesce(
                dedupe_key="k3", connector="c", tool="t", gate_kind="review", request_id="r3",
            )

    def test_cap_is_not_charged_against_a_coalescing_hit(self):
        registry = make_registry(max_pending=1)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        # Same key again -- coalesces, does not count as a second "new" entry.
        _, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r2",
        )
        assert created is False


class TestAnswerVsFinalize:
    def test_answer_resolves_the_ui_step_only_not_the_whole_approval(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.answer(approval.id, "accept") is True
        assert approval.event.is_set()
        assert not approval.is_finalized()

    def test_finalize_writes_the_ledger_and_wakes_waiters(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.finalize(approval.id, "accept", "some_rule") is True
        assert approval.is_finalized()
        hit = registry.consume_ledger("k1")
        assert hit == ("accept", "some_rule", approval.decided_at)

    def test_answer_is_idempotent_first_wins(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.answer(approval.id, "accept") is True
        assert registry.answer(approval.id, "deny") is False
        assert approval.result == "accept"

    def test_finalize_is_idempotent_first_wins(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.finalize(approval.id, "accept") is True
        assert registry.finalize(approval.id, "deny") is False
        assert approval.final_decision == "accept"

    def test_answer_and_finalize_on_an_unknown_id_return_false(self):
        registry = make_registry()
        assert registry.answer("nope", "accept") is False
        assert registry.finalize("nope", "accept") is False


class TestLedgerSingleUse:
    def test_review_gate_ledger_entry_is_reusable(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        first = registry.consume_ledger("k1")
        second = registry.consume_ledger("k1")
        assert first is not None
        assert second == first

    def test_popup_gate_ledger_entry_is_single_use(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        first = registry.consume_ledger("k1")
        second = registry.consume_ledger("k1")
        assert first is not None
        assert second is None

    def test_consuming_a_single_use_entry_frees_the_key_for_a_fresh_registration(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        registry.consume_ledger("k1")
        fresh, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r2",
        )
        assert created is True
        assert fresh is not approval

    def test_unfinalized_approval_has_no_ledger_entry_yet(self):
        registry = make_registry()
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.consume_ledger("k1") is None


class TestHoldWindow:
    async def test_wait_async_returns_true_once_finalized_within_the_window(self):
        registry = make_registry(hold_window=1.0)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        assert await registry.wait_async(approval, 1.0) is True

    async def test_wait_async_returns_false_when_nothing_decides_in_time(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert await registry.wait_async(approval, 0.05) is False
        assert not approval.is_finalized()


class TestPendingTTLExpiry:
    def test_unanswered_approval_past_its_ttl_is_reported_as_expired(self):
        registry = make_registry(pending_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        time.sleep(0.02)
        expired = registry.pop_expired_events()
        assert [a.id for a in expired] == [approval.id]
        assert approval.final_decision == "expired"
        assert approval.is_finalized()

    def test_expiry_frees_the_dedupe_key_for_a_fresh_registration(self):
        registry = make_registry(pending_ttl=0.01)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        time.sleep(0.02)
        registry.pop_expired_events()
        fresh, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r2",
        )
        assert created is True

    def test_a_not_yet_expired_approval_is_not_reported(self):
        registry = make_registry(pending_ttl=5.0)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.pop_expired_events() == []

    def test_a_finalized_approval_is_never_reported_as_pending_expired(self):
        registry = make_registry(pending_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        time.sleep(0.02)
        assert registry.pop_expired_events() == []


class TestLedgerTTLExpiry:
    def test_a_decided_but_never_reclaimed_entry_is_reported_once_the_ledger_ttl_lapses(self):
        registry = make_registry(ledger_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        time.sleep(0.02)
        events = registry.pop_expired_ledger_events()
        assert [a.id for a in events] == [approval.id]
        # Not consumable anymore -- either via the ledger (it just expired)
        # or by finding it in the registry at all (swept on report).
        assert registry.consume_ledger("k1") is None
        assert registry.get(approval.id) is None

    def test_a_reclaimed_entry_is_never_reported_as_a_ledger_expiry(self):
        registry = make_registry(ledger_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        registry.consume_ledger("k1")  # reclaimed (and popped -- single-use)
        time.sleep(0.02)
        assert registry.pop_expired_ledger_events() == []


class TestReevaluateAll:
    def test_a_pending_card_covered_by_a_new_rule_is_auto_accepted(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", review_ctx=object(),
        )

        def should_auto_accept(operation_key, ctx):
            return True, "trusted_sender_domain"

        resolved = registry.reevaluate_all(should_auto_accept)

        assert [a.id for a in resolved] == [approval.id]
        assert approval.final_decision == "auto_accepted"
        assert approval.final_rule_name == "trusted_sender_domain"
        assert approval.is_finalized()

    def test_a_card_not_covered_by_any_rule_is_left_alone(self):
        registry = make_registry()
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", review_ctx=object(),
        )

        resolved = registry.reevaluate_all(lambda op, ctx: (False, ""))

        assert resolved == []

    def test_a_pii_forced_card_is_never_auto_resolved_even_if_a_rule_would_match(self):
        registry = make_registry()
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", review_ctx=object(), pii_forces_confirmation=True,
        )

        resolved = registry.reevaluate_all(lambda op, ctx: (True, "trusted_sender_domain"))

        assert resolved == []

    def test_an_already_answered_card_is_not_reevaluated(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", review_ctx=object(),
        )
        registry.answer(approval.id, "deny")  # a human already clicked

        resolved = registry.reevaluate_all(lambda op, ctx: (True, "trusted_sender_domain"))

        assert resolved == []

    def test_a_confirm_dialog_with_no_operation_key_is_never_reevaluated(self):
        registry = make_registry()
        registry.register_confirm()

        resolved = registry.reevaluate_all(lambda op, ctx: (True, "some_rule"))

        assert resolved == []


class TestApprovalUrl:
    def test_no_base_url_configured_returns_none(self):
        registry = make_registry()
        assert registry.approval_url("abc123") is None

    def test_base_url_is_used_once_set(self):
        registry = make_registry()
        registry.set_base_url("http://localhost:8765")
        assert registry.approval_url("abc123") == "http://localhost:8765/approvals/abc123"


class TestAwaitStatus:
    def test_unknown_id_is_unknown(self):
        registry = make_registry()
        assert registry.await_status("nope") == "unknown"

    def test_unanswered_is_pending(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.await_status(approval.id) == "pending"

    def test_finalized_accept_is_approved(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        assert registry.await_status(approval.id) == "approved"

    def test_finalized_accept_all_is_approved(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept_all", "some_rule")
        assert registry.await_status(approval.id) == "approved"

    def test_finalized_deny_is_denied(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "deny")
        assert registry.await_status(approval.id) == "denied"

    def test_finalized_expired_is_expired(self):
        registry = make_registry(pending_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        time.sleep(0.02)
        registry.pop_expired_events()
        assert registry.await_status(approval.id) == "expired"

    def test_status_never_leaks_content(self):
        # Structural check on the contract: await_status's return type is a
        # plain str enum member, never the approval's own html/summary/etc.
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            summary="a very secret subject line",
        )
        registry.finalize(approval.id, "accept")
        status = registry.await_status(approval.id)
        assert status == "approved"
        assert "secret" not in status


class TestRegisterConfirm:
    def test_creates_a_confirm_kind_entry_with_no_dedupe_key(self):
        registry = make_registry()
        approval = registry.register_confirm()
        assert approval.kind == "confirm"
        assert approval.dedupe_key is None
        assert approval in registry.list_pending()

    def test_two_confirms_never_coalesce(self):
        registry = make_registry()
        first = registry.register_confirm()
        second = registry.register_confirm()
        assert first.id != second.id
        assert len(registry.list_pending()) == 2

    def test_does_not_count_against_the_pending_cap(self):
        registry = make_registry(max_pending=1)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        # Would raise if this were charged against the same cap as cards.
        registry.register_confirm()


class TestListPendingAndGet:
    def test_list_pending_excludes_answered_cards(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert approval in registry.list_pending()
        registry.answer(approval.id, "accept")
        assert approval not in registry.list_pending()

    def test_list_pending_is_newest_first(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        first.created_at -= 10  # force a deterministic ordering
        second, _ = registry.register_or_coalesce(
            dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
        )
        assert registry.list_pending() == [second, first]

    def test_get_returns_none_for_an_unknown_id(self):
        registry = make_registry()
        assert registry.get("nope") is None


class TestPrincipalDimension:
    """P9: approvals.py finally gains the principal dimension approval_ui.py's
    own module docstring already promised (see approvals.py's own module
    docstring). Every approval defaults to LOCAL_PRINCIPAL_ID when nothing
    entered principal_scope() -- so every test above this class, none of
    which passes a principal_id anywhere, stays correct unchanged."""

    def test_default_principal_is_local(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert approval.principal_id == "local"

    def test_registration_stamps_the_current_principal(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        assert approval.principal_id == "alice"

    def test_two_principals_with_the_identical_dedupe_key_get_two_approvals(self):
        # The P7-precedented cross-principal dedupe-key collision (see
        # approvals.py's own module docstring) -- without the principal
        # dimension in _by_key, the second registration below would
        # coalesce onto the first instead of creating its own.
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            alice_approval, alice_created = registry.register_or_coalesce(
                dedupe_key="same-key", connector="c", tool="t", gate_kind="popup", request_id="r1",
            )
        with principal_scope(Principal(id="bob")):
            bob_approval, bob_created = registry.register_or_coalesce(
                dedupe_key="same-key", connector="c", tool="t", gate_kind="popup", request_id="r2",
            )
        assert alice_created and bob_created
        assert alice_approval.id != bob_approval.id

    def test_ledger_entry_is_not_shared_across_principals(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="same-key", connector="c", tool="t", gate_kind="popup", request_id="r1",
            )
            registry.finalize(approval.id, "accept")
            assert registry.consume_ledger("same-key") == ("accept", "", approval.decided_at)
        with principal_scope(Principal(id="bob")):
            # Bob issuing the identical call must not see Alice's decision.
            assert registry.consume_ledger("same-key") is None

    def test_list_pending_filters_by_principal(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        with principal_scope(Principal(id="bob")):
            registry.register_or_coalesce(
                dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
            )
        assert len(registry.list_pending("alice")) == 1
        assert len(registry.list_pending("bob")) == 1
        assert len(registry.list_pending()) == 2  # no filter -- every pre-P9 caller

    def test_get_with_principal_id_rejects_a_foreign_approval(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        assert registry.get(approval.id, principal_id="bob") is None
        assert registry.get(approval.id, principal_id="alice") is approval
        assert registry.get(approval.id) is approval  # unfiltered

    def test_answer_with_principal_id_rejects_a_foreign_decision(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        assert registry.answer(approval.id, "accept", principal_id="bob") is False
        assert registry.answer(approval.id, "accept", principal_id="alice") is True

    def test_await_status_with_principal_id_hides_a_foreign_approval(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        assert registry.await_status(approval.id, principal_id="bob") == "unknown"
        assert registry.await_status(approval.id, principal_id="alice") == "pending"

    def test_reevaluate_all_only_touches_the_current_principals_own_cards(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            alice_approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
                operation_key="op1",
            )
        with principal_scope(Principal(id="bob")):
            bob_approval, _ = registry.register_or_coalesce(
                dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
                operation_key="op1",
            )
            # Bob's rules now cover op1 -- must not touch Alice's own
            # pending card for the same operation_key under her rules.
            resolved = registry.reevaluate_all(lambda op, ctx: (True, "bobs-rule"))
        assert resolved == [bob_approval]
        assert not alice_approval.is_finalized()
        assert bob_approval.is_finalized()
