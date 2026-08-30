"""
Unit economics: what does one decision actually cost, and what's the ROI?

The honest headline here has two parts, and conflating them would be
misleading: (1) the core decision engine -- rule-based diagnosis, EV
math, compliance, the bandit -- costs essentially nothing per event (a
few milliseconds of CPU, no external API call), so its ROI is already
enormous before any AI spend is considered. (2) The OPTIONAL LLM layer
(confidence refinement, Hinglish voice scripts) adds a small, real,
measurable cost per call. This module reports both honestly rather than
blending them into one number that overstates what the LLM is responsible
for -- the LLM does not change WHICH action gets picked in this design, so
crediting it with the full recovery number would be a real attribution
error, not just an exaggeration.

Pricing is real published per-token pricing (paid tier -- the free tier is
$0 today but isn't representative of unit economics at scale), current as
of August 2026, cited in README.md rather than assumed from training data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# USD per 1M tokens, paid tier, as of August 2026 (see README for sources).
# The free tier used throughout this project's own demos costs $0 within
# quota -- these prices are for the "what would this cost at real scale"
# question, not what we've actually been billed.
PRICING_USD_PER_1M_TOKENS = {
    "groq": {"input": 0.075, "output": 0.30},     # openai/gpt-oss-20b
    "gemini": {"input": 0.30, "output": 2.50},    # gemini-2.5-flash
}

# Illustrative only, for order-of-magnitude INR framing -- not a live FX feed.
USD_TO_INR = 88.0


def llm_call_cost_usd(provider: str | None, prompt_tokens: int, completion_tokens: int) -> float:
    if not provider or provider not in PRICING_USD_PER_1M_TOKENS:
        return 0.0
    p = PRICING_USD_PER_1M_TOKENS[provider]
    return (prompt_tokens / 1_000_000) * p["input"] + (completion_tokens / 1_000_000) * p["output"]


@dataclass
class UnitEconomicsReport:
    total_events: int
    llm_attempted_count: int
    total_llm_cost_usd: float
    total_llm_cost_inr: float
    total_recovered_inr: float
    cost_per_event_inr: float
    cost_per_llm_call_inr: float
    roi_multiple: float | None  # None when LLM cost is exactly $0 (division undefined, not infinite)
    provider_breakdown: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_events": self.total_events,
            "llm_attempted_count": self.llm_attempted_count,
            "total_llm_cost_usd": round(self.total_llm_cost_usd, 6),
            "total_llm_cost_inr": round(self.total_llm_cost_inr, 4),
            "total_recovered_inr": round(self.total_recovered_inr, 2),
            "cost_per_event_inr": round(self.cost_per_event_inr, 6),
            "cost_per_llm_call_inr": round(self.cost_per_llm_call_inr, 6),
            "roi_multiple": round(self.roi_multiple, 1) if self.roi_multiple is not None else None,
            "provider_breakdown": self.provider_breakdown,
        }


def compute_unit_economics(records: list) -> UnitEconomicsReport:
    """records: list[core.engine.DecisionRecord] from a batch run with
    use_llm=True (diagnosis-refinement cost only -- voice-script generation
    is on-demand per case, reported separately by
    estimate_voice_script_cost, not part of a batch's automatic cost)."""
    total_cost_usd = 0.0
    attempted = 0
    provider_breakdown: dict[str, dict] = {}

    for r in records:
        d = r.diagnosis
        if not d.llm_attempted:
            continue
        attempted += 1
        cost = llm_call_cost_usd(d.llm_provider, d.llm_prompt_tokens, d.llm_completion_tokens)
        total_cost_usd += cost
        key = d.llm_provider or "unknown"
        bucket = provider_breakdown.setdefault(key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
        bucket["calls"] += 1
        bucket["prompt_tokens"] += d.llm_prompt_tokens
        bucket["completion_tokens"] += d.llm_completion_tokens
        bucket["cost_usd"] += cost

    total_recovered = sum(r.recovered_amount for r in records)
    total_cost_inr = total_cost_usd * USD_TO_INR
    n = len(records)

    return UnitEconomicsReport(
        total_events=n,
        llm_attempted_count=attempted,
        total_llm_cost_usd=total_cost_usd,
        total_llm_cost_inr=total_cost_inr,
        total_recovered_inr=total_recovered,
        cost_per_event_inr=(total_cost_inr / n) if n else 0.0,
        cost_per_llm_call_inr=(total_cost_inr / attempted) if attempted else 0.0,
        roi_multiple=(total_recovered / total_cost_inr) if total_cost_inr > 0 else None,
        provider_breakdown=provider_breakdown,
    )


def estimate_voice_script_cost(script) -> dict:
    """Per-call cost for one on-demand voice-script generation (core.voice_recovery.VoiceScript)."""
    cost_usd = llm_call_cost_usd(script.llm_provider, script.llm_prompt_tokens, script.llm_completion_tokens)
    return {
        "provider": script.llm_provider,
        "prompt_tokens": script.llm_prompt_tokens,
        "completion_tokens": script.llm_completion_tokens,
        "cost_usd": round(cost_usd, 6),
        "cost_inr": round(cost_usd * USD_TO_INR, 4),
    }
