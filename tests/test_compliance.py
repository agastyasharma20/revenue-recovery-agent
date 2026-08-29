"""
Phase 4: each NPCI-style stopping rule in core/rules.yaml, tested in
isolation, both the "blocks" and "doesn't wrongly block" side.
"""

from datetime import datetime, timedelta, timezone

from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment, Action
from core.compliance import ComplianceChecker

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def _event(**overrides):
    defaults = dict(
        source=EventSource.SUBSCRIPTION_FAILED,
        decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        amount=1500.0,
        customer_segment=CustomerSegment.MEDIUM_LTV,
        created_at=NOW - timedelta(days=1),
        last_attempt_at=NOW - timedelta(hours=48),
        retry_count=0,
        mandate_created_at=NOW - timedelta(days=30),
    )
    defaults.update(overrides)
    return RevenueEvent(**defaults)


def test_pursuit_window_cutoff_blocks_stale_event():
    checker = ComplianceChecker()
    stale = _event(created_at=NOW - timedelta(days=8))
    result = checker.check(stale, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is False
    assert "pursuit window" in result.reason


def test_pursuit_window_allows_fresh_event():
    checker = ComplianceChecker()
    fresh = _event(created_at=NOW - timedelta(days=2))
    result = checker.check(fresh, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is True


def test_max_retries_blocks_retry_action():
    checker = ComplianceChecker()
    exhausted = _event(retry_count=3)
    result = checker.check(exhausted, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is False
    assert "max_retries" in result.reason


def test_max_retries_does_not_block_non_retry_action():
    """The retry cap should only govern actual retry actions -- a reminder
    or human call isn't a mandate retry attempt."""
    checker = ComplianceChecker()
    exhausted = _event(retry_count=5)
    result = checker.check(exhausted, Action.SEND_REMINDER_SMS, now=NOW)
    assert result.allowed is True


def test_min_gap_between_retries_blocks_too_soon():
    checker = ComplianceChecker()
    too_soon = _event(retry_count=1, last_attempt_at=NOW - timedelta(hours=5))
    result = checker.check(too_soon, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is False
    assert "minimum gap" in result.reason


def test_min_gap_between_retries_allows_after_cooldown():
    checker = ComplianceChecker()
    cooled_down = _event(retry_count=1, last_attempt_at=NOW - timedelta(hours=30))
    result = checker.check(cooled_down, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is True


def test_mandate_cooldown_blocks_retry_right_after_creation():
    checker = ComplianceChecker()
    new_mandate = _event(mandate_created_at=NOW - timedelta(hours=2))
    result = checker.check(new_mandate, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is False
    assert "Mandate created" in result.reason


def test_mandate_cooldown_allows_retry_after_window():
    checker = ComplianceChecker()
    old_mandate = _event(mandate_created_at=NOW - timedelta(hours=48))
    result = checker.check(old_mandate, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is True


def test_non_subscription_sources_are_exempt_from_mandate_retry_rules():
    """A checkout-abandonment reminder or a B2B collections call isn't an
    e-mandate retry -- NPCI retry-cap/gap rules shouldn't apply to it even
    though it superficially shares an action name."""
    checker = ComplianceChecker()
    b2b_event = RevenueEvent(
        source=EventSource.B2B_RECEIVABLE_OVERDUE,
        decline_reason=DeclineReason.INVOICE_OVERDUE,
        amount=50000.0,
        customer_segment=CustomerSegment.HIGH_LTV,
        created_at=NOW - timedelta(days=2),
        last_attempt_at=NOW - timedelta(days=2),
        retry_count=0,
    )
    result = checker.check(b2b_event, Action.ESCALATE_TO_COLLECTIONS, now=NOW)
    assert result.allowed is True
