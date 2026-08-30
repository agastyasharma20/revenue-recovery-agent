"""
Outcome simulator: given a chosen action on a given event, samples whether
recovery succeeds.

Deliberately harsh and explicit: this is a per-decline-reason x per-action
matrix of real-world-plausible success probabilities, not a flat "any action
has some % chance" model. Nonsensical action/reason pairings (e.g. RETRY on
an abandoned checkout, which has no failed transaction to retry; RETRY on an
expired card, which will keep failing until the card itself changes) are
pinned at or near zero on purpose -- an agent that doesn't know this will get
punished in the evaluation, which is the point.

WhatsApp reminder probabilities are set above SMS and (for pure reminder
scenarios) above email too -- this mirrors both Razorpay's own Failed
Payment Recovery product (which leads with WhatsApp+Email+SMS payment
links) and widely-reported WhatsApp open/read rates in the Indian market
being far higher than SMS. For decline reasons where the underlying problem
is technical (bank timeout, network error) rather than something a
reminder message can fix, the channel barely matters and all three stay low.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from core.schema import DeclineReason, Action

# probability of successful recovery, given (decline_reason, action)
# 0.0 = structurally impossible / nonsensical, not just "low"
EFFECTIVENESS_MATRIX: dict[DeclineReason, dict[Action, float]] = {
    DeclineReason.INSUFFICIENT_FUNDS: {
        Action.RETRY_PAYMENT: 0.32,               # funds may have replenished
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.28,
        Action.SEND_REMINDER_SMS: 0.14,
        Action.SEND_REMINDER_EMAIL: 0.08,
        Action.SEND_REMINDER_WHATSAPP: 0.20,
        Action.OFFER_DISCOUNT: 0.10,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.12,
        Action.ESCALATE_TO_HUMAN_CALL: 0.22,
        Action.ESCALATE_TO_COLLECTIONS: 0.05,
    },
    DeclineReason.EXPIRED_CARD: {
        Action.RETRY_PAYMENT: 0.01,                # same expired card will fail again -- near zero
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.38,
        Action.SEND_REMINDER_SMS: 0.10,
        Action.SEND_REMINDER_EMAIL: 0.07,
        Action.SEND_REMINDER_WHATSAPP: 0.16,
        Action.OFFER_DISCOUNT: 0.04,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.55,   # correct fix: get a new card on file
        Action.ESCALATE_TO_HUMAN_CALL: 0.18,
        Action.ESCALATE_TO_COLLECTIONS: 0.05,
    },
    DeclineReason.BANK_SERVER_TIMEOUT: {
        Action.RETRY_PAYMENT: 0.62,                 # transient -- retry usually just works
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.30,
        Action.SEND_REMINDER_SMS: 0.05,
        Action.SEND_REMINDER_EMAIL: 0.03,
        Action.SEND_REMINDER_WHATSAPP: 0.06,
        Action.OFFER_DISCOUNT: 0.02,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.03,
        Action.ESCALATE_TO_HUMAN_CALL: 0.08,
        Action.ESCALATE_TO_COLLECTIONS: 0.01,
    },
    DeclineReason.NETWORK_ERROR: {
        Action.RETRY_PAYMENT: 0.58,
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.27,
        Action.SEND_REMINDER_SMS: 0.05,
        Action.SEND_REMINDER_EMAIL: 0.03,
        Action.SEND_REMINDER_WHATSAPP: 0.06,
        Action.OFFER_DISCOUNT: 0.02,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.03,
        Action.ESCALATE_TO_HUMAN_CALL: 0.07,
        Action.ESCALATE_TO_COLLECTIONS: 0.01,
    },
    DeclineReason.ISSUER_DECLINED: {
        Action.RETRY_PAYMENT: 0.07,
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.22,
        Action.SEND_REMINDER_SMS: 0.06,
        Action.SEND_REMINDER_EMAIL: 0.04,
        Action.SEND_REMINDER_WHATSAPP: 0.10,
        Action.OFFER_DISCOUNT: 0.05,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.24,
        Action.ESCALATE_TO_HUMAN_CALL: 0.28,
        Action.ESCALATE_TO_COLLECTIONS: 0.06,
    },
    DeclineReason.INVALID_CVV: {
        Action.RETRY_PAYMENT: 0.04,                 # same wrong CVV -> fails again
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.33,
        Action.SEND_REMINDER_SMS: 0.10,
        Action.SEND_REMINDER_EMAIL: 0.07,
        Action.SEND_REMINDER_WHATSAPP: 0.15,
        Action.OFFER_DISCOUNT: 0.03,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.48,    # correct fix: re-enter card details
        Action.ESCALATE_TO_HUMAN_CALL: 0.15,
        Action.ESCALATE_TO_COLLECTIONS: 0.03,
    },
    DeclineReason.FRAUD_SUSPECTED: {
        Action.RETRY_PAYMENT: 0.00,                  # blocked deliberately; retry can worsen the block
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.02,
        Action.SEND_REMINDER_SMS: 0.02,
        Action.SEND_REMINDER_EMAIL: 0.02,
        Action.SEND_REMINDER_WHATSAPP: 0.03,
        Action.OFFER_DISCOUNT: 0.00,                 # irrelevant to a risk block
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.08,
        Action.ESCALATE_TO_HUMAN_CALL: 0.28,         # verify identity manually
        Action.ESCALATE_TO_COLLECTIONS: 0.00,
    },
    DeclineReason.CARD_BLOCKED: {
        Action.RETRY_PAYMENT: 0.00,                  # card is blocked; retry cannot succeed
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.35,
        Action.SEND_REMINDER_SMS: 0.08,
        Action.SEND_REMINDER_EMAIL: 0.05,
        Action.SEND_REMINDER_WHATSAPP: 0.13,
        Action.OFFER_DISCOUNT: 0.02,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.42,
        Action.ESCALATE_TO_HUMAN_CALL: 0.16,
        Action.ESCALATE_TO_COLLECTIONS: 0.03,
    },
    DeclineReason.EXCEEDS_LIMIT: {
        Action.RETRY_PAYMENT: 0.06,
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.30,
        Action.SEND_REMINDER_SMS: 0.07,
        Action.SEND_REMINDER_EMAIL: 0.05,
        Action.SEND_REMINDER_WHATSAPP: 0.11,
        Action.OFFER_DISCOUNT: 0.09,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.15,
        Action.ESCALATE_TO_HUMAN_CALL: 0.14,
        Action.ESCALATE_TO_COLLECTIONS: 0.03,
    },
    DeclineReason.MANDATE_NOT_APPROVED: {
        Action.RETRY_PAYMENT: 0.05,
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.20,
        Action.SEND_REMINDER_SMS: 0.09,
        Action.SEND_REMINDER_EMAIL: 0.06,
        Action.SEND_REMINDER_WHATSAPP: 0.14,
        Action.OFFER_DISCOUNT: 0.03,
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.30,     # re-authorize mandate
        Action.ESCALATE_TO_HUMAN_CALL: 0.20,
        Action.ESCALATE_TO_COLLECTIONS: 0.04,
    },
    DeclineReason.CHECKOUT_ABANDONED: {
        # No failed transaction ever existed here -- "retry" is nonsensical.
        Action.RETRY_PAYMENT: 0.00,
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.00,
        Action.SEND_REMINDER_SMS: 0.18,
        Action.SEND_REMINDER_EMAIL: 0.22,
        Action.SEND_REMINDER_WHATSAPP: 0.29,           # highest-read-rate channel for retail cart nudges
        Action.OFFER_DISCOUNT: 0.34,                  # classic cart-abandonment recovery lever
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.00,
        Action.ESCALATE_TO_HUMAN_CALL: 0.04,           # rarely worth a human call for retail cart
        Action.ESCALATE_TO_COLLECTIONS: 0.00,
    },
    DeclineReason.INVOICE_OVERDUE: {
        # No card transaction exists here either -- B2B receivable.
        Action.RETRY_PAYMENT: 0.00,
        Action.RETRY_WITH_ALTERNATE_METHOD: 0.00,
        Action.SEND_REMINDER_SMS: 0.14,
        Action.SEND_REMINDER_EMAIL: 0.28,
        Action.SEND_REMINDER_WHATSAPP: 0.32,           # WhatsApp Business is a standard B2B vendor-comms channel in India
        Action.OFFER_DISCOUNT: 0.20,                   # early-payment discount
        Action.UPDATE_PAYMENT_METHOD_LINK: 0.00,
        Action.ESCALATE_TO_HUMAN_CALL: 0.38,
        Action.ESCALATE_TO_COLLECTIONS: 0.50,
    },
    DeclineReason.UNKNOWN: {a: 0.02 for a in Action if a != Action.NO_ACTION_DO_NOT_PURSUE},
}

for _reason, _matrix in EFFECTIVENESS_MATRIX.items():
    _matrix[Action.NO_ACTION_DO_NOT_PURSUE] = 0.0


@dataclass
class OutcomeResult:
    recovered: bool
    probability_used: float
    amount_recovered: float


def simulate_outcome(
    decline_reason: DeclineReason, action: Action, amount: float, rng: random.Random | None = None
) -> OutcomeResult:
    rng = rng or random
    prob = EFFECTIVENESS_MATRIX.get(decline_reason, {}).get(action, 0.0)
    success = rng.random() < prob
    return OutcomeResult(
        recovered=success,
        probability_used=prob,
        amount_recovered=amount if success else 0.0,
    )
