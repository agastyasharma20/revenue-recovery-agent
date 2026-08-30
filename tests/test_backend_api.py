"""
FastAPI backend smoke tests via TestClient (no real server/port needed).
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


@pytest.fixture(scope="module")
def run_id():
    resp = client.post("/api/runs", json={"n": 300, "seed": 2, "inject_spike": True})
    assert resp.status_code == 200
    return resp.json()["run_id"]


def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_create_run_returns_summary_and_incidents():
    resp = client.post("/api/runs", json={"n": 300, "seed": 2, "inject_spike": True})
    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body
    assert body["summary"]["total_events"] == 320  # 300 + 20-event injected spike
    assert len(body["incidents"]) >= 1


def test_create_run_rejects_bad_params():
    resp = client.post("/api/runs", json={"n": 1})
    assert resp.status_code == 400
    resp = client.post("/api/runs", json={"n": 100, "policy_mode": "not_a_real_mode"})
    assert resp.status_code == 400


def test_unknown_run_id_is_404(run_id):
    resp = client.get("/api/runs/does_not_exist/summary")
    assert resp.status_code == 404


def test_audit_verify_reports_intact_chain(run_id):
    resp = client.get(f"/api/runs/{run_id}/audit/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["total_records"] > 0


def test_cases_and_case_detail_roundtrip(run_id):
    cases = client.get(f"/api/runs/{run_id}/cases?pursued_only=true&limit=5").json()
    assert len(cases) > 0
    event_id = cases[0]["event_id"]

    detail = client.get(f"/api/runs/{run_id}/cases/{event_id}").json()
    assert detail["case"]["event_id"] == event_id
    assert detail["payload"]["event_id"] == event_id
    assert detail["audit_chain"] is not None
    assert "this_hash" in detail["audit_chain"]


def test_case_detail_404_for_bad_event_id(run_id):
    resp = client.get(f"/api/runs/{run_id}/cases/not-a-real-event-id")
    assert resp.status_code == 404


def test_portfolio_endpoint(run_id):
    resp = client.get(f"/api/runs/{run_id}/portfolio?capacity_hours=15")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cases_available"] >= 0
    if body["cases_available"] > 0:
        assert body["knapsack"]["value"] >= body["topn"]["value"] - 1e-6


def test_promises_endpoint(run_id):
    resp = client.get(f"/api/runs/{run_id}/promises")
    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body
    assert "total_promises" in body["summary"]


def test_voice_script_endpoint_never_crashes(run_id):
    cases = client.get(f"/api/runs/{run_id}/cases?pursued_only=true&limit=1").json()
    event_id = cases[0]["event_id"]
    resp = client.post(f"/api/runs/{run_id}/cases/{event_id}/voice-script")
    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_by"] in ("llm", "template_fallback")
    assert body["opening_line"]


def test_bandit_convergence_endpoint_small():
    resp = client.get("/api/bandit-convergence?rounds=500&window=250&seed=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["window_rates"]) == 2
    assert body["total_segments"] == 12


def test_websocket_live_replay_completes(run_id):
    with client.websocket_connect(f"/ws/runs/{run_id}/live?speed_ms=0") as ws:
        saw_done = False
        for _ in range(1000):  # generous cap, loop breaks on "done"
            msg = ws.receive_json()
            if msg["type"] == "done":
                saw_done = True
                assert msg["running_total"] >= 0
                break
        assert saw_done
