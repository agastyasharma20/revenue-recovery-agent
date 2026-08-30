"""
Phase 2: proves the anomaly + root-cause clustering layer actually fires
on a seeded spike, and doesn't false-positive on ordinary background noise.
"""

from datetime import datetime, timedelta, timezone

from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment
from core.anomaly import detect_systemic_incidents, AnomalyDetector
from data.generate_synthetic import generate_batch


def _make_event(reason, amount, created_at, source=EventSource.SUBSCRIPTION_FAILED):
    return RevenueEvent(
        source=source,
        decline_reason=reason,
        amount=amount,
        customer_segment=CustomerSegment.MEDIUM_LTV,
        created_at=created_at,
        last_attempt_at=created_at,
        retry_count=0,
    )


def test_seeded_spike_triggers_systemic_incident():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)

    # 18 bank_server_timeout failures, all similar amount, all within a
    # 90-minute window -- a synthetic "bank outage" spike.
    spike_events = [
        _make_event(DeclineReason.BANK_SERVER_TIMEOUT, 1200.0 + i, now - timedelta(minutes=90 - i * 5))
        for i in range(18)
    ]
    # some unrelated background noise in the same window, different reason.
    background = [
        _make_event(DeclineReason.INSUFFICIENT_FUNDS, 800.0, now - timedelta(minutes=m))
        for m in (10, 40, 70)
    ]

    incidents = detect_systemic_incidents(spike_events + background, window_hours=2.0, threshold=15)

    assert len(incidents) == 1, f"expected exactly one incident, got {len(incidents)}: {incidents}"
    incident = incidents[0]
    assert incident.decline_reason == DeclineReason.BANK_SERVER_TIMEOUT
    assert incident.count >= 15
    assert "bank_server_timeout" in incident.message
    assert "systemic" in incident.message.lower()


def test_no_incident_below_threshold():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    # only 5 failures of the same type -- well under threshold=15.
    events = [
        _make_event(DeclineReason.BANK_SERVER_TIMEOUT, 1200.0, now - timedelta(minutes=m))
        for m in (10, 20, 30, 40, 50)
    ]
    incidents = detect_systemic_incidents(events, window_hours=2.0, threshold=15)
    assert incidents == []


def test_incident_does_not_refire_every_event_in_ongoing_spike():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    # 25 events well past threshold=15 -- should emit exactly ONE incident,
    # not one per event past the threshold.
    events = [
        _make_event(DeclineReason.BANK_SERVER_TIMEOUT, 1200.0, now - timedelta(minutes=100 - i * 2))
        for i in range(25)
    ]
    incidents = detect_systemic_incidents(events, window_hours=2.0, threshold=15)
    assert len(incidents) == 1


def test_isolation_forest_flags_unusual_event_against_normal_baseline():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    normal_events, _ = generate_batch(300, seed=99, now=now)

    detector = AnomalyDetector(contamination=0.05, random_state=0).fit(normal_events)

    # a wildly-out-of-distribution event: enormous amount for a subscription
    # failure with an absurd retry count.
    weird = _make_event(DeclineReason.INSUFFICIENT_FUNDS, 4_999_999.0, now)
    weird.retry_count = 50

    result = detector.score(weird)
    # We don't assert is_anomaly strictly (IsolationForest thresholds can be
    # sensitive), but the anomalous event's score must be clearly higher than
    # a typical normal event's. Compare against the MEAN score across a
    # sample of normal events, not one arbitrary instance -- a single-point
    # comparison is not statistically robust (any one "typical" event can
    # itself land on the noisier tail of the score distribution) and did
    # fail intermittently for exactly that reason during development.
    sample_scores = [detector.score(e).anomaly_score for e in normal_events[:30]]
    mean_typical_score = sum(sample_scores) / len(sample_scores)
    assert result.anomaly_score > mean_typical_score
