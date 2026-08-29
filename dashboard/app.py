"""
Streamlit dashboard for the AI Revenue Recovery Agent (Phase 6).

Run with:
    streamlit run dashboard/app.py

Everything here runs on synthetic data generated in-process (see the
"What's simulated vs real" note in the README) -- this is a demo surface
over the same core/ pipeline used by run_evaluation.py, not a separate
implementation.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import streamlit as st
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.engine import RecoveryEngine
from core.metrics import summarize
from core.anomaly import detect_systemic_incidents
from core.audit import verify_chain, AuditLog
from core.portfolio import build_cases, compare as portfolio_compare
from core.contextual_bandit import LinUCBBandit, build_context
from core.policy import BANDIT_ARMS
from core.outcome_simulator import simulate_outcome
from data.generate_synthetic import generate_batch

st.set_page_config(page_title="Revenue Recovery Agent", layout="wide")

AUDIT_PATH = "results/dashboard_audit_log.jsonl"


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------
st.sidebar.title("Controls")
n_events = st.sidebar.slider("Events in batch", 100, 2000, 500, step=100)
seed = st.sidebar.number_input("Random seed", min_value=1, max_value=9999, value=1)
inject_spike = st.sidebar.checkbox("Inject synthetic bank-outage spike (demo)", value=True)
capacity_hours = st.sidebar.slider("Daily human-review capacity (hours)", 1, 60, 20)
animate = st.sidebar.checkbox("Animate 'live' recovery counter", value=True)

st.sidebar.markdown("---")
st.sidebar.caption(
    "All numbers on this page come from the synthetic generator "
    "(data/generate_synthetic.py) run through the real core/ pipeline -- "
    "same code as run_evaluation.py. See README for what's simulated vs real."
)


# ---------------------------------------------------------------------------
# Data + pipeline (cached per seed/n so the sidebar feels responsive)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Generating synthetic batch and running the agent...")
def run_pipeline(n, seed, inject_spike):
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    events, ground_truth = generate_batch(n, seed=seed, now=now)

    if inject_spike:
        from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment

        spike = [
            RevenueEvent(
                source=EventSource.SUBSCRIPTION_FAILED,
                decline_reason=DeclineReason.BANK_SERVER_TIMEOUT,
                amount=1200.0 + i,
                customer_segment=CustomerSegment.MEDIUM_LTV,
                created_at=now - timedelta(minutes=90 - i * 4),
                last_attempt_at=now - timedelta(minutes=90 - i * 4),
            )
            for i in range(20)
        ]
        events = events + spike

    if os.path.exists(AUDIT_PATH):
        os.remove(AUDIT_PATH)
    engine = RecoveryEngine(
        use_llm=bool(os.environ.get("GROQ_API_KEY")),
        policy_mode="deterministic",
        audit_path=AUDIT_PATH,
        seed=seed * 1000 + 2,
        log_path=None,
    )
    records = engine.process_batch(events, now=now)
    incidents = detect_systemic_incidents(events, window_hours=2.0, threshold=15)
    return events, records, incidents


@st.cache_data(show_spinner="Running bandit convergence (one-time per session)...")
def run_bandit_convergence(seed, rounds=8000, window=400):
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    events, _ = generate_batch(rounds, seed=seed, now=now)
    bandit = LinUCBBandit(arms=BANDIT_ARMS, alpha=0.6)
    import random as _random

    rng = _random.Random(seed * 999 + 1)
    rates, buf = [], []
    for e in events:
        ctx = build_context(e, e.created_at)
        action = bandit.select_action(ctx)
        outcome = simulate_outcome(e.decline_reason, action, e.amount, rng=rng)
        reward = 1.0 if outcome.recovered else 0.0
        bandit.update(action, ctx, reward)
        buf.append(reward)
        if len(buf) == window:
            rates.append(sum(buf) / len(buf))
            buf = []
    return rates


events, records, incidents = run_pipeline(n_events, seed, inject_spike)
metrics = summarize(records)

st.title("💸 AI Revenue Recovery Agent")

# ---------------------------------------------------------------------------
# Systemic incident banner
# ---------------------------------------------------------------------------
if incidents:
    for inc in incidents:
        st.error(f"🚨 SYSTEMIC INCIDENT: {inc.message}")
else:
    st.success("No systemic incidents detected in the current batch.")

st.markdown("---")

# ---------------------------------------------------------------------------
# Headline counter
# ---------------------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
counter_placeholder = col1.empty()
col2.metric("Events processed", metrics.total_events)
col3.metric("Events pursued", metrics.events_pursued)
col4.metric("Recovery rate (of pursued)", f"{metrics.recovery_rate_of_pursued*100:.1f}%")

if animate:
    running_total = 0.0
    recovered_amounts = [r.recovered_amount for r in records if r.outcome and r.outcome.recovered]
    step = max(1, len(recovered_amounts) // 40)
    for i in range(0, len(recovered_amounts), step):
        running_total = sum(recovered_amounts[: i + step])
        counter_placeholder.metric("Total INR recovered", f"₹{running_total:,.0f}")
        time.sleep(0.02)
    counter_placeholder.metric("Total INR recovered", f"₹{metrics.total_recovered_inr:,.0f}")
else:
    counter_placeholder.metric("Total INR recovered", f"₹{metrics.total_recovered_inr:,.0f}")

st.markdown("---")

# ---------------------------------------------------------------------------
# Per-case drill-down with full hash-chained audit trail
# ---------------------------------------------------------------------------
st.header("🔍 Per-case audit drill-down")

verify_result = verify_chain(AUDIT_PATH)
if verify_result.ok:
    st.caption(f"✅ Audit chain verified intact: {verify_result.detail}")
else:
    st.warning(f"⚠️ Audit chain verification FAILED: {verify_result.detail}")

pursued_records = [r for r in records if r.pursued]
if pursued_records:
    options = {
        f"{r.event.event_id[:8]}... | {r.event.source.value} | {r.event.decline_reason.value} | ₹{r.event.amount:,.0f}": r.event.event_id
        for r in pursued_records[:200]
    }
    choice = st.selectbox("Select a pursued case to inspect", list(options.keys()))
    selected_id = options[choice]

    audit_records = AuditLog(AUDIT_PATH).read_all()
    match = next((r for r in audit_records if r["payload"]["event_id"] == selected_id), None)

    if match:
        p = match["payload"]
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Event")
            st.json(
                {
                    "event_id": p["event_id"],
                    "trace_id": p["trace_id"],
                    "source": p["source"],
                    "decline_reason": p["decline_reason"],
                    "amount": p["amount"],
                    "retry_count": p["retry_count"],
                    "customer_segment": p["customer_segment"],
                }
            )
            st.subheader("Diagnosis")
            st.json(p["diagnosis"])
        with c2:
            st.subheader("Prioritization (EV)")
            st.json(p["priority"])
            st.subheader("Compliance")
            st.json(p["compliance"])
            st.subheader("Action + outcome")
            st.json({"chosen_action": p["chosen_action"], "outcome": p["outcome"]})

        st.subheader("Tamper-evidence: this record's position in the hash chain")
        st.code(
            f"prev_hash: {match['prev_hash']}\n"
            f"this_hash: {match['this_hash']}\n"
            f"logged_at: {match['logged_at']}",
            language="text",
        )
else:
    st.info("No pursued cases in this batch to drill into.")

st.markdown("---")

# ---------------------------------------------------------------------------
# Bandit convergence chart
# ---------------------------------------------------------------------------
st.header("🎰 Contextual bandit (LinUCB) convergence")
rates = run_bandit_convergence(seed)
fig, ax = plt.subplots(figsize=(9, 3.5))
ax.plot(range(1, len(rates) + 1), [r * 100 for r in rates], marker="o", markersize=3)
ax.set_xlabel("Window (400 rounds each)")
ax.set_ylabel("Recovery rate (%)")
ax.set_title("Recovery rate climbing as the bandit learns, carried forward across rounds")
ax.grid(alpha=0.3)
st.pyplot(fig)
if len(rates) >= 2:
    st.caption(f"First window: {rates[0]*100:.1f}%  →  Last window: {rates[-1]*100:.1f}%")

st.markdown("---")

# ---------------------------------------------------------------------------
# Portfolio: top-N vs knapsack-optimal
# ---------------------------------------------------------------------------
st.header("🎒 Portfolio optimization: top-N by EV vs knapsack-optimal")
cases = build_cases(records)
if cases:
    result = portfolio_compare(cases, capacity_hours)
    pc1, pc2 = st.columns(2)
    with pc1:
        st.subheader("Top-N by EV (greedy)")
        st.metric("Cases selected", result.topn_selected)
        st.metric("Hours used", f"{result.topn_hours_used:.2f} / {capacity_hours}")
        st.metric("Total EV captured", f"₹{result.topn_value:,.0f}")
    with pc2:
        st.subheader("Knapsack-optimal (exact DP)")
        st.metric("Cases selected", result.knapsack_selected)
        st.metric("Hours used", f"{result.knapsack_hours_used:.2f} / {capacity_hours}")
        st.metric("Total EV captured", f"₹{result.knapsack_value:,.0f}")

    st.metric(
        "Knapsack advantage",
        f"₹{result.value_gain:,.0f}",
        delta=f"{result.value_gain_pct:+.2f}%",
    )
    fig2, ax2 = plt.subplots(figsize=(5, 3))
    ax2.bar(["Top-N by EV", "Knapsack-optimal"], [result.topn_value, result.knapsack_value])
    ax2.set_ylabel("Total EV captured (INR)")
    st.pyplot(fig2)
else:
    st.info("No human-escalation cases in this batch to optimize a portfolio over.")

st.markdown("---")
st.caption(
    "Data note: all events, outcomes, and recovered amounts on this page are generated "
    "by data/generate_synthetic.py and scored by core/outcome_simulator.py's effectiveness "
    "matrix -- a controlled simulation, not a live payments feed. See README.md's "
    "'What's simulated vs real' section for the full breakdown."
)
