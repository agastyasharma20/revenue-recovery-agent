"""
Phase 4: proves the hash-chained audit log is actually tamper-evident --
an intact log verifies clean, and altering/deleting/reordering a record is
detected and pinpointed.
"""

import json
import os

import pytest

from core.audit import AuditLog, verify_chain


@pytest.fixture
def log_path(tmp_path):
    return str(tmp_path / "audit.jsonl")


def test_empty_log_verifies_trivially(log_path):
    result = verify_chain(log_path)
    assert result.ok is True
    assert result.total_records == 0


def test_intact_chain_verifies_ok(log_path):
    log = AuditLog(log_path)
    for i in range(20):
        log.append({"seq": i, "note": f"record {i}"})

    result = verify_chain(log_path)
    assert result.ok is True
    assert result.total_records == 20
    assert result.first_bad_index is None


def test_altering_a_record_is_detected(log_path):
    log = AuditLog(log_path)
    for i in range(10):
        log.append({"seq": i, "amount": 100.0 * i})

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    rec = json.loads(lines[4])
    rec["payload"]["amount"] = 999999.0  # tamper
    lines[4] = json.dumps(rec, sort_keys=True) + "\n"
    with open(log_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    result = verify_chain(log_path)
    assert result.ok is False
    assert result.first_bad_index == 4


def test_deleting_a_record_is_detected(log_path):
    log = AuditLog(log_path)
    for i in range(10):
        log.append({"seq": i})

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    del lines[3]  # remove a record entirely
    with open(log_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    result = verify_chain(log_path)
    assert result.ok is False
    # record 3 is gone, so what's now at index 3 (formerly index 4) has a
    # prev_hash that no longer matches -- chain break detected at index 3.
    assert result.first_bad_index == 3


def test_reordering_records_is_detected(log_path):
    log = AuditLog(log_path)
    for i in range(6):
        log.append({"seq": i})

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    lines[2], lines[3] = lines[3], lines[2]  # swap two records
    with open(log_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    result = verify_chain(log_path)
    assert result.ok is False
    assert result.first_bad_index == 2


def test_chain_continues_correctly_across_multiple_appendlog_instances(log_path):
    """A new AuditLog() pointed at an existing file must pick up the real
    last hash and continue the chain, not restart from genesis."""
    log1 = AuditLog(log_path)
    log1.append({"seq": 0})
    log1.append({"seq": 1})

    log2 = AuditLog(log_path)  # simulates a process restart
    log2.append({"seq": 2})

    result = verify_chain(log_path)
    assert result.ok is True
    assert result.total_records == 3
