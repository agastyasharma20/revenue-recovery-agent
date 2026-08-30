"""
Action selection.

Two independent mechanisms, both selectable at the engine level:

1. DeterministicPolicy -- a diagnosis-informed lookup table. This is the
   reliable default: given a (source, diagnosis category) it returns an
   ordered list of sensible candidate actions, most-preferred first, and the
   engine picks the first one that survives compliance. This encodes real
   domain knowledge and is what you'd ship on day one.

2. ThompsonSamplingBandit -- a genuine multi-armed bandit with NO hardcoded
   reason-to-action mapping. It starts with a flat Beta(1,1) prior on every
   (segment, action) pair and learns purely from observed outcome feedback
   which action actually works best for which failure-type segment. Given
   enough rounds it should independently rediscover the same sensible
   mappings the deterministic policy was handed -- that convergence is
   demonstrated in Phase 1 (bandit_convergence_demo.py), not assumed here.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from core.schema import RevenueEvent, EventSource, DiagnosisCategory, Action
from core.classifier import Diagnosis

# Actions the bandit is allowed to pick among (excludes the "give up" action --
# that's a prioritizer-level decision, not a recovery action).
BANDIT_ARMS = [a for a in Action if a != Action.NO_ACTION_DO_NOT_PURSUE]

# --- 1. Deterministic, diagnosis-informed policy ---------------------------

_CANDIDATE_ACTIONS: dict[tuple[EventSource, DiagnosisCategory], list[Action]] = {
    (EventSource.SUBSCRIPTION_FAILED, DiagnosisCategory.TRANSIENT_RETRIABLE): [
        Action.RETRY_PAYMENT,
        Action.RETRY_WITH_ALTERNATE_METHOD,
        Action.SEND_REMINDER_WHATSAPP,
        Action.SEND_REMINDER_SMS,
    ],
    (EventSource.SUBSCRIPTION_FAILED, DiagnosisCategory.SOFT_DECLINE_RETRIABLE): [
        Action.RETRY_PAYMENT,
        Action.RETRY_WITH_ALTERNATE_METHOD,
        Action.SEND_REMINDER_WHATSAPP,
        Action.SEND_REMINDER_SMS,
        Action.ESCALATE_TO_HUMAN_CALL,
    ],
    (EventSource.SUBSCRIPTION_FAILED, DiagnosisCategory.HARD_DECLINE_UNRETRIABLE): [
        Action.UPDATE_PAYMENT_METHOD_LINK,
        Action.RETRY_WITH_ALTERNATE_METHOD,
        Action.SEND_REMINDER_WHATSAPP,
        Action.ESCALATE_TO_HUMAN_CALL,
    ],
    (EventSource.SUBSCRIPTION_FAILED, DiagnosisCategory.RISK_BLOCK): [
        Action.ESCALATE_TO_HUMAN_CALL,
        Action.UPDATE_PAYMENT_METHOD_LINK,
    ],
    (EventSource.CHECKOUT_ABANDONED, DiagnosisCategory.CUSTOMER_INACTION): [
        Action.OFFER_DISCOUNT,
        Action.SEND_REMINDER_WHATSAPP,
        Action.SEND_REMINDER_EMAIL,
        Action.SEND_REMINDER_SMS,
    ],
    (EventSource.B2B_RECEIVABLE_OVERDUE, DiagnosisCategory.CUSTOMER_INACTION): [
        Action.ESCALATE_TO_COLLECTIONS,
        Action.ESCALATE_TO_HUMAN_CALL,
        Action.SEND_REMINDER_WHATSAPP,
        Action.SEND_REMINDER_EMAIL,
        Action.OFFER_DISCOUNT,
    ],
}

_FALLBACK_CANDIDATES = [Action.SEND_REMINDER_WHATSAPP, Action.SEND_REMINDER_EMAIL, Action.SEND_REMINDER_SMS]


class DeterministicPolicy:
    def candidate_actions(self, event: RevenueEvent, diagnosis: Diagnosis) -> list[Action]:
        key = (event.source, diagnosis.category)
        return list(_CANDIDATE_ACTIONS.get(key, _FALLBACK_CANDIDATES))


# --- 2. Thompson Sampling contextual-free bandit ---------------------------


@dataclass
class ThompsonSamplingBandit:
    """Beta-Bernoulli Thompson Sampling, one independent bandit per segment.

    segment: a string key representing "failure type" (we use decline_reason).
    No action is ever excluded a priori -- the bandit tries everything and
    lets outcome feedback do the pruning.
    """

    rng: random.Random = field(default_factory=random.Random)
    alpha: dict = field(default_factory=lambda: defaultdict(lambda: 1.0))
    beta: dict = field(default_factory=lambda: defaultdict(lambda: 1.0))
    pulls: dict = field(default_factory=lambda: defaultdict(int))

    def select_action(self, segment: str, arms: Optional[list[Action]] = None) -> Action:
        arms = arms or BANDIT_ARMS
        samples = {
            a: self.rng.betavariate(self.alpha[(segment, a)], self.beta[(segment, a)])
            for a in arms
        }
        return max(samples, key=samples.get)

    def update(self, segment: str, action: Action, reward: int) -> None:
        self.pulls[(segment, action)] += 1
        if reward:
            self.alpha[(segment, action)] += 1
        else:
            self.beta[(segment, action)] += 1

    def best_action_per_segment(self, segments: list[str], arms: Optional[list[Action]] = None) -> dict:
        arms = arms or BANDIT_ARMS
        result = {}
        for seg in segments:
            means = {
                a: self.alpha[(seg, a)] / (self.alpha[(seg, a)] + self.beta[(seg, a)])
                for a in arms
            }
            best = max(means, key=means.get)
            result[seg] = {
                "best_action": best,
                "posterior_mean": means[best],
                "pulls": self.pulls.get((seg, best), 0),
            }
        return result
