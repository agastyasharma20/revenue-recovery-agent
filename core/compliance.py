"""
Compliance / stopping rules.

Everything here is a *hard* rule: if it fires, the event does not get an
automated action this round, full stop, regardless of how good the expected
value looks. Thresholds live in rules.yaml (not hardcoded) so they're
auditable and changeable without a code deploy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import yaml

from core.schema import RevenueEvent, EventSource, Action

_RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.yaml")


@dataclass
class ComplianceResult:
    allowed: bool
    reason: str
    rules_version: int


def load_rules(path: str = _RULES_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class ComplianceChecker:
    def __init__(self, rules: Optional[dict] = None):
        self.rules = rules or load_rules()
        self.version = self.rules.get("version", 0)
        self._m = self.rules["npci_mandate_retry"]
        self._b2b = self.rules.get("b2b_receivables", {})

    def _pursuit_window_days(self, event: RevenueEvent) -> int:
        """B2B receivables follow accounts-receivable aging conventions
        (~90 days), not the NPCI mandate retry window (~7 days) -- these are
        different regulatory/business contexts and sharing one cutoff was a
        real bug caught during testing (see rules.yaml)."""
        if event.source == EventSource.B2B_RECEIVABLE_OVERDUE and "pursuit_window_days" in self._b2b:
            return self._b2b["pursuit_window_days"]
        return self._m["pursuit_window_days"]

    def check(
        self, event: RevenueEvent, candidate_action: Action, now: Optional[datetime] = None
    ) -> ComplianceResult:
        now = now or datetime.now(timezone.utc)
        v = self.version

        # 1. Pursuit window cutoff -- stop chasing stale events entirely,
        #    regardless of action.
        pursuit_window_days = self._pursuit_window_days(event)
        age_days = (now - event.created_at).total_seconds() / 86400
        if age_days > pursuit_window_days:
            return ComplianceResult(
                False,
                f"Event is {age_days:.1f} days old, past the "
                f"{pursuit_window_days}-day pursuit window cutoff.",
                v,
            )

        # Rules below only apply to actions that constitute a "retry" of a
        # payment attempt. Reminders/escalations/discounts on abandoned
        # checkouts or overdue invoices aren't mandate retries, so NPCI
        # retry limits don't govern them.
        is_retry_action = candidate_action in (
            Action.RETRY_PAYMENT,
            Action.RETRY_WITH_ALTERNATE_METHOD,
        )
        applies_to_mandate_flow = event.source == EventSource.SUBSCRIPTION_FAILED

        if is_retry_action and applies_to_mandate_flow:
            # 2. Max retries.
            if event.retry_count >= self._m["max_retries"]:
                return ComplianceResult(
                    False,
                    f"retry_count={event.retry_count} has reached max_retries="
                    f"{self._m['max_retries']} (NPCI e-mandate cap).",
                    v,
                )

            # 3. Minimum gap between retries.
            hours_since_last = (now - event.last_attempt_at).total_seconds() / 3600
            min_gap = self._m["min_hours_between_retries"]
            if event.retry_count > 0 and hours_since_last < min_gap:
                return ComplianceResult(
                    False,
                    f"Only {hours_since_last:.1f}h since last attempt; "
                    f"minimum gap is {min_gap}h.",
                    v,
                )

            # 4. No retry too soon after mandate creation.
            if event.mandate_created_at is not None:
                hours_since_mandate = (now - event.mandate_created_at).total_seconds() / 3600
                cooldown = self._m["no_retry_within_hours_of_mandate_creation"]
                if hours_since_mandate < cooldown:
                    return ComplianceResult(
                        False,
                        f"Mandate created {hours_since_mandate:.1f}h ago; must wait "
                        f"{cooldown}h before retrying.",
                        v,
                    )

        return ComplianceResult(True, "All compliance checks passed.", v)
