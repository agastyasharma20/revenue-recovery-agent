"""
Phase 4: a webhook delivered twice with the same idempotency key must only
be ingested once.
"""

from datetime import datetime, timezone

from core.schema import RevenueEvent, EventSource, DeclineReason, CustomerSegment
from core.ingestion import IngestionGateway, IdempotencyStore

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def _event():
    return RevenueEvent(
        source=EventSource.SUBSCRIPTION_FAILED,
        decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        amount=999.0,
        customer_segment=CustomerSegment.LOW_LTV,
        created_at=NOW,
        last_attempt_at=NOW,
    )


def test_duplicate_webhook_delivery_is_rejected():
    gateway = IngestionGateway()
    key = "razorpay_webhook_evt_ABC123"

    first = gateway.ingest(_event(), idempotency_key=key)
    assert first.is_duplicate is False
    assert first.event is not None

    second = gateway.ingest(_event(), idempotency_key=key)  # same webhook redelivered
    assert second.is_duplicate is True
    assert second.event is None


def test_different_keys_both_ingest():
    gateway = IngestionGateway()
    r1 = gateway.ingest(_event(), idempotency_key="key-1")
    r2 = gateway.ingest(_event(), idempotency_key="key-2")
    assert r1.is_duplicate is False
    assert r2.is_duplicate is False


def test_idempotency_store_persists_across_instances(tmp_path):
    path = str(tmp_path / "idempotency.jsonl")
    store1 = IdempotencyStore(path=path)
    gateway1 = IngestionGateway(store=store1)
    gateway1.ingest(_event(), idempotency_key="persisted-key")

    # simulate a process restart: fresh store/gateway reading the same file
    store2 = IdempotencyStore(path=path)
    gateway2 = IngestionGateway(store=store2)
    result = gateway2.ingest(_event(), idempotency_key="persisted-key")
    assert result.is_duplicate is True
