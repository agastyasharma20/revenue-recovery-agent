"""
Voice-based recovery, Hinglish supported -- named explicitly in Razorpay's
Track 03 brief.

HONESTY NOTE, read before treating this as more than it is: this module
generates a CALL SCRIPT (opening line, main ask, objection handling,
closing line) in Hinglish for a human collections/support agent to read
from, or for a future voice-bot's dialogue policy to be seeded with. It
does NOT place phone calls, does NOT do text-to-speech or speech-to-text,
and does NOT talk to a real customer. Wiring this to an actual voice
channel (e.g. Exotel/Twilio for telephony + a TTS/STT loop) is real,
separately-scoped work that a one-week build should not pretend to have
done. What's genuinely real here: the script content itself is generated
by a live LLM call (or a tested template fallback), tailored to the
specific decline reason, amount, and customer segment -- not a canned string.

Compliance note (not enforced by this module, but worth stating for anyone
extending it toward real telephony): outbound collections/recovery calls in
India are subject to TRAI calling-time restrictions and DND-registry rules
independent of the NPCI retry rules in core/compliance.py. A real voice
channel would need its own compliance gate before core/policy.py's action
selection reaches an actual dialer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.schema import RevenueEvent, DiagnosisCategory, EventSource
from core.classifier import Diagnosis
from core.llm_client import call_llm, extract_json_object


@dataclass
class VoiceScript:
    event_id: str
    language: str
    opening_line: str
    main_ask: str
    objection_handling: dict
    closing_line: str
    generated_by: str  # "llm" | "template_fallback"
    llm_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "language": self.language,
            "opening_line": self.opening_line,
            "main_ask": self.main_ask,
            "objection_handling": self.objection_handling,
            "closing_line": self.closing_line,
            "generated_by": self.generated_by,
            "llm_error": self.llm_error,
        }

    def render(self) -> str:
        lines = [f"[Opening] {self.opening_line}", f"[Main ask] {self.main_ask}"]
        for objection, response in self.objection_handling.items():
            lines.append(f"[If customer says: \"{objection}\"] -> {response}")
        lines.append(f"[Closing] {self.closing_line}")
        return "\n".join(lines)


# Template fallback -- always available, no LLM required. Keyed by
# diagnosis category so it still says something reason-appropriate even
# offline.
_TEMPLATES = {
    DiagnosisCategory.SOFT_DECLINE_RETRIABLE: VoiceScript(
        event_id="", language="hinglish",
        opening_line="Namaste! Main [Company] se baat kar raha/rahi hoon, aapka time ho toh 2 minute baat kar sakte hain?",
        main_ask="Aapka payment of Rs. {amount} fail ho gaya tha insufficient balance ki wajah se. Kya aap abhi ek baar phir se try kar sakte hain, ya kal tak fund arrange ho jayega?",
        objection_handling={
            "Abhi paisa nahi hai": "Koi baat nahi, main aapko ek payment link SMS aur WhatsApp par bhej deta/deti hoon, jab convenient ho tab kar dijiyega, 3 din tak valid rahega.",
            "Mujhe yaad nahi ye kis cheez ka hai": "Ye aapki subscription ka renewal payment tha, agar continue nahi karna hai toh bata dijiye main note kar loon/loongi.",
        },
        closing_line="Dhanyavaad aapke time ke liye, agar koi issue ho toh humein call kar sakte hain.",
        generated_by="template_fallback",
    ),
    DiagnosisCategory.HARD_DECLINE_UNRETRIABLE: VoiceScript(
        event_id="", language="hinglish",
        opening_line="Namaste! Main [Company] se baat kar raha/rahi hoon, aapke payment method mein thoda issue aaya hai.",
        main_ask="Aapka card expire ho chuka hai ya block ho gaya hai, isliye payment of Rs. {amount} nahi ho paya. Kya aap naya card ya UPI update kar sakte hain?",
        objection_handling={
            "Naya card nahi hai abhi": "Koi baat nahi, aap UPI ya net banking se bhi pay kar sakte hain, main link bhej deta/deti hoon.",
        },
        closing_line="Update karne ke baad payment automatically retry ho jayega, dhanyavaad!",
        generated_by="template_fallback",
    ),
    DiagnosisCategory.CUSTOMER_INACTION: VoiceScript(
        event_id="", language="hinglish",
        opening_line="Namaste! Aapne cart mein kuch items add kiye the / aapka invoice pending hai, follow-up karne ke liye call kar raha/rahi hoon.",
        main_ask="Kya koi issue aaya tha checkout/payment karte waqt? Hum aapki madad kar sakte hain.",
        objection_handling={
            "Baad mein karunga": "Koi baat nahi, main aapko reminder bhej doonga/doongi, aur agar 24 ghante ke andar complete karte hain toh ek chota discount bhi mil sakta hai.",
            "Interested nahi hoon ab": "Samajh gaya/gayi, dhanyavaad aapke time ke liye, hum aapko dobara disturb nahi karenge.",
        },
        closing_line="Dhanyavaad, aapka din shubh ho!",
        generated_by="template_fallback",
    ),
}

_DEFAULT_TEMPLATE = VoiceScript(
    event_id="", language="hinglish",
    opening_line="Namaste! Main [Company] se baat kar raha/rahi hoon.",
    main_ask="Aapke account mein Rs. {amount} ka ek pending payment hai, kya hum iske baare mein baat kar sakte hain?",
    objection_handling={"Abhi busy hoon": "Koi baat nahi, main baad mein dobara call karunga/karungi, ya aapko details SMS kar doon?"},
    closing_line="Dhanyavaad aapke time ke liye.",
    generated_by="template_fallback",
)


def _template_script(event: RevenueEvent, diagnosis: Diagnosis) -> VoiceScript:
    base = _TEMPLATES.get(diagnosis.category, _DEFAULT_TEMPLATE)
    return VoiceScript(
        event_id=event.event_id,
        language="hinglish",
        opening_line=base.opening_line,
        main_ask=base.main_ask.format(amount=f"{event.amount:,.0f}"),
        objection_handling=dict(base.objection_handling),
        closing_line=base.closing_line,
        generated_by="template_fallback",
    )


def _llm_script(event: RevenueEvent, diagnosis: Diagnosis) -> tuple[Optional[VoiceScript], Optional[str]]:
    context = "a B2B overdue invoice" if event.source == EventSource.B2B_RECEIVABLE_OVERDUE else (
        "an abandoned checkout" if event.source == EventSource.CHECKOUT_ABANDONED else "a failed subscription payment"
    )
    prompt = (
        "Write a short outbound recovery call script in HINGLISH (natural mixed Hindi-English, "
        "written in Latin/Roman script, the way Indian customer support agents actually speak -- "
        "not pure Hindi, not pure English) for a payments company agent calling about "
        f"{context}. Decline/context reason: {event.decline_reason.value}. "
        f"Amount: Rs. {event.amount:,.0f}. Customer segment: {event.customer_segment.value}. "
        "Tone: polite, brief, non-pushy, respectful of the customer's time. "
        "Reply with ONLY a single valid JSON object, nothing before or after it, with EXACTLY "
        "these 4 keys and no others: "
        '{"opening_line": "...", "main_ask": "...", '
        '"objection_handling": {"<likely objection in Hinglish>": "<agent response in Hinglish>", '
        '"<a second likely objection>": "<response>"}, "closing_line": "..."}. '
        "objection_handling must contain EXACTLY 2 key-value pairs, no more, no fewer. "
        "All string values must be in Hinglish. Keep each value to one or two sentences. "
        "Double-check the JSON is syntactically valid before answering."
    )

    # A 4-field Hinglish script is a meaningfully longer generation task
    # than the classifier's one-line confidence+rationale refinement --
    # 600 tokens still got truncated mid-JSON often enough during testing
    # to matter (reasoning_effort="low" reduces but doesn't eliminate
    # reasoning-token overhead). 1000 leaves real headroom.
    result = call_llm(prompt, max_tokens=1000, temperature=0.6)
    if not result.ok:
        return None, result.error

    parsed = extract_json_object(result.content)
    required_keys = ("opening_line", "main_ask", "objection_handling", "closing_line")
    if parsed is None or not all(k in parsed for k in required_keys):
        return None, f"could_not_parse_expected_json_shape: {result.content[:200]!r}"

    script = VoiceScript(
        event_id=event.event_id,
        language="hinglish",
        opening_line=str(parsed["opening_line"]),
        main_ask=str(parsed["main_ask"]),
        objection_handling={str(k): str(v) for k, v in dict(parsed["objection_handling"]).items()},
        closing_line=str(parsed["closing_line"]),
        generated_by="llm",
    )
    return script, None


def generate_voice_script(event: RevenueEvent, diagnosis: Diagnosis) -> VoiceScript:
    """Tries the LLM first (richer, tailored, varies naturally); falls back
    to a tested static template on any failure -- missing key, network
    error, malformed response. Always returns a usable script."""
    llm_result, error = _llm_script(event, diagnosis)
    if llm_result is not None:
        return llm_result

    fallback = _template_script(event, diagnosis)
    fallback.llm_error = error
    return fallback
