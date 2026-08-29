"""
Phase 5: signature verification and payload mapping, tested against
hand-built payloads matching Razorpay's documented webhook shape (no live
credentials needed for these -- they exercise our code, not Razorpay's
servers). End-to-end webhook delivery against a live Razorpay account still
needs real test-mode keys and a reachable URL (see .env.example / README).
"""

import hashlib
import hmac
import json

from core.razorpay_integration import (
    verify_webhook_signature,
    parse_webhook_payload,
    map_decline_reason,
)
from core.schema import DeclineReason, EventSource


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payment_failed_body(error_reason="insufficient_funds", amount_paise=150000, created_at=1735000000):
    return json.dumps(
        {
            "entity": "event",
            "event": "payment.failed",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test_abc123",
                        "amount": amount_paise,
                        "currency": "INR",
                        "status": "failed",
                        "order_id": "order_test123",
                        "method": "card",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_reason": error_reason,
                        "created_at": created_at,
                    }
                }
            },
            "created_at": created_at,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_valid_signature_is_accepted():
    secret = "whsec_test"
    body = _payment_failed_body()
    sig = _sign(body, secret)
    assert verify_webhook_signature(body, sig, secret) is True


def test_wrong_signature_is_rejected():
    secret = "whsec_test"
    body = _payment_failed_body()
    assert verify_webhook_signature(body, "0" * 64, secret) is False


def test_tampered_body_after_signing_is_rejected():
    secret = "whsec_test"
    body = _payment_failed_body(amount_paise=150000)
    sig = _sign(body, secret)
    tampered = _payment_failed_body(amount_paise=99999999)
    assert verify_webhook_signature(tampered, sig, secret) is False


def test_wrong_secret_is_rejected():
    body = _payment_failed_body()
    sig = _sign(body, "correct_secret")
    assert verify_webhook_signature(body, sig, "wrong_secret") is False


def test_payment_failed_maps_to_revenue_event():
    body = _payment_failed_body(error_reason="expired_card", amount_paise=999900)
    event = parse_webhook_payload(json.loads(body))
    assert event is not None
    assert event.source == EventSource.SUBSCRIPTION_FAILED
    assert event.decline_reason == DeclineReason.EXPIRED_CARD
    assert event.amount == 9999.0  # paise -> INR
    assert event.metadata["razorpay_payment_id"] == "pay_test_abc123"


def test_unmapped_error_reason_falls_back_to_unknown():
    assert map_decline_reason("some_new_reason_razorpay_added_later") == DeclineReason.UNKNOWN
    assert map_decline_reason(None) == DeclineReason.UNKNOWN


def test_unhandled_event_type_returns_none():
    body = json.dumps({"event": "refund.processed", "payload": {}}).encode()
    assert parse_webhook_payload(json.loads(body)) is None
