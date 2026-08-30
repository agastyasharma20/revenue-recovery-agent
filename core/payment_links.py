"""
Real Razorpay Payment Link creation -- the one piece of this project that
upgrades from "simulated action" to "genuinely real, verifiable artifact,"
using the SAME test-mode credentials (RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET)
already required for webhook verification. No new external account needed,
unlike WhatsApp Business or telephony integration.

Used for actions where handing the customer a real payment link is exactly
what the action means: RETRY_WITH_ALTERNATE_METHOD, UPDATE_PAYMENT_METHOD_LINK,
OFFER_DISCOUNT (amount reduced by the discount), ESCALATE_TO_COLLECTIONS on a
B2B invoice. Every other action (reminders, human calls, voice scripts)
doesn't involve a payment link and this module isn't invoked for them.

Fails closed like every other external call in this project: no
credentials, a network error, or a malformed response all return
PaymentLinkResult(created=False, ...) with the reason, never an exception
that would crash the pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from core.schema import RevenueEvent, Action, OFFER_DISCOUNT_PCT_OF_AMOUNT

# Actions where "hand the customer a working payment link" is literally
# what the action means.
PAYMENT_LINK_ACTIONS = {
    Action.RETRY_WITH_ALTERNATE_METHOD,
    Action.UPDATE_PAYMENT_METHOD_LINK,
    Action.OFFER_DISCOUNT,
    Action.ESCALATE_TO_COLLECTIONS,
}


@dataclass
class PaymentLinkResult:
    created: bool
    link_url: Optional[str]
    payment_link_id: Optional[str]
    amount_charged: float
    error: Optional[str] = None


def _amount_for_action(event: RevenueEvent, action: Action) -> float:
    if action == Action.OFFER_DISCOUNT:
        return round(event.amount * (1 - OFFER_DISCOUNT_PCT_OF_AMOUNT), 2)
    return event.amount


def create_payment_link(event: RevenueEvent, action: Action) -> Optional[PaymentLinkResult]:
    """Returns None if this action doesn't involve a payment link at all
    (most actions don't) -- distinct from PaymentLinkResult(created=False,...)
    which means it SHOULD have created one but couldn't."""
    if action not in PAYMENT_LINK_ACTIONS:
        return None

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    amount = _amount_for_action(event, action)

    if not key_id or not key_secret:
        return PaymentLinkResult(created=False, link_url=None, payment_link_id=None,
                                  amount_charged=amount, error="no_razorpay_credentials")

    try:
        import razorpay  # imported lazily so the whole project doesn't hard-depend on it at import time

        client = razorpay.Client(auth=(key_id, key_secret))
        payload = {
            "amount": int(round(amount * 100)),  # INR -> paise
            "currency": event.currency,
            "description": (
                f"Payment recovery link ({action.value}) for {event.source.value} "
                f"/ {event.decline_reason.value}"
            ),
            "reference_id": event.event_id[:40],
            "notes": {
                "revenue_recovery_agent": "true",
                "trace_id": event.trace_id,
                "chosen_action": action.value,
            },
            "reminder_enable": True,
        }
        result = client.payment_link.create(data=payload)
        return PaymentLinkResult(
            created=True,
            link_url=result.get("short_url"),
            payment_link_id=result.get("id"),
            amount_charged=amount,
        )
    except Exception as exc:  # noqa: BLE001 -- external API call, must never crash the pipeline
        return PaymentLinkResult(
            created=False, link_url=None, payment_link_id=None,
            amount_charged=amount, error=f"{type(exc).__name__}: {exc}",
        )
