"""Forensics on rejected trades.

For each picked TRADE_REJECTED / SLTP_INVALID event in audit.jsonl, replay the
M15 bars *after* the rejection and judge whether the would-be trade would have
hit a hypothetical 2R TP or 1R SL first.

Heuristic per sample:
  - Find the most recently CLOSED M15 bar at the rejection ts. Use its close as
    the hypothetical entry.
  - SL distance = 1.0 x ATR14 of that bar (proxy for what the structural SL
    would have been; matches the system's typical 15-30 pip range on majors).
  - TP distance = 2.0 x SL distance.
  - Walk forward up to N=24 M15 bars (~6h). Whichever level hits first wins.
  - For HTF blocks, also pull the H4 bar at the rejection ts to confirm the
    bias direction the system reported.

Output: per-sample verdict (WOULD_WIN / WOULD_LOSE / FLAT) + class summary so
we can tell whether HTF / LLM filters are saving losers or strangling winners.
"""
from __future__ import annotations
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import MetaTrader5 as mt5
import numpy as np

if not mt5.initialize():
    print(f"initialize() failed: {mt5.last_error()}")
    sys.exit(1)
login = int(os.environ["MT5_LOGIN"])
pwd = os.environ["MT5_PASSWORD"]
server = os.environ["MT5_SERVER"]
if not mt5.login(login, password=pwd, server=server):
    print(f"login() failed: {mt5.last_error()}")
    mt5.shutdown()
    sys.exit(1)

LOOK_FORWARD_BARS = 24  # 24 M15 bars = 6 hours


def m15_floor(ts: datetime) -> datetime:
    minute = (ts.minute // 15) * 15
    return ts.replace(minute=minute, second=0, microsecond=0)


def atr14_at(symbol: str, ts: datetime) -> float | None:
    bars = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M15, ts, 30)
    if bars is None or len(bars) < 15:
        return None
    highs = np.array([b["high"] for b in bars])
    lows = np.array([b["low"] for b in bars])
    closes = np.array([b["close"] for b in bars])
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    return float(tr[-14:].mean())


def replay(symbol: str, side: str, ts: datetime) -> dict:
    """Replay the rejection and return verdict."""
    entry_bar_ts = m15_floor(ts)
    # Pull entry bar + look-forward.
    bars = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M15,
                                entry_bar_ts + timedelta(minutes=15 * LOOK_FORWARD_BARS), 60)
    if bars is None or len(bars) == 0:
        return {"verdict": "NO_DATA"}
    # Find the entry bar.
    entry_idx = None
    for i, b in enumerate(bars):
        if b["time"] == int(entry_bar_ts.timestamp()):
            entry_idx = i
            break
    if entry_idx is None:
        return {"verdict": "NO_ENTRY_BAR"}

    entry = float(bars[entry_idx]["close"])
    atr = atr14_at(symbol, entry_bar_ts)
    if not atr or atr <= 0:
        return {"verdict": "NO_ATR"}

    sl_dist = atr  # 1xATR — typical structural SL
    tp_dist = 2 * sl_dist

    if side == "BUY":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist

    # Walk forward.
    sl_hit_at = tp_hit_at = None
    forward = bars[entry_idx + 1: entry_idx + 1 + LOOK_FORWARD_BARS]
    for i, b in enumerate(forward):
        h, l = float(b["high"]), float(b["low"])
        if side == "BUY":
            if l <= sl:
                sl_hit_at = i
                break
            if h >= tp:
                tp_hit_at = i
                break
        else:
            if h >= sl:
                sl_hit_at = i
                break
            if l <= tp:
                tp_hit_at = i
                break

    if tp_hit_at is not None and (sl_hit_at is None or tp_hit_at < sl_hit_at):
        verdict = "WOULD_WIN"
    elif sl_hit_at is not None:
        verdict = "WOULD_LOSE"
    else:
        verdict = "FLAT"

    # H4 bias context.
    h4 = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_H4, entry_bar_ts, 3)
    h4_dir = "?"
    if h4 is not None and len(h4) >= 2:
        h4_dir = "UP" if h4[-1]["close"] > h4[-2]["close"] else "DOWN"

    return {
        "verdict": verdict,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "sl_dist": sl_dist,
        "h4_dir": h4_dir,
        "tp_hit_at": tp_hit_at,
        "sl_hit_at": sl_hit_at,
    }


SAMPLES = [
    # class, ts, symbol, side
    ("HTF_BLOCK_BULLISH_blocks_SELL", "2026-04-29T13:30:00+00:00", "USDJPY", "SELL"),
    ("HTF_BLOCK_BULLISH_blocks_SELL", "2026-04-29T11:00:00+00:00", "USDJPY", "SELL"),
    ("HTF_BLOCK_BULLISH_blocks_SELL", "2026-04-28T15:00:00+00:00", "USDJPY", "SELL"),
    ("HTF_BLOCK_BEARISH_blocks_BUY",  "2026-04-29T13:30:00+00:00", "GBPUSD", "BUY"),
    ("HTF_BLOCK_BEARISH_blocks_BUY",  "2026-04-29T11:00:00+00:00", "GBPUSD", "BUY"),
    ("HTF_BLOCK_BEARISH_blocks_BUY",  "2026-04-28T15:00:00+00:00", "GBPUSD", "BUY"),
    ("LLM_REFUSE_EURUSD_SELL",        "2026-04-29T14:30:00+00:00", "EURUSD", "SELL"),
    ("LLM_REFUSE_EURUSD_SELL",        "2026-04-29T11:00:00+00:00", "EURUSD", "SELL"),
    ("LLM_REFUSE_EURUSD_SELL",        "2026-04-28T15:00:00+00:00", "EURUSD", "SELL"),
    # SLTP_INVALID — these had ALLOW from LLM but SL too tight. Use the side/symbol from the audit log:
    ("SLTP_INVALID_EURUSD",           "2026-04-27T14:36:00+00:00", "EURUSD", "BUY"),
    ("SLTP_INVALID_EURUSD",           "2026-04-27T14:48:00+00:00", "EURUSD", "BUY"),
    ("SLTP_INVALID_EURUSD",           "2026-04-27T14:52:00+00:00", "EURUSD", "BUY"),
]

print(f"{'CLASS':<35} {'TS':<26} {'SYM':<8} {'SIDE':<5} {'VERDICT':<12} {'H4':<5}  ENTRY -> SL/TP")
print("-" * 130)

class_stats = {}
for klass, ts_iso, sym, side in SAMPLES:
    ts = datetime.fromisoformat(ts_iso)
    r = replay(sym, side, ts)
    v = r.get("verdict", "?")
    class_stats.setdefault(klass, []).append(v)
    if v in ("WOULD_WIN", "WOULD_LOSE", "FLAT"):
        print(f"{klass:<35} {ts_iso:<26} {sym:<8} {side:<5} {v:<12} {r['h4_dir']:<5}  "
              f"{r['entry']:.5f} -> SL {r['sl']:.5f} / TP {r['tp']:.5f}")
    else:
        print(f"{klass:<35} {ts_iso:<26} {sym:<8} {side:<5} {v}")

print()
print("=" * 60)
print("SUMMARY by class:")
for klass, verdicts in class_stats.items():
    wins = verdicts.count("WOULD_WIN")
    losses = verdicts.count("WOULD_LOSE")
    flats = verdicts.count("FLAT")
    print(f"  {klass:<35}  W:{wins} L:{losses} F:{flats}")
print()
print("Interpretation:")
print("  W (WOULD_WIN)  = filter blocked a winner (cost)")
print("  L (WOULD_LOSE) = filter blocked a loser  (correct save)")
print("  F (FLAT)       = no resolution in 6h    (neutral)")

mt5.shutdown()
