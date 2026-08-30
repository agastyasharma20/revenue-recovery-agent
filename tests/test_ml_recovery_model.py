"""
Supervised recovery-probability model: trains fast enough for a test suite
(small n), and its evaluation must be internally honest -- no leakage, no
threshold picked on the test set, metrics that make sense together.
"""

from datetime import datetime, timezone

from core.ml_recovery_model import train_and_evaluate, encode, FEATURE_NAMES, ACTION_LIST
from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment, Action

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def test_encode_produces_expected_dimensionality():
    event = RevenueEvent(
        source=EventSource.SUBSCRIPTION_FAILED, decline_reason=DeclineReason.EXPIRED_CARD, amount=1000.0,
        customer_segment=CustomerSegment.MEDIUM_LTV, created_at=NOW, last_attempt_at=NOW,
    )
    x = encode(event, Action.UPDATE_PAYMENT_METHOD_LINK, NOW)
    assert x.shape == (len(FEATURE_NAMES),)
    assert x.sum() > 0  # at least the one-hot entries fired


def test_train_and_evaluate_returns_sane_metrics():
    model, ev = train_and_evaluate(n_samples=1500, seed=1)
    assert 0.5 <= ev.auc <= 1.0  # better than random guessing (0.5) on a real signal
    assert 0.0 <= ev.precision <= 1.0
    assert 0.0 <= ev.recall <= 1.0
    assert ev.n_train > 0 and ev.n_test > 0
    assert len(ev.top_features) == 10
    assert len(ev.threshold_sweep) > 0


def test_average_precision_beats_random_baseline():
    """A model with zero signal would have PR-AUC equal to the positive
    rate; this one must genuinely beat that baseline, not just report a
    number that happens to look plausible."""
    _, ev = train_and_evaluate(n_samples=3000, seed=2)
    assert ev.average_precision > ev.positive_rate_test


def test_threshold_is_tuned_not_hardcoded_default():
    """If every seed happened to land on exactly 0.5, that would suggest
    the tuning loop isn't actually running."""
    thresholds_seen = set()
    for seed in (1, 2, 3):
        _, ev = train_and_evaluate(n_samples=1500, seed=seed)
        thresholds_seen.add(ev.threshold_used)
    assert len(thresholds_seen) > 1 or list(thresholds_seen)[0] != 0.5


def test_predict_proba_returns_valid_probability():
    model, _ = train_and_evaluate(n_samples=1500, seed=1)
    event = RevenueEvent(
        source=EventSource.CHECKOUT_ABANDONED, decline_reason=DeclineReason.CHECKOUT_ABANDONED, amount=3000.0,
        customer_segment=CustomerSegment.LOW_LTV, created_at=NOW, last_attempt_at=NOW,
    )
    p = model.predict_proba(event, Action.OFFER_DISCOUNT, NOW)
    assert 0.0 <= p <= 1.0


def test_all_bandit_arms_are_encodable_actions():
    assert len(ACTION_LIST) > 0
    for a in ACTION_LIST:
        assert isinstance(a, Action)
