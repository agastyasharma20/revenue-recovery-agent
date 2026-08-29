"""
Phase 5: Razorpay test-mode webhook integration.

Wires real Razorpay webhooks (payment.failed, subscription.charged.failed)
into the same RevenueEvent -> engine pipeline the synthetic data uses, via
Razorpay's own Python SDK for signature verification (core.ingestion's
idempotency layer sits in front of this so a redelivered webhook is a no-op).

Credentials are read ONLY from environment variables, never hardcoded:
  RAZORPAY_KEY_ID        -- from your Razorpay test-mode dashboard
  RAZORPAY_KEY_SECRET    -- from your Razorpay test-mode dashboard
  RAZORPAY_WEBHOOK_SECRET -- set when you configure the webhook in the
                             dashboard (Settings -> Webhooks); Razorpay signs
                             every webhook body with this secret via
                             X-Razorpay-Signature, and the SDK's
                             Utility.verify_webhook_signature() checks it.

Put these in a local `.env` file (already covered by .gitignore -- never
commit real keys) and load it with python-dotenv, or export them in your
shell before running webhook_server.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import razorpay
from razorpay.errors import SignatureVerificationError

from core.schema import (
    RevenueEvent,
    EventSource,
    DeclineReason,
    CustomerSegment,
)

# Razorpay's error_reason values (from payment.entity.error_reason on a
# failed payment) mapped onto our DeclineReason vocabulary. This list is
# built from Razorpay's published error-reason taxonomy; verify against a
# live payload before relying on it in a real demo, since gateways add
# reasons over time and this mapping should be treated as a starting point,
# not gospel.
_RAZORPAY_ERROR_REASON_MAP = {
    "insufficient_funds": DeclineReason.INSUFFICIENT_FUNDS,
    "expired_card": DeclineReason.EXPIRED_CARD,
    "card_expired": DeclineReason.EXPIRED_CARD,
    "invalid_cvv": DeclineReason.INVALID_CVV,
    "incorrect_cvv": DeclineReason.INVALID_CVV,
    "payment_declined": DeclineReason.ISSUER_DECLINED,
    "transaction_declined": DeclineReason.ISSUER_DECLINED,
    "issuer_unavailable": DeclineReason.BANK_SERVER_TIMEOUT,
    "gateway_timeout": DeclineReason.BANK_SERVER_TIMEOUT,
    "server_error": DeclineReason.BANK_SERVER_TIMEOUT,
    "network_error": DeclineReason.NETWORK_ERROR,
    "fraudulent": DeclineReason.FRAUD_SUSPECTED,
    "risk_check_failed": DeclineReason.FRAUD_SUSPECTED,
    "card_blacklisted": DeclineReason.CARD_BLOCKED,
    "restricted_card": DeclineReason.CARD_BLOCKED,
    "payment_limit_breached": DeclineReason.EXCEEDS_LIMIT,
    "authentication_failed": DeclineReason.MANDATE_NOT_APPROVED,
}


def map_decline_reason(razorpay_error_reason: Optional[str]) -> DeclineReason:
    if not razorpay_error_reason:
        return DeclineReason.UNKNOWN
    return _RAZORPAY_ERROR_REASON_MAP.get(razorpay_error_reason.lower(), DeclineReason.UNKNOWN)


def verify_webhook_signature(body: bytes, signature: str, webhook_secret: str) -> bool:
    """Uses Razorpay's own SDK utility (HMAC-SHA256 over the raw body) so we
    aren't hand-rolling crypto. Returns False on any mismatch or malformed
    input instead of raising, so a bad/missing signature is just "reject
    this request" rather than a crash."""
    try:
        client = razorpay.Client(auth=("", ""))  # no API call is made for signature verification
        return client.utility.verify_webhook_signature(body.decode("utf-8"), signature, webhook_secret)
    except SignatureVerificationError:
        return False
    except Exception:
        return False


def parse_webhook_payload(payload: dict) -> Optional[RevenueEvent]:
    """Maps a Razorpay webhook JSON body (already signature-verified) to a
    RevenueEvent. Returns None for event types we don't handle -- caller
    should ack the webhook (2xx) regardless so Razorpay doesn't retry
    forever, just skip pipeline processing for unhandled types."""
    event_type = payload.get("event")

    if event_type == "payment.failed":
        entity = payload["payload"]["payment"]["entity"]
        amount = entity["amount"] / 100.0  # paise -> INR
        reason = map_decline_reason(entity.get("error_reason"))
        created_at = datetime.fromtimestamp(entity.get("created_at", 0), tz=timezone.utc)
        return RevenueEvent(
            source=EventSource.SUBSCRIPTION_FAILED,
            decline_reason=reason,
            amount=amount,
            customer_segment=CustomerSegment.MEDIUM_LTV,  # not present on the webhook; would come from your own CRM/customer table
            created_at=created_at,
            last_attempt_at=created_at,
            retry_count=0,
            metadata={"razorpay_payment_id": entity.get("id"), "razorpay_order_id": entity.get("order_id"), "raw_error_reason": entity.get("error_reason")},
        )

    if event_type == "subscription.charged.failed":
        sub_entity = payload["payload"].get("subscription", {}).get("entity", {})
        pay_entity = payload["payload"].get("payment", {}).get("entity", {})
        amount = pay_entity.get("amount", 0) / 100.0
        reason = map_decline_reason(pay_entity.get("error_reason"))
        created_at = datetime.fromtimestamp(pay_entity.get("created_at", 0), tz=timezone.utc)
        mandate_created_at = (
            datetime.fromtimestamp(sub_entity["created_at"], tz=timezone.utc)
            if sub_entity.get("created_at")
            else None
        )
        return RevenueEvent(
            source=EventSource.SUBSCRIPTION_FAILED,
            decline_reason=reason,
            amount=amount,
            customer_segment=CustomerSegment.MEDIUM_LTV,
            created_at=created_at,
            last_attempt_at=created_at,
            retry_count=sub_entity.get("failed_attempts", sub_entity.get("paid_count", 0)) or 0,
            mandate_created_at=mandate_created_at,
            metadata={"razorpay_subscription_id": sub_entity.get("id"), "razorpay_payment_id": pay_entity.get("id")},
        )

    return None
