"""
Voice-recovery script generation: the template fallback must always work
(no network dependency), and the LLM path (when a key is configured) must
produce a well-formed script or fall back cleanly -- never raise.
"""

import os
from datetime import datetime, timezone

from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment
from core.classifier import Classifier
from core.voice_recovery import generate_voice_script, _template_script, VoiceScript

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def _event(source, reason, amount=1500.0):
    return RevenueEvent(
        source=source, decline_reason=reason, amount=amount,
        customer_segment=CustomerSegment.MEDIUM_LTV, created_at=NOW, last_attempt_at=NOW,
    )


def test_template_fallback_never_needs_network_and_fills_amount():
    event = _event(EventSource.SUBSCRIPTION_FAILED, DeclineReason.INSUFFICIENT_FUNDS, amount=2500.0)
    diagnosis = Classifier(use_llm=False).diagnose(event)
    script = _template_script(event, diagnosis)
    assert script.generated_by == "template_fallback"
    assert "2,500" in script.main_ask
    assert script.language == "hinglish"
    assert len(script.objection_handling) >= 1


def test_generate_voice_script_never_raises_without_api_key():
    saved = os.environ.pop("GROQ_API_KEY", None)
    try:
        event = _event(EventSource.CHECKOUT_ABANDONED, DeclineReason.CHECKOUT_ABANDONED)
        diagnosis = Classifier(use_llm=False).diagnose(event)
        script = generate_voice_script(event, diagnosis)
        assert isinstance(script, VoiceScript)
        assert script.generated_by == "template_fallback"
        assert script.llm_error == "no_api_key"
    finally:
        if saved is not None:
            os.environ["GROQ_API_KEY"] = saved


def test_generate_voice_script_never_raises_with_garbage_api_key():
    saved = os.environ.get("GROQ_API_KEY")
    os.environ["GROQ_API_KEY"] = "sk-totally-fake-invalid-key"
    try:
        event = _event(EventSource.B2B_RECEIVABLE_OVERDUE, DeclineReason.INVOICE_OVERDUE, amount=90000.0)
        diagnosis = Classifier(use_llm=False).diagnose(event)
        script = generate_voice_script(event, diagnosis)
        assert script.generated_by == "template_fallback"
        assert script.llm_error is not None
    finally:
        if saved is not None:
            os.environ["GROQ_API_KEY"] = saved
        else:
            os.environ.pop("GROQ_API_KEY", None)


def test_render_produces_readable_script_text():
    event = _event(EventSource.SUBSCRIPTION_FAILED, DeclineReason.EXPIRED_CARD)
    diagnosis = Classifier(use_llm=False).diagnose(event)
    script = _template_script(event, diagnosis)
    text = script.render()
    assert "[Opening]" in text and "[Closing]" in text and "[Main ask]" in text
