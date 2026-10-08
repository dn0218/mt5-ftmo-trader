"""Tests for the storage capacity guard (task 3).

Threshold trigger / tiered deletion order / CRITICAL stop / STORAGE_FULL
trade blocking via risk_manager.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import storage_guard as sg


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Full fake storage/ tree with the guard pointed at tmp."""
    storage = tmp_path / "storage"
    for sub in ("logs", "snapshots", "audit_lifecycle", "db"):
        (storage / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sg, "STORAGE", storage)
    monkeypatch.setattr(sg, "STATE_PATH", storage / "db" / "storage_state.json")
    # silence alerts/audit writes to prod paths
    monkeypatch.setattr(sg, "_notify", lambda msg: None)
    monkeypatch.setattr(sg, "_audit", lambda ev, d: None)
    yield storage


def _mk(p: Path, size: int, mtime_days_ago: float = 0.0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    if mtime_days_ago:
        ts = p.stat().st_mtime - mtime_days_ago * 86400
        os.utime(p, (ts, ts))


NOW = datetime(2026, 10, 5, 4, 0, 0, tzinfo=timezone.utc)


class TestStorageGuard:
    def test_under_threshold_no_deletion_and_status_written(self, store):
        """≤800 MB: nothing deleted, flag cleared, normal exit."""
        _mk(store / "db" / "performance.json", 1000)
        _mk(store / "snapshots" / "20261005" / "a.json.gz", 500)
        old_ops = store / "logs" / "audit_20260801.jsonl"
        _mk(old_ops, 1000, mtime_days_ago=70)   # >30d but under budget

        rep = sg.run_guard(now=NOW)
        assert rep["total_before"] < sg.WARN_BYTES
        assert old_ops.exists()                 # under budget → nothing touched
        assert rep["freed"] == {"operations": 0, "snapshots": 0, "lifecycle": 0}
        assert rep["critical"] is False and rep["storage_full"] is False
        # status flag written, full=False
        flag = json.loads(sg.STATE_PATH.read_text())
        assert flag["storage_full"] is False

    def test_tiered_deletion_order_ops_then_snapshots(self, store):
        """>800 MB: operations archives (>30d) and snapshots (>90d) go first;
        lifecycle archives stay unless still over budget AND >180d."""
        # ops archives: one old (deletable), one fresh (keep). Ops tier is
        # SMALL so tier 1 alone can't get under budget → tier 2 must run.
        _mk(store / "logs" / "audit_20260701.jsonl", 50 * 1024 * 1024, mtime_days_ago=95)
        _mk(store / "logs" / "audit_20261004.jsonl", 1024, mtime_days_ago=1)
        # snapshots: old (deletable) + fresh (keep)
        _mk(store / "snapshots" / "20260601" / "s.json.gz", 850 * 1024 * 1024, mtime_days_ago=120)
        _mk(store / "snapshots" / "20261005" / "s2.json.gz", 1024)
        # lifecycle: old enough to be deletable but must survive this pass
        # (budget satisfied by tiers 1+2 → lifecycle tier never reached)
        _mk(store / "audit_lifecycle" / "2026-03.jsonl.gz", 200 * 1024 * 1024, mtime_days_ago=200)

        rep = sg.run_guard(now=NOW)

        assert not (store / "logs" / "audit_20260701.jsonl").exists()   # tier 1 gone
        assert (store / "logs" / "audit_20261004.jsonl").exists()       # fresh kept
        assert not (store / "snapshots" / "20260601").exists()          # tier 2 gone
        assert (store / "snapshots" / "20261005").exists()              # fresh kept
        # lifecycle untouched: budget already satisfied by tiers 1+2
        assert (store / "audit_lifecycle" / "2026-03.jsonl.gz").exists()
        assert rep["freed"]["operations"] > 0
        assert rep["freed"]["snapshots"] > 0
        assert rep["freed"]["lifecycle"] == 0
        assert rep["critical"] is False

    def test_critical_stop_when_whitelist_exhausted(self, store):
        """Over budget with nothing deletable → STOP deleting, critical=True,
        Telegram alerted (notify spy), no lifecycle guesses."""
        # only undeletable content: fresh + unknown-name files, huge
        _mk(store / "logs" / "audit_20261004.jsonl", 850 * 1024 * 1024, mtime_days_ago=1)
        _mk(store / "db" / "performance.json", 10 * 1024 * 1024)
        _mk(store / "snapshots" / "backup_who_knows" / "x.bin", 5 * 1024 * 1024)

        alerted = []
        orig_notify = sg._notify
        sg._notify = alerted.append
        try:
            rep = sg.run_guard(now=NOW)
        finally:
            sg._notify = orig_notify

        assert rep["critical"] is True
        assert (store / "logs" / "audit_20261004.jsonl").exists()   # fresh → kept
        assert (store / "db" / "performance.json").exists()         # db → untouchable
        assert (store / "snapshots" / "backup_who_knows").exists()  # unknown → kept
        assert any("CRITICAL" in m for m in alerted)
        # flag says full=False (under 950) but critical event recorded
        assert rep["storage_full"] is False

    def test_storage_full_blocks_new_trades_via_risk_manager(self, store, monkeypatch):
        """>950 MB → storage_state.json flags full; RiskManager.can_trade
        refuses with STORAGE_FULL; under 950 it trades normally."""
        # big undeletable fresh file pushes over 950 MB
        _mk(store / "logs" / "audit_20261004.jsonl", 960 * 1024 * 1024, mtime_days_ago=1)

        rep = sg.run_guard(now=NOW)
        assert rep["storage_full"] is True

        flag = json.loads(sg.STATE_PATH.read_text())
        assert flag["storage_full"] is True

        # risk manager honours the flag via STORAGE_STATE_PATH override
        monkeypatch.setenv("STORAGE_STATE_PATH", str(sg.STATE_PATH))
        from services.risk.risk_manager import RiskManager
        rm = RiskManager()

        can, reason = rm.can_trade("EURUSD.")
        assert can is False
        assert "STORAGE_FULL" in reason

        # and when the flag clears, trading resumes
        sg.write_storage_flag(False, 0)
        can2, reason2 = rm.can_trade("EURUSD.")
        assert can2 is True or "STORAGE_FULL" not in (reason2 or "")


class _FakeFile:
    def __init__(self, path: Path):
        self._path = path
    def read(self, *a):
        return self._path.read_text(encoding="utf-8")
    def write(self, *a):
        raise OSError("read-only fake")
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
