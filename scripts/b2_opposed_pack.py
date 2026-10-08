"""B2 v2: the 0.80-layer kill list is directly identifiable.

TRADE_REJECTED reason strings carry the ACTIVE threshold:
  'LOW_CONFIDENCE (0.74 < 0.8)'  <- HTF-opposed floor (0.80 layer) did the kill
  'LOW_CONFIDENCE (0.68 < 0.7)'  <- global floor (0.70 layer)
Both carry symbol + side -> true post-hoc W/L per layer via MT5 replay.
"""
from __future__ import annotations
import os, sys, json, re
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

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
    sys.exit("mt5 init failed")
if not mt5.login(int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"],
                 server=os.environ["MT5_SERVER"]):
    mt5.shutdown(); sys.exit("login failed")

LOOK = 24

def m15_floor(ts):
    return ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)

def replay(symbol, side, ts, look=LOOK):
    e = m15_floor(ts)
    bars = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M15,
                               e + timedelta(minutes=15 * look), look + 60)
    if bars is None or len(bars) == 0:
        return "NO_DATA"
    idx = next((i for i, b in enumerate(bars) if b["time"] == int(e.timestamp())), None)
    if idx is None:
        return "NO_ENTRY_BAR"
    entry = float(bars[idx]["close"])
    win = bars[max(0, idx - 15):idx + 1]
    if len(win) < 15:
        return "NO_ATR"
    h = np.array([b["high"] for b in win]); l = np.array([b["low"] for b in win])
    c = np.array([b["close"] for b in win])
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    atr = float(tr[-14:].mean())
    if atr <= 0:
        return "NO_ATR"
    fwd = bars[idx + 1: idx + 1 + look]
    sl_hit = tp_hit = None
    for i, b in enumerate(fwd):
        bh, bl = float(b["high"]), float(b["low"])
        if side == "BUY":
            if bl <= entry - atr: sl_hit = i; break
            if bh >= entry + 2 * atr: tp_hit = i; break
        else:
            if bh >= entry + atr: sl_hit = i; break
            if bl <= entry - 2 * atr: tp_hit = i; break
    if tp_hit is not None and (sl_hit is None or tp_hit < sl_hit):
        return "WOULD_WIN"
    if sl_hit is not None:
        return "WOULD_LOSE"
    return "FLAT"

def h4_bias(symbol, ts):
    bars = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_H4, ts, 3)
    if bars is None or len(bars) < 2:
        return "?"
    return "UP" if bars[-1]["close"] > bars[-2]["close"] else "DOWN"

rows = []
for p in [ROOT / "storage/logs/audit.jsonl"]:
    if not p.exists():
        continue
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"LOW_CONFIDENCE' not in line:
            continue
        r = json.loads(line)
        ts = r.get("ts", "")
        if not ("2026-09-14" <= ts[:10] <= "2026-09-22"):
            continue
        d = r.get("data") or {}
        m = re.search(r"\(([\d.]+) < ([\d.]+)\)", d.get("reason") or "")
        if not m:
            continue
        rows.append({"ts": datetime.fromisoformat(ts), "symbol": d.get("symbol", "?"),
                     "side": d.get("side", "?"), "conf": float(m.group(1)),
                     "thr": float(m.group(2)),
                     "layer": "opposed_080" if m.group(2).startswith("0.8") else "global_070"})

# dedupe to distinct setups: same symbol+side+15min bar
seen, dedup = set(), []
for r in sorted(rows, key=lambda x: x["ts"]):
    key = (r["symbol"], r["side"], m15_floor(r["ts"]))
    if key in seen:
        continue
    seen.add(key)
    dedup.append(r)

print(f"LOW_CONFIDENCE kills 9/14-9/22: {len(rows)} raw -> {len(dedup)} distinct setups")
by_layer = Counter(r["layer"] for r in dedup)
print(f"by layer: {dict(by_layer)}")

# hard-ban impact = opposed setups (killed by 0.80) as share of ALL adopted
# signals: adopted = ALLOW & passed conf gate. We know DRY_RUN_TRADE=2 in this
# window; opposed_080 kills are signals that WOULD have been adopted if the
# floor were 0.70 (assuming they pass later gates).
print(f"\nIf 0.80 floor were lowered to 0.70: up to {by_layer['opposed_080']} more setups "
      f"enter downstream gates (vs 2 adopted in the whole window)")

# replay each killed setup
for r in dedup:
    r["verdict"] = replay(r["symbol"], r["side"], r["ts"])
    r["h4"] = h4_bias(r["symbol"], r["ts"])

print("\n=== post-hoc verdict of KILLED setups (2R-first vs 1R-first, 6h window) ===")
for layer in ("opposed_080", "global_070"):
    rs = [r for r in dedup if r["layer"] == layer]
    c = Counter(r["verdict"] for r in rs)
    print(f"{layer:<14} n={len(rs):>3}  {dict(c)}")

print("\n=== opposed_080 killed setups, detail (Top 12) ===")
for r in [r for r in dedup if r["layer"] == "opposed_080"][:12]:
    print(f"{r['ts']:%m-%d %H:%M} {r['symbol']:<9} {r['side']:<4} conf={r['conf']:.2f} "
          f"H4={r['h4']:<5} -> {r['verdict']}")

mt5.shutdown()
