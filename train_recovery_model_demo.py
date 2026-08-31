"""
Trains the supervised recovery-probability model and reports honest
held-out metrics -- run this yourself, don't take the numbers on faith.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from core.ml_recovery_model import train_and_evaluate
from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment, Action


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # SHAP feature names can include non-ASCII in some locales
    except AttributeError:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=8000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--test-size", type=float, default=0.2)
    args = parser.parse_args()

    print(f"Generating {args.n} (event, randomly-exposed action) training examples...")
    model, ev = train_and_evaluate(n_samples=args.n, seed=args.seed, test_size=args.test_size)

    print()
    print("=" * 70)
    print(f"HELD-OUT EVALUATION ({ev.n_train} train / {ev.n_test} test, "
          f"{ev.positive_rate_test*100:.1f}% positive rate in test set)")
    print("=" * 70)
    print(f"  AUC (threshold-independent)   : {ev.auc:.4f}")
    print(f"  Average precision / PR-AUC    : {ev.average_precision:.4f}  (baseline for random = {ev.positive_rate_test:.4f})")
    print(f"  Decision threshold used       : {ev.threshold_used:.2f}  (tuned on a VALIDATION split, not the test set)")
    print(f"  Precision @ threshold         : {ev.precision:.4f}")
    print(f"  Recall @ threshold            : {ev.recall:.4f}")
    print(f"  F1 @ threshold                : {ev.f1:.4f}")
    print(f"  Confusion matrix [[TN, FP], [FN, TP]]: {ev.confusion_matrix}")

    print()
    print("Threshold sweep on the TEST set (shows the full precision/recall")
    print("tradeoff -- not just the one cherry-picked point above):")
    print(f"  {'threshold':>10s} {'precision':>10s} {'recall':>10s} {'f1':>10s}")
    for t, p, r, f in ev.threshold_sweep:
        marker = " <-- used" if abs(t - ev.threshold_used) < 1e-9 else ""
        print(f"  {t:>10.2f} {p:>10.4f} {r:>10.4f} {f:>10.4f}{marker}")

    print()
    print("Top 10 features by GBM's built-in (impurity-based) importance:")
    for name, importance in ev.top_features:
        print(f"  {name:28s} {importance:.4f}")

    print()
    print(f"Top 10 features by SHAP importance (mean |contribution| over a "
          f"{min(300, ev.n_test)}-row test sample) -- computed separately because")
    print("impurity-based importance is known to bias toward high-cardinality one-hot")
    print("groups, which this feature set has several of:")
    for name, importance in ev.shap_top_features:
        print(f"  {name:28s} {importance:.4f}")
    top_impurity_names = {n for n, _ in ev.top_features[:5]}
    top_shap_names = {n for n, _ in ev.shap_top_features[:5]}
    agreement = len(top_impurity_names & top_shap_names)
    print(f"  -> {agreement}/5 features agree between the two top-5 rankings "
          f"(reported, not asserted -- this number can come out anywhere from 0 to 5).")

    print()
    print("Per-case SHAP explanation -- WHY one specific prediction came out the way it")
    print("did, signed (positive pushes recovery probability up, negative pushes it down),")
    print("not just a global ranking:")
    example_event = RevenueEvent(
        source=EventSource.SUBSCRIPTION_FAILED, decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        amount=2500.0, customer_segment=CustomerSegment.HIGH_LTV,
        created_at=datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc),
        last_attempt_at=datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc),
    )
    proba = model.predict_proba(example_event, Action.RETRY_PAYMENT, example_event.created_at)
    print(f"  case: {example_event.source.value} / {example_event.decline_reason.value} / "
          f"Rs.{example_event.amount:,.0f} / {example_event.customer_segment.value}, action=retry_payment")
    print(f"  predicted P(recovery) = {proba:.3f}")
    for name, contribution in model.explain(example_event, Action.RETRY_PAYMENT, example_event.created_at):
        sign = "+" if contribution >= 0 else "-"
        print(f"    {sign} {name:28s} {abs(contribution):.4f}")

    print()
    print("HONESTY NOTE: trained on outcomes sampled from core/outcome_simulator.py's")
    print("effectiveness matrix -- the only source of ground-truth recovery outcomes")
    print("in this synthetic world, same as what the online bandit learns from. NOT")
    print("wired into the default decision path (core/prioritizer.py keeps its own,")
    print("independent, not-oracle-derived prior) -- this is a complementary,")
    print("honestly-evaluated signal, not a silent swap-in for the verified numbers")
    print("reported elsewhere in this project.")


if __name__ == "__main__":
    main()
