"""
Per-case state-machine timeline: stages appear in order, and a pending
case's timeline genuinely grows when later approved/rejected.
"""

from datetime import datetime, timezone

from core.engine import RecoveryEngine
from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment

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


def test_executed_case_timeline_is_ordered_and_ends_in_audited():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1)
    record = engine.process_event(_b2b_event(amount=5000.0), now=NOW)
    stages = [e["stage"] for e in record.timeline]
    assert stages[0] == "ingested"
    assert stages[-1] == "audited"
    assert "diagnosed" in stages and "prioritized" in stages
    # timestamps must be non-decreasing
    timestamps = [e["at"] for e in record.timeline]
    assert timestamps == sorted(timestamps)


def test_pending_case_timeline_grows_on_approval():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1, auto_approve=False)
    record = engine.process_event(_b2b_event(amount=250000.0), now=NOW)
    assert record.approval_status == "pending"
    stages_before = [e["stage"] for e in record.timeline]
    assert "pending_approval" in stages_before
    assert "executed" not in stages_before

    resolved = engine.approve(record.event.event_id, now=NOW)
    stages_after = [e["stage"] for e in resolved.timeline]
    assert stages_after[: len(stages_before)] == stages_before  # nothing rewritten, only appended
    assert "approved" in stages_after
    assert stages_after[-1] == "executed"


def test_rejected_case_timeline_never_reaches_executed():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1, auto_approve=False)
    record = engine.process_event(_b2b_event(amount=250000.0), now=NOW)
    resolved = engine.reject(record.event.event_id, reason="test rejection")
    stages = [e["stage"] for e in resolved.timeline]
    assert "rejected" in stages
    assert "executed" not in stages


def test_negative_ev_case_closes_immediately():
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1)
    tiny_event = RevenueEvent(
        source=EventSource.SUBSCRIPTION_FAILED,
        decline_reason=DeclineReason.FRAUD_SUSPECTED,
        amount=10.0,  # tiny amount -> negative EV against the ~150 INR human-call cost
        customer_segment=CustomerSegment.LOW_LTV,
        created_at=NOW,
        last_attempt_at=NOW,
    )
    record = engine.process_event(tiny_event, now=NOW)
    assert not record.priority.pursue
    stages = [e["stage"] for e in record.timeline]
    assert stages[-2:] == ["closed", "audited"]
