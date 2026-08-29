"""
/metrics-style summary: aggregate stats over a batch of DecisionRecords for
observability -- what you'd expose on a metrics endpoint or dashboard tile.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass
class MetricsSummary:
    total_events: int
    events_pursued: int
    events_recovered: int
    total_recovered_inr: float
    recovery_rate_of_pursued: float
    recovery_rate_of_total: float
    avg_latency_ms_per_layer: dict
    llm_fallback_rate: float
    llm_used_rate: float
    llm_error_count: int

    def to_dict(self) -> dict:
        return {
            "total_events": self.total_events,
            "events_pursued": self.events_pursued,
            "events_recovered": self.events_recovered,
            "total_recovered_inr": round(self.total_recovered_inr, 2),
            "recovery_rate_of_pursued": round(self.recovery_rate_of_pursued, 4),
            "recovery_rate_of_total": round(self.recovery_rate_of_total, 4),
            "avg_latency_ms_per_layer": {k: round(v, 4) for k, v in self.avg_latency_ms_per_layer.items()},
            "llm_fallback_rate": round(self.llm_fallback_rate, 4),
            "llm_used_rate": round(self.llm_used_rate, 4),
            "llm_error_count": self.llm_error_count,
        }


def summarize(records: list) -> MetricsSummary:
    total = len(records)
    pursued = [r for r in records if r.pursued]
    recovered = [r for r in records if r.outcome and r.outcome.recovered]
    total_recovered = sum(r.recovered_amount for r in records)

    layer_names = set()
    for r in records:
        layer_names.update(r.latencies_ms.keys())
    avg_latency = {
        layer: statistics.mean(r.latencies_ms[layer] for r in records if layer in r.latencies_ms)
        for layer in layer_names
    } if layer_names else {}

    llm_used_count = sum(1 for r in records if r.diagnosis.llm_used)
    llm_error_count = sum(1 for r in records if r.diagnosis.llm_error)
    llm_attempted_count = sum(1 for r in records if r.diagnosis.llm_attempted)
    # fallback rate is only meaningful relative to attempts: of the diagnoses
    # that actually tried to call the LLM (use_llm=True, API key present,
    # breaker closed), what fraction ended up falling back to rule-based
    # anyway (call failed)? If the LLM was never attempted (disabled, no key,
    # breaker open) this is simply undefined (0/0 -> 0.0), not "100% failure".
    fallback_count = llm_attempted_count - llm_used_count

    return MetricsSummary(
        total_events=total,
        events_pursued=len(pursued),
        events_recovered=len(recovered),
        total_recovered_inr=total_recovered,
        recovery_rate_of_pursued=(len(recovered) / len(pursued)) if pursued else 0.0,
        recovery_rate_of_total=(len(recovered) / total) if total else 0.0,
        avg_latency_ms_per_layer=avg_latency,
        llm_fallback_rate=(fallback_count / llm_attempted_count) if llm_attempted_count else 0.0,
        llm_used_rate=(llm_used_count / total) if total else 0.0,
        llm_error_count=llm_error_count,
    )
