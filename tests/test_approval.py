"""
Human-in-the-loop approval gate: auto-approve mode must behave identically
to no gate at all (verified separately via run_evaluation.py's unchanged
numbers); HITL mode must genuinely defer execution until approve()/reject().
"""

from datetime import datetime, timezone

from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment, Action
from core.engine import RecoveryEngine

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def _b2b_event(amount=250000.0):
    return RevenueEvent(
        source=EventSource.B2B_RECEIVABLE_OVERDUE,
        decline_reason=DeclineReason.INVOICE_OVERDUE,
        amount=amount,
        customer_segment=CustomerSegment.HIGH_LTV,
        created_at=NOW,
        last_attempt_at=NOW,
    )


def _fraud_event():
    return RevenueEvent(
        source=EventSource.SUBSCRIPTION_FAILED,
        decline_reason=DeclineReason.FRAUD_SUSPECTED,
        amount=5000.0,
        customer_segment=CustomerSegment.MEDIUM_LTV,
        created_at=NOW,
        last_attempt_at=NOW,
    )


def test_large_collections_case_auto_approves_by_default():
    """Default engine (auto_approve from rules.yaml, currently True) must
    still execute -- this is what keeps existing batch numbers unchanged."""
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1)
    record = engine.process_event(_b2b_event(amount=250000.0), now=NOW)
    assert record.requires_approval is True
    assert record.approval_status == "auto_approved"
    assert record.outcome is not None  # actually executed


def test_small_case_never_requires_approval():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1)
    record = engine.process_event(_b2b_event(amount=5000.0), now=NOW)
    assert record.requires_approval is False
    assert record.approval_status == "not_required"


def test_hitl_mode_defers_execution_until_approved():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1, auto_approve=False)
    record = engine.process_event(_b2b_event(amount=250000.0), now=NOW)

    assert record.requires_approval is True
    assert record.approval_status == "pending"
    assert record.outcome is None  # NOT executed yet
    assert record.event.event_id in engine.pending_approvals

    resolved = engine.approve(record.event.event_id, now=NOW)
    assert resolved.approval_status == "approved"
    assert resolved.outcome is not None  # executed now
    assert record.event.event_id not in engine.pending_approvals


def test_hitl_mode_rejection_never_executes():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1, auto_approve=False)
    record = engine.process_event(_b2b_event(amount=250000.0), now=NOW)
    assert record.approval_status == "pending"

    resolved = engine.reject(record.event.event_id, reason="looks like a duplicate invoice")
    assert resolved.approval_status == "rejected"
    assert resolved.outcome is None
    assert "REJECTED" in resolved.approval_reason
    assert record.event.event_id not in engine.pending_approvals


def test_risk_block_requires_approval_regardless_of_amount():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1, auto_approve=False)
    record = engine.process_event(_fraud_event(), now=NOW)
    if record.pursued:  # fraud cases can be skipped as negative-EV depending on amount/confidence
        assert record.requires_approval is True
        assert record.approval_status == "pending"


def test_approving_unknown_event_id_raises():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1, auto_approve=False)
    try:
        engine.approve("not-a-real-id")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_hitl_mode_still_updates_bandit_on_approval():
    # RISK_BLOCK requires approval regardless of which action the bandit
    # picks (unlike the amount-threshold rules, which are action-specific
    # and a fresh, historyless bandit isn't guaranteed to pick escalate_to_
    # collections on its very first pull for a segment).
    engine = RecoveryEngine(use_llm=False, policy_mode="bandit", audit_path=None, seed=3, auto_approve=False)
    record = engine.process_event(_fraud_event(), now=NOW)
    assert record.pursued, "fraud event unexpectedly had negative EV under this seed"
    assert record.approval_status == "pending"

    pulls_before = dict(engine.bandit.pulls)
    engine.approve(record.event.event_id, now=NOW)
    assert engine.bandit.pulls != pulls_before  # bandit only learns once actually executed
