"""
Diagnosis layer.

Rule-based classification is the reliable backbone: it is deterministic,
free, instant, and never fails. An optional Groq LLM call can refine the
confidence score and attach a natural-language rationale, but it is pure
upside -- if the API key is missing, the network is down, or Groq returns
garbage, we fall back to the rule-based result and keep going. The pipeline
must never depend on the LLM to function.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from core.schema import RevenueEvent, EventSource, DeclineReason, DiagnosisCategory
from core.llm_client import call_llm, extract_json_object, any_provider_configured

# --- deterministic rule table: decline_reason -> (diagnosis, base_confidence, is_retriable) ---
_RULES = {
    DeclineReason.BANK_SERVER_TIMEOUT: (DiagnosisCategory.TRANSIENT_RETRIABLE, 0.90, True),
    DeclineReason.NETWORK_ERROR: (DiagnosisCategory.TRANSIENT_RETRIABLE, 0.88, True),
    DeclineReason.INSUFFICIENT_FUNDS: (DiagnosisCategory.SOFT_DECLINE_RETRIABLE, 0.70, True),
    DeclineReason.EXCEEDS_LIMIT: (DiagnosisCategory.SOFT_DECLINE_RETRIABLE, 0.60, True),
    DeclineReason.EXPIRED_CARD: (DiagnosisCategory.HARD_DECLINE_UNRETRIABLE, 0.95, False),
    DeclineReason.CARD_BLOCKED: (DiagnosisCategory.HARD_DECLINE_UNRETRIABLE, 0.92, False),
    DeclineReason.INVALID_CVV: (DiagnosisCategory.HARD_DECLINE_UNRETRIABLE, 0.85, False),
    DeclineReason.ISSUER_DECLINED: (DiagnosisCategory.HARD_DECLINE_UNRETRIABLE, 0.55, False),
    DeclineReason.MANDATE_NOT_APPROVED: (DiagnosisCategory.HARD_DECLINE_UNRETRIABLE, 0.65, False),
    DeclineReason.FRAUD_SUSPECTED: (DiagnosisCategory.RISK_BLOCK, 0.90, False),
    DeclineReason.CHECKOUT_ABANDONED: (DiagnosisCategory.CUSTOMER_INACTION, 0.80, False),
    DeclineReason.INVOICE_OVERDUE: (DiagnosisCategory.CUSTOMER_INACTION, 0.80, False),
    DeclineReason.UNKNOWN: (DiagnosisCategory.UNKNOWN, 0.30, False),
}


@dataclass
class Diagnosis:
    category: DiagnosisCategory
    confidence: float
    is_retriable_by_rule: bool
    rationale: str
    llm_used: bool
    llm_error: Optional[str] = None
    llm_attempted: bool = False  # True only if an actual API call was made (use_llm=True and breaker allowed it)
    llm_provider: Optional[str] = None  # "groq" | "gemini" | None -- whichever actually served the call
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0


def _rule_based_diagnosis(event: RevenueEvent) -> Diagnosis:
    category, confidence, retriable = _RULES.get(
        event.decline_reason, (DiagnosisCategory.UNKNOWN, 0.3, False)
    )
    rationale = (
        f"Rule-based: source={event.source.value}, reason={event.decline_reason.value} "
        f"-> {category.value} (base confidence {confidence:.2f})."
    )
    return Diagnosis(
        category=category,
        confidence=confidence,
        is_retriable_by_rule=retriable,
        rationale=rationale,
        llm_used=False,
    )


def _refine_via_llm(event: RevenueEvent, base: Diagnosis) -> Optional[dict]:
    """Returns {"confidence":..., "rationale":...} on success, or
    {"_error": "..."} on any failure -- caller falls back to rule-based."""
    prompt = (
        "You are refining a rule-based payment-failure diagnosis. "
        f"Event: source={event.source.value}, decline_reason={event.decline_reason.value}, "
        f"amount_inr={event.amount:.2f}, retry_count={event.retry_count}, "
        f"customer_segment={event.customer_segment.value}. "
        f"Rule-based diagnosis: category={base.category.value}, confidence={base.confidence:.2f}. "
        "Reply with ONLY a compact JSON object: "
        '{"confidence": <float 0-1>, "rationale": "<one sentence>"}. '
        "Do not change the category, only refine confidence and add a short human-readable rationale."
    )

    result = call_llm(prompt, max_tokens=400, temperature=0.2)
    usage = {"provider": result.provider, "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens}
    if not result.ok:
        return {"_error": result.error, **usage}

    parsed = extract_json_object(result.content)
    if parsed is None:
        return {"_error": f"could_not_parse_json: {result.content[:200]!r}", **usage}

    conf = max(0.0, min(1.0, float(parsed.get("confidence", base.confidence))))
    rationale = str(parsed.get("rationale", "")).strip() or base.rationale
    return {"confidence": conf, "rationale": rationale, **usage}


class Classifier:
    """Wraps rule-based diagnosis + optional LLM refinement behind one call.

    A circuit breaker (added in Phase 4) lives here too: after N consecutive
    LLM failures we stop calling Groq for a cooldown window and just use the
    rule-based backbone, logging that we did so.
    """

    def __init__(self, use_llm: bool = True, breaker=None):
        self.use_llm = use_llm
        self.breaker = breaker  # core.circuit_breaker.CircuitBreaker, optional

    def diagnose(self, event: RevenueEvent) -> Diagnosis:
        base = _rule_based_diagnosis(event)

        if not self.use_llm:
            return base

        if self.breaker is not None and not self.breaker.allow_call():
            base.rationale += " [LLM skipped: circuit breaker open, rule-based-only mode]"
            return base

        if not any_provider_configured():
            # no API key configured for ANY provider -- never even attempted, not a "fallback"
            return base

        base.llm_attempted = True
        t0 = time.time()
        refinement = _refine_via_llm(event, base)
        latency = time.time() - t0
        base.llm_provider = refinement.get("provider")
        base.llm_prompt_tokens = refinement.get("prompt_tokens", 0)
        base.llm_completion_tokens = refinement.get("completion_tokens", 0)

        if "_error" in refinement:
            if self.breaker is not None:
                self.breaker.record_failure()
            base.llm_error = refinement["_error"]
            base.rationale += f" [LLM call failed, used rule-based fallback: {refinement['_error']}]"
            return base

        if self.breaker is not None:
            self.breaker.record_success()

        base.confidence = refinement["confidence"]
        base.rationale = refinement["rationale"] + f" (LLM-refined via {base.llm_provider}, {latency*1000:.0f}ms)"
        base.llm_used = True
        return base
