"""B7: chase anatomy — WHY does the engine enter at motion-end points?

Three distances per setup (plus prior-runup context), ATR-normalized:
  d_swing : |entry - nearest swing price|          (chase depth, hypothesis A)
  d_bos   : (entry - broken_level) in side dir     (breakout-entry, hypothesis B)
            >0 = beyond broken level (confirmed), <0 = already back below it
  runup5/20: net move in entry direction over prior 5/20 bars (trend-end test)
  bar_range: signal bar range / ATR

Groups: XAU+JPY vs EUR/GBP/AUD | H4 range/mild/strong | winners vs losers,
RANDOM control for reference. Verdict per the decision tree:
  d_bos << d_swing      -> breakout-entry bug (fix: wait for retest)
  d_swing big everywhere -> chase bug (fix: depth cap)
  no separation          -> SMC definition itself (redesign)
"""
from __future__ import annotations
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import MetaTrader5 as mt5
from core.detection.structure import detect_swing_points, detect_bos

if not mt5.initialize():
    sys.exit("mt5 init failed")
if not mt5.login(int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"],
                 server=os.environ["MT5_SERVER"]):
    mt5.shutdown()
    sys.exit("login failed")

rows = json.loads((ROOT / "scripts/b6_real_ladder.json").read_text(encoding="utf-8"))
for r in rows:
    r["ts"] = datetime.fromisoformat(r["ts"])
setups = [r for r in rows if r.get("group") in ("REFUSE", "ALLOW")]
randoms = [r for r in rows if r.get("group") == "RANDOM"]
print(f"engine setups={len(setups)} random={len(randoms)}", flush=True)

def anatomy(sym, side, ts):
    hist = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M15, ts - timedelta(hours=14), ts)
    if hist is None or len(hist) < 30:
        return None
    df = pd.DataFrame(hist)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    o = df["open"].values.astype(float)
    trs = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    atr = float(np.mean(trs[-14:]))
    if atr <= 0:
        return None
    entry = float(c[-1])
    sign = 1 if side == "BUY" else -1

    swings = detect_swing_points(df, lookback=5)
    if not swings:
        return None
    d_swing = min(abs(entry - s["price"]) for s in swings) / atr

    bos = detect_bos(swings)
    want = "UP" if side == "BUY" else "DOWN"
    same = [b for b in bos if b["direction"] == want and b["bar"] <= len(df) - 1]
    d_bos = None
    bos_age = None
    if same:
        b = max(same, key=lambda x: x["bar"])
        d_bos = sign * (entry - b["broken_level"]) / atr   # >0 beyond, <0 behind
        bos_age = (len(df) - 1) - b["bar"]

    runup5 = sign * (entry - float(c[-6])) / atr if len(c) >= 6 else None
    runup20 = sign * (entry - float(c[-21])) / atr if len(c) >= 21 else None
    bar_range = (h[-1] - l[-1]) / atr
    return {"d_swing": round(d_swing, 2), "d_bos": (round(d_bos, 2) if d_bos is not None else None),
            "bos_age": bos_age, "runup5": (round(runup5, 2) if runup5 is not None else None),
            "runup20": (round(runup20, 2) if runup20 is not None else None),
            "bar_range": round(bar_range, 2)}

for r in setups + randoms:
    r["anat"] = anatomy(r["symbol"], r["side"], r["ts"])
print("anatomy done", flush=True)

def med(rows, key, pred=lambda r: True):
    vals = [r["anat"][key] for r in rows
            if r.get("anat") and r["anat"].get(key) is not None and pred(r)]
    return f"p50={np.median(vals):+.2f}" if vals else "-"

def line(rows, label):
    print(f"{label:<26} n={sum(1 for r in rows if r.get('anat')):<4} "
          f"d_swing {med(rows,'d_swing'):<12} d_bos {med(rows,'d_bos'):<12} "
          f"age {med(rows,'bos_age'):<9} runup5 {med(rows,'runup5'):<12} "
          f"runup20 {med(rows,'runup20'):<12} bar {med(rows,'bar_range')}", flush=True)

W = lambda r: r.get("res") and r["res"]["r"] > 0
L = lambda r: r.get("res") and r["res"]["r"] <= 0
XJ = lambda r: r["symbol"] in ("XAUUSD.", "USDJPY.")
EGA = lambda r: r["symbol"] not in ("XAUUSD.", "USDJPY.")

print("\n=== chase anatomy (ATR units) ===", flush=True)
line(setups, "ALL ENGINE")
line([r for r in setups if XJ(r)], "XAU+JPY")
line([r for r in setups if EGA(r)], "EUR/GBP/AUD")
line([r for r in setups if W(r)], "winners")
line([r for r in setups if L(r)], "losers")
line([r for r in setups if XJ(r) and L(r)], "XAU+JPY losers")
line([r for r in setups if EGA(r) and W(r)], "EUR/GBP/AUD winners")
line(randoms, "RANDOM")

print("\n=== H4 regime split ===", flush=True)
for lo, hi, tag in ((0, 1, "range<1"), (1, 2, "mild 1-2"), (2, 99, "strong>=2")):
    grp = [r for r in setups if r.get("h4s") is not None and lo <= r["h4s"] < hi]
    line(grp, f"engine {tag}")

# d_bos sign decomposition — the third state
print("\n=== d_bos sign decomposition (engine) ===", flush=True)
for label, pred in (("beyond broken lvl (d_bos>0)", lambda r: r["anat"]["d_bos"] is not None and r["anat"]["d_bos"] > 0),
                    ("back below lvl (d_bos<0)", lambda r: r["anat"]["d_bos"] is not None and r["anat"]["d_bos"] <= 0),
                    ("no same-dir BOS in window", lambda r: r["anat"]["d_bos"] is None)):
    grp = [r for r in setups if r.get("anat") and pred(r)]
    if not grp:
        continue
    rs = [r["res"]["r"] for r in grp if r.get("res")]
    print(f"{label:<30} n={len(grp):<4} meanR={np.mean(rs):+.3f}" if rs else
          f"{label:<30} n={len(grp)}", flush=True)

out = ROOT / "scripts" / "b7_chase_anatomy.json"
out.write_text(json.dumps([
    {**{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items() if k != "anat"},
     **{"anat": r.get("anat")}} for r in setups + randoms],
    indent=1, ensure_ascii=False), encoding="utf-8")
print(f"\nartifact -> {out}", flush=True)
mt5.shutdown()
