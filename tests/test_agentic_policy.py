"""
Agentic action selection (core/agentic_policy.py + RecoveryEngine's
policy_mode="agentic"). The property that actually matters here isn't "does
the LLM pick something reasonable" -- it's "CAN the LLM ever cause an
out-of-bounds action to execute." It cannot, and that's what most of these
tests are built to prove, not just assert in a docstring.

Testing philosophy match with the rest of this suite: real network calls
with the real/garbage keys already used by test_llm_client.py and
test_voice_recovery.py wherever the property under test doesn't need a
specific model response. The one exception is the out-of-bounds-rejection
test below, which monkeypatches call_llm directly -- a real model won't
reliably hallucinate on command, so the only way to deterministically
exercise "the model returned something outside the candidate list" is to
simulate that response and check it gets rejected, not executed.
"""

import os
from datetime import datetime, timezone

import core.agentic_policy as agentic_policy
from core.agentic_policy import select_action, AgenticDecision
from core.llm_client import LLMResult
from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment, Action
from core.classifier import Classifier
from core.engine import RecoveryEngine

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def _event(source=EventSource.SUBSCRIPTION_FAILED, reason=DeclineReason.INSUFFICIENT_FUNDS, amount=1500.0):
    return RevenueEvent(
        source=source, decline_reason=reason, amount=amount,
        customer_segment=CustomerSegment.MEDIUM_LTV, created_at=NOW, last_attempt_at=NOW,
    )


def _diagnosis(event):
    return Classifier(use_llm=False).diagnose(event)


# --- the core safety property: the LLM can never pick outside the given list ---


def test_out_of_bounds_llm_response_is_rejected_not_executed(monkeypatch):
    """The single most important test in this file. Simulates the LLM
    returning a syntactically valid action name that is simply not one of
    the offered candidates (e.g. it tried to pick collections escalation
    when only reminder channels were offered) -- must fall back, never
    return that action."""
    event = _event()
    diagnosis = _diagnosis(event)
    candidates = [Action.SEND_REMINDER_WHATSAPP, Action.SEND_REMINDER_SMS]

    def fake_call_llm(prompt, max_tokens=300, temperature=0.2, timeout=10.0):
        return LLMResult(
            content='{"chosen_action": "escalate_to_collections", "rationale": "trying to escalate anyway"}',
            error=None, provider="groq", prompt_tokens=50, completion_tokens=20,
        )

    monkeypatch.setattr(agentic_policy, "call_llm", fake_call_llm)
    decision = select_action(event, diagnosis, candidates)

    assert decision.source == "deterministic_fallback"
    assert decision.action in candidates  # never the out-of-bounds action
    assert decision.action == candidates[0]
    assert "out_of_bounds_action" in decision.llm_error
    assert "escalate_to_collections" in decision.llm_error


def test_valid_in_bounds_llm_choice_is_honored(monkeypatch):
    event = _event()
    diagnosis = _diagnosis(event)
    candidates = [Action.SEND_REMINDER_WHATSAPP, Action.SEND_REMINDER_SMS, Action.RETRY_PAYMENT]

    def fake_call_llm(prompt, max_tokens=300, temperature=0.2, timeout=10.0):
        return LLMResult(
            content='{"chosen_action": "send_reminder_sms", "rationale": "SMS is more reliable here"}',
            error=None, provider="groq", prompt_tokens=50, completion_tokens=20,
        )

    monkeypatch.setattr(agentic_policy, "call_llm", fake_call_llm)
    decision = select_action(event, diagnosis, candidates)

    assert decision.source == "llm"
    assert decision.action == Action.SEND_REMINDER_SMS
    assert decision.rationale == "SMS is more reliable here"
    assert decision.llm_error is None


def test_malformed_json_falls_back_cleanly(monkeypatch):
    event = _event()
    diagnosis = _diagnosis(event)
    candidates = [Action.SEND_REMINDER_EMAIL]

    def fake_call_llm(prompt, max_tokens=300, temperature=0.2, timeout=10.0):
        return LLMResult(content="not json at all", error=None, provider="groq")

    monkeypatch.setattr(agentic_policy, "call_llm", fake_call_llm)
    decision = select_action(event, diagnosis, candidates)

    assert decision.source == "deterministic_fallback"
    assert decision.action == candidates[0]
    assert "could_not_parse_expected_json_shape" in decision.llm_error


# --- real network paths (mirrors test_voice_recovery.py's discipline) ---


def test_select_action_never_raises_without_api_key():
    saved_groq = os.environ.pop("GROQ_API_KEY", None)
    saved_gemini = os.environ.pop("GEMINI_API_KEY", None)
    try:
        event = _event()
        diagnosis = _diagnosis(event)
        candidates = [Action.SEND_REMINDER_WHATSAPP, Action.SEND_REMINDER_SMS]
        decision = select_action(event, diagnosis, candidates)
        assert isinstance(decision, AgenticDecision)
        assert decision.source == "deterministic_fallback"
        assert decision.action == candidates[0]
        assert decision.llm_error == "no_provider_configured"
    finally:
        if saved_groq is not None:
            os.environ["GROQ_API_KEY"] = saved_groq
        if saved_gemini is not None:
            os.environ["GEMINI_API_KEY"] = saved_gemini


def test_select_action_raises_on_empty_candidate_list():
    event = _event()
    diagnosis = _diagnosis(event)
    try:
        select_action(event, diagnosis, [])
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- engine-level: the bound holds end to end, through the real pipeline ---


def test_engine_agentic_mode_only_ever_executes_a_compliant_action(monkeypatch):
    """Doesn't depend on a real API key -- forces the fallback path (no key)
    and checks the engine-level contract: whatever action comes out of
    policy_mode="agentic" always passes compliance, and agentic_decision is
    populated exactly when an action was actually selected."""
    saved_groq = os.environ.pop("GROQ_API_KEY", None)
    saved_gemini = os.environ.pop("GEMINI_API_KEY", None)
    try:
        engine = RecoveryEngine(policy_mode="agentic", audit_path=None, log_path=None, seed=1)
        events = [
            _event(EventSource.SUBSCRIPTION_FAILED, DeclineReason.INSUFFICIENT_FUNDS, 1200.0),
            _event(EventSource.CHECKOUT_ABANDONED, DeclineReason.CHECKOUT_ABANDONED, 800.0),
            _event(EventSource.B2B_RECEIVABLE_OVERDUE, DeclineReason.INVOICE_OVERDUE, 150000.0),
        ]
        for event in events:
            record = engine.process_event(event, now=NOW)
            assert record.compliance.allowed or record.chosen_action == Action.NO_ACTION_DO_NOT_PURSUE
            if record.chosen_action != Action.NO_ACTION_DO_NOT_PURSUE and record.priority.pursue:
                assert record.agentic_decision is not None
                assert record.agentic_decision.action == record.chosen_action
                assert record.agentic_decision.source == "deterministic_fallback"  # no key configured
    finally:
        if saved_groq is not None:
            os.environ["GROQ_API_KEY"] = saved_groq
        if saved_gemini is not None:
            os.environ["GEMINI_API_KEY"] = saved_gemini


def test_engine_accepts_agentic_as_a_valid_policy_mode():
    engine = RecoveryEngine(policy_mode="agentic", audit_path=None, log_path=None, seed=1)
    assert engine.policy_mode == "agentic"
