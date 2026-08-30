"""
Phase (Razorpay-brief addition): promise-to-pay tracking and its feedback
into future EV for the same customer.
"""

from datetime import datetime, timedelta, timezone

from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment, Action
from core.promise_tracking import PromiseTracker, PromiseStatus, PROMISE_ELIGIBLE_ACTIONS
from core.engine import RecoveryEngine

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def _b2b_event(customer_id, amount=50000.0, created_at=None):
    return RevenueEvent(
        source=EventSource.B2B_RECEIVABLE_OVERDUE,
        decline_reason=DeclineReason.INVOICE_OVERDUE,
        amount=amount,
        customer_segment=CustomerSegment.MEDIUM_LTV,
        created_at=created_at or NOW,
        last_attempt_at=created_at or NOW,
        customer_id=customer_id,
    )


def test_non_escalation_action_does_not_record_a_promise():
    tracker = PromiseTracker()
    event = _b2b_event("cust_1")
    promise = tracker.maybe_record_promise(event, Action.SEND_REMINDER_EMAIL, recovered=True, now=NOW)
    assert promise is None
    assert tracker.customer_history("cust_1") == []


def test_escalation_action_records_a_promise_matching_outcome():
    tracker = PromiseTracker()
    event = _b2b_event("cust_2")

    kept = tracker.maybe_record_promise(event, Action.ESCALATE_TO_COLLECTIONS, recovered=True, now=NOW)
    assert kept is not None
    assert kept.status == PromiseStatus.KEPT
    assert kept.promised_date > NOW

    event2 = _b2b_event("cust_3")
    broken = tracker.maybe_record_promise(event2, Action.ESCALATE_TO_HUMAN_CALL, recovered=False, now=NOW)
    assert broken.status == PromiseStatus.BROKEN


def test_reliability_score_reflects_track_record():
    tracker = PromiseTracker()
    cid = "cust_reliable"
    for _ in range(5):
        tracker.maybe_record_promise(_b2b_event(cid), Action.ESCALATE_TO_COLLECTIONS, recovered=True, now=NOW)

    cid_bad = "cust_unreliable"
    for _ in range(5):
        tracker.maybe_record_promise(_b2b_event(cid_bad), Action.ESCALATE_TO_COLLECTIONS, recovered=False, now=NOW)

    assert tracker.customer_reliability_score(cid) > 0.7
    assert tracker.customer_reliability_score(cid_bad) < 0.3


def test_no_history_gives_neutral_reliability():
    tracker = PromiseTracker()
    assert tracker.customer_reliability_score("never_seen") == 0.5


def test_engine_lowers_ev_for_customer_with_broken_promise_history():
    """End-to-end: force a customer's first invoice to break its promise
    (via a rigged rng that always fails outcomes), then check their second
    invoice's EV reflects the worse reliability score, all else equal."""
    import random

    class AlwaysFailRandom(random.Random):
        def random(self):
            return 0.999  # always above any success probability -> always "not recovered"

    engine = RecoveryEngine(
        use_llm=False, policy_mode="deterministic", audit_path=None,
        rng=AlwaysFailRandom(), seed=1,
    )

    cid = "cust_repeat"
    invoice1 = _b2b_event(cid, amount=80000.0, created_at=NOW - timedelta(days=3))
    invoice2 = _b2b_event(cid, amount=80000.0, created_at=NOW)

    r1 = engine.process_event(invoice1, now=NOW)
    r2 = engine.process_event(invoice2, now=NOW)

    assert r1.promise is not None and r1.promise.status == PromiseStatus.BROKEN
    assert "customer reliability" in r2.priority.reason
    # same amount and diagnosis category -> the only thing that changed
    # between the two is the customer's now-broken track record, so EV2
    # must be strictly lower than what invoice1's EV was.
    assert r2.priority.ev < r1.priority.ev
