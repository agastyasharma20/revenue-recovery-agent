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

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import urllib.request
import urllib.error

from core.schema import RevenueEvent, EventSource, DeclineReason, DiagnosisCategory

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.1-8b-instant was retired from Groq's lineup; openai/gpt-oss-20b is
# a current, fast, free-tier-available model. Override via GROQ_MODEL if
# your account's available models (GET /openai/v1/models) differ.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

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


def _call_groq_for_refinement(event: RevenueEvent, base: Diagnosis) -> Optional[dict]:
    """Best-effort call to Groq's OpenAI-compatible endpoint. Returns None on
    any failure -- caller falls back to the rule-based result."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None

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

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 400,
    }
    if "gpt-oss" in GROQ_MODEL or "qwen" in GROQ_MODEL:
        # Reasoning models spend tokens on hidden reasoning before the
        # visible answer -- at default effort this task alone burned
        # 298/300 tokens on reasoning and got cut off with EMPTY content
        # (finish_reason="length") before ever writing the JSON. "low"
        # effort reliably leaves room for the actual answer on a task this
        # small. IMPORTANT: this field is NOT universal -- Groq returns a
        # 400 ("reasoning_effort is not supported with this model") for
        # non-reasoning models, so it's only sent for models known to
        # support it rather than always-on.
        payload["reasoning_effort"] = "low"
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        GROQ_API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Without a browser-like User-Agent, Groq's Cloudflare front-end
            # blocks the request outright (HTTP 403, Cloudflare error 1010)
            # before it ever reaches Groq's API -- this bit a real API key
            # during testing, not just missing/invalid ones.
            "User-Agent": "Mozilla/5.0 (compatible; revenue-recovery-agent/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        # tolerate the model wrapping JSON in prose / code fences
        start, end = content.find("{"), content.rfind("}")
        parsed = json.loads(content[start : end + 1])
        conf = float(parsed.get("confidence", base.confidence))
        conf = max(0.0, min(1.0, conf))
        rationale = str(parsed.get("rationale", "")).strip() or base.rationale
        return {"confidence": conf, "rationale": rationale}
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, this must never crash the pipeline
        return {"_error": f"{type(exc).__name__}: {exc}"}


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

        import os as _os
        if not _os.environ.get("GROQ_API_KEY"):
            # no API key configured -- never even attempted, not a "fallback"
            return base

        base.llm_attempted = True
        t0 = time.time()
        refinement = _call_groq_for_refinement(event, base)
        latency = time.time() - t0

        if refinement is None:
            return base

        if "_error" in refinement:
            if self.breaker is not None:
                self.breaker.record_failure()
            base.llm_error = refinement["_error"]
            base.rationale += f" [LLM call failed, used rule-based fallback: {refinement['_error']}]"
            return base

        if self.breaker is not None:
            self.breaker.record_success()

        base.confidence = refinement["confidence"]
        base.rationale = refinement["rationale"] + f" (LLM-refined, {latency*1000:.0f}ms)"
        base.llm_used = True
        return base
