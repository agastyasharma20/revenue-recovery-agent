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


def test_b2b_receivables_use_a_longer_pursuit_window_than_subscriptions():
    """A B2B invoice ~9 days old must NOT be blocked by the 7-day
    subscription/mandate pursuit window -- B2B follows a ~90-day
    receivables-aging convention instead (rules.yaml: b2b_receivables)."""
    checker = ComplianceChecker()
    aging_invoice = RevenueEvent(
        source=EventSource.B2B_RECEIVABLE_OVERDUE,
        decline_reason=DeclineReason.INVOICE_OVERDUE,
        amount=250000.0,
        customer_segment=CustomerSegment.HIGH_LTV,
        created_at=NOW - timedelta(days=9),
        last_attempt_at=NOW - timedelta(days=9),
        retry_count=0,
    )
    result = checker.check(aging_invoice, Action.ESCALATE_TO_COLLECTIONS, now=NOW)
    assert result.allowed is True

    # but a subscription failure the same age SHOULD be blocked by the
    # shorter 7-day window -- confirms the two windows are genuinely
    # independent, not that the B2B rule silently swallowed everything.
    stale_subscription = _event(created_at=NOW - timedelta(days=9))
    result2 = checker.check(stale_subscription, Action.RETRY_PAYMENT, now=NOW)
    assert result2.allowed is False


def test_rbi_pre_debit_notice_blocks_first_attempt_retry_above_threshold():
    """RBI's e-mandate framework requires a pre-debit notice at least 24h
    before an automatic debit above the threshold -- auto-retrying a large
    mandate the instant it fails, with zero notice ever sent, is exactly
    what this rule exists to catch."""
    checker = ComplianceChecker()
    large_first_attempt = _event(amount=7500.0, retry_count=0)
    result = checker.check(large_first_attempt, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is False
    assert "RBI e-mandate pre-debit notice" in result.reason


def test_rbi_pre_debit_notice_does_not_block_below_threshold():
    checker = ComplianceChecker()
    small_first_attempt = _event(amount=1500.0, retry_count=0)
    result = checker.check(small_first_attempt, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is True


def test_rbi_pre_debit_notice_does_not_block_once_a_notice_could_have_gone_out():
    """The rule only bites on retry_count==0 (the very first, unnoticed
    attempt) -- once retry_count > 0, a reminder had the chance to reach the
    customer first, so the notice requirement is satisfied by construction
    of the retry sequence."""
    checker = ComplianceChecker()
    already_retried_once = _event(amount=7500.0, retry_count=1, last_attempt_at=NOW - timedelta(hours=30))
    result = checker.check(already_retried_once, Action.RETRY_PAYMENT, now=NOW)
    assert result.allowed is True


def test_rbi_pre_debit_notice_does_not_block_non_retry_actions():
    """A reminder IS how the notice gets delivered -- it must never be
    blocked by the very rule it satisfies."""
    checker = ComplianceChecker()
    large_first_attempt = _event(amount=7500.0, retry_count=0)
    result = checker.check(large_first_attempt, Action.SEND_REMINDER_WHATSAPP, now=NOW)
    assert result.allowed is True


def test_trai_quiet_hours_blocks_sms_at_night_ist():
    checker = ComplianceChecker()
    event = _event()
    # 2026-08-29 20:00 UTC = 2026-08-30 01:30 IST -- well inside quiet hours.
    night_ist = datetime(2026, 8, 29, 20, 0, 0, tzinfo=timezone.utc)
    result = checker.check(event, Action.SEND_REMINDER_SMS, now=night_ist)
    assert result.allowed is False
    assert "quiet-hours" in result.reason


def test_trai_quiet_hours_allows_sms_during_the_day_ist():
    checker = ComplianceChecker()
    event = _event()
    # 2026-08-29 12:00 UTC = 17:30 IST -- daytime.
    day_ist = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    result = checker.check(event, Action.SEND_REMINDER_SMS, now=day_ist)
    assert result.allowed is True


def test_trai_quiet_hours_does_not_restrict_unlisted_actions():
    """Email/retry/collections aren't in restricted_actions -- quiet hours
    is deliberately scoped to direct-contact channels, not everything."""
    checker = ComplianceChecker()
    event = _event()
    night_ist = datetime(2026, 8, 29, 20, 0, 0, tzinfo=timezone.utc)
    result = checker.check(event, Action.SEND_REMINDER_EMAIL, now=night_ist)
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
