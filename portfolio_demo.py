"""
Phase 3 demo: on a sample batch, compare the naive "top-N by EV, greedily
fit under the hour budget" heuristic against the exact knapsack-optimal
selection, and print the numeric gap so the knapsack layer's value is
visible, not just asserted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from core.engine import RecoveryEngine
from core.portfolio import build_cases, top_n_by_ev, knapsack_optimal, compare, greedy_by_value, solve_knapsack
from data.generate_synthetic import generate_batch


def textbook_counterexample():
    """A hand-picked, no-hidden-parameters instance proving the algorithm
    itself is correct and that greedy-by-value can lose meaningfully -- this
    doesn't depend on whether the synthetic batch happens to produce a case
    mix where the gap shows up."""
    values, weights, capacity = [30.0, 24.0, 24.0], [6.0, 5.0, 5.0], 10.0
    g = greedy_by_value(values, weights, capacity)
    k = solve_knapsack(values, weights, capacity, resolution=1)
    g_val, k_val = sum(values[i] for i in g), sum(values[i] for i in k)

    print("Textbook counter-example (proves the algorithm, independent of synthetic data):")
    print("  items: A(value=30,hours=6)  B(value=24,hours=5)  C(value=24,hours=5)  capacity=10h")
    print(f"  greedy_by_value picks {[chr(65+i) for i in g]} -> value {g_val:.0f}")
    print(f"  solve_knapsack  picks {[chr(65+i) for i in k]} -> value {k_val:.0f}")
    print(f"  Greedy grabs A first (highest single value) and has no room left for B+C, "
          f"which together beat it ({k_val:.0f} vs {g_val:.0f}). This is exactly the failure "
          f"mode 0/1 knapsack exists to fix.\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=800)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--capacity-hours", type=float, default=20.0)
    args = parser.parse_args()

    textbook_counterexample()

    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    events, _ = generate_batch(args.n, seed=args.seed, now=now)

    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=args.seed * 1000 + 3)
    records = engine.process_batch(events, now=now)

    cases = build_cases(records)
    print(f"Generated {args.n} events; {len(cases)} were escalated to a human (call or collections).")
    print(f"Daily human-review capacity: {args.capacity_hours} hours.\n")

    total_hours_if_all_worked = sum(c.weight_hours for c in cases)
    print(f"Total human-hours ALL escalated cases would need: {total_hours_if_all_worked:.2f}h "
          f"(vs {args.capacity_hours}h available -- {'fits easily' if total_hours_if_all_worked <= args.capacity_hours else 'far exceeds capacity, selection matters'})\n")

    result = compare(cases, args.capacity_hours)

    print("Top-N by EV (greedy, stop when the next case would blow the budget):")
    print(f"  cases selected : {result.topn_selected}")
    print(f"  hours used     : {result.topn_hours_used:.2f} / {result.capacity_hours}")
    print(f"  total EV (INR) : {result.topn_value:,.2f}")
    print()
    print("Knapsack-optimal (0/1 DP, exact):")
    print(f"  cases selected : {result.knapsack_selected}")
    print(f"  hours used     : {result.knapsack_hours_used:.2f} / {result.capacity_hours}")
    print(f"  total EV (INR) : {result.knapsack_value:,.2f}")
    print()
    print(f"--> Knapsack captures {result.value_gain:,.2f} more INR of EV than greedy top-N "
          f"({result.value_gain_pct:+.2f}%) from the SAME pool of cases and the SAME hour budget.")

    if result.value_gain < 0:
        print("*** WARNING: knapsack did WORSE than greedy top-N -- this should never happen for an exact DP. Bug. ***")
    elif result.value_gain == 0:
        print("(No gap on this batch/capacity combo -- greedy happened to find the optimum here. "
              "Try a different --capacity-hours or --seed to see a gap; the DP is still exact and never does worse.)")
    else:
        print("HONESTY NOTE: on this synthetic data, value and handling-time both scale with deal size "
              "(bigger deals are worth more AND take longer to negotiate), so greedy-by-value is usually "
              "already close to optimal -- the gap above is real but typically small (well under 1% of "
              "total EV) and only shows up for specific capacity/case-mix combinations. The textbook "
              "example above is the honest illustration of the failure mode this layer guards against; "
              "on THIS data it's an occasional, modest improvement plus a correctness guarantee, not a "
              "consistently large one -- don't oversell it.")


if __name__ == "__main__":
    main()
