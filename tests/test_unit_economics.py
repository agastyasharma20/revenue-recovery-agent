"""
Unit economics math: pure functions, no network dependency.
"""

from core.unit_economics import llm_call_cost_usd, compute_unit_economics, PRICING_USD_PER_1M_TOKENS


def test_zero_tokens_costs_zero():
    assert llm_call_cost_usd("groq", 0, 0) == 0.0


def test_unknown_provider_costs_zero():
    assert llm_call_cost_usd(None, 1000, 1000) == 0.0
    assert llm_call_cost_usd("some_future_provider", 1000, 1000) == 0.0


def test_known_provider_pricing_matches_published_rates():
    # 1M input + 1M output tokens should cost exactly input_rate + output_rate
    cost = llm_call_cost_usd("groq", 1_000_000, 1_000_000)
    expected = PRICING_USD_PER_1M_TOKENS["groq"]["input"] + PRICING_USD_PER_1M_TOKENS["groq"]["output"]
    assert abs(cost - expected) < 1e-9


class _FakeOutcome:
    def __init__(self, recovered_amount):
        self.recovered_amount = recovered_amount


class _FakeDiagnosis:
    def __init__(self, attempted, provider=None, prompt_tokens=0, completion_tokens=0):
        self.llm_attempted = attempted
        self.llm_provider = provider
        self.llm_prompt_tokens = prompt_tokens
        self.llm_completion_tokens = completion_tokens


class _FakeRecord:
    def __init__(self, recovered_amount, diagnosis):
        self.recovered_amount = recovered_amount
        self.diagnosis = diagnosis


def test_compute_unit_economics_skips_non_attempted_and_sums_correctly():
    records = [
        _FakeRecord(1000.0, _FakeDiagnosis(attempted=False)),  # no LLM cost -- must be excluded
        _FakeRecord(2000.0, _FakeDiagnosis(attempted=True, provider="groq", prompt_tokens=200, completion_tokens=100)),
        _FakeRecord(0.0, _FakeDiagnosis(attempted=True, provider="groq", prompt_tokens=200, completion_tokens=100)),
    ]
    report = compute_unit_economics(records)
    assert report.total_events == 3
    assert report.llm_attempted_count == 2
    assert report.total_recovered_inr == 3000.0
    assert report.total_llm_cost_usd > 0
    assert report.roi_multiple is not None
    assert report.roi_multiple > 0
    assert "groq" in report.provider_breakdown
    assert report.provider_breakdown["groq"]["calls"] == 2


def test_zero_llm_cost_gives_none_roi_not_infinite():
    records = [_FakeRecord(1000.0, _FakeDiagnosis(attempted=False))]
    report = compute_unit_economics(records)
    assert report.total_llm_cost_usd == 0.0
    assert report.roi_multiple is None
