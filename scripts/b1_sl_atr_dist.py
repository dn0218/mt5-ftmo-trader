"""B1: SL/ATR distribution from SLTP_INVALID events 9/14-9/22.

Reconstructs ATR14 at each event time from MT5 history (same formula as
core/indicators), computes sl_over_atr, reports overall + per-session
percentiles, plus a 30-day ATR jump distribution for the lag-band threshold.

NOTE the selection bias: these are REJECTED SL proposals only (the gate's
survivors are never logged through this path). n is small for FX MIN side.
"""
from __future__ import annotations
import os, sys, json, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import numpy as np
import MetaTrader5 as mt5

if not mt5.initialize():
    print(f"mt5 init failed: {mt5.last_error()}"); sys.exit(1)
if not mt5.login(int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"],
                 server=os.environ["MT5_SERVER"]):
    print("login failed"); mt5.shutdown(); sys.exit(1)

# ---- collect events ----
events = []
for p in [ROOT / "storage/logs/audit.jsonl"]:
    if not p.exists():
        continue
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"SLTP_INVALID"' not in line:
            continue
        r = json.loads(line)
        ts = r.get("ts", "")
        if not ("2026-09-14" <= ts[:10] <= "2026-09-22"):
            continue
        d = r.get("data") or {}
        m = re.search(r"([\d.]+) pips ([<>]) ([\d.]+)", d.get("reason") or "")
        if not m:
            continue
        events.append({
            "ts": datetime.fromisoformat(ts), "symbol": d.get("symbol", "?"),
            "side": d.get("side", "?"), "pips": float(m.group(1)),
            "bound": m.group(2), "limit": float(m.group(3)),
            "family": "TIGHT" if m.group(2) == "<" else "WIDE",
        })
print(f"parsed {len(events)} SLTP_INVALID events with pips in reason")

# ---- ATR14 at event time (identical formula to engine's atr()) ----
def atr14_at(symbol: str, when: datetime, n=14) -> float | None:
    raw = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M15, when, 60)
    if raw is None or len(raw) < n + 2:
        return None
    h = np.array([b["high"] for b in raw][1:], dtype=float)
    l = np.array([b["low"] for b in raw][1:], dtype=float)
    c = np.array([b["close"] for b in raw][:-1], dtype=float)
    tr = np.maximum.reduce([h - l, np.abs(h - c), np.abs(l - c)])
    return float(tr[-n:].mean())
# pip size per symbol (XAU point*10 etc.) — use core source of truth
from core.mt5.symbols import pip_size
def pip_of(sym: str) -> float:
    info = mt5.symbol_info(sym)
    return pip_size(sym, info) if info else 0.0

SESSIONS = [("asia", 0, 7), ("london", 7, 12), ("overlap", 12, 16), ("ny", 16, 21)]
def sess(h): return next((n for n, a, b in SESSIONS if a <= h < b), "off")

rows = []
for e in events:
    pip = pip_of(e["symbol"])
    if pip <= 0:
        continue
    atr = atr14_at(e["symbol"], e["ts"])
    if not atr or atr <= 0:
        continue
    sl_dist_price = e["pips"] * pip
    rows.append({**e, "atr": atr, "sl_over_atr": sl_dist_price / atr,
                 "session": sess(e["ts"].hour)})

def pct(xs, q):
    if xs is None or len(xs) == 0: return float("nan")
    xs = sorted(xs); k = (len(xs)-1) * q
    f, c = int(k), min(int(k)+1, len(xs)-1)
    return xs[f] + (xs[c]-xs[f]) * (k-f)

print(f"\n{'sym':<9}{'fam':<6}{'n':>3}  {'p10':>6}{'p50':>6}{'p95':>6}   sessions")
by = defaultdict(list)
for r in rows:
    by[(r["symbol"], r["family"])].append(r)
for (sym, fam), rs in sorted(by.items()):
    xs = [r["sl_over_atr"] for r in rs]
    ses = defaultdict(list)
    for r in rs: ses[r["session"]].append(r["sl_over_atr"])
    s_str = " ".join(f"{k}:{len(v)}(p95={pct(v,0.95):.1f})" for k, v in sorted(ses.items()))
    print(f"{sym:<9}{fam:<6}{len(rs):>3}  {pct(xs,0.10):>6.2f}{pct(xs,0.50):>6.2f}{pct(xs,0.95):>6.2f}   {s_str}")

# ---- 30-day ATR jump distribution (bar-to-bar, per symbol) ----
print("\n=== 30-day ATR(14) bar-to-bar jump distribution (M15) ===")
end = datetime(2026, 9, 23, tzinfo=timezone.utc)
start = end - timedelta(days=30)
for sym in ["EURUSD.", "GBPUSD.", "AUDUSD.", "XAUUSD."]:
    raw = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M15, start, end)
    if raw is None or len(raw) < 100:
        print(f"{sym:<9} no data"); continue
    h = np.array([b["high"] for b in raw], dtype=float)
    l = np.array([b["low"] for b in raw], dtype=float)
    c = np.array([b["close"] for b in raw], dtype=float)
    tr = np.maximum.reduce([h[1:]-l[1:], np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])])
    # rolling ATR14 series
    atr_s = np.convolve(tr, np.ones(14)/14, mode="valid")
    jumps = np.abs(np.diff(atr_s)) / atr_s[:-1]
    jumps = jumps[~np.isnan(jumps)]
    print(f"{sym:<9} bars={len(raw)}  jump p50={pct(jumps,0.50)*100:.1f}% "
          f"p95={pct(jumps,0.95)*100:.1f}% p99={pct(jumps,0.99)*100:.1f}% max={jumps.max()*100:.1f}%")

mt5.shutdown()
