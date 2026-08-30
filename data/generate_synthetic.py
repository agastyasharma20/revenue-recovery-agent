"""
Synthetic event generator with HIDDEN ground truth.

Each generated RevenueEvent is accompanied by a ground-truth record
(recoverable flag + true best action) computed from the oracle effectiveness
matrix in core.outcome_simulator. This ground truth is returned in a SEPARATE
dict, keyed by event_id -- it is never attached to the RevenueEvent object
itself and the agent pipeline never sees it. It exists only so
run_evaluation.py can score decisions after the fact.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.schema import (
    RevenueEvent,
    EventSource,
    DeclineReason,
    CustomerSegment,
    VALID_REASONS_FOR_SOURCE,
    Action,
    ACTION_FLAT_COST,
    OFFER_DISCOUNT_PCT_OF_AMOUNT,
)
from core.outcome_simulator import EFFECTIVENESS_MATRIX

# Rough real-world mix: subscription failures are the most common leakage
# source, followed by cart abandonment, with B2B overdue invoices rarer but
# much larger in amount.
SOURCE_WEIGHTS = {
    EventSource.SUBSCRIPTION_FAILED: 0.55,
    EventSource.CHECKOUT_ABANDONED: 0.35,
    EventSource.B2B_RECEIVABLE_OVERDUE: 0.10,
}

REASON_WEIGHTS = {
    DeclineReason.INSUFFICIENT_FUNDS: 0.28,
    DeclineReason.EXPIRED_CARD: 0.14,
    DeclineReason.BANK_SERVER_TIMEOUT: 0.12,
    DeclineReason.ISSUER_DECLINED: 0.14,
    DeclineReason.INVALID_CVV: 0.08,
    DeclineReason.FRAUD_SUSPECTED: 0.05,
    DeclineReason.CARD_BLOCKED: 0.07,
    DeclineReason.EXCEEDS_LIMIT: 0.06,
    DeclineReason.NETWORK_ERROR: 0.04,
    DeclineReason.MANDATE_NOT_APPROVED: 0.02,
}

SEGMENT_WEIGHTS = {
    CustomerSegment.NEW_CUSTOMER: 0.30,
    CustomerSegment.LOW_LTV: 0.30,
    CustomerSegment.MEDIUM_LTV: 0.25,
    CustomerSegment.HIGH_LTV: 0.15,
}

AMOUNT_RANGES = {
    EventSource.SUBSCRIPTION_FAILED: (199.0, 4999.0),
    EventSource.CHECKOUT_ABANDONED: (150.0, 15000.0),
    EventSource.B2B_RECEIVABLE_OVERDUE: (5000.0, 500000.0),
}


@dataclass
class GroundTruth:
    recoverable: bool
    best_action: Action
    best_action_prob: float


def _weighted_choice(rng: random.Random, weights: dict):
    keys = list(weights.keys())
    probs = list(weights.values())
    return rng.choices(keys, weights=probs, k=1)[0]


def _oracle_ground_truth(decline_reason: DeclineReason, amount: float) -> GroundTruth:
    matrix = EFFECTIVENESS_MATRIX.get(decline_reason, {})
    non_noop = {a: p for a, p in matrix.items() if a != Action.NO_ACTION_DO_NOT_PURSUE}
    best_action = max(non_noop, key=non_noop.get)
    best_prob = non_noop[best_action]

    if best_action == Action.OFFER_DISCOUNT:
        cost = amount * OFFER_DISCOUNT_PCT_OF_AMOUNT
    else:
        cost = ACTION_FLAT_COST.get(best_action, 2.0)

    oracle_ev = best_prob * amount - cost
    return GroundTruth(recoverable=oracle_ev > 0, best_action=best_action, best_action_prob=best_prob)


def _make_b2b_customer_pool(rng: random.Random, seed: int, n_events: int) -> list[dict]:
    """A fixed pool of recurring B2B customers (businesses reasonably have
    multiple overdue invoices over an observation window, unlike a
    one-off consumer checkout). Each has a stable segment and a hidden
    "reliability" trait -- some businesses are just more likely to keep a
    payment promise than others -- used by core/promise_tracking.py.
    Pool size scales with batch size so small batches still get plausible
    repeat customers without overcrowding tiny batches."""
    pool_size = max(8, n_events // 15)
    pool = []
    for i in range(pool_size):
        pool.append(
            {
                "customer_id": f"biz_{seed}_{i:03d}",
                "segment": _weighted_choice(rng, SEGMENT_WEIGHTS),
                "reliability": rng.betavariate(2, 2),  # most businesses cluster around 0.5, some very high/low
            }
        )
    return pool


def generate_batch(n: int, seed: int, now: datetime | None = None):
    """Returns (events: list[RevenueEvent], ground_truth: dict[event_id, GroundTruth])."""
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)

    b2b_customers = _make_b2b_customer_pool(rng, seed, n)
    events = []
    ground_truth = {}

    for _ in range(n):
        source = _weighted_choice(rng, SOURCE_WEIGHTS)
        valid_reasons = VALID_REASONS_FOR_SOURCE[source]

        if source == EventSource.SUBSCRIPTION_FAILED:
            reason_weights = {r: w for r, w in REASON_WEIGHTS.items() if r in valid_reasons}
            decline_reason = _weighted_choice(rng, reason_weights)
        else:
            decline_reason = next(iter(valid_reasons))

        lo, hi = AMOUNT_RANGES[source]
        amount = round(rng.uniform(lo, hi), 2)

        b2b_customer = None
        if source == EventSource.B2B_RECEIVABLE_OVERDUE:
            b2b_customer = rng.choice(b2b_customers)
            segment = CustomerSegment(b2b_customer["segment"])
        else:
            segment = _weighted_choice(rng, SEGMENT_WEIGHTS)

        # created_at spread over the last 10 days, deliberately including
        # some events past the 7-day pursuit window so compliance has
        # something real to filter.
        age_days = rng.uniform(0, 10)
        created_at = now - timedelta(days=age_days)

        if source == EventSource.SUBSCRIPTION_FAILED:
            retry_count = rng.choices([0, 1, 2, 3, 4], weights=[0.45, 0.25, 0.15, 0.10, 0.05])[0]
            # last attempt: sometimes very recent (<24h, to trip the min-gap
            # rule), sometimes long enough ago to be compliant.
            hours_since_last = rng.choices(
                [rng.uniform(0.5, 20), rng.uniform(25, 96)], weights=[0.35, 0.65]
            )[0]
            last_attempt_at = now - timedelta(hours=hours_since_last)
            # mandate created: sometimes brand-new (<24h ago), sometimes old.
            mandate_age_hours = rng.choices(
                [rng.uniform(1, 20), rng.uniform(48, 24 * 30)], weights=[0.15, 0.85]
            )[0]
            mandate_created_at = now - timedelta(hours=mandate_age_hours)
        else:
            retry_count = 0
            last_attempt_at = created_at
            mandate_created_at = None

        event_kwargs = dict(
            source=source,
            decline_reason=decline_reason,
            amount=amount,
            customer_segment=segment,
            created_at=created_at,
            last_attempt_at=last_attempt_at,
            retry_count=retry_count,
            mandate_created_at=mandate_created_at,
        )
        if b2b_customer is not None:
            # reuse the same customer_id across this business's invoices, and
            # stash their hidden reliability trait for promise_tracking.py --
            # never read by the diagnosis/prioritization/policy layers, only
            # by the post-hoc promise simulation (same "hidden ground truth"
            # discipline as GroundTruth above).
            event_kwargs["customer_id"] = b2b_customer["customer_id"]
            event_kwargs["metadata"] = {"_b2b_reliability": b2b_customer["reliability"]}

        event = RevenueEvent(**event_kwargs)
        events.append(event)
        ground_truth[event.event_id] = _oracle_ground_truth(decline_reason, amount)

    return events, ground_truth


if __name__ == "__main__":
    evs, gt = generate_batch(10, seed=42)
    for e in evs:
        g = gt[e.event_id]
        print(
            f"{e.source.value:28s} {e.decline_reason.value:20s} "
            f"amt={e.amount:>10.2f} retry={e.retry_count} | "
            f"GT: recoverable={g.recoverable!s:5s} best_action={g.best_action.value}"
        )
