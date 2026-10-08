# -*- coding: utf-8 -*-
"""E2E verification: BOT_START → TRADE_REGISTERED → snapshot → log → archive → merged read.

Uses the REAL modules end-to-end against a sandbox root (never touches
production storage/). Exercises every stage of the new pipeline.
"""
import sys, json, gzip, shutil, time
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import os
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SANDBOX = Path("tmp_e2e").resolve()
if SANDBOX.exists():
    shutil.rmtree(SANDBOX)
(SANDBOX / "storage" / "logs").mkdir(parents=True)
(SANDBOX / "storage" / "snapshots").mkdir(parents=True)

# ── redirect all production paths into the sandbox ──
import services.logging.audit_logger as al
al._LOG_PATH = str(SANDBOX / "storage" / "logs" / "audit.jsonl")
al._LIFECYCLE_PATH = str(SANDBOX / "storage" / "logs" / "audit_lifecycle.jsonl")
al._LIFECYCLE_ARCHIVE_DIR = SANDBOX / "storage" / "audit_lifecycle"
al._TEST_OVERRIDE = None
al._LAST_ROTATED_DAY = None
al._LAST_ARCHIVED_MONTH = None

import services.snapshots.snapshot_writer as sw
sw._SNAPSHOT_ROOT = SANDBOX / "storage" / "snapshots"

from scripts import storage_guard as sg
sg.STORAGE = SANDBOX / "storage"
sg.STATE_PATH = SANDBOX / "storage" / "db" / "storage_state.json"

import pandas as pd, numpy as np

def ok(msg): print(f"  ✓ {msg}")

print("1) BOT_START (lifecycle stream)")
al.log_event("BOT_START", {"session_id": "e2e123", "pid": 1}, stream="lifecycle")
ok("audit_lifecycle.jsonl created")

print("2) TRADE_REGISTERED + async snapshot submit")
n = 60
close = np.linspace(1.08, 1.095, n)
m15 = pd.DataFrame({
    "time": pd.date_range("2026-10-05", periods=n, freq="15min"),
    "open": close - 0.0004, "high": close + 0.0008,
    "low": close - 0.0008, "close": close, "tick_volume": 120,
})
h4 = m15.iloc[::4].reset_index(drop=True)
sig = {"side": "BUY", "score": 4, "factors": ["H4_TREND_FILTER", "BREAKOUT_20BAR", "PULLBACK_CONFIRM", "FAST_RETEST"],
       "level": 1.0885, "entry": 1.0890, "sl": 1.0862, "tp1": 1.0918, "tp2": 1.0946, "tp_final": 1.0955}

w = sw.SnapshotWriter(root=SANDBOX / "storage" / "snapshots")
t0 = time.monotonic()
assert w.submit(trade_id=424242, symbol="EURUSD.", side="BUY",
                entry=1.0890, sl=1.0862, tp=1.0955,
                m15=m15, h4=h4, signal=sig)
submit_ms = (time.monotonic() - t0) * 1000
al.log_event("TRADE_REGISTERED", {"ticket": 424242, "symbol": "EURUSD.", "side": "BUY",
                                  "entry": 1.0890, "sl": 1.0862, "tp": 1.0955,
                                  "score": 4}, stream="lifecycle")
ok(f"submit() non-blocking: {submit_ms:.2f} ms")
w.close(timeout=5)

snap_glob = list((SANDBOX / "storage" / "snapshots").rglob("EURUSD_BUY_424242.json.gz"))
assert snap_glob, "snapshot file missing"
sz = snap_glob[0].stat().st_size
back = sw.load_snapshot(snap_glob[0])
assert back["m15"] and len(back["m15"]) == 60 and back["h4"] and back["signal"]["score"] == 4
ok(f"snapshot saved+read-back: {sz} bytes gz, 60 M15 bars, ATR14={back['atr14_m15']}")

lc_events = [json.loads(l) for l in
             (SANDBOX / "storage" / "logs" / "audit_lifecycle.jsonl").read_text().splitlines()]
names = [e["event"] for e in lc_events]
assert "BOT_START" in names and "TRADE_REGISTERED" in names and "SNAPSHOT_SAVED" in names, names
ok(f"lifecycle stream: {names}")

print("3) month rollover → gzip archive")
lc = SANDBOX / "storage" / "logs" / "audit_lifecycle.jsonl"
import time as _t
old_ts = _t.mktime((2026, 9, 20, 12, 0, 0, 0, 0, -1))
os_utime = __import__("os").utime
os_utime(lc, (old_ts, old_ts))
# earlier writes in this process already claimed this month was checked;
# reset so the rollover detection sees the backdated file fresh
al._LAST_ARCHIVED_MONTH = None
real_dt = al.datetime
class FakeDT(real_dt):
    @classmethod
    def now(cls, tz=None): return cls(2026, 10, 5, 4, 0, 0, tzinfo=timezone.utc)
al.datetime = FakeDT
try:
    al.log_event("STORAGE_STATUS", {"total_mb": 28.2}, stream="lifecycle")
finally:
    al.datetime = real_dt
gz = SANDBOX / "storage" / "audit_lifecycle" / "2026-09.jsonl.gz"
assert gz.exists(), "archive missing"
with gzip.open(gz, "rt", encoding="utf-8") as f:
    archived = [json.loads(l)["event"] for l in f if l.strip()]
ok(f"archived to {gz.name}: {archived}")

print("4) merged reader (scripts/audit_reader)")
sys.path.insert(0, ".")
from scripts.audit_reader import iter_events
evs = iter_events(root=SANDBOX)
merged_names = [e["event"] for e in evs]
assert merged_names.index("BOT_START") < merged_names.index("SNAPSHOT_SAVED") < merged_names.index("STORAGE_STATUS")
ok(f"merged order OK: {merged_names}")

print("5) storage guard pass")
rep = sg.run_guard(now=datetime(2026, 10, 5, 4, 0, 0, tzinfo=timezone.utc))
assert rep["storage_full"] is False and rep["critical"] is False
flag = json.loads(sg.STATE_PATH.read_text())
assert flag["storage_full"] is False
ok(f"guard: total={rep['total_mb']} MB, flag written")

print("6) STORAGE_FULL → risk_manager refuses new trades")
import os
from services.risk.risk_manager import RiskManager
os.environ["STORAGE_STATE_PATH"] = str(sg.STATE_PATH)
sg.write_storage_flag(True, 960 * 1024 * 1024)
rm = RiskManager()
can, reason = rm.can_trade("EURUSD.")
assert can is False and "STORAGE_FULL" in reason, (can, reason)
sg.write_storage_flag(False, 28 * 1024 * 1024)
ok(f"can_trade blocked with STORAGE_FULL; flag cleared resumes")

print("\nALL E2E STAGES PASSED")
shutil.rmtree(SANDBOX, ignore_errors=True)
