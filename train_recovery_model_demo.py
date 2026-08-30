"""
Trains the supervised recovery-probability model and reports honest
held-out metrics -- run this yourself, don't take the numbers on faith.
"""

from __future__ import annotations

import argparse

from core.ml_recovery_model import train_and_evaluate


def main():
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
    print("Top 10 features by importance:")
    for name, importance in ev.top_features:
        print(f"  {name:28s} {importance:.4f}")

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
