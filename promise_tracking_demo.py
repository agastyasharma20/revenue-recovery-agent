"""
Promise-to-pay tracking demo.

Generates a batch, filters to B2B receivables (the only source with repeat
customers), processes them in CHRONOLOGICAL order (temporal causality
matters here: a promise can only affect a LATER decision, never an earlier
one), and shows concretely that a customer's broken-promise history changes
the EV computed for their next invoice.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone

from core.engine import RecoveryEngine
from core.schema import EventSource
from data.generate_synthetic import generate_batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=1500, help="total synthetic events (B2B is ~10% of this)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    events, _ = generate_batch(args.n, seed=args.seed, now=now)

    b2b_events = sorted(
        (e for e in events if e.source == EventSource.B2B_RECEIVABLE_OVERDUE),
        key=lambda e: e.created_at,
    )
    print(f"Generated {args.n} events; {len(b2b_events)} are B2B receivables (repeat customers).")

    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=args.seed * 1000 + 5)
    records = engine.process_batch(b2b_events, now=now)

    by_customer = defaultdict(list)
    for r in records:
        by_customer[r.event.customer_id].append(r)

    repeat_customers = {cid: recs for cid, recs in by_customer.items() if len(recs) >= 2}
    print(f"{len(by_customer)} distinct businesses, {len(repeat_customers)} with 2+ invoices in this window.\n")

    print("=" * 100)
    print("Reliability feedback loop -- same customer, invoices over time, EV shifts with their track record:")
    print("=" * 100)

    shown = 0
    for cid, recs in repeat_customers.items():
        if shown >= 5:
            break
        recs = sorted(recs, key=lambda r: r.event.created_at)
        print(f"\nCustomer {cid} ({len(recs)} invoices):")
        for i, r in enumerate(recs):
            promise_note = ""
            if r.promise:
                promise_note = f" | promise -> {r.promise.status.value} (due {r.promise.promised_date.date()})"
            reliability_note = ""
            if "customer reliability" in r.priority.reason:
                reliability_note = " <-- reliability adjustment applied"
            print(
                f"  invoice {i+1}: {r.event.created_at.date()} amount=INR{r.event.amount:>10,.0f} "
                f"action={r.chosen_action.value:24s} EV={r.priority.ev:>10,.2f}{promise_note}{reliability_note}"
            )
        shown += 1

    summary = engine.promise_tracker.summary()
    print("\n" + "=" * 100)
    print("Overall promise ledger:")
    print("=" * 100)
    print(f"  total promises made : {summary['total_promises']}")
    print(f"  kept                : {summary['kept']}")
    print(f"  broken              : {summary['broken']}")
    print(f"  kept rate           : {summary['kept_rate']*100:.1f}%")
    print(f"  distinct customers involved: {summary['distinct_customers']}")
    print(f"  total INR promised  : {summary['total_inr_promised']:,.2f}")
    print(f"  total INR kept      : {summary['total_inr_kept']:,.2f}")


if __name__ == "__main__":
    main()
