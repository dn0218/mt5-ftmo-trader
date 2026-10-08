"""P0/P1 pack: (1) opposed vs aligned confidence pipeline, (2) H4 daily regime
distribution, (3) the one real trade's full context.

H4 bias approximation: close vs prev close on the H4 series at event time
(same heuristic analyze_rejects.py uses; the engine's internal HTF logic may
differ in detail - stated as approximation).
"""
from __future__ import annotations
import os, sys, json, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

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

SYMBOLS = ["EURUSD.", "GBPUSD.", "AUDUSD.", "XAUUSD."]
T0, T1 = datetime(2026, 9, 14, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc)

# ---------- load H4 series once per symbol ----------
h4 = {}
for s in SYMBOLS:
    raw = mt5.copy_rates_range(s, mt5.TIMEFRAME_H4, T0 - timedelta(days=4), T1)
    h4[s] = [(int(b["time"]), float(b["close"])) for b in raw] if raw is not None else []

def h4_bias(symbol, ts):
    """UP/DOWN/?: last fully-closed H4 bar close vs the one before it."""
    t = ts.timestamp()
    arr = h4.get(symbol) or []
    # bars with time <= t are closed at t (H4 bar stamps at open)
    idx = None
    for i in range(len(arr) - 1, -1, -1):
        if arr[i][0] <= t - 14400:   # fully closed before t
            idx = i
            break
    if idx is None or idx < 1:
        return "?"
    return "UP" if arr[idx][1] > arr[idx - 1][1] else "DOWN"

# ---------- collect deduped LLM-layer outcomes ----------
events = []
seen = set()
for p in [ROOT / "storage/logs/audit.jsonl"]:
    if not p.exists():
        continue
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"TRADE_REJECTED"' not in line and '"DRY_RUN_TRADE"' not in line:
            continue
        if line in seen:
            continue
        seen.add(line)
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = r.get("ts", "")
        if not ("2026-09-14" <= ts[:10] <= "2026-09-22"):
            continue
        d = r.get("data") or {}
        events.append({"ts": datetime.fromisoformat(ts), "ev": r.get("event"), **d})

# dedupe to distinct setups
setups, sseen = [], set()
for e in sorted(events, key=lambda x: x["ts"]):
    bar = e["ts"].replace(minute=(e["ts"].minute // 15) * 15, second=0, microsecond=0)
    key = (e.get("symbol"), e.get("side"), bar)
    if key in sseen:
        continue
    sseen.add(key)
    e["bar"] = bar
    setups.append(e)

pipe = defaultdict(Counter)      # (align, side) -> outcome
opposed_allow_confs, aligned_allow_confs = [], []
for e in setups:
    sym, side, reason = e.get("symbol", "?"), e.get("side", "?"), e.get("reason", "")
    bias = h4_bias(sym, e["ts"])
    align = ("opposed" if (side == "BUY" and bias == "DOWN") or (side == "SELL" and bias == "UP")
             else "aligned_or_neutral")
    if e.get("ev") == "DRY_RUN_TRADE":
        outcome = "ADOPTED"
    elif reason.startswith("LLM_REFUSE"):
        outcome = "REFUSE"
    elif reason.startswith("LOW_CONFIDENCE"):
        m = re.search(r"\(([\d.]+) < ([\d.]+)\)", reason)
        conf, thr = (float(m.group(1)), float(m.group(2))) if m else (None, None)
        outcome = "KILLED_080" if thr and thr >= 0.75 else "KILLED_070"
        (opposed_allow_confs if align == "opposed" else aligned_allow_confs).append(conf)
    elif reason.startswith("LLM_UNAVAILABLE"):
        outcome = "UNAVAILABLE"
    else:
        outcome = "OTHER"
    pipe[(align, side)][outcome] += 1

print("=== (1) LLM-layer pipeline by alignment (deduped setups, 9/14-9/22) ===")
for (align, side), c in sorted(pipe.items()):
    print(f"{align:<20}{side:<5} {dict(c)}")

def hist(xs, lo=0.50, hi=0.90, step=0.05):
    if not xs:
        return "(empty)"
    out = []
    x = lo
    while x < hi - 1e-9:
        n = sum(1 for v in xs if v is not None and x <= v < x + step)
        if n:
            out.append(f"{x:.2f}-{x+step:.2f}:{n}")
        x += step
    xs2 = [v for v in xs if v is not None]
    return f"n={len(xs2)} min={min(xs2):.2f} max={max(xs2):.2f}  " + " ".join(out)

print("\nopposed ALLOW confs (killed by floors):")
print("  ", hist(opposed_allow_confs))
print("aligned/neutral ALLOW confs (killed by 0.70 floor):")
print("  ", hist(aligned_allow_confs))

# ---------- (2) H4 daily regime ----------
print("\n=== (2) H4 daily regime per symbol (|day move| / H4-ATR) ===")
print("S=strong(>=2.0)  M=mild(1.0-2.0)  R=range(<1.0)")
hdr = f"{'day':<12}" + "".join(f"{s[:-1]:>10}" for s in SYMBOLS)
print(hdr)
regime_count = Counter()
for day in range(14, 23):
    d0 = datetime(2026, 9, day, tzinfo=timezone.utc)
    if d0.weekday() >= 5:
        continue
    row = f"{d0:%m-%d}       "
    for s in SYMBOLS:
        arr = h4.get(s) or []
        day_bars = [(t, c) for t, c in arr
                    if d0.timestamp() <= t < (d0 + timedelta(days=1)).timestamp()]
        if len(day_bars) < 4:
            row += f"{'-':>10}"
            continue
        closes = np.array([c for _, c in day_bars])
        move = closes[-1] - closes[0]
        trs = np.abs(np.diff(closes))
        atr = float(trs.mean()) or 1e-9
        ratio = abs(move) / atr
        tag = "S" if ratio >= 2.0 else ("M" if ratio >= 1.0 else "R")
        regime_count[(s[:-1], tag)] += 1
        row += f"{tag+f'({ratio:.1f})':>10}"
    print(row)
print("\nsummary (symbol: S/M/R days):")
for s in SYMBOLS:
    k = s[:-1]
    print(f"  {k:<8} S={regime_count[(k,'S')]} M={regime_count[(k,'M')]} R={regime_count[(k,'R')]}")

# ---------- (3) the one real trade ----------
print("\n=== (3) 9/14 13:10 EURUSD SELL context ===")
ENTRY, SL = 1.15327, 1.1548175
TP1, TP2, TPF = 1.1517025, 1.14936625, 1.1454725
sl_pips = abs(SL - ENTRY) / 0.0001
print(f"SL dist = {sl_pips:.3f} pips (MIN gate was 15.0; two retries at 14.0/14.5 were rejected)")
b_entry = h4_bias("EURUSD.", datetime(2026, 9, 14, 13, 10, tzinfo=timezone.utc))
b_stop = h4_bias("EURUSD.", datetime(2026, 9, 14, 14, 15, tzinfo=timezone.utc))
print(f"H4 bias at entry 13:10: {b_entry}   at SL hit 14:15: {b_stop}")
print(f"side=SELL -> alignment at entry: {'OPPOSED' if b_entry=='UP' else ('aligned' if b_entry=='DOWN' else 'neutral')}")
print(f"TP ladder: TP1={TP1} ({abs(ENTRY-TP1)/0.0001:.1f}p) TP2={TP2} ({abs(ENTRY-TP2)/0.0001:.1f}p) final={TPF} ({abs(ENTRY-TPF)/0.0001:.1f}p)")

mt5.shutdown()
