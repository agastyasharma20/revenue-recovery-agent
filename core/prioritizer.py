"""
Expected-value prioritization.

EV = P(recovery) * amount - cost_of_pursuing.

Important honesty note: the probabilities used here are the agent's *belief*
about recovery likelihood for the best-fit action of a given diagnosis
category -- a coarse, hand-set prior representing what a business would
plausibly estimate from historical data. It is deliberately NOT read from
core.outcome_simulator's ground-truth effectiveness matrix. If the
prioritizer could see the simulator's exact numbers it would be cheating
(an oracle), which would make the evaluation meaningless. Some mismatch
between this prior and the simulator's ground truth is expected and
realistic -- a real system's estimates are never perfectly calibrated either.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.classifier import Diagnosis
from core.schema import RevenueEvent, DiagnosisCategory, OFFER_DISCOUNT_PCT_OF_AMOUNT

# Agent's prior belief: P(recovery via the best-fit action for this diagnosis).
_PRIOR_RECOVERY_PROB = {
    DiagnosisCategory.TRANSIENT_RETRIABLE: 0.55,
    DiagnosisCategory.SOFT_DECLINE_RETRIABLE: 0.30,
    DiagnosisCategory.HARD_DECLINE_UNRETRIABLE: 0.40,
    DiagnosisCategory.RISK_BLOCK: 0.15,
    DiagnosisCategory.CUSTOMER_INACTION: 0.25,
    DiagnosisCategory.UNKNOWN: 0.05,
}

# Estimated flat cost (INR) of the best-fit action for this diagnosis.
# CUSTOMER_INACTION's best-fit action (discount) has an amount-proportional
# cost instead, handled separately below.
_PRIOR_ACTION_COST = {
    DiagnosisCategory.TRANSIENT_RETRIABLE: 2.0,      # retry_payment
    DiagnosisCategory.SOFT_DECLINE_RETRIABLE: 5.0,   # retry_with_alternate_method
    DiagnosisCategory.HARD_DECLINE_UNRETRIABLE: 1.0,  # update_payment_method_link
    DiagnosisCategory.RISK_BLOCK: 150.0,             # escalate_to_human_call
    DiagnosisCategory.UNKNOWN: 2.0,
}


@dataclass
class PriorityResult:
    ev: float
    estimated_prob: float
    estimated_cost: float
    pursue: bool
    reason: str


def score(event: RevenueEvent, diagnosis: Diagnosis) -> PriorityResult:
    prior_prob = _PRIOR_RECOVERY_PROB.get(diagnosis.category, 0.05)

    # Blend the category prior with classifier confidence: a low-confidence
    # diagnosis shouldn't get the full prior probability at face value.
    effective_prob = prior_prob * (0.5 + 0.5 * diagnosis.confidence)

    if diagnosis.category == DiagnosisCategory.CUSTOMER_INACTION:
        cost = event.amount * OFFER_DISCOUNT_PCT_OF_AMOUNT
    else:
        cost = _PRIOR_ACTION_COST.get(diagnosis.category, 2.0)

    ev = effective_prob * event.amount - cost
    pursue = ev > 0

    if pursue:
        reason = (
            f"EV=+{ev:,.2f} INR (P~{effective_prob:.2f} x amount {event.amount:,.2f} "
            f"- cost {cost:,.2f}) -> worth pursuing."
        )
    else:
        reason = (
            f"EV={ev:,.2f} INR (P~{effective_prob:.2f} x amount {event.amount:,.2f} "
            f"- cost {cost:,.2f}) is not positive -> do not pursue."
        )

    return PriorityResult(
        ev=ev, estimated_prob=effective_prob, estimated_cost=cost, pursue=pursue, reason=reason
    )
