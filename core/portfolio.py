"""
Portfolio optimization for human-escalation capacity (Phase 3).

The naive way to spend a limited daily human-review budget is "take the top
N cases by EV". That only works cleanly if every case costs the same amount
of a human's time -- which escalations don't: a quick identity-verification
call and a drawn-out B2B collections negotiation are both "one case" but
consume very different amounts of the actual constrained resource (hours).

So the real constraint is a BUDGET OF HOURS, not a headcount, and each case
has its own (value=EV, weight=estimated_hours). That is exactly 0/1
knapsack: choose the subset of cases maximizing total EV without the total
estimated hours exceeding today's capacity. Solved here via DP over a
discretized hour axis (exact, not an approximation) -- tractable because
daily case counts and hour budgets are small.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.engine import DecisionRecord
from core.schema import Action

# Estimated human-handling time per escalation action, in hours. These are
# the "quick call" vs "drawn-out negotiation" difference that makes a flat
# per-case count the wrong unit of capacity.
_BASE_HOURS = {
    Action.ESCALATE_TO_HUMAN_CALL: 0.2,        # ~12 min identity/soft-decline call
    Action.ESCALATE_TO_COLLECTIONS: 0.5,       # ~30 min baseline collections outreach
}


def estimate_handling_hours(record: DecisionRecord) -> float:
    """Handling time is not flat per action -- realistically it scales with
    case complexity:
      - human calls run a bit longer for high-LTV customers (more
        white-glove/retention effort) and for customers already several
        retries deep (more frustrated, more to untangle);
      - collections negotiations scale with the invoice amount (steeply --
        a INR 5,00,000 invoice is a genuinely different negotiation than a
        INR 5,000 one) and with how overdue the invoice already is.
    This is what actually makes the capacity constraint a knapsack problem
    rather than a same-cost-per-slot count: cases now have meaningfully
    different weight/value ratios, so greedy-by-value can leave capacity
    stranded that a different combination would have used better."""
    action = record.chosen_action
    event = record.event

    if action == Action.ESCALATE_TO_HUMAN_CALL:
        from core.schema import CustomerSegment

        hours = _BASE_HOURS[action]
        if event.customer_segment == CustomerSegment.HIGH_LTV:
            hours += 0.2
        hours += 0.05 * event.retry_count
    elif action == Action.ESCALATE_TO_COLLECTIONS:
        hours = _BASE_HOURS[action]
        hours += 0.35 * math.log10(max(event.amount, 5000) / 5000)
    else:
        hours = _BASE_HOURS.get(action, 0.25)

    return round(max(hours, 0.1), 3)


@dataclass
class PortfolioCase:
    record: DecisionRecord
    value: float  # EV, INR
    weight_hours: float

    @property
    def case_id(self) -> str:
        return self.record.event.event_id


def build_cases(records: list[DecisionRecord]) -> list[PortfolioCase]:
    """Cases needing human escalation = decisions that chose a human action."""
    escalation_actions = {Action.ESCALATE_TO_HUMAN_CALL, Action.ESCALATE_TO_COLLECTIONS}
    cases = []
    for r in records:
        if r.chosen_action in escalation_actions:
            cases.append(PortfolioCase(record=r, value=r.priority.ev, weight_hours=estimate_handling_hours(r)))
    return cases


def greedy_by_value(values: list[float], weights: list[float], capacity: float) -> list[int]:
    """Naive baseline: rank by value, greedily take items in that order,
    SKIPPING (not stopping at) any item that would blow the budget. This is
    what "just take the top N by EV" looks like once you're honest that
    capacity is hours, not a headcount -- a reasonable-looking heuristic
    that ignores weight/value ratio and so can still leave value on the
    table vs the true DP optimum. Returns selected indices."""
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
    selected = []
    used = 0.0
    for i in order:
        if used + weights[i] <= capacity:
            selected.append(i)
            used += weights[i]
    return selected


def solve_knapsack(values: list[float], weights: list[float], capacity: float, resolution: float = 0.001) -> list[int]:
    """Exact 0/1 knapsack via DP, discretizing the weight axis into
    `resolution`-sized units so the DP table is a plain integer array.
    Returns selected indices.

    IMPORTANT: `resolution` must be fine enough to exactly represent every
    item's weight, or rounding can silently perturb which combination looks
    best and the "optimal" solver can come out *worse* than a greedy
    heuristic on the true (undiscretized) weights -- which defeats the
    entire point of using an exact algorithm. This was caught during
    development: a coarser resolution (0.05h) produced a knapsack selection
    that scored LOWER than greedy_by_value on real data, which is
    impossible for a correct exact solver and was the tell that something
    was wrong with the discretization, not the algorithm. The default
    (0.001) matches the 3-decimal rounding used by estimate_handling_hours()
    with zero rounding error."""
    n = len(values)
    if n == 0:
        return []

    cap_units = int(round(capacity / resolution))
    w_units = [max(1, int(round(w / resolution))) for w in weights]

    # dp[w] = best total value achievable with total weight <= w
    dp = [0.0] * (cap_units + 1)
    choice = [[False] * (cap_units + 1) for _ in range(n)]  # choice[i][w] = item i taken to reach dp[w] during item i's own pass

    for i in range(n):
        wt, val = w_units[i], values[i]
        for w in range(cap_units, wt - 1, -1):
            candidate = dp[w - wt] + val
            if candidate > dp[w]:
                dp[w] = candidate
                choice[i][w] = True

    selected = []
    w = cap_units
    for i in range(n - 1, -1, -1):
        if choice[i][w]:
            selected.append(i)
            w -= w_units[i]

    return selected


def top_n_by_ev(cases: list[PortfolioCase], capacity_hours: float) -> list[PortfolioCase]:
    values = [c.value for c in cases]
    weights = [c.weight_hours for c in cases]
    idx = greedy_by_value(values, weights, capacity_hours)
    return [cases[i] for i in idx]


def knapsack_optimal(cases: list[PortfolioCase], capacity_hours: float, resolution: float = 0.001) -> list[PortfolioCase]:
    values = [c.value for c in cases]
    weights = [c.weight_hours for c in cases]
    idx = solve_knapsack(values, weights, capacity_hours, resolution=resolution)
    return [cases[i] for i in idx]


@dataclass
class PortfolioComparison:
    capacity_hours: float
    topn_selected: int
    topn_value: float
    topn_hours_used: float
    knapsack_selected: int
    knapsack_value: float
    knapsack_hours_used: float

    @property
    def value_gain(self) -> float:
        return self.knapsack_value - self.topn_value

    @property
    def value_gain_pct(self) -> float:
        return (self.value_gain / self.topn_value * 100) if self.topn_value else 0.0


def compare(cases: list[PortfolioCase], capacity_hours: float) -> PortfolioComparison:
    topn = top_n_by_ev(cases, capacity_hours)
    optimal = knapsack_optimal(cases, capacity_hours)
    return PortfolioComparison(
        capacity_hours=capacity_hours,
        topn_selected=len(topn),
        topn_value=sum(c.value for c in topn),
        topn_hours_used=sum(c.weight_hours for c in topn),
        knapsack_selected=len(optimal),
        knapsack_value=sum(c.value for c in optimal),
        knapsack_hours_used=sum(c.weight_hours for c in optimal),
    )
