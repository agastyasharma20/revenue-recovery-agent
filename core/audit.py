"""
Tamper-evident structured audit log.

Every decision the engine makes is appended as one JSON line. Each record
carries the SHA-256 hash of the *previous* record's canonical bytes, so the
log forms a hash chain -- exactly like a minimal blockchain. Altering,
deleting, or reordering any past record breaks the chain from that point
forward, and verify_chain() will say exactly where.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

GENESIS_HASH = "0" * 64


def _canonical_bytes(record: dict) -> bytes:
    # sort_keys + fixed separators => same dict always serializes identically,
    # which is required for the hash to be reproducible on verification.
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_record(record: dict) -> str:
    return hashlib.sha256(_canonical_bytes(record)).hexdigest()


@dataclass
class VerificationResult:
    ok: bool
    total_records: int
    first_bad_index: Optional[int]
    detail: str


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._last_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not os.path.exists(self.path):
            return GENESIS_HASH
        last = GENESIS_HASH
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                last = rec["this_hash"]
        return last

    def append(self, payload: dict) -> dict:
        """Appends one record. payload should be JSON-serializable (already
        e.g. via .to_dict() on dataclasses/enums -- plain str/float/int/bool/
        dict/list/None only)."""
        record_body = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "prev_hash": self._last_hash,
            "payload": payload,
        }
        this_hash = _hash_record(record_body)
        record = {**record_body, "this_hash": this_hash}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
        self._last_hash = this_hash
        return record

    def read_all(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


def verify_chain(path: str) -> VerificationResult:
    if not os.path.exists(path):
        return VerificationResult(True, 0, None, "No log file yet -- trivially valid.")

    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    expected_prev = GENESIS_HASH
    for i, rec in enumerate(records):
        if rec.get("prev_hash") != expected_prev:
            return VerificationResult(
                False, len(records), i,
                f"Record {i} has prev_hash={rec.get('prev_hash')!r} but expected "
                f"{expected_prev!r} (chain link broken -- record {i} or an earlier "
                "one was altered, deleted, reordered, or inserted).",
            )
        body = {
            "logged_at": rec["logged_at"],
            "prev_hash": rec["prev_hash"],
            "payload": rec["payload"],
        }
        recomputed = _hash_record(body)
        if recomputed != rec.get("this_hash"):
            return VerificationResult(
                False, len(records), i,
                f"Record {i}'s stored hash {rec.get('this_hash')!r} does not match "
                f"recomputed hash {recomputed!r} -- this record's content was "
                "altered after it was written.",
            )
        expected_prev = rec["this_hash"]

    return VerificationResult(True, len(records), None, f"All {len(records)} records verified intact.")
