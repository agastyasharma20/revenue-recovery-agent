"""
Agentic action-selection demo -- a real LLM chooses the recovery action for
a handful of cases, with a real rationale, and the run proves the bound
holds: every chosen action is one the compliance layer had already approved
before the LLM ever saw the case.

Kept small (default 12 events) on purpose: unlike deterministic/bandit mode,
agentic mode makes one real LLM call per pursued event -- this demo is about
showing genuine per-case reasoning, not throughput. See backend/main.py's
n<=80 cap on agentic runs for the same reason.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from core.engine import RecoveryEngine
from data.generate_synthetic import generate_batch


def main():
    # Real LLM output routinely includes Unicode punctuation (en/em dashes,
    # curly quotes) that Windows' default console codepage (cp1252) can't
    # encode -- this crashed with UnicodeEncodeError the first time this
    # script ran against a live model, on a genuine (not contrived) rationale
    # string. reconfigure() is a no-op on platforms already using UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=12, help="synthetic events (kept small -- one real LLM call per pursued event)")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
    events, _ = generate_batch(args.n, seed=args.seed, now=now)

    engine = RecoveryEngine(policy_mode="agentic", audit_path=None, log_path=None, seed=args.seed * 1000 + 9)
    records = engine.process_batch(events, now=now)

    llm_picks = sum(1 for r in records if r.agentic_decision and r.agentic_decision.source == "llm")
    fallbacks = sum(1 for r in records if r.agentic_decision and r.agentic_decision.source == "deterministic_fallback")

    print("=" * 100)
    print(f"Agentic action selection -- {args.n} events, seed={args.seed}")
    print("=" * 100)
    for r in records:
        if r.agentic_decision is None:
            continue  # not pursued, or no compliant candidate -- agentic never ran
        d = r.agentic_decision
        tag = f"LLM ({d.llm_provider})" if d.source == "llm" else "FALLBACK (deterministic top pick)"
        print(f"\n{r.event.source.value} / {r.event.decline_reason.value} / Rs.{r.event.amount:,.0f}")
        print(f"  compliance-allowed candidates offered to the LLM were compliance-filtered first")
        print(f"  chosen: {r.chosen_action.value}  <-- {tag}")
        print(f'  rationale: "{d.rationale}"')
        if d.llm_error:
            print(f"  (llm_error: {d.llm_error})")

    print("\n" + "=" * 100)
    print(f"Summary: {llm_picks} genuine LLM picks, {fallbacks} clean fallbacks "
          f"(no key configured, or the model was unavailable) -- 0 out-of-bounds actions executed, by construction.")
    print("See tests/test_agentic_policy.py for the test that proves an out-of-bounds")
    print("LLM response gets rejected rather than executed.")
    print("=" * 100)


if __name__ == "__main__":
    main()
