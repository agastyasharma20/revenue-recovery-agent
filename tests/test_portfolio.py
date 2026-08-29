"""
Phase 3: knapsack must never score worse than the greedy baseline (it's an
exact solver over the same search space plus more), and must actually beat
greedy on the classic instance where value-only ordering fails.
"""

from datetime import datetime, timezone

from core.portfolio import greedy_by_value, solve_knapsack, build_cases, top_n_by_ev, knapsack_optimal
from core.engine import RecoveryEngine
from data.generate_synthetic import generate_batch


def test_knapsack_beats_greedy_on_textbook_instance():
    values, weights, capacity = [30.0, 24.0, 24.0], [6.0, 5.0, 5.0], 10.0
    g = greedy_by_value(values, weights, capacity)
    k = solve_knapsack(values, weights, capacity, resolution=1)
    assert sum(values[i] for i in g) == 30.0
    assert sum(values[i] for i in k) == 48.0
    assert sum(values[i] for i in k) > sum(values[i] for i in g)


def test_knapsack_never_worse_than_greedy_on_real_batches():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    for seed in [1, 2, 3, 11, 22, 33, 44, 55]:
        events, _ = generate_batch(600, seed=seed, now=now)
        engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=seed * 10 + 1)
        records = engine.process_batch(events, now=now)
        cases = build_cases(records)
        if not cases:
            continue
        for capacity in (5.0, 10.0, 20.0, 30.0):
            topn_value = sum(c.value for c in top_n_by_ev(cases, capacity))
            ks_value = sum(c.value for c in knapsack_optimal(cases, capacity))
            assert ks_value >= topn_value - 1e-6, (
                f"seed={seed} capacity={capacity}: knapsack ({ks_value}) < greedy ({topn_value})"
            )


def test_knapsack_never_exceeds_capacity():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    events, _ = generate_batch(400, seed=5, now=now)
    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=5001)
    records = engine.process_batch(events, now=now)
    cases = build_cases(records)
    for capacity in (5.0, 10.0, 20.0):
        selected = knapsack_optimal(cases, capacity)
        assert sum(c.weight_hours for c in selected) <= capacity + 1e-6
