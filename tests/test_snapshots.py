"""Tests for the K-line snapshot mechanism (task 1).

save → read-back → expiry, all against a tmp storage root.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import gzip
import time
from datetime import datetime, timezone, timedelta

import pytest
import pandas as pd

from services.snapshots import snapshot_writer as sw


@pytest.fixture
def snap_root(tmp_path, monkeypatch):
    """Redirect the snapshot root into tmp."""
    monkeypatch.setattr(sw, "_SNAPSHOT_ROOT", tmp_path / "snapshots")
    yield tmp_path / "snapshots"


def _m15(n=30):
    times = pd.date_range("2026-10-01", periods=n, freq="15min")
    import numpy as np
    close = 1.08 + np.linspace(0, 0.01, n)
    return pd.DataFrame({
        "time": times, "open": close - 0.0005, "high": close + 0.001,
        "low": close - 0.001, "close": close, "tick_volume": 100,
    })


class TestSnapshotSaveRead:
    def test_save_and_read_back_roundtrip(self, snap_root):
        """Saved gzip snapshot loads back with OHLCV + indicators intact."""
        m15 = _m15()
        snap = sw.build_snapshot(
            symbol="EURUSD.", side="BUY", entry=1.085, sl=1.080, tp=1.095,
            m15=m15, h4=None,
            signal={"score": 4, "factors": ["BREAKOUT_20BAR"]},
        )
        snap["trade_id"] = 12345
        p = sw.save_snapshot(snap)
        assert p.exists()
        assert p.parent == snap_root / datetime.now(timezone.utc).strftime("%Y%m%d")
        assert p.name == "EURUSD_BUY_12345.json.gz"
        assert p.stat().st_size > 0
        # gzip magic bytes
        assert p.read_bytes()[:2] == b"\x1f\x8b"

        back = sw.load_snapshot(p)
        assert back["symbol"] == "EURUSD."
        assert back["side"] == "BUY"
        assert back["entry"] == pytest.approx(1.085)
        assert len(back["m15"]) == 30
        assert back["m15"][0]["close"] == pytest.approx(1.08, abs=1e-4)
        assert back["atr14_m15"] is not None and back["atr14_m15"] > 0
        assert back["ema50_m15"] is not None
        assert back["signal"]["score"] == 4

    def test_async_submit_does_not_block_and_writes(self, snap_root):
        """SnapshotWriter.submit returns immediately; the daemon persists."""
        w = sw.SnapshotWriter(root=snap_root)
        try:
            m15 = _m15()
            t0 = time.monotonic()
            ok = w.submit(
                trade_id=99901, symbol="XAUUSD.", side="SELL",
                entry=4150.0, sl=4165.0, tp=4120.0,
                m15=m15, h4=None, signal={"score": 3},
            )
            elapsed = time.monotonic() - t0
            assert ok is True
            assert elapsed < 0.5  # enqueue only, no disk I/O in the call
            # wait for the daemon to drain
            deadline = time.monotonic() + 5
            expected = snap_root / datetime.now(timezone.utc).strftime("%Y%m%d") / "XAUUSD_SELL_99901.json.gz"
            while not expected.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert expected.exists()
            back = sw.load_snapshot(expected)
            assert back["symbol"] == "XAUUSD."
            assert len(back["m15"]) == 30
        finally:
            w.close(timeout=2)

    def test_expired_snapshots_pruned_by_age(self, snap_root, monkeypatch):
        """storage_guard prune_snapshots removes day-dirs older than 90 days,
        keeps fresh ones, and never touches unknown dir names."""
        from scripts import storage_guard as sg
        monkeypatch.setattr(sg, "STORAGE", snap_root.parent)
        now = datetime.now(timezone.utc)

        old_day = snap_root / (now - timedelta(days=120)).strftime("%Y%m%d")
        new_day = snap_root / now.strftime("%Y%m%d")
        weird = snap_root / "not_a_date"
        for d in (old_day, new_day, weird):
            d.mkdir(parents=True, exist_ok=True)
        (old_day / "XAUUSD_BUY_1.json.gz").write_bytes(b"x" * 100)
        (new_day / "XAUUSD_BUY_2.json.gz").write_bytes(b"x" * 100)
        (weird / "mystery.bin").write_bytes(b"x" * 100)

        freed = sg.prune_snapshots(now)
        assert freed == 100                      # only the old one counted
        assert not old_day.exists()              # expired → deleted
        assert new_day.exists()                  # fresh → kept
        assert weird.exists()                    # unknown name → untouched
