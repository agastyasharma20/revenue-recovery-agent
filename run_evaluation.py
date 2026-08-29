"""
Baseline ("retry everything once") vs full agent pipeline, on the SAME
synthetic batch, across multiple random seeds.

This is deliberately run across 5+ seeds and every seed's result is printed
-- if the agent doesn't beat the baseline consistently, that must show up
here rather than being cherry-picked away.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from core.schema import Action, ACTION_FLAT_COST
from core.engine import RecoveryEngine
from core.outcome_simulator import simulate_outcome
from data.generate_synthetic import generate_batch


def run_baseline(events, now, seed) -> dict:
    """Naive baseline: retry every event exactly once via RETRY_PAYMENT,
    no diagnosis, no compliance, no EV gating."""
    rng = random.Random(seed * 1000 + 1)
    pursued = 0
    recovered = 0
    total_recovered = 0.0
    total_cost = 0.0
    for e in events:
        pursued += 1
        cost = ACTION_FLAT_COST[Action.RETRY_PAYMENT]
        total_cost += cost
        outcome = simulate_outcome(e.decline_reason, Action.RETRY_PAYMENT, e.amount, rng=rng)
        if outcome.recovered:
            recovered += 1
            total_recovered += outcome.amount_recovered
    return {
        "label": "Naive baseline (retry everything once)",
        "total_events": len(events),
        "pursued": pursued,
        "recovered": recovered,
        "total_recovered_rupees": total_recovered,
        "total_cost_rupees": total_cost,
        "net_rupees": total_recovered - total_cost,
        "recovery_rate_of_pursued": recovered / pursued if pursued else 0.0,
        "recovery_rate_of_total": recovered / len(events) if events else 0.0,
    }


def run_agent(events, now, seed, policy_mode="deterministic", audit_path=None) -> dict:
    engine = RecoveryEngine(
        use_llm=False,
        policy_mode=policy_mode,
        audit_path=audit_path,
        seed=seed * 1000 + 2,
    )
    records = engine.process_batch(events, now=now)

    pursued = sum(1 for r in records if r.pursued)
    recovered = sum(1 for r in records if r.outcome and r.outcome.recovered)
    total_recovered = sum(r.recovered_amount for r in records)
    total_cost = sum(
        (r.event.amount * 0.05 if r.chosen_action == Action.OFFER_DISCOUNT else ACTION_FLAT_COST.get(r.chosen_action, 0.0))
        for r in records
        if r.pursued
    )
    not_pursued_negative_ev = sum(1 for r in records if not r.priority.pursue)
    blocked_by_compliance = sum(
        1 for r in records if r.priority.pursue and not r.pursued
    )

    return {
        "label": f"Full agent pipeline ({policy_mode} policy)",
        "total_events": len(events),
        "pursued": pursued,
        "recovered": recovered,
        "total_recovered_rupees": total_recovered,
        "total_cost_rupees": total_cost,
        "net_rupees": total_recovered - total_cost,
        "recovery_rate_of_pursued": recovered / pursued if pursued else 0.0,
        "recovery_rate_of_total": recovered / len(events) if events else 0.0,
        "not_pursued_negative_ev": not_pursued_negative_ev,
        "blocked_by_compliance": blocked_by_compliance,
        "records": records,
    }


def per_source_breakdown(records) -> dict:
    """So the rupee headline can't hide behind one dominant source."""
    by_source = defaultdict(lambda: {"n": 0, "pursued": 0, "recovered": 0, "rupees": 0.0})
    for r in records:
        s = r.event.source.value
        d = by_source[s]
        d["n"] += 1
        if r.pursued:
            d["pursued"] += 1
        if r.outcome and r.outcome.recovered:
            d["recovered"] += 1
            d["rupees"] += r.recovered_amount
    return dict(by_source)


def print_source_breakdown(by_source: dict):
    print("    per-source breakdown (n / pursued / recovered / INR recovered):")
    for s, d in sorted(by_source.items()):
        print(f"      {s:26s} n={d['n']:4d} pursued={d['pursued']:4d} recovered={d['recovered']:4d} INR={d['rupees']:>13,.2f}")


def print_result(res: dict):
    print(f"  {res['label']}:")
    print(f"    events pursued        : {res['pursued']} / {res['total_events']}")
    print(f"    events recovered      : {res['recovered']}")
    print(f"    total INR recovered   : {res['total_recovered_rupees']:,.2f}")
    print(f"    total INR cost spent  : {res['total_cost_rupees']:,.2f}")
    print(f"    net INR (recov-cost)  : {res['net_rupees']:,.2f}")
    print(f"    recovery rate (of pursued): {res['recovery_rate_of_pursued']*100:.1f}%")
    print(f"    recovery rate (of total)  : {res['recovery_rate_of_total']*100:.1f}%")
    if "not_pursued_negative_ev" in res:
        print(f"    skipped as negative-EV   : {res['not_pursued_negative_ev']}")
        print(f"    blocked by compliance    : {res['blocked_by_compliance']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=500, help="events per batch")
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    parser.add_argument("--policy", choices=["deterministic", "bandit"], default="deterministic")
    args = parser.parse_args()

    os.makedirs("results", exist_ok=True)
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)

    baseline_net, agent_net = [], []
    baseline_recovered, agent_recovered = [], []
    baseline_rate, agent_rate = [], []

    for seed in args.seeds:
        events, ground_truth = generate_batch(args.n, seed=seed, now=now)
        print(f"\n=== Seed {seed} (n={args.n} events) ===")

        b = run_baseline(events, now, seed)
        a = run_agent(events, now, seed, policy_mode=args.policy, audit_path=f"results/audit_seed{seed}.jsonl")

        print_result(b)
        print_result(a)
        print_source_breakdown(per_source_breakdown(a["records"]))

        delta_recovered_rupees = a["total_recovered_rupees"] - b["total_recovered_rupees"]
        delta_net_rupees = a["net_rupees"] - b["net_rupees"]
        winner = "AGENT" if delta_net_rupees > 0 else ("BASELINE" if delta_net_rupees < 0 else "TIE")
        print(f"  --> Delta gross INR recovered (agent - baseline): {delta_recovered_rupees:,.2f}")
        print(f"  --> Delta NET INR (agent - baseline):             {delta_net_rupees:,.2f}   [{winner} wins net]")

        baseline_net.append(b["net_rupees"])
        agent_net.append(a["net_rupees"])
        baseline_recovered.append(b["total_recovered_rupees"])
        agent_recovered.append(a["total_recovered_rupees"])
        baseline_rate.append(b["recovery_rate_of_pursued"])
        agent_rate.append(a["recovery_rate_of_pursued"])

    print("\n" + "=" * 70)
    print(f"SUMMARY ACROSS {len(args.seeds)} SEEDS (seeds={args.seeds})")
    print("=" * 70)
    wins = sum(1 for a_net, b_net in zip(agent_net, baseline_net) if a_net > b_net)
    print(f"Agent beats baseline on NET INR in {wins}/{len(args.seeds)} seeds.")
    print(f"Mean baseline net INR : {statistics.mean(baseline_net):,.2f}  (stdev {statistics.pstdev(baseline_net):,.2f})")
    print(f"Mean agent net INR    : {statistics.mean(agent_net):,.2f}  (stdev {statistics.pstdev(agent_net):,.2f})")
    print(f"Mean baseline gross recovered INR : {statistics.mean(baseline_recovered):,.2f}")
    print(f"Mean agent gross recovered INR    : {statistics.mean(agent_recovered):,.2f}")
    print(f"Mean baseline recovery rate (of pursued) : {statistics.mean(baseline_rate)*100:.1f}%")
    print(f"Mean agent recovery rate (of pursued)    : {statistics.mean(agent_rate)*100:.1f}%")

    if wins < len(args.seeds):
        print("\n*** WARNING: agent did NOT beat baseline on every seed. Investigate before claiming a win. ***")
    else:
        print("\nAgent beat the baseline on net INR recovered on every seed tested.")
        print(
            "\nHONESTY NOTE: the rupee gap above is dominated by B2B invoice recovery -- "
            "B2B events are ~10% of volume but ~100x the amount of a subscription event, "
            "and the naive baseline structurally recovers INR 0 from B2B/checkout sources "
            "(RETRY_PAYMENT has 0% success where there's no transaction to retry). "
            "The recovery-RATE comparison above (computed per-pursued-event, source-agnostic) "
            "is the fairer headline number; the rupee comparison is real but scale-dominated "
            "by whichever source has the largest amounts in a given batch."
        )


if __name__ == "__main__":
    main()
