"""
Contextual bandit convergence demo (Phase 1).

Runs many small rounds SEQUENTIALLY, carrying one LinUCBBandit's state
forward round after round (never reset), and tracks recovery rate in
rolling windows to show it climbing over time -- this is meant to be a
demonstration you can actually see happen, not an assertion.

The bandit is handed the FULL action set every round (no compliance
filtering, no reason->action shortcuts) so that whatever it converges to
is purely a product of reward feedback.

At the end it prints, per decline-reason segment, the action it
independently learned has the highest predicted reward -- compare this
against core/policy.py's hand-coded deterministic mapping and
core/outcome_simulator.py's ground-truth best action to sanity-check that
it rediscovered something sensible.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import numpy as np

from core.schema import DeclineReason, CustomerSegment, Action
from core.contextual_bandit import LinUCBBandit, build_context
from core.policy import BANDIT_ARMS
from core.outcome_simulator import simulate_outcome, EFFECTIVENESS_MATRIX
from data.generate_synthetic import generate_batch


def representative_context(reason: DeclineReason, now: datetime, amount: float = 3000.0,
                            segment: CustomerSegment = CustomerSegment.MEDIUM_LTV):
    from core.schema import RevenueEvent, EventSource, VALID_REASONS_FOR_SOURCE

    source = None
    for s, reasons in VALID_REASONS_FOR_SOURCE.items():
        if reason in reasons:
            source = s
            break
    ev = RevenueEvent(
        source=source,
        decline_reason=reason,
        amount=amount,
        customer_segment=segment,
        created_at=now,
        last_attempt_at=now,
        retry_count=0,
    )
    return build_context(ev, now)


def main():
    parser = argparse.ArgumentParser()
    # 30,000 (not 20,000): adding SEND_REMINDER_WHATSAPP gave invoice_overdue
    # a third competitive arm (collections=0.50 vs human_call=0.38 vs
    # whatsapp=0.32 in the oracle matrix) instead of two -- distinguishing
    # three closer options confidently needs more samples than two more
    # separated ones, and invoice_overdue is only ~10% of traffic to begin
    # with. Verified: 20,000 rounds left this one segment still unresolved;
    # 30,000 reliably reaches 12/12.
    parser.add_argument("--rounds", type=int, default=30000)
    parser.add_argument("--window", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--alpha", type=float, default=0.6, help="LinUCB exploration strength")
    parser.add_argument("--plot", default="results/bandit_convergence.png")
    args = parser.parse_args()

    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    events, _ = generate_batch(args.rounds, seed=args.seed, now=now)

    bandit = LinUCBBandit(arms=BANDIT_ARMS, alpha=args.alpha)
    rng_outcome = __import__("random").Random(args.seed * 999 + 1)

    window_rewards = []
    all_rewards = []
    window_rates = []

    print(f"Running {args.rounds} sequential rounds, window={args.window}, LinUCB alpha={args.alpha}\n")
    print(f"{'window':>8s} {'rounds':>14s} {'recovery_rate':>14s}")

    for i, event in enumerate(events):
        ctx = build_context(event, event.created_at)
        action = bandit.select_action(ctx)  # full action set -- no filtering
        outcome = simulate_outcome(event.decline_reason, action, event.amount, rng=rng_outcome)
        reward = 1.0 if outcome.recovered else 0.0
        bandit.update(action, ctx, reward)

        window_rewards.append(reward)
        all_rewards.append(reward)

        if (i + 1) % args.window == 0:
            rate = sum(window_rewards) / len(window_rewards)
            window_rates.append(rate)
            print(f"{(i+1)//args.window:>8d} {i+1:>14d} {rate*100:>13.1f}%")
            window_rewards = []

    overall_rate = sum(all_rewards) / len(all_rewards)
    first_window_rate = sum(all_rewards[: args.window]) / args.window
    last_window_rate = sum(all_rewards[-args.window :]) / args.window
    print(f"\nOverall recovery rate across all {args.rounds} rounds: {overall_rate*100:.1f}%")
    print(f"First window recovery rate: {first_window_rate*100:.1f}%  ->  Last window: {last_window_rate*100:.1f}%")
    if last_window_rate > first_window_rate:
        print("Recovery rate climbed from first window to last window -- bandit is learning.")
    else:
        print("*** WARNING: recovery rate did NOT improve from first to last window. Investigate. ***")

    try:
        import os
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        os.makedirs(os.path.dirname(args.plot) or ".", exist_ok=True)
        rounds_axis = [(i + 1) * args.window for i in range(len(window_rates))]
        plt.figure(figsize=(9, 5))
        plt.plot(rounds_axis, [r * 100 for r in window_rates], marker="o", markersize=3)
        plt.xlabel("Rounds processed")
        plt.ylabel("Recovery rate in window (%)")
        plt.title(f"LinUCB contextual bandit convergence (window={args.window} rounds)")
        plt.grid(alpha=0.3)
        plt.tight_layout()
        plt.savefig(args.plot, dpi=120)
        print(f"\nSaved convergence plot to {args.plot}")
    except Exception as exc:  # noqa: BLE001 -- plotting is best-effort, never fatal
        print(f"\n(Plot skipped: {exc})")

    print("\n" + "=" * 78)
    print("What the bandit independently learned (predicted best action per decline reason)")
    print("vs. the deterministic policy's hardcoded first choice, vs. the oracle ground truth:")
    print("=" * 78)
    from core.policy import DeterministicPolicy, _CANDIDATE_ACTIONS
    from core.schema import EventSource, DiagnosisCategory
    from core.classifier import _RULES

    from core.schema import VALID_REASONS_FOR_SOURCE

    for reason in DeclineReason:
        if reason == DeclineReason.UNKNOWN:
            continue
        ctx = representative_context(reason, now)
        means = {a: bandit.predicted_mean(a, ctx) for a in BANDIT_ARMS}
        learned_best = max(means, key=means.get)

        oracle_matrix = {a: p for a, p in EFFECTIVENESS_MATRIX[reason].items() if a != Action.NO_ACTION_DO_NOT_PURSUE}
        oracle_best = max(oracle_matrix, key=oracle_matrix.get)

        source = next(s for s, reasons in VALID_REASONS_FOR_SOURCE.items() if reason in reasons)
        category, _, _ = _RULES.get(reason, (None, None, None))
        det_candidates = _CANDIDATE_ACTIONS.get((source, category), [])
        det_first_choice = det_candidates[0].value if det_candidates else "(fallback)"

        match = "MATCH" if learned_best == oracle_best else "differs"
        print(
            f"  {reason.value:22s} bandit_learned={learned_best.value:28s} "
            f"deterministic_default={det_first_choice:28s} oracle_best={oracle_best.value:28s} [{match}]"
        )


if __name__ == "__main__":
    main()
