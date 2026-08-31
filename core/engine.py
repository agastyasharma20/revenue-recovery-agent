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
from core.agentic_policy import select_action as agentic_select_action, AgenticDecision
from core.outcome_simulator import simulate_outcome, OutcomeResult
from core.audit import AuditLog
from core.circuit_breaker import CircuitBreaker
from core.logging_config import get_logger
from core.promise_tracking import PromiseTracker, Promise
from core.approval import ApprovalGate
from core import alerting


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
    # Human-in-the-loop governance (core/approval.py). approval_status is one
    # of: not_required | auto_approved | pending | approved | rejected.
    requires_approval: bool = False
    approval_reason: str = ""
    approval_status: str = "not_required"
    # Populated only when policy_mode="agentic" -- which action the LLM
    # actually picked (or that it was rejected and fell back), for full
    # transparency in the audit trail. None for every other policy mode.
    agentic_decision: Optional[AgenticDecision] = None
    # Explicit lifecycle: named states + timestamps + a short human-readable
    # note, in the order they actually happened. This is what makes "bounded
    # recovery workflow" a checkable claim rather than a description -- the
    # same information latencies_ms/logger already capture, just structured
    # as a named state machine so a dashboard can render it as a timeline/
    # stepper instead of a flat log.
    timeline: list = field(default_factory=list)

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
            "approval": {
                "requires_approval": self.requires_approval,
                "reason": self.approval_reason,
                "status": self.approval_status,
            },
            "agentic_decision": self.agentic_decision.to_dict() if self.agentic_decision else None,
            "timeline": self.timeline,
        }


class RecoveryEngine:
    def __init__(
        self,
        use_llm: bool = False,
        policy_mode: str = "deterministic",  # "deterministic" | "bandit" | "agentic"
        audit_path: Optional[str] = "results/audit_log.jsonl",
        bandit: Optional[ThompsonSamplingBandit] = None,
        rng: Optional[random.Random] = None,
        seed: Optional[int] = None,
        breaker: Optional[CircuitBreaker] = None,
        log_path: Optional[str] = "results/agent.jsonl",
        promise_tracker: Optional[PromiseTracker] = None,
        auto_approve: Optional[bool] = None,
    ):
        assert policy_mode in ("deterministic", "bandit", "agentic")
        self.breaker = breaker if breaker is not None else CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)
        self.classifier = Classifier(use_llm=use_llm, breaker=self.breaker)
        self.compliance = ComplianceChecker()
        self.approval_gate = ApprovalGate(self.compliance.rules)
        # None -> defer to rules.yaml's human_approval.auto_approve_in_simulation
        # (default True, so every existing caller's behavior/numbers are
        # unchanged unless they explicitly opt into auto_approve=False).
        self.auto_approve = auto_approve if auto_approve is not None else self.approval_gate.auto_approve_in_simulation
        self.pending_approvals: dict[str, DecisionRecord] = {}
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
        """Returns (chosen_action, compliance_result, agentic_decision_or_None)."""
        if self.policy_mode == "deterministic":
            candidates = self.det_policy.candidate_actions(event, diagnosis)
            last_cr = None
            for cand in candidates:
                cr = self.compliance.check(event, cand, now)
                last_cr = cr
                if cr.allowed:
                    return cand, cr, None
            return Action.NO_ACTION_DO_NOT_PURSUE, last_cr, None
        elif self.policy_mode == "agentic":
            # THE bound: filter to compliance-allowed candidates FIRST, then
            # let the LLM pick among only those -- it never even sees an
            # action it isn't allowed to choose. See core/agentic_policy.py's
            # module docstring for the full safety argument.
            candidates = [
                cand for cand in self.det_policy.candidate_actions(event, diagnosis)
                if self.compliance.check(event, cand, now).allowed
            ]
            if not candidates:
                cr = self.compliance.check(event, Action.RETRY_PAYMENT, now)
                return Action.NO_ACTION_DO_NOT_PURSUE, cr, None
            decision = agentic_select_action(event, diagnosis, candidates)
            cr = self.compliance.check(event, decision.action, now)
            return decision.action, cr, decision
        else:
            allowed_arms = [a for a in BANDIT_ARMS if self.compliance.check(event, a, now).allowed]
            if not allowed_arms:
                cr = self.compliance.check(event, Action.RETRY_PAYMENT, now)
                return Action.NO_ACTION_DO_NOT_PURSUE, cr, None
            segment = event.decline_reason.value
            chosen = self.bandit.select_action(segment, arms=allowed_arms)
            cr = self.compliance.check(event, chosen, now)
            return chosen, cr, None

    def _log(self, trace_id: str, layer: str, message: str) -> None:
        if self.logger is not None:
            self.logger.info(message, extra={"trace_id": trace_id, "layer": layer})

    @staticmethod
    def _timeline_entry(stage: str, note: str, now: datetime) -> dict:
        return {"stage": stage, "at": now.isoformat(), "note": note}

    def _execute_action(self, event: RevenueEvent, chosen_action: Action, now: datetime):
        """The actual "do it" step -- outcome simulation, bandit feedback,
        promise recording. Shared by the autonomous/auto-approved path and
        by approve() on a previously-pending case, so both go through
        identical logic (and identical RNG consumption order for the
        autonomous path, so existing reproducible numbers don't shift)."""
        outcome = simulate_outcome(event.decline_reason, chosen_action, event.amount, rng=self.rng)
        if self.policy_mode == "bandit":
            self.bandit.update(event.decline_reason.value, chosen_action, int(outcome.recovered))
        promise = self.promise_tracker.maybe_record_promise(event, chosen_action, outcome.recovered, now)
        return outcome, promise

    def process_event(self, event: RevenueEvent, now: Optional[datetime] = None) -> DecisionRecord:
        now = now or datetime.now(timezone.utc)  # BUSINESS time -- what compliance/rules evaluate against
        trace_id = event.trace_id
        latencies: dict[str, float] = {}
        # timeline entries use real wall-clock time (distinct from `now`
        # above), because that's what's actually informative here: the
        # automated stages happen microseconds apart (worth showing --
        # proves the pipeline is fast), and a pending-approval case's gap
        # between "proposed" and "approved" is real elapsed time until a
        # human acts, not a business-time artifact.
        timeline = [self._timeline_entry("ingested", f"{event.source.value} event received", datetime.now(timezone.utc))]

        t0 = time.perf_counter()
        diagnosis = self.classifier.diagnose(event)
        latencies["diagnose"] = (time.perf_counter() - t0) * 1000
        timeline.append(self._timeline_entry(
            "diagnosed", f"{diagnosis.category.value} (confidence {diagnosis.confidence:.2f})", datetime.now(timezone.utc)
        ))
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
        timeline.append(self._timeline_entry(
            "prioritized", f"EV={priority.ev:,.2f} pursue={priority.pursue}", datetime.now(timezone.utc)
        ))
        self._log(trace_id, "prioritize", f"ev={priority.ev:.2f} pursue={priority.pursue}")

        t0 = time.perf_counter()
        outcome, promise = None, None
        agentic_decision: Optional[AgenticDecision] = None
        requires_approval, approval_reason, approval_status = False, "", "not_required"
        if not priority.pursue:
            chosen_action = Action.NO_ACTION_DO_NOT_PURSUE
            compliance_result = ComplianceResult(True, "Not pursued (negative EV) -- compliance N/A.", self.compliance.version)
            timeline.append(self._timeline_entry("closed", "negative EV -- not pursued", datetime.now(timezone.utc)))
        else:
            chosen_action, compliance_result, agentic_decision = self._select_action(event, diagnosis, now)
            timeline.append(self._timeline_entry(
                "compliance_checked",
                f"{'allowed' if compliance_result.allowed else 'blocked'}: {compliance_result.reason}",
                datetime.now(timezone.utc),
            ))
            if chosen_action == Action.NO_ACTION_DO_NOT_PURSUE:
                timeline.append(self._timeline_entry("closed", "no compliant action available", datetime.now(timezone.utc)))
            else:
                action_note = chosen_action.value
                if agentic_decision is not None:
                    action_note += f" ({agentic_decision.source}: {agentic_decision.rationale})"
                timeline.append(self._timeline_entry("action_selected", action_note, datetime.now(timezone.utc)))
                decision = self.approval_gate.check(event, diagnosis, chosen_action)
                requires_approval, approval_reason = decision.required, decision.reason
                if requires_approval and not self.auto_approve:
                    approval_status = "pending"  # execution deferred -- see approve()/reject()
                    timeline.append(self._timeline_entry("pending_approval", approval_reason, datetime.now(timezone.utc)))
                    alerting.send_alert(
                        "pending_approval",
                        f"{chosen_action.value} on {event.source.value} (Rs.{event.amount:,.0f}) needs sign-off: {approval_reason}",
                        context={"event_id": event.event_id, "customer_segment": event.customer_segment.value},
                    )
                else:
                    approval_status = "auto_approved" if requires_approval else "not_required"
                    if requires_approval:
                        timeline.append(self._timeline_entry("auto_approved", approval_reason, datetime.now(timezone.utc)))
                    outcome, promise = self._execute_action(event, chosen_action, now)
                    timeline.append(self._timeline_entry(
                        "executed", f"{'recovered' if outcome.recovered else 'not recovered'} (p={outcome.probability_used:.2f})",
                        datetime.now(timezone.utc),
                    ))
        latencies["policy_and_compliance"] = (time.perf_counter() - t0) * 1000
        self._log(
            trace_id, "policy",
            f"action={chosen_action.value} compliance_allowed={compliance_result.allowed} "
            f"approval_status={approval_status} outcome_recovered={outcome.recovered if outcome else None}",
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
            requires_approval=requires_approval,
            approval_reason=approval_reason,
            approval_status=approval_status,
            agentic_decision=agentic_decision,
            timeline=timeline,
        )

        t0 = time.perf_counter()
        if self.audit is not None:
            self.audit.append(record.to_audit_payload())
        latencies["audit"] = (time.perf_counter() - t0) * 1000
        timeline.append(self._timeline_entry("audited", "appended to hash-chained audit log", datetime.now(timezone.utc)))
        self._log(trace_id, "audit", "decision appended to hash-chained audit log")

        if approval_status == "pending":
            self.pending_approvals[event.event_id] = record

        return record

    def approve(self, event_id: str, now: Optional[datetime] = None) -> DecisionRecord:
        """A human authorizes a pending action -- executes it now, using
        the exact same logic path as the autonomous case."""
        now = now or datetime.now(timezone.utc)
        record = self.pending_approvals.pop(event_id, None)
        if record is None:
            raise KeyError(f"No pending approval for event_id={event_id}")

        wall_now = datetime.now(timezone.utc)
        record.timeline.append(self._timeline_entry("approved", "human authorized -- executing now", wall_now))
        record.outcome, record.promise = self._execute_action(record.event, record.chosen_action, now)
        record.approval_status = "approved"
        record.timeline.append(self._timeline_entry(
            "executed", f"{'recovered' if record.outcome.recovered else 'not recovered'} (p={record.outcome.probability_used:.2f})",
            datetime.now(timezone.utc),
        ))
        if self.audit is not None:
            self.audit.append(record.to_audit_payload())  # second, later audit entry -- a real resolution trail
        return record

    def reject(self, event_id: str, reason: str = "rejected by reviewer", now: Optional[datetime] = None) -> DecisionRecord:
        """A human declines a pending action -- it never executes."""
        record = self.pending_approvals.pop(event_id, None)
        if record is None:
            raise KeyError(f"No pending approval for event_id={event_id}")

        record.approval_status = "rejected"
        record.approval_reason = f"{record.approval_reason} | REJECTED: {reason}"
        record.timeline.append(self._timeline_entry("rejected", reason, datetime.now(timezone.utc)))
        if self.audit is not None:
            self.audit.append(record.to_audit_payload())
        return record

    def process_batch(self, events: list[RevenueEvent], now: Optional[datetime] = None) -> list[DecisionRecord]:
        return [self.process_event(e, now=now) for e in events]
