"""
FastAPI backend for the AI Revenue Recovery Agent frontend.

Run with:
    uvicorn backend.main:app --reload --port 8000

This is a thin HTTP/WebSocket layer over core/ -- every endpoint just calls
into the same engine, portfolio, anomaly, and bandit code the CLI demo
scripts (run_evaluation.py, portfolio_demo.py, bandit_convergence_demo.py)
use. No decision logic lives here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.state import run_store, RunState
from core.audit import AuditLog, verify_chain
from core.metrics import summarize
from core.portfolio import build_cases, compare as portfolio_compare
from core.voice_recovery import generate_voice_script
from core.contextual_bandit import LinUCBBandit, build_context
from core.policy import BANDIT_ARMS
from core.outcome_simulator import simulate_outcome, EFFECTIVENESS_MATRIX
from core.schema import DeclineReason, Action, VALID_REASONS_FOR_SOURCE
from data.generate_synthetic import generate_batch

app = FastAPI(title="AI Revenue Recovery Agent API", version="1.0.0")

# Local-dev CORS: the Vite frontend runs on a different port. Wide open
# because this is a demo/judging build, not a production deployment --
# stated plainly rather than silently loosened.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- request/response models -----------------------------------------------


class CreateRunRequest(BaseModel):
    n: int = 500
    seed: int = 1
    policy_mode: str = "deterministic"
    inject_spike: bool = True
    use_llm: bool = False
    auto_approve: bool = True


def _get_run_or_404(run_id: str) -> RunState:
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found (server restarts clear runs)")
    return run


def _case_summary(r) -> dict:
    return {
        "event_id": r.event.event_id,
        "source": r.event.source.value,
        "decline_reason": r.event.decline_reason.value,
        "amount": r.event.amount,
        "customer_segment": r.event.customer_segment.value,
        "customer_id": r.event.customer_id,
        "chosen_action": r.chosen_action.value,
        "pursued": r.pursued,
        # None (not False) when nothing has executed yet -- a pending or
        # rejected approval has no outcome at all, which is a different
        # thing from "executed and failed to recover."
        "recovered": (r.outcome.recovered if r.outcome else None),
        "recovered_amount": r.recovered_amount,
        "ev": round(r.priority.ev, 2),
        "diagnosis_category": r.diagnosis.category.value,
        "requires_approval": r.requires_approval,
        "approval_status": r.approval_status,
        "approval_reason": r.approval_reason,
    }


# --- health ------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# --- runs ------------------------------------------------------------


@app.post("/api/runs")
def create_run(req: CreateRunRequest):
    if req.n < 10 or req.n > 5000:
        raise HTTPException(400, "n must be between 10 and 5000")
    if req.policy_mode not in ("deterministic", "bandit"):
        raise HTTPException(400, "policy_mode must be 'deterministic' or 'bandit'")

    run = run_store.create_run(
        n=req.n, seed=req.seed, policy_mode=req.policy_mode,
        inject_spike=req.inject_spike, use_llm=req.use_llm, auto_approve=req.auto_approve,
    )
    metrics = summarize(run.records)
    return {
        "run_id": run.run_id,
        "summary": metrics.to_dict(),
        "incidents": [
            {
                "decline_reason": i.decline_reason.value,
                "amount_bucket": i.amount_bucket,
                "count": i.count,
                "message": i.message,
            }
            for i in run.incidents
        ],
    }


@app.get("/api/runs/{run_id}/summary")
def get_summary(run_id: str):
    run = _get_run_or_404(run_id)
    return summarize(run.records).to_dict()


@app.get("/api/runs/{run_id}/incidents")
def get_incidents(run_id: str):
    run = _get_run_or_404(run_id)
    return [
        {
            "decline_reason": i.decline_reason.value,
            "amount_bucket": i.amount_bucket,
            "count": i.count,
            "window_start": i.window_start.isoformat(),
            "window_end": i.window_end.isoformat(),
            "message": i.message,
        }
        for i in run.incidents
    ]


@app.get("/api/runs/{run_id}/cases")
def get_cases(run_id: str, pursued_only: bool = False, limit: int = 300):
    run = _get_run_or_404(run_id)
    records = run.records
    if pursued_only:
        records = [r for r in records if r.pursued]
    return [_case_summary(r) for r in records[:limit]]


@app.get("/api/runs/{run_id}/cases/{event_id}")
def get_case_detail(run_id: str, event_id: str):
    run = _get_run_or_404(run_id)
    record = next((r for r in run.records if r.event.event_id == event_id), None)
    if record is None:
        raise HTTPException(404, "event_id not found in this run")

    audit_records = AuditLog(run.audit_path).read_all()
    audit_entry = next((a for a in audit_records if a["payload"]["event_id"] == event_id), None)

    return {
        "case": _case_summary(record),
        "payload": record.to_audit_payload(),
        "audit_chain": (
            {
                "prev_hash": audit_entry["prev_hash"],
                "this_hash": audit_entry["this_hash"],
                "logged_at": audit_entry["logged_at"],
            }
            if audit_entry
            else None
        ),
    }


@app.get("/api/runs/{run_id}/audit/verify")
def verify_audit(run_id: str):
    run = _get_run_or_404(run_id)
    result = verify_chain(run.audit_path)
    return {
        "ok": result.ok,
        "total_records": result.total_records,
        "first_bad_index": result.first_bad_index,
        "detail": result.detail,
    }


@app.get("/api/runs/{run_id}/portfolio")
def get_portfolio(run_id: str, capacity_hours: float = 20.0):
    run = _get_run_or_404(run_id)
    cases = build_cases(run.records)
    if not cases:
        return {"cases_available": 0}
    result = portfolio_compare(cases, capacity_hours)
    return {
        "cases_available": len(cases),
        "capacity_hours": result.capacity_hours,
        "topn": {
            "selected": result.topn_selected,
            "value": round(result.topn_value, 2),
            "hours_used": round(result.topn_hours_used, 2),
        },
        "knapsack": {
            "selected": result.knapsack_selected,
            "value": round(result.knapsack_value, 2),
            "hours_used": round(result.knapsack_hours_used, 2),
        },
        "value_gain": round(result.value_gain, 2),
        "value_gain_pct": round(result.value_gain_pct, 2),
    }


@app.get("/api/runs/{run_id}/promises")
def get_promises(run_id: str):
    run = _get_run_or_404(run_id)
    summary = run.engine.promise_tracker.summary()

    by_customer: dict[str, list] = {}
    for r in run.records:
        if r.promise:
            by_customer.setdefault(r.event.customer_id, []).append(
                {
                    "event_id": r.event.event_id,
                    "amount": r.event.amount,
                    "created_at": r.event.created_at.isoformat(),
                    "status": r.promise.status.value,
                    "promised_date": r.promise.promised_date.isoformat(),
                    "ev": round(r.priority.ev, 2),
                    "reliability_adjustment_applied": "customer reliability" in r.priority.reason,
                }
            )
    repeat_customers = {cid: hist for cid, hist in by_customer.items() if len(hist) >= 2}

    return {
        "summary": summary,
        "repeat_customers": repeat_customers,
    }


@app.post("/api/runs/{run_id}/cases/{event_id}/voice-script")
def get_voice_script(run_id: str, event_id: str):
    run = _get_run_or_404(run_id)
    record = next((r for r in run.records if r.event.event_id == event_id), None)
    if record is None:
        raise HTTPException(404, "event_id not found in this run")

    script = generate_voice_script(record.event, record.diagnosis)
    return script.to_dict()


# --- human-in-the-loop approvals ---


@app.get("/api/runs/{run_id}/pending-approvals")
def get_pending_approvals(run_id: str):
    run = _get_run_or_404(run_id)
    return [
        {**_case_summary(r), "approval_reason": r.approval_reason}
        for r in run.engine.pending_approvals.values()
    ]


@app.post("/api/runs/{run_id}/cases/{event_id}/approve")
def approve_case(run_id: str, event_id: str):
    run = _get_run_or_404(run_id)
    try:
        record = run.engine.approve(event_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return _case_summary(record)


@app.post("/api/runs/{run_id}/cases/{event_id}/reject")
def reject_case(run_id: str, event_id: str, reason: str = "rejected by reviewer"):
    run = _get_run_or_404(run_id)
    try:
        record = run.engine.reject(event_id, reason=reason)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return _case_summary(record)


# --- bandit convergence (cached across requests -- expensive to recompute) ---

_bandit_cache: dict[tuple, dict] = {}


@app.get("/api/bandit-convergence")
def get_bandit_convergence(seed: int = 7, rounds: int = 8000, window: int = 400, alpha: float = 0.6):
    key = (seed, rounds, window, alpha)
    if key in _bandit_cache:
        return _bandit_cache[key]

    import random as _random

    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    events, _ = generate_batch(rounds, seed=seed, now=now)
    bandit = LinUCBBandit(arms=BANDIT_ARMS, alpha=alpha)
    rng = _random.Random(seed * 999 + 1)

    window_rates, buf = [], []
    for e in events:
        ctx = build_context(e, e.created_at)
        action = bandit.select_action(ctx)
        outcome = simulate_outcome(e.decline_reason, action, e.amount, rng=rng)
        reward = 1.0 if outcome.recovered else 0.0
        bandit.update(action, ctx, reward)
        buf.append(reward)
        if len(buf) == window:
            window_rates.append(sum(buf) / len(buf))
            buf = []

    learned = []
    for reason in DeclineReason:
        if reason == DeclineReason.UNKNOWN:
            continue
        source = next(s for s, reasons in VALID_REASONS_FOR_SOURCE.items() if reason in reasons)
        from core.schema import RevenueEvent, CustomerSegment

        rep_event = RevenueEvent(
            source=source, decline_reason=reason, amount=3000.0,
            customer_segment=CustomerSegment.MEDIUM_LTV, created_at=now, last_attempt_at=now,
        )
        ctx = build_context(rep_event, now)
        means = {a: bandit.predicted_mean(a, ctx) for a in BANDIT_ARMS}
        learned_best = max(means, key=means.get)
        oracle_matrix = {a: p for a, p in EFFECTIVENESS_MATRIX[reason].items() if a != Action.NO_ACTION_DO_NOT_PURSUE}
        oracle_best = max(oracle_matrix, key=oracle_matrix.get)
        learned.append(
            {
                "decline_reason": reason.value,
                "bandit_learned": learned_best.value,
                "oracle_best": oracle_best.value,
                "match": learned_best == oracle_best,
            }
        )

    result = {
        "window_rates": window_rates,
        "window_size": window,
        "rounds": rounds,
        "learned_mapping": learned,
        "match_count": sum(1 for l in learned if l["match"]),
        "total_segments": len(learned),
    }
    _bandit_cache[key] = result
    return result


# --- live replay over WebSocket (for a genuinely live ticking counter) ---


@app.websocket("/ws/runs/{run_id}/live")
async def live_replay(websocket: WebSocket, run_id: str, speed_ms: int = 15):
    await websocket.accept()
    run = run_store.get(run_id)
    if run is None:
        await websocket.send_json({"type": "error", "message": f"run {run_id} not found"})
        await websocket.close()
        return

    try:
        running_total = 0.0
        running_recovered = 0
        for i, r in enumerate(run.records):
            if r.outcome and r.outcome.recovered:
                running_total += r.recovered_amount
                running_recovered += 1
            await websocket.send_json(
                {
                    "type": "tick",
                    "index": i,
                    "total": len(run.records),
                    "event_id": r.event.event_id,
                    "source": r.event.source.value,
                    "decline_reason": r.event.decline_reason.value,
                    "chosen_action": r.chosen_action.value,
                    "pursued": r.pursued,
                    "recovered": bool(r.outcome and r.outcome.recovered),
                    "recovered_amount": r.recovered_amount,
                    "running_total": running_total,
                    "running_recovered": running_recovered,
                }
            )
            await asyncio.sleep(speed_ms / 1000)
        await websocket.send_json({"type": "done", "running_total": running_total, "running_recovered": running_recovered})
    except WebSocketDisconnect:
        pass
