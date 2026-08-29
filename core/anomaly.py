"""
Anomaly detection and root-cause clustering (Phase 2).

Two independent mechanisms:

1. AnomalyDetector -- an Isolation Forest trained on "normal" failure
   patterns (amount, retry count, time-of-day/week, decline reason, source),
   flagging individual events that look statistically unusual against that
   baseline. This catches one-off weirdness (e.g. a single huge, oddly-timed
   failure) that isn't necessarily part of a bigger pattern.

2. detect_systemic_incidents -- root-cause clustering that groups failures
   within a rolling time window by shared attributes (same decline reason +
   same amount bucket, standing in for "same bank/BIN range" without needing
   real BIN data) and raises a SYSTEMIC INCIDENT flag once a cluster's count
   crosses a threshold. This catches the opposite failure mode: many
   individually-unremarkable failures that are actually one shared root
   cause (a bank outage) and should be handled/escalated as ONE incident
   instead of N separate cases.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest

from core.schema import RevenueEvent, DeclineReason, EventSource

REASON_LIST = list(DeclineReason)
SOURCE_LIST = list(EventSource)


def _amount_bucket_label(amount: float) -> str:
    if amount < 2000:
        return "low"
    if amount < 50000:
        return "medium"
    return "high"


def _features(event: RevenueEvent) -> list[float]:
    x = [math.log1p(event.amount), float(event.retry_count)]

    hour_angle = 2 * math.pi * event.created_at.hour / 24
    x += [math.sin(hour_angle), math.cos(hour_angle)]

    dow_angle = 2 * math.pi * event.created_at.weekday() / 7
    x += [math.sin(dow_angle), math.cos(dow_angle)]

    reason_onehot = [1.0 if event.decline_reason == r else 0.0 for r in REASON_LIST]
    source_onehot = [1.0 if event.source == s else 0.0 for s in SOURCE_LIST]
    return x + reason_onehot + source_onehot


@dataclass
class AnomalyResult:
    is_anomaly: bool
    anomaly_score: float  # higher = more anomalous (negated sklearn score)


class AnomalyDetector:
    """Wraps sklearn's IsolationForest with the feature encoding above."""

    def __init__(self, contamination: float = 0.05, random_state: int = 0):
        self.model = IsolationForest(contamination=contamination, random_state=random_state)
        self._fitted = False

    def fit(self, normal_events: list[RevenueEvent]) -> "AnomalyDetector":
        X = np.array([_features(e) for e in normal_events])
        self.model.fit(X)
        self._fitted = True
        return self

    def score(self, event: RevenueEvent) -> AnomalyResult:
        if not self._fitted:
            raise RuntimeError("AnomalyDetector.fit() must be called before score().")
        x = np.array([_features(event)])
        # sklearn: decision_function is higher = more normal. We negate so
        # higher = more anomalous, which reads more naturally in logs.
        raw_score = -float(self.model.decision_function(x)[0])
        is_anomaly = bool(self.model.predict(x)[0] == -1)
        return AnomalyResult(is_anomaly=is_anomaly, anomaly_score=raw_score)

    def score_batch(self, events: list[RevenueEvent]) -> list[AnomalyResult]:
        return [self.score(e) for e in events]


@dataclass
class SystemicIncident:
    decline_reason: DeclineReason
    amount_bucket: str
    count: int
    window_start: datetime
    window_end: datetime
    event_ids: list[str]
    message: str


def detect_systemic_incidents(
    events: list[RevenueEvent],
    window_hours: float = 2.0,
    threshold: int = 15,
) -> list[SystemicIncident]:
    """Processes events in chronological order, maintaining a rolling
    trailing window per (decline_reason, amount_bucket) cluster key. Emits
    ONE SystemicIncident the moment a cluster's rolling count first crosses
    `threshold`, then requires the count to drop back below threshold
    before it can fire again for that same key (avoids re-flagging the same
    ongoing spike once per event)."""

    ordered = sorted(events, key=lambda e: e.created_at)
    window = timedelta(hours=window_hours)

    buffers: dict[tuple, deque] = {}
    active: dict[tuple, bool] = {}
    incidents: list[SystemicIncident] = []

    for event in ordered:
        key = (event.decline_reason, _amount_bucket_label(event.amount))
        buf = buffers.setdefault(key, deque())
        buf.append(event)

        cutoff = event.created_at - window
        while buf and buf[0].created_at < cutoff:
            buf.popleft()

        count = len(buf)
        is_active = active.get(key, False)

        if count >= threshold and not is_active:
            active[key] = True
            reason, bucket = key
            incidents.append(
                SystemicIncident(
                    decline_reason=reason,
                    amount_bucket=bucket,
                    count=count,
                    window_start=buf[0].created_at,
                    window_end=event.created_at,
                    event_ids=[e.event_id for e in buf],
                    message=(
                        f"{count} {reason.value} failures ({bucket}-amount bucket) in the last "
                        f"{window_hours:.0f}h -- possible systemic issue, escalating as ONE incident "
                        f"rather than {count} separate cases."
                    ),
                )
            )
        elif count < threshold and is_active:
            active[key] = False

    return incidents
