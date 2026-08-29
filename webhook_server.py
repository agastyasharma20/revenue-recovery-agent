"""
Phase 5: Razorpay test-mode webhook receiver.

Run locally with:
    python webhook_server.py

Then expose it to the internet for Razorpay to actually reach it (Razorpay's
dashboard can't call localhost) with a tunnel, e.g.:
    ngrok http 5000

...and register https://<your-ngrok-id>.ngrok-free.app/webhook/razorpay as
the webhook URL in Razorpay Dashboard -> Settings -> Webhooks, subscribed to
at least `payment.failed` and `subscription.charged.failed`, with a webhook
secret of your choosing.

REQUIRED environment variables (put them in a local `.env` file -- already
excluded via .gitignore, never commit real keys):
    RAZORPAY_KEY_ID          -- from Razorpay Dashboard -> Settings -> API Keys (test mode)
    RAZORPAY_KEY_SECRET      -- from the same place
    RAZORPAY_WEBHOOK_SECRET  -- the secret you set when creating the webhook

This process never calls the Razorpay API with KEY_ID/KEY_SECRET (webhook
signature verification only needs the webhook secret) -- they're read here
so the same .env covers both this server and any future calls that do need
the API client (fetching a payment's full details, issuing a refund, etc).
"""

from __future__ import annotations

import os

from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()  # reads a local .env if present; no-op if it doesn't exist

from core.schema import Action
from core.ingestion import IngestionGateway, IdempotencyStore
from core.engine import RecoveryEngine
from core.razorpay_integration import verify_webhook_signature, parse_webhook_payload

app = Flask(__name__)

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET")

gateway = IngestionGateway(store=IdempotencyStore(path="results/razorpay_idempotency.jsonl"))
engine = RecoveryEngine(
    use_llm=bool(os.environ.get("GROQ_API_KEY")),
    policy_mode="deterministic",
    audit_path="results/razorpay_audit_log.jsonl",
    log_path="results/razorpay_agent.jsonl",
)


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify(
        {
            "status": "ok",
            "razorpay_keys_configured": bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET),
            "webhook_secret_configured": bool(RAZORPAY_WEBHOOK_SECRET),
        }
    )


@app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not RAZORPAY_WEBHOOK_SECRET:
        return jsonify({"error": "RAZORPAY_WEBHOOK_SECRET not configured on this server"}), 500

    if not verify_webhook_signature(raw_body, signature, RAZORPAY_WEBHOOK_SECRET):
        return jsonify({"error": "invalid signature"}), 400

    payload = request.get_json(force=True, silent=True) or {}
    event = parse_webhook_payload(payload)

    if event is None:
        # unhandled event type -- ack it so Razorpay stops retrying, but don't process it
        return jsonify({"status": "ignored", "event": payload.get("event")}), 200

    idempotency_key = payload.get("payload", {}).get("payment", {}).get("entity", {}).get("id") or event.event_id
    result = gateway.ingest(event, idempotency_key=idempotency_key)

    if result.is_duplicate:
        return jsonify({"status": "duplicate_ignored", "idempotency_key": idempotency_key}), 200

    record = engine.process_event(result.event)
    return jsonify(
        {
            "status": "processed",
            "event_id": record.event.event_id,
            "diagnosis": record.diagnosis.category.value,
            "chosen_action": record.chosen_action.value,
            "pursued": record.pursued,
            "outcome_recovered": record.outcome.recovered if record.outcome else None,
        }
    ), 200


if __name__ == "__main__":
    print("Razorpay webhook server starting on http://localhost:5000")
    print(f"  RAZORPAY_KEY_ID configured:      {bool(RAZORPAY_KEY_ID)}")
    print(f"  RAZORPAY_KEY_SECRET configured:  {bool(RAZORPAY_KEY_SECRET)}")
    print(f"  RAZORPAY_WEBHOOK_SECRET configured: {bool(RAZORPAY_WEBHOOK_SECRET)}")
    if not RAZORPAY_WEBHOOK_SECRET:
        print("  WARNING: no webhook secret set -- incoming webhooks will be rejected until you set one.")
    app.run(host="0.0.0.0", port=5000, debug=False)
