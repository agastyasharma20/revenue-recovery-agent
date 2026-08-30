"""
Orchestrator: ingest -> diagnose -> prioritize -> compliance check ->
action selection -> outcome -> audit log.

One call to process_event() runs an event through every layer and returns a
DecisionRecord with the full trail. Every call also appends one hash-chained
line to the audit log (if configured), so replaying the JSONL file after the
fact reconstructs exactly what the engine did and why.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.schema import RevenueEvent, Action
from core.classifier import Classifier, Diagnosis
from core.prioritizer import score as prioritize, PriorityResult
from core.compliance import ComplianceChecker, ComplianceResult
from core.policy import DeterministicPolicy, ThompsonSamplingBandit, BANDIT_ARMS
from core.outcome_simulator import simulate_outcome, OutcomeResult
from core.audit import AuditLog
from core.circuit_breaker import CircuitBreaker
from core.logging_config import get_logger
from core.promise_tracking import PromiseTracker, Promise


@dataclass
class DecisionRecord:
    event: RevenueEvent
    diagnosis: Diagnosis
    priority: PriorityResult
    chosen_action: Action
    compliance: ComplianceResult
    outcome: Optional[OutcomeResult]
    latencies_ms: dict = field(default_factory=dict)
    promise: Optional[Promise] = None

    @property
    def recovered_amount(self) -> float:
        return self.outcome.amount_recovered if self.outcome else 0.0

    @property
    def pursued(self) -> bool:
        return self.chosen_action != Action.NO_ACTION_DO_NOT_PURSUE

    def to_audit_payload(self) -> dict:
        return {
            "event_id": self.event.event_id,
            "trace_id": self.event.trace_id,
            "customer_id": self.event.customer_id,
            "source": self.event.source.value,
            "decline_reason": self.event.decline_reason.value,
            "amount": self.event.amount,
            "retry_count": self.event.retry_count,
            "customer_segment": self.event.customer_segment.value,
            "diagnosis": {
                "category": self.diagnosis.category.value,
                "confidence": round(self.diagnosis.confidence, 4),
                "rationale": self.diagnosis.rationale,
                "llm_used": self.diagnosis.llm_used,
            },
            "priority": {
                "ev": round(self.priority.ev, 4),
                "pursue": self.priority.pursue,
                "reason": self.priority.reason,
            },
            "compliance": {
                "allowed": self.compliance.allowed,
                "reason": self.compliance.reason,
                "rules_version": self.compliance.rules_version,
            },
            "chosen_action": self.chosen_action.value,
            "outcome": (
                {
                    "recovered": self.outcome.recovered,
                    "probability_used": round(self.outcome.probability_used, 4),
                    "amount_recovered": self.outcome.amount_recovered,
                }
                if self.outcome
                else None
            ),
            "promise": self.promise.to_dict() if self.promise else None,
        }


class RecoveryEngine:
    def __init__(
        self,
        use_llm: bool = False,
        policy_mode: str = "deterministic",  # "deterministic" | "bandit"
        audit_path: Optional[str] = "results/audit_log.jsonl",
        bandit: Optional[ThompsonSamplingBandit] = None,
        rng: Optional[random.Random] = None,
        seed: Optional[int] = None,
        breaker: Optional[CircuitBreaker] = None,
        log_path: Optional[str] = "results/agent.jsonl",
        promise_tracker: Optional[PromiseTracker] = None,
    ):
        assert policy_mode in ("deterministic", "bandit")
        self.breaker = breaker if breaker is not None else CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)
        self.classifier = Classifier(use_llm=use_llm, breaker=self.breaker)
        self.compliance = ComplianceChecker()
        self.det_policy = DeterministicPolicy()
        self.bandit = bandit if bandit is not None else ThompsonSamplingBandit(
            rng=random.Random(seed) if seed is not None else random.Random()
        )
        self.policy_mode = policy_mode
        self.audit = AuditLog(audit_path) if audit_path else None
        self.rng = rng if rng is not None else (random.Random(seed) if seed is not None else random.Random())
        self.logger = get_logger(log_path=log_path) if log_path else None
        self.promise_tracker = promise_tracker if promise_tracker is not None else PromiseTracker(
            rng=random.Random(seed + 777) if seed is not None else random.Random()
        )

    def _select_action(self, event: RevenueEvent, diagnosis: Diagnosis, now: datetime):
        """Returns (chosen_action, compliance_result)."""
        if self.policy_mode == "deterministic":
            candidates = self.det_policy.candidate_actions(event, diagnosis)
            last_cr = None
            for cand in candidates:
                cr = self.compliance.check(event, cand, now)
                last_cr = cr
                if cr.allowed:
                    return cand, cr
            return Action.NO_ACTION_DO_NOT_PURSUE, last_cr
        else:
            allowed_arms = [a for a in BANDIT_ARMS if self.compliance.check(event, a, now).allowed]
            if not allowed_arms:
                cr = self.compliance.check(event, Action.RETRY_PAYMENT, now)
                return Action.NO_ACTION_DO_NOT_PURSUE, cr
            segment = event.decline_reason.value
            chosen = self.bandit.select_action(segment, arms=allowed_arms)
            cr = self.compliance.check(event, chosen, now)
            return chosen, cr

    def _log(self, trace_id: str, layer: str, message: str) -> None:
        if self.logger is not None:
            self.logger.info(message, extra={"trace_id": trace_id, "layer": layer})

    def process_event(self, event: RevenueEvent, now: Optional[datetime] = None) -> DecisionRecord:
        now = now or datetime.now(timezone.utc)
        trace_id = event.trace_id
        latencies: dict[str, float] = {}

        t0 = time.perf_counter()
        diagnosis = self.classifier.diagnose(event)
        latencies["diagnose"] = (time.perf_counter() - t0) * 1000
        self._log(trace_id, "diagnose", f"category={diagnosis.category.value} confidence={diagnosis.confidence:.2f} llm_used={diagnosis.llm_used}")

        t0 = time.perf_counter()
        # A customer's own broken/kept promise history (from EARLIER events
        # in this engine's lifetime) feeds forward into this decision. Only
        # passed when there's actual history -- a first-time customer gets
        # no adjustment rather than a misleading "neutral reliability" note.
        prior_history = self.promise_tracker.customer_history(event.customer_id)
        customer_reliability = (
            self.promise_tracker.customer_reliability_score(event.customer_id) if prior_history else None
        )
        priority = prioritize(event, diagnosis, customer_reliability=customer_reliability)
        latencies["prioritize"] = (time.perf_counter() - t0) * 1000
        self._log(trace_id, "prioritize", f"ev={priority.ev:.2f} pursue={priority.pursue}")

        t0 = time.perf_counter()
        if not priority.pursue:
            chosen_action = Action.NO_ACTION_DO_NOT_PURSUE
            compliance_result = ComplianceResult(True, "Not pursued (negative EV) -- compliance N/A.", self.compliance.version)
            outcome = None
        else:
            chosen_action, compliance_result = self._select_action(event, diagnosis, now)
            if chosen_action == Action.NO_ACTION_DO_NOT_PURSUE:
                outcome = None
            else:
                outcome = simulate_outcome(event.decline_reason, chosen_action, event.amount, rng=self.rng)
                if self.policy_mode == "bandit":
                    self.bandit.update(event.decline_reason.value, chosen_action, int(outcome.recovered))
        promise = None
        if outcome is not None:
            promise = self.promise_tracker.maybe_record_promise(event, chosen_action, outcome.recovered, now)
        latencies["policy_and_compliance"] = (time.perf_counter() - t0) * 1000
        self._log(
            trace_id, "policy",
            f"action={chosen_action.value} compliance_allowed={compliance_result.allowed} "
            f"outcome_recovered={outcome.recovered if outcome else None}",
        )

        record = DecisionRecord(
            event=event,
            diagnosis=diagnosis,
            priority=priority,
            chosen_action=chosen_action,
            compliance=compliance_result,
            outcome=outcome,
            latencies_ms=latencies,
            promise=promise,
        )

        t0 = time.perf_counter()
        if self.audit is not None:
            self.audit.append(record.to_audit_payload())
        latencies["audit"] = (time.perf_counter() - t0) * 1000
        self._log(trace_id, "audit", "decision appended to hash-chained audit log")

        return record

    def process_batch(self, events: list[RevenueEvent], now: Optional[datetime] = None) -> list[DecisionRecord]:
        return [self.process_event(e, now=now) for e in events]
