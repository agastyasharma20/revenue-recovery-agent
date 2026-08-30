"""
Unit economics demo: what does this agent actually cost to run, and what's
the ROI?

Uses a SMALL batch with use_llm=True on purpose -- this makes real LLM API
calls (free tier, but real network calls), and a small batch keeps this
demo fast and comfortably within free-tier rate limits. Cost scales
linearly; see the per-event and per-call figures below to extrapolate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from core.engine import RecoveryEngine
from core.unit_economics import compute_unit_economics, estimate_voice_script_cost, PRICING_USD_PER_1M_TOKENS, USD_TO_INR
from core.voice_recovery import generate_voice_script
from data.generate_synthetic import generate_batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=30, help="small on purpose -- real API calls, free-tier rate limits")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    events, _ = generate_batch(args.n, seed=args.seed, now=now)

    print(f"Running {args.n} events with use_llm=True (real API calls, free tier)...")
    engine = RecoveryEngine(use_llm=True, policy_mode="deterministic", audit_path=None, seed=args.seed * 1000 + 9, log_path=None)
    records = engine.process_batch(events, now=now)

    report = compute_unit_economics(records)

    print()
    print("=" * 78)
    print("PRICING ASSUMPTIONS (paid tier, USD per 1M tokens -- see README for sources)")
    print("=" * 78)
    for provider, p in PRICING_USD_PER_1M_TOKENS.items():
        print(f"  {provider:10s} input=${p['input']:.3f}  output=${p['output']:.3f}   (USD/INR assumed: {USD_TO_INR})")

    print()
    print("=" * 78)
    print("CORE DECISION ENGINE (diagnosis + EV + compliance + bandit + audit)")
    print("=" * 78)
    print("  Cost per decision: effectively $0 -- no external API call, a few")
    print("  milliseconds of CPU. This is why the base ROI is enormous before any")
    print("  AI spend is even considered: near-zero marginal cost, real INR captured.")

    print()
    print("=" * 78)
    print(f"OPTIONAL LLM LAYER: diagnosis confidence refinement ({report.total_events} events)")
    print("=" * 78)
    print(f"  LLM attempted on: {report.llm_attempted_count}/{report.total_events} events")
    for provider, b in report.provider_breakdown.items():
        print(f"    {provider}: {b['calls']} calls, {b['prompt_tokens']} prompt + {b['completion_tokens']} completion tokens, ${b['cost_usd']:.6f}")
    print(f"  Total LLM cost this batch : ${report.total_llm_cost_usd:.6f} (~INR {report.total_llm_cost_inr:.4f})")
    print(f"  Cost per event (blended)  : INR {report.cost_per_event_inr:.6f}")
    print(f"  Cost per LLM call         : INR {report.cost_per_llm_call_inr:.6f}")
    print(f"  Total INR recovered       : {report.total_recovered_inr:,.2f}")
    if report.roi_multiple is not None:
        print(f"  ROI multiple (recovered / LLM cost): {report.roi_multiple:,.0f}x")
    print()
    print("  HONEST ATTRIBUTION NOTE: the LLM refines confidence/rationale for")
    print("  explainability -- it does NOT change which action gets picked in this")
    print("  design (core/policy.py and core/contextual_bandit.py do that, at $0")
    print("  marginal cost). Crediting the LLM with the recovered INR above would be")
    print("  an attribution error. Its real value is richer, human-readable audit")
    print("  trails and Hinglish voice scripts -- trust/compliance/CX value, not a")
    print("  recovery-rate lever, at a cost small enough to be a rounding error.")

    print()
    print("=" * 78)
    print("ON-DEMAND: one Hinglish voice-script generation")
    print("=" * 78)
    pursued = [r for r in records if r.pursued]
    if pursued:
        script = generate_voice_script(pursued[0].event, pursued[0].diagnosis)
        cost = estimate_voice_script_cost(script)
        print(f"  generated_by={script.generated_by} provider={cost['provider']}")
        print(f"  tokens: {cost['prompt_tokens']} prompt + {cost['completion_tokens']} completion")
        print(f"  cost: ${cost['cost_usd']:.6f} (~INR {cost['cost_inr']:.4f}) -- per case, on demand only")
    else:
        print("  (no pursued cases in this small batch to demo on)")


if __name__ == "__main__":
    main()
