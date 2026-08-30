"""
Multi-provider LLM fallback chain. Uses fake/garbage keys for the network-
dependent paths (no real Gemini key available in CI) -- these still prove
the FALLBACK LOGIC is correct (skips unconfigured providers, tries the next
one on failure, reports the last real error) without needing live network
access to assert on. The genuinely-live Groq path is exercised manually
(see README) and by classifier.py/voice_recovery.py's own tests.
"""

import os

from core.llm_client import call_llm, any_provider_configured, _provider_order


def _clear_all_provider_keys():
    return os.environ.pop("GROQ_API_KEY", None), os.environ.pop("GEMINI_API_KEY", None)


def _restore(saved_groq, saved_gemini):
    if saved_groq is not None:
        os.environ["GROQ_API_KEY"] = saved_groq
    if saved_gemini is not None:
        os.environ["GEMINI_API_KEY"] = saved_gemini


def test_no_providers_configured_returns_clean_error():
    saved = _clear_all_provider_keys()
    try:
        result = call_llm("hello")
        assert not result.ok
        assert result.error == "no_provider_configured"
    finally:
        _restore(*saved)


def test_any_provider_configured_reflects_either_key():
    saved = _clear_all_provider_keys()
    try:
        assert any_provider_configured() is False
        os.environ["GEMINI_API_KEY"] = "fake"
        assert any_provider_configured() is True
    finally:
        os.environ.pop("GEMINI_API_KEY", None)
        _restore(*saved)


def test_unconfigured_provider_is_skipped_not_counted_as_failure():
    """With only a garbage Gemini key set (Groq unset), the error returned
    must come from Gemini's real attempt, not a generic 'groq missing key'
    message -- proving Groq was silently skipped rather than treated as a
    failed attempt."""
    saved = _clear_all_provider_keys()
    os.environ["GEMINI_API_KEY"] = "totally-fake-invalid-key"
    try:
        result = call_llm("hello", timeout=5.0)
        assert not result.ok
        assert result.provider == "gemini"
    finally:
        os.environ.pop("GEMINI_API_KEY", None)
        _restore(*saved)


def test_provider_priority_override_via_env_var():
    saved_priority = os.environ.get("LLM_PROVIDER_PRIORITY")
    os.environ["LLM_PROVIDER_PRIORITY"] = "gemini,groq"
    try:
        assert _provider_order() == ["gemini", "groq"]
    finally:
        if saved_priority is not None:
            os.environ["LLM_PROVIDER_PRIORITY"] = saved_priority
        else:
            os.environ.pop("LLM_PROVIDER_PRIORITY", None)


def test_default_provider_order_is_groq_then_gemini():
    saved_priority = os.environ.pop("LLM_PROVIDER_PRIORITY", None)
    try:
        assert _provider_order() == ["groq", "gemini"]
    finally:
        if saved_priority is not None:
            os.environ["LLM_PROVIDER_PRIORITY"] = saved_priority


def test_both_providers_failing_reports_the_last_attempted_providers_error():
    saved = _clear_all_provider_keys()
    os.environ["GROQ_API_KEY"] = "sk-fake"
    os.environ["GEMINI_API_KEY"] = "fake-gemini-key"
    try:
        result = call_llm("hello", timeout=5.0)
        assert not result.ok
        assert result.provider == "gemini"  # last one tried, per DEFAULT_PROVIDER_ORDER
        assert result.error is not None
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        _restore(*saved)
