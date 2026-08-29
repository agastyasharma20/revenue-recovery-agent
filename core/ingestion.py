"""
Ingestion layer with idempotency keys.

A webhook provider (Razorpay included -- see Phase 5) will retry delivery on
timeout, ambiguous responses, or its own internal retries. Without
idempotency, a payment.failed webhook delivered twice would be diagnosed,
prioritized, and actioned twice -- e.g. two SMS reminders, or two collections
escalations, for the same underlying event. IdempotencyStore tracks which
keys have already been ingested and IngestionGateway.ingest() refuses to
hand back a "new" event for a key it's seen before.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Optional

from core.schema import RevenueEvent


class IdempotencyStore:
    """In-memory set of seen idempotency keys, optionally persisted to a
    JSONL file so it survives a process restart (a real deployment would use
    Redis or a DB row with a unique constraint instead -- the interface is
    the same either way)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._seen.add(json.loads(line)["idempotency_key"])

    def has_seen(self, key: str) -> bool:
        with self._lock:
            return key in self._seen

    def mark_seen(self, key: str) -> None:
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
            if self.path:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"idempotency_key": key}) + "\n")


@dataclass
class IngestionResult:
    event: Optional[RevenueEvent]
    is_duplicate: bool
    idempotency_key: str


class IngestionGateway:
    def __init__(self, store: Optional[IdempotencyStore] = None):
        self.store = store or IdempotencyStore()

    def ingest(self, event: RevenueEvent, idempotency_key: Optional[str] = None) -> IngestionResult:
        """Returns event=None with is_duplicate=True if this idempotency_key
        was already ingested -- caller must not reprocess it. Thread-safe:
        the "check-and-mark" is atomic under the store's lock, so two
        concurrent deliveries of the same webhook can't both slip through."""
        key = idempotency_key or event.idempotency_key or event.event_id

        with self.store._lock:
            if key in self.store._seen:
                return IngestionResult(event=None, is_duplicate=True, idempotency_key=key)
            self.store._seen.add(key)
            if self.store.path:
                os.makedirs(os.path.dirname(os.path.abspath(self.store.path)) or ".", exist_ok=True)
                with open(self.store.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"idempotency_key": key}) + "\n")

        event.idempotency_key = key
        return IngestionResult(event=event, is_duplicate=False, idempotency_key=key)
