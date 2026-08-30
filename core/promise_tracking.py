"""
Promise-to-pay (PTP) tracking -- named explicitly in Razorpay's Track 03
brief alongside B2B receivables chasing.

In real collections/dunning workflows, escalating a case (a collections
call, a human call) often doesn't recover money on the spot -- it extracts a
COMMITMENT ("we'll pay by the 15th"). What happens next matters as much as
the commitment itself: a kept promise closes the case; a broken promise
should trigger firmer follow-up and should make the system trust that
customer's next promise less. This module tracks that lifecycle and folds
broken-promise history back into future EV estimates for the same customer
-- a genuine cross-event feedback loop, not a per-event decision in isolation.

Design note on "hidden" data: a customer's true reliability trait
(data/generate_synthetic.py's `_b2b_reliability`) is deliberately kept out
of RevenueEvent's normal fields and out of the diagnosis/prioritization/
policy layers -- exactly like the oracle GroundTruth used for scoring. Only
this module (acting as part of the outcome simulation, not the decision
pipeline) is allowed to read it, to decide whether a promise gets kept.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from core.schema import RevenueEvent, Action

# Actions that plausibly extract a promise rather than an immediate payment --
# a collections call or human call is a negotiation, not an instant charge.
PROMISE_ELIGIBLE_ACTIONS = {Action.ESCALATE_TO_COLLECTIONS, Action.ESCALATE_TO_HUMAN_CALL}


class PromiseStatus(str, Enum):
    PENDING = "pending"
    KEPT = "kept"
    BROKEN = "broken"


@dataclass
class Promise:
    promise_id: str
    event_id: str
    customer_id: str
    amount: float
    channel: Action
    made_at: datetime
    promised_date: datetime
    status: PromiseStatus = PromiseStatus.PENDING
    resolved_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "promise_id": self.promise_id,
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "amount": self.amount,
            "channel": self.channel.value,
            "made_at": self.made_at.isoformat(),
            "promised_date": self.promised_date.isoformat(),
            "status": self.status.value,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


def _promise_window_days(amount: float, rng: random.Random) -> int:
    """Bigger invoices get negotiated longer payment windows -- a
    realistic collections dynamic, not just noise."""
    base = 3 if amount < 20000 else 7 if amount < 100000 else 14
    return base + rng.randint(-1, 3)


def _default_reliability(event: RevenueEvent) -> float:
    """Fallback used when an event has no hidden _b2b_reliability trait
    (e.g. synthetic events built directly in tests, or a real Razorpay
    webhook event where we have no collections history yet) -- a neutral
    prior, not an assumption of either good or bad faith."""
    return event.metadata.get("_b2b_reliability", 0.5)


class PromiseTracker:
    """In-memory promise ledger, one per engine/session. A production
    system would persist this in a database keyed by customer_id so
    reliability survives restarts -- kept in-memory here for simplicity,
    same tradeoff core.policy's bandit state makes."""

    def __init__(self, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random()
        self.promises: dict[str, Promise] = {}
        self._by_customer: dict[str, list[str]] = {}
        self._counter = 0

    def maybe_record_promise(
        self, event: RevenueEvent, chosen_action: Action, recovered: bool, now: datetime
    ) -> Optional[Promise]:
        """Called once per decision. If the chosen action is a
        promise-eligible escalation, records a Promise and -- since this
        simulation resolves outcomes immediately rather than waiting for a
        real calendar to pass -- also resolves it immediately using the
        SAME `recovered` flag core.outcome_simulator already decided. This
        keeps promise status consistent with the rest of the pipeline: a
        promise is "kept" exactly when the underlying action recovered the
        money, "broken" exactly when it didn't. What this module adds is
        the tracking, the customer-level history, and the amount/date
        realism -- not a second independent coin flip."""
        if chosen_action not in PROMISE_ELIGIBLE_ACTIONS:
            return None

        self._counter += 1
        promise_id = f"ptp_{self._counter:06d}"
        promised_date = now + timedelta(days=_promise_window_days(event.amount, self.rng))

        promise = Promise(
            promise_id=promise_id,
            event_id=event.event_id,
            customer_id=event.customer_id,
            amount=event.amount,
            channel=chosen_action,
            made_at=now,
            promised_date=promised_date,
            status=PromiseStatus.KEPT if recovered else PromiseStatus.BROKEN,
            resolved_at=promised_date if recovered else None,
        )
        self.promises[promise_id] = promise
        self._by_customer.setdefault(event.customer_id, []).append(promise_id)
        return promise

    def customer_history(self, customer_id: str) -> list[Promise]:
        return [self.promises[pid] for pid in self._by_customer.get(customer_id, [])]

    def customer_reliability_score(self, customer_id: str) -> float:
        """kept / (kept + broken), Laplace-smoothed so a customer with no
        history yet gets a neutral 0.5 rather than an undefined 0/0."""
        history = self.customer_history(customer_id)
        kept = sum(1 for p in history if p.status == PromiseStatus.KEPT)
        broken = sum(1 for p in history if p.status == PromiseStatus.BROKEN)
        return (kept + 1) / (kept + broken + 2)

    def summary(self) -> dict:
        all_promises = list(self.promises.values())
        kept = sum(1 for p in all_promises if p.status == PromiseStatus.KEPT)
        broken = sum(1 for p in all_promises if p.status == PromiseStatus.BROKEN)
        total = len(all_promises)
        return {
            "total_promises": total,
            "kept": kept,
            "broken": broken,
            "kept_rate": (kept / total) if total else 0.0,
            "distinct_customers": len(self._by_customer),
            "total_inr_promised": sum(p.amount for p in all_promises),
            "total_inr_kept": sum(p.amount for p in all_promises if p.status == PromiseStatus.KEPT),
        }
