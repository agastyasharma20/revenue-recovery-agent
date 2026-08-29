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


@dataclass
class DecisionRecord:
    event: RevenueEvent
    diagnosis: Diagnosis
    priority: PriorityResult
    chosen_action: Action
    compliance: ComplianceResult
    outcome: Optional[OutcomeResult]

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
    ):
        assert policy_mode in ("deterministic", "bandit")
        self.classifier = Classifier(use_llm=use_llm)
        self.compliance = ComplianceChecker()
        self.det_policy = DeterministicPolicy()
        self.bandit = bandit if bandit is not None else ThompsonSamplingBandit(
            rng=random.Random(seed) if seed is not None else random.Random()
        )
        self.policy_mode = policy_mode
        self.audit = AuditLog(audit_path) if audit_path else None
        self.rng = rng if rng is not None else (random.Random(seed) if seed is not None else random.Random())

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

    def process_event(self, event: RevenueEvent, now: Optional[datetime] = None) -> DecisionRecord:
        now = now or datetime.now(timezone.utc)

        diagnosis = self.classifier.diagnose(event)
        priority = prioritize(event, diagnosis)

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

        record = DecisionRecord(
            event=event,
            diagnosis=diagnosis,
            priority=priority,
            chosen_action=chosen_action,
            compliance=compliance_result,
            outcome=outcome,
        )

        if self.audit is not None:
            self.audit.append(record.to_audit_payload())

        return record

    def process_batch(self, events: list[RevenueEvent], now: Optional[datetime] = None) -> list[DecisionRecord]:
        return [self.process_event(e, now=now) for e in events]
