"""
Shared Groq chat-completion client, used by both core/classifier.py
(diagnosis confidence refinement) and core/voice_recovery.py (Hinglish call
script generation). Centralized so the three real bugs found while
integrating Groq -- Cloudflare blocking urllib's default User-Agent, a
retired default model, and gpt-oss silently truncating to empty content
without reasoning_effort -- are fixed in exactly one place instead of
copy-pasted (and re-broken) wherever an LLM call is needed next.
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


@dataclass
class LLMResult:
    content: Optional[str]
    error: Optional[str]

    @property
    def ok(self) -> bool:
        return self.content is not None


def call_groq(prompt: str, max_tokens: int = 400, temperature: float = 0.3, timeout: float = 10.0) -> LLMResult:
    """Best-effort call to Groq's OpenAI-compatible endpoint. Never raises --
    every failure mode (no key, network error, bad response, truncated
    reasoning-model output) comes back as LLMResult(content=None, error=...)
    so callers can fall back cleanly."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return LLMResult(content=None, error="no_api_key")

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
        if not content:
            return LLMResult(content=None, error="empty_content (likely reasoning-token truncation)")
        return LLMResult(content=content, error=None)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, must never crash the pipeline
        return LLMResult(content=None, error=f"{type(exc).__name__}: {exc}")


def extract_json_object(text: str) -> Optional[dict]:
    """Tolerates the model wrapping JSON in prose or code fences."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
