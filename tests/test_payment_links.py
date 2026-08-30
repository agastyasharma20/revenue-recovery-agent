"""
Real Razorpay Payment Link creation. These tests avoid depending on network
access / real credentials for CI reliability -- they cover the "action not
applicable" and "no credentials" fallback paths directly, and a bad-key path
that still resolves over the network but must never raise. The genuinely
live "it created a real https://rzp.io/... link" path is exercised manually
(see README) since it requires the developer's own Razorpay test keys.
"""

import os
from datetime import datetime, timezone

from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment, Action
from core.payment_links import create_payment_link, PAYMENT_LINK_ACTIONS

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def _event(amount=999.0):
    return RevenueEvent(
        source=EventSource.SUBSCRIPTION_FAILED,
        decline_reason=DeclineReason.EXPIRED_CARD,
        amount=amount,
        customer_segment=CustomerSegment.MEDIUM_LTV,
        created_at=NOW,
        last_attempt_at=NOW,
    )


def test_non_applicable_action_returns_none():
    assert create_payment_link(_event(), Action.SEND_REMINDER_SMS) is None
    assert create_payment_link(_event(), Action.ESCALATE_TO_HUMAN_CALL) is None
    assert create_payment_link(_event(), Action.NO_ACTION_DO_NOT_PURSUE) is None


def test_missing_credentials_fails_closed():
    saved_id = os.environ.pop("RAZORPAY_KEY_ID", None)
    saved_secret = os.environ.pop("RAZORPAY_KEY_SECRET", None)
    try:
        result = create_payment_link(_event(), Action.UPDATE_PAYMENT_METHOD_LINK)
        assert result.created is False
        assert result.error == "no_razorpay_credentials"
    finally:
        if saved_id is not None:
            os.environ["RAZORPAY_KEY_ID"] = saved_id
        if saved_secret is not None:
            os.environ["RAZORPAY_KEY_SECRET"] = saved_secret


def test_bad_credentials_fails_closed_not_raises():
    saved_id = os.environ.get("RAZORPAY_KEY_ID")
    saved_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    os.environ["RAZORPAY_KEY_ID"] = "rzp_test_totallyfake"
    os.environ["RAZORPAY_KEY_SECRET"] = "fakefakefake"
    try:
        result = create_payment_link(_event(), Action.UPDATE_PAYMENT_METHOD_LINK)
        assert result.created is False
        assert result.error is not None
    finally:
        if saved_id is not None:
            os.environ["RAZORPAY_KEY_ID"] = saved_id
        else:
            os.environ.pop("RAZORPAY_KEY_ID", None)
        if saved_secret is not None:
            os.environ["RAZORPAY_KEY_SECRET"] = saved_secret
        else:
            os.environ.pop("RAZORPAY_KEY_SECRET", None)


def test_discount_action_reduces_charged_amount():
    saved_id = os.environ.pop("RAZORPAY_KEY_ID", None)
    saved_secret = os.environ.pop("RAZORPAY_KEY_SECRET", None)
    try:
        result = create_payment_link(_event(amount=1000.0), Action.OFFER_DISCOUNT)
        assert result.amount_charged == 950.0  # 5% off, no credentials needed to compute this
    finally:
        if saved_id is not None:
            os.environ["RAZORPAY_KEY_ID"] = saved_id
        if saved_secret is not None:
            os.environ["RAZORPAY_KEY_SECRET"] = saved_secret


def test_all_payment_link_actions_are_real_actions():
    for a in PAYMENT_LINK_ACTIONS:
        assert isinstance(a, Action)
