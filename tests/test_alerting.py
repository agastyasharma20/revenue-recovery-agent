"""
Ops alerting (core/alerting.py): the no-webhook-configured path must be a
true no-op (no network attempted, no exception), and the actual HTTP call
must be a real POST when a webhook IS configured -- verified against a
real local HTTP server, not a mock of urllib, so this tests the actual
wire format a real Slack webhook would receive.

Then: the three call sites (engine.py's pending-approval path, state.py's
systemic-incident path, main.py's audit-verify-failure path) actually fire
send_alert with the right event_type -- verified by monkeypatching
send_alert itself, since those tests shouldn't depend on network access.
"""

import http.server
import json
import os
import threading
from datetime import datetime, timezone

from core import alerting
import core.engine as engine_module
import backend.state as state_module
import backend.main as main_module
from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment
from core.engine import RecoveryEngine

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def test_send_alert_is_a_true_noop_without_a_webhook_url():
    saved = os.environ.pop(alerting.SLACK_WEBHOOK_URL_ENV, None)
    try:
        result = alerting.send_alert("test_event", "should never attempt a network call")
        assert result is False
    finally:
        if saved is not None:
            os.environ[alerting.SLACK_WEBHOOK_URL_ENV] = saved


def test_send_alert_makes_a_real_post_to_a_real_local_server():
    """Runs an actual tiny HTTP server and points send_alert at it -- proves
    the wire format (a real HTTP POST with a JSON {"text": ...} body, the
    shape a real Slack incoming webhook expects) is genuinely correct, not
    just plausible-looking."""
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received["body"] = json.loads(self.rfile.read(length))
            received["content_type"] = self.headers["Content-Type"]
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass  # quiet -- don't spam test output

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    saved = os.environ.get(alerting.SLACK_WEBHOOK_URL_ENV)
    os.environ[alerting.SLACK_WEBHOOK_URL_ENV] = f"http://127.0.0.1:{port}/webhook"
    try:
        result = alerting.send_alert("systemic_incident", "15 failures in 2h", context={"decline_reason": "bank_server_timeout"})
        thread.join(timeout=5)
        assert result is True
        assert received["content_type"] == "application/json"
        assert "[systemic_incident]" in received["body"]["text"]
        assert "15 failures in 2h" in received["body"]["text"]
        assert "decline_reason: bank_server_timeout" in received["body"]["text"]
    finally:
        if saved is not None:
            os.environ[alerting.SLACK_WEBHOOK_URL_ENV] = saved
        else:
            os.environ.pop(alerting.SLACK_WEBHOOK_URL_ENV, None)


def test_send_alert_fails_closed_on_unreachable_webhook():
    saved = os.environ.get(alerting.SLACK_WEBHOOK_URL_ENV)
    os.environ[alerting.SLACK_WEBHOOK_URL_ENV] = "http://127.0.0.1:1/definitely-not-listening"
    try:
        result = alerting.send_alert("test_event", "should fail closed, not raise", timeout=2.0)
        assert result is False
    finally:
        if saved is not None:
            os.environ[alerting.SLACK_WEBHOOK_URL_ENV] = saved
        else:
            os.environ.pop(alerting.SLACK_WEBHOOK_URL_ENV, None)


# --- the three real call sites actually fire, with the right event_type ---


def test_engine_fires_pending_approval_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_module.alerting, "send_alert", lambda *a, **k: calls.append((a, k)) or True)

    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1, auto_approve=False)
    event = RevenueEvent(
        source=EventSource.B2B_RECEIVABLE_OVERDUE, decline_reason=DeclineReason.INVOICE_OVERDUE,
        amount=250000.0, customer_segment=CustomerSegment.HIGH_LTV, created_at=NOW, last_attempt_at=NOW,
    )
    record = engine.process_event(event, now=NOW)

    assert record.approval_status == "pending"
    assert len(calls) == 1
    assert calls[0][0][0] == "pending_approval"


def test_engine_does_not_alert_when_action_does_not_require_approval(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_module.alerting, "send_alert", lambda *a, **k: calls.append((a, k)) or True)

    engine = RecoveryEngine(use_llm=False, policy_mode="deterministic", audit_path=None, seed=1, auto_approve=False)
    small_event = RevenueEvent(
        source=EventSource.SUBSCRIPTION_FAILED, decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        amount=500.0, customer_segment=CustomerSegment.MEDIUM_LTV, created_at=NOW, last_attempt_at=NOW,
    )
    engine.process_event(small_event, now=NOW)
    assert calls == []


def test_run_store_fires_systemic_incident_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(state_module.alerting, "send_alert", lambda *a, **k: calls.append((a, k)) or True)

    store = state_module.RunStore(audit_dir="results/test_alerting_runs")
    # A bank-outage spike (20 near-simultaneous timeouts) is exactly what
    # detect_systemic_incidents(window_hours=2.0, threshold=15) is built to
    # catch -- inject_spike=True guarantees at least one incident fires.
    store.create_run(n=50, seed=1, policy_mode="deterministic", inject_spike=True, use_llm=False, auto_approve=True)

    assert len(calls) >= 1
    assert calls[0][0][0] == "systemic_incident"


def test_audit_verify_endpoint_alerts_on_a_broken_chain(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(main_module.alerting, "send_alert", lambda *a, **k: calls.append((a, k)) or True)

    # Build a tiny real run, then corrupt its on-disk audit log directly --
    # this exercises the real verify_chain() failure path, not a mock.
    store = state_module.RunStore(audit_dir=str(tmp_path))
    run = store.create_run(n=5, seed=1, policy_mode="deterministic", inject_spike=False, use_llm=False, auto_approve=True)
    with open(run.audit_path, "a", encoding="utf-8") as f:
        f.write('{"payload": {"event_id": "tampered"}, "prev_hash": "x", "this_hash": "y", "logged_at": "2026-01-01T00:00:00+00:00"}\n')

    saved_store = main_module.run_store
    main_module.run_store = store
    try:
        result = main_module.verify_audit(run.run_id)
    finally:
        main_module.run_store = saved_store

    assert result["ok"] is False
    assert len(calls) == 1
    assert calls[0][0][0] == "audit_chain_broken"
