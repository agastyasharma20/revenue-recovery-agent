"""
Multi-provider LLM client with a genuine fallback chain, used by both
core/classifier.py (diagnosis confidence refinement) and
core/voice_recovery.py (Hinglish call script generation).

Principal-engineer-level reason this exists as its own module rather than
"just call Groq": a production system that hard-depends on exactly one LLM
vendor inherits that vendor's outages and rate limits as ITS OWN outages.
call_llm() tries providers in priority order and only fails if EVERY
configured provider fails -- if Groq is down or rate-limited, a Gemini key
(also free-tier) picks up the same request transparently. Both call sites
already treat "no LLM available" as a normal, tested code path (rule-based
diagnosis, template call scripts), so this fallback chain is pure upside:
more providers configured = fewer silent drops to the rule-based fallback,
never a new failure mode.

Centralized also because three real bugs were found integrating Groq alone
-- Cloudflare blocking urllib's default User-Agent, a retired default
model, and a reasoning model silently truncating to empty content -- and
fixing them in one place means they can't be re-broken by a second call site.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.1-8b-instant was retired from Groq's lineup; openai/gpt-oss-20b is
# a current, fast, free-tier-available model. Override via GROQ_MODEL if
# your account's available models (GET /openai/v1/models) differ.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

# Gemini 2.5 Flash: the best balance of the free tier as of 2026 (10 RPM /
# 250 requests per day at time of writing) -- Flash-Lite has a higher quota
# but noticeably weaker output for a generation task like the voice script;
# 2.5 Pro's free quota is too small (5 RPM / 100 per day) to rely on.
# Override via GEMINI_MODEL if you'd rather trade one for the other.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Provider priority: try Groq first (already verified fast + reliable in
# this project), fall back to Gemini. Override with LLM_PROVIDER_PRIORITY
# as a comma-separated list, e.g. "gemini,groq" to reverse it.
DEFAULT_PROVIDER_ORDER = ["groq", "gemini"]


@dataclass
class LLMResult:
    content: Optional[str]
    error: Optional[str]
    provider: Optional[str] = None  # which provider actually served this, or attempted last
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.content is not None


def _call_groq_raw(prompt: str, max_tokens: int, temperature: float, timeout: float) -> LLMResult:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return LLMResult(content=None, error="no_api_key", provider="groq")

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if "gpt-oss" in GROQ_MODEL or "qwen" in GROQ_MODEL:
        # Reasoning models spend tokens on hidden reasoning before the
        # visible answer -- at default effort a short task can burn the
        # entire max_tokens budget on reasoning and return EMPTY content
        # (finish_reason="length"). "low" effort reliably leaves room for
        # the actual answer on tasks this size. NOT universal: Groq returns
        # 400 for this field on non-reasoning models, so it's conditional.
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
            # before it ever reaches Groq's API.
            "User-Agent": "Mozilla/5.0 (compatible; revenue-recovery-agent/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        if not content:
            return LLMResult(content=None, error="empty_content (likely reasoning-token truncation)",
                              provider="groq", prompt_tokens=pt, completion_tokens=ct)
        return LLMResult(content=content, error=None, provider="groq", prompt_tokens=pt, completion_tokens=ct)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, must never crash the pipeline
        return LLMResult(content=None, error=f"{type(exc).__name__}: {exc}", provider="groq")


def _call_gemini_raw(prompt: str, max_tokens: int, temperature: float, timeout: float) -> LLMResult:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return LLMResult(content=None, error="no_api_key", provider="gemini")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": "Mozilla/5.0 (compatible; revenue-recovery-agent/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        candidates = data.get("candidates") or []
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "no_candidates")
            return LLMResult(content=None, error=f"no_candidates: {reason}", provider="gemini")
        parts = candidates[0].get("content", {}).get("parts", [])
        content = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata", {})
        pt, ct = usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)
        if not content:
            finish_reason = candidates[0].get("finishReason", "unknown")
            return LLMResult(content=None, error=f"empty_content (finishReason={finish_reason})",
                              provider="gemini", prompt_tokens=pt, completion_tokens=ct)
        return LLMResult(content=content, error=None, provider="gemini", prompt_tokens=pt, completion_tokens=ct)
    except Exception as exc:  # noqa: BLE001
        return LLMResult(content=None, error=f"{type(exc).__name__}: {exc}", provider="gemini")


_PROVIDERS = {
    "groq": _call_groq_raw,
    "gemini": _call_gemini_raw,
}


def _provider_order() -> list[str]:
    override = os.environ.get("LLM_PROVIDER_PRIORITY")
    if override:
        return [p.strip() for p in override.split(",") if p.strip() in _PROVIDERS]
    return DEFAULT_PROVIDER_ORDER


def call_llm(prompt: str, max_tokens: int = 400, temperature: float = 0.3, timeout: float = 10.0) -> LLMResult:
    """Tries each configured provider in priority order. A provider with no
    API key set is skipped instantly (not counted as a "failure" worth
    logging loudly -- that's the expected, tested state for most deployments
    of this project). Returns the first success; if every provider fails or
    is unconfigured, returns the LAST attempted provider's error so callers
    still get an informative message for a diagnosis/rationale field."""
    last_result: Optional[LLMResult] = None
    for provider_name in _provider_order():
        fn = _PROVIDERS[provider_name]
        result = fn(prompt, max_tokens, temperature, timeout)
        if result.ok:
            return result
        if result.error != "no_api_key":
            last_result = result  # only remember genuine attempts, not skipped/unconfigured ones
    return last_result or LLMResult(content=None, error="no_provider_configured", provider=None)


# Backward-compatible alias -- existing call sites and tests predate the
# multi-provider rename.
call_groq = call_llm


def any_provider_configured() -> bool:
    """Used by callers that want to distinguish "we never even tried" (no
    key for ANY provider) from "we tried and it failed" -- checking a
    single hardcoded env var here would wrongly skip a Gemini-only setup
    just because GROQ_API_KEY isn't the one that's set."""
    return os.environ.get("GROQ_API_KEY") is not None or os.environ.get("GEMINI_API_KEY") is not None


def extract_json_object(text: str) -> Optional[dict]:
    """Tolerates the model wrapping JSON in prose or code fences."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
