"""
Human-in-the-loop approval gate.

Everything upstream of this (diagnosis, prioritization, compliance, action
selection) can run fully autonomously -- that's the point of the agent. But
a few actions are consequential enough that a real deployment should not
let them fire unattended: a large collections escalation, a large discount
offer, or anything the classifier itself flagged as a risk block (fraud
suspected). For those, the agent PROPOSES the action and a human AUTHORIZES
it before it executes -- "AI proposes, policy authorizes, execution
follows," not "AI decides and acts."

This is a real governance layer, not just a label: RecoveryEngine can run in
two modes (see auto_approve on RecoveryEngine). In autonomous/simulation
mode, a pending-approval case still resolves immediately (auto-approved,
clearly logged as such) so batch evaluation runs end-to-end without a human
attached. In HITL mode, a pending case genuinely stops -- chosen_action is
recorded but no outcome is simulated -- until something calls
RecoveryEngine.approve()/reject() on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.schema import RevenueEvent, Action, DiagnosisCategory
from core.classifier import Diagnosis


@dataclass
class ApprovalDecision:
    required: bool
    reason: str


class ApprovalGate:
    def __init__(self, rules: dict):
        self._m = rules.get("human_approval", {})

    def check(self, event: RevenueEvent, diagnosis: Diagnosis, action: Action) -> ApprovalDecision:
        if self._m.get("require_approval_for_risk_block") and diagnosis.category == DiagnosisCategory.RISK_BLOCK:
            return ApprovalDecision(
                True,
                f"Diagnosis is {DiagnosisCategory.RISK_BLOCK.value} (e.g. suspected fraud) -- "
                "requires human sign-off before any action executes.",
            )

        collections_threshold = self._m.get("escalate_to_collections_above_inr")
        if action == Action.ESCALATE_TO_COLLECTIONS and collections_threshold is not None:
            if event.amount > collections_threshold:
                return ApprovalDecision(
                    True,
                    f"Collections escalation for INR {event.amount:,.2f} exceeds the "
                    f"INR {collections_threshold:,.0f} auto-approval threshold.",
                )

        discount_threshold = self._m.get("offer_discount_above_inr")
        if action == Action.OFFER_DISCOUNT and discount_threshold is not None:
            if event.amount > discount_threshold:
                return ApprovalDecision(
                    True,
                    f"Discount offer on INR {event.amount:,.2f} exceeds the "
                    f"INR {discount_threshold:,.0f} auto-approval threshold.",
                )

        return ApprovalDecision(False, "Within autonomous-execution limits -- no approval required.")

    @property
    def auto_approve_in_simulation(self) -> bool:
        return bool(self._m.get("auto_approve_in_simulation", True))
