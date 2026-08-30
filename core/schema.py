"""
Unified RevenueEvent data model.

Every leakage signal the agent can act on -- a failed subscription charge, an
abandoned checkout, or an overdue B2B receivable -- is normalized into this one
shape before it enters the pipeline. Keeping one schema (instead of three
source-specific ones) is what lets diagnosis/prioritization/policy/compliance
be written once and reused across all three leakage types.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class EventSource(str, Enum):
    SUBSCRIPTION_FAILED = "subscription_failed"
    CHECKOUT_ABANDONED = "checkout_abandoned"
    B2B_RECEIVABLE_OVERDUE = "b2b_receivable_overdue"


class DeclineReason(str, Enum):
    """Why the money didn't come in.

    The first block are real payment-gateway decline reasons (apply to
    SUBSCRIPTION_FAILED). The next two are synthetic "reasons" that exist so
    every event source shares one field -- CHECKOUT_ABANDONED and
    B2B_RECEIVABLE_OVERDUE never had a *failed transaction*, so they get a
    placeholder reason instead of a bank decline code. This distinction
    matters a lot downstream: retrying a "payment" is nonsensical when there
    was never a transaction to retry.
    """

    # --- real gateway/bank decline codes (subscription_failed) ---
    INSUFFICIENT_FUNDS = "insufficient_funds"
    EXPIRED_CARD = "expired_card"
    BANK_SERVER_TIMEOUT = "bank_server_timeout"
    ISSUER_DECLINED = "issuer_declined"
    INVALID_CVV = "invalid_cvv"
    FRAUD_SUSPECTED = "fraud_suspected"
    CARD_BLOCKED = "card_blocked"
    EXCEEDS_LIMIT = "exceeds_limit"
    NETWORK_ERROR = "network_error"
    MANDATE_NOT_APPROVED = "mandate_not_approved"

    # --- no actual decline; placeholder "reasons" for the other two sources ---
    CHECKOUT_ABANDONED = "checkout_abandoned"
    INVOICE_OVERDUE = "invoice_overdue"

    UNKNOWN = "unknown"


class CustomerSegment(str, Enum):
    NEW_CUSTOMER = "new_customer"
    LOW_LTV = "low_ltv"
    MEDIUM_LTV = "medium_ltv"
    HIGH_LTV = "high_ltv"


# Which decline reasons are even possible for a given event source. Used both
# by the synthetic generator (so it doesn't produce nonsense like a B2B
# invoice with an "expired_card" reason) and as a sanity check in ingestion.
VALID_REASONS_FOR_SOURCE = {
    EventSource.SUBSCRIPTION_FAILED: {
        DeclineReason.INSUFFICIENT_FUNDS,
        DeclineReason.EXPIRED_CARD,
        DeclineReason.BANK_SERVER_TIMEOUT,
        DeclineReason.ISSUER_DECLINED,
        DeclineReason.INVALID_CVV,
        DeclineReason.FRAUD_SUSPECTED,
        DeclineReason.CARD_BLOCKED,
        DeclineReason.EXCEEDS_LIMIT,
        DeclineReason.NETWORK_ERROR,
        DeclineReason.MANDATE_NOT_APPROVED,
    },
    EventSource.CHECKOUT_ABANDONED: {DeclineReason.CHECKOUT_ABANDONED},
    EventSource.B2B_RECEIVABLE_OVERDUE: {DeclineReason.INVOICE_OVERDUE},
}


class DiagnosisCategory(str, Enum):
    """Coarse bucket the rule-based classifier sorts a decline reason into.

    This is the reusable "why" that prioritization and the deterministic
    policy key off of -- much more stable than 10 raw decline reasons.
    """

    TRANSIENT_RETRIABLE = "transient_retriable"          # e.g. bank timeout, network error
    SOFT_DECLINE_RETRIABLE = "soft_decline_retriable"     # e.g. insufficient funds -- may clear later
    HARD_DECLINE_UNRETRIABLE = "hard_decline_unretriable"  # e.g. expired/blocked card, bad cvv
    RISK_BLOCK = "risk_block"                             # e.g. fraud suspected -- do not blindly retry
    CUSTOMER_INACTION = "customer_inaction"               # e.g. abandoned checkout, overdue invoice
    UNKNOWN = "unknown"


class Action(str, Enum):
    """The action vocabulary both the deterministic policy and the bandit pick from."""

    RETRY_PAYMENT = "retry_payment"
    RETRY_WITH_ALTERNATE_METHOD = "retry_with_alternate_method"
    SEND_REMINDER_SMS = "send_reminder_sms"
    SEND_REMINDER_EMAIL = "send_reminder_email"
    SEND_REMINDER_WHATSAPP = "send_reminder_whatsapp"  # mirrors Razorpay's real Failed Payment Recovery (WhatsApp+Email+SMS payment links)
    OFFER_DISCOUNT = "offer_discount"
    UPDATE_PAYMENT_METHOD_LINK = "update_payment_method_link"
    ESCALATE_TO_HUMAN_CALL = "escalate_to_human_call"
    ESCALATE_TO_COLLECTIONS = "escalate_to_collections"
    NO_ACTION_DO_NOT_PURSUE = "no_action_do_not_pursue"


# Flat estimated cost (INR) of *attempting* each action, independent of outcome.
# Used by the prioritizer for EV math. OFFER_DISCOUNT's cost is amount-dependent
# and is computed separately (see prioritizer.py).
ACTION_FLAT_COST = {
    Action.RETRY_PAYMENT: 2.0,
    Action.RETRY_WITH_ALTERNATE_METHOD: 5.0,
    Action.SEND_REMINDER_SMS: 0.5,
    Action.SEND_REMINDER_EMAIL: 0.1,
    Action.SEND_REMINDER_WHATSAPP: 0.3,  # cheaper than SMS at scale but has per-conversation API cost
    Action.OFFER_DISCOUNT: 0.0,  # real cost added on top in prioritizer (% of amount)
    Action.UPDATE_PAYMENT_METHOD_LINK: 1.0,
    Action.ESCALATE_TO_HUMAN_CALL: 150.0,
    Action.ESCALATE_TO_COLLECTIONS: 300.0,
    Action.NO_ACTION_DO_NOT_PURSUE: 0.0,
}

OFFER_DISCOUNT_PCT_OF_AMOUNT = 0.05  # 5% discount cost when OFFER_DISCOUNT is used


@dataclass
class RevenueEvent:
    source: EventSource
    decline_reason: DeclineReason
    amount: float
    customer_segment: CustomerSegment
    created_at: datetime
    last_attempt_at: datetime
    retry_count: int = 0
    mandate_created_at: Optional[datetime] = None
    currency: str = "INR"
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    customer_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        valid = VALID_REASONS_FOR_SOURCE.get(self.source, set())
        if valid and self.decline_reason not in valid:
            raise ValueError(
                f"decline_reason={self.decline_reason} is not valid for "
                f"source={self.source}. Valid reasons: {sorted(r.value for r in valid)}"
            )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.value
        d["decline_reason"] = self.decline_reason.value
        d["customer_segment"] = self.customer_segment.value
        d["created_at"] = self.created_at.isoformat()
        d["last_attempt_at"] = self.last_attempt_at.isoformat()
        d["mandate_created_at"] = (
            self.mandate_created_at.isoformat() if self.mandate_created_at else None
        )
        return d

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)
