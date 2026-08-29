"""
Contextual bandit upgrade (Phase 1): disjoint LinUCB.

Each arm (Action) gets its own linear model over a shared context feature
vector: decline reason (one-hot), amount bucket (one-hot low/medium/high),
retry-count bucket (one-hot), hour-of-day (cyclical sin/cos), day-of-week
(cyclical sin/cos), and customer LTV segment (one-hot). No reason->action
mapping is hardcoded anywhere in this file -- the bandit only ever sees
raw features and a reward signal (recovered: 0/1) and has to work out for
itself which arm has the best expected reward in which context.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from core.schema import RevenueEvent, DeclineReason, CustomerSegment, Action

REASON_LIST = list(DeclineReason)
SEGMENT_LIST = list(CustomerSegment)

# 3 amount buckets, thresholds chosen to span both consumer (subscription/
# checkout, INR 150-15,000) and B2B (INR 5,000-500,000) amounts.
def _amount_bucket(amount: float) -> int:
    if amount < 2000:
        return 0  # low
    if amount < 50000:
        return 1  # medium
    return 2  # high


def _retry_bucket(retry_count: int) -> int:
    return min(retry_count, 3)  # 0, 1, 2, 3+


FEATURE_DIM = len(REASON_LIST) + 3 + 4 + 2 + 2 + len(SEGMENT_LIST) + 1  # + bias


def build_context(event: RevenueEvent, now: datetime) -> np.ndarray:
    x = np.zeros(FEATURE_DIM, dtype=float)
    i = 0

    reason_idx = REASON_LIST.index(event.decline_reason)
    x[i + reason_idx] = 1.0
    i += len(REASON_LIST)

    x[i + _amount_bucket(event.amount)] = 1.0
    i += 3

    x[i + _retry_bucket(event.retry_count)] = 1.0
    i += 4

    hour_angle = 2 * math.pi * now.hour / 24
    x[i] = math.sin(hour_angle)
    x[i + 1] = math.cos(hour_angle)
    i += 2

    dow_angle = 2 * math.pi * now.weekday() / 7
    x[i] = math.sin(dow_angle)
    x[i + 1] = math.cos(dow_angle)
    i += 2

    seg_idx = SEGMENT_LIST.index(event.customer_segment)
    x[i + seg_idx] = 1.0
    i += len(SEGMENT_LIST)

    x[i] = 1.0  # bias
    i += 1

    assert i == FEATURE_DIM
    return x


@dataclass
class LinUCBBandit:
    """Disjoint LinUCB: one (A, b) pair per arm, shared feature space."""

    arms: list[Action]
    alpha: float = 1.0  # exploration strength
    d: int = FEATURE_DIM
    A: dict = field(default_factory=dict)
    b: dict = field(default_factory=dict)

    def __post_init__(self):
        for a in self.arms:
            if a not in self.A:
                self.A[a] = np.eye(self.d)
                self.b[a] = np.zeros(self.d)

    def select_action(self, x: np.ndarray, arms: list[Action] | None = None) -> Action:
        arms = arms or self.arms
        best_arm, best_score = None, -math.inf
        for a in arms:
            A_inv = np.linalg.inv(self.A[a])
            theta = A_inv @ self.b[a]
            mean = float(theta @ x)
            bonus = self.alpha * math.sqrt(max(float(x @ A_inv @ x), 0.0))
            score = mean + bonus
            if score > best_score:
                best_score, best_arm = score, a
        return best_arm

    def update(self, action: Action, x: np.ndarray, reward: float) -> None:
        self.A[action] += np.outer(x, x)
        self.b[action] += reward * x

    def predicted_mean(self, action: Action, x: np.ndarray) -> float:
        theta = np.linalg.inv(self.A[action]) @ self.b[action]
        return float(theta @ x)
