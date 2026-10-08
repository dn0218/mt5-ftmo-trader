"""B6: real-ladder replay — is B5's 6.9% an odds artifact or true anti-selection?

Four verifications in one pass:
  1. Re-run the engine-vs-random comparison using the ENGINE'S REAL exit
     ladder: structural SL (recent swing +/- 0.5*ATR buffer), TP1=1R (50%),
     BE after TP1, TP2=2.5R (15%), runner trails 1.5*ATR / TP-final=5R.
     R accounting uses the A2-corrected fractions (0.5/0.15/0.35).
  2. MAE/MFE distributions (ATR units): XAU/JPY zero-win vs rest;
     winners vs losers.  MAE_med < 1 -> SL too tight; MAE big & MFE small
     -> entry structure wrong.
  3. H4 trend-strength bins (24h |move|/ATR_H4): does the engine lose
     specifically in strong trends (SMC sweep->reversal assumption fails)?
  4. Timing probe: same setups entered at the signal bar OPEN instead of
     its close (15 min earlier) - isolates "entry at extension end".

Setups loaded from b5_llm_value.json (deduped). Random control regenerated
with the same seed/slots as b5. Live audit only. 6h forward window.
"""
from __future__ import annotations
import os
import sys
import json
import random
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

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
from core.detection.structure import detect_swing_points

if not mt5.initialize():
    sys.exit("mt5 init failed")
if not mt5.login(int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"],
                 server=os.environ["MT5_SERVER"]):
    mt5.shutdown()
    sys.exit("login failed")

SYMBOLS = ["EURUSD.", "GBPUSD.", "AUDUSD.", "XAUUSD.", "USDJPY."]
FWD_H = 6
TP1_R, TP2_R, TPF_R = 1.0, 2.5, 5.0     # rr_floor assumption: 2.5 (score-4)
TRAIL_MULT = 1.5

# ---------- load setups + regenerate random control (same as b5) ----------
b5 = json.loads((ROOT / "scripts/b5_llm_value.json").read_text(encoding="utf-8"))
setups = [{"ts": datetime.fromisoformat(r["ts"]), "symbol": r["symbol"],
           "side": r["side"], "group": r["group"]} for r in b5["refuse"] + b5["allow"]]

run_hours = defaultdict(set)
for line in (ROOT / "storage/logs/audit.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
    if '"SCAN_START"' in line:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = r.get("ts", "")
        if "2026-09-14" <= ts[:10] <= "2026-09-22":
            run_hours[ts[:10]].add(int(ts[11:13]))
rng = random.Random(42)
slots = [(d, h) for d, hs in run_hours.items() for h in hs]
randoms = []
for _ in range(200):
    d, h = rng.choice(slots)
    randoms.append({"ts": datetime.fromisoformat(
        f"{d}T{h:02d}:{rng.randrange(0, 60, 1):02d}:00+00:00"),
        "symbol": rng.choice(SYMBOLS), "side": rng.choice(["BUY", "SELL"]),
        "group": "RANDOM"})
print(f"setups={len(setups)} randoms={len(randoms)}", flush=True)

# ---------- H4 strength cache ----------
h4_cache = {}
def h4_strength(sym, ts):
    key = (sym, ts.strftime("%Y%m%d%H"))
    if key in h4_cache:
        return h4_cache[key]
    raw = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H4, ts - timedelta(hours=40), ts)
    if raw is None or len(raw) < 8:
        h4_cache[key] = None
        return None
    closes = np.array([b["close"] for b in raw], dtype=float)
    move = abs(closes[-1] - closes[-7])
    atr = float(np.mean(np.abs(np.diff(closes[-8:])))) or 1e-9
    v = move / atr
    h4_cache[key] = v
    return v

def run_point(sym, side, ts, entry_mode="close"):
    """Real-ladder replay. Returns dict or None (no data)."""
    hist = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M15, ts - timedelta(hours=14), ts)
    if hist is None or len(hist) < 30:
        return None
    df = pd.DataFrame(hist)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    trs = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    atr = float(np.mean(trs[-14:]))
    if atr <= 0:
        return None
    entry = float(c[-1]) if entry_mode == "close" else float(df["open"].values[-1])

    # structural SL (engine logic): most recent swing beyond price +/- 0.5 ATR
    swings = detect_swing_points(df, lookback=5)
    fallback = False
    if side == "SELL":
        cands = [s for s in swings if s["type"] == "SWING_HIGH" and s["price"] > entry]
        if cands:
            pick = sorted(cands, key=lambda s: -s["bar"])[0]
            sl = float(pick["price"]) + 0.5 * atr
        else:
            sl = entry + atr
            fallback = True
    else:
        cands = [s for s in swings if s["type"] == "SWING_LOW" and s["price"] < entry]
        if cands:
            pick = sorted(cands, key=lambda s: -s["bar"])[0]
            sl = float(pick["price"]) - 0.5 * atr
        else:
            sl = entry - atr
            fallback = True
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return None
    sign = 1 if side == "BUY" else -1
    tp1 = entry + sign * TP1_R * sl_dist
    tp2 = entry + sign * TP2_R * sl_dist
    tpf = entry + sign * TPF_R * sl_dist

    fwd = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M15, ts, ts + timedelta(hours=FWD_H))
    if fwd is None or len(fwd) < 3:
        return None
    fh = [float(b["high"]) for b in fwd]
    fl = [float(b["low"]) for b in fwd]
    fc = [float(b["close"]) for b in fwd]

    # MAE/MFE over forward window (ATR units)
    adv = [(entry - x) * sign for x in fl]     # negative = adverse for this side
    fav = [(x - entry) * sign for x in fh]
    mae = max(0.0, -min(adv)) / atr if adv else 0.0
    mfe = max(0.0, max(fav)) / atr if fav else 0.0

    # ladder walk
    be_done = tp1_done = tp2_done = False
    trail = None
    r = None
    exit_type = None
    for i in range(len(fwd)):
        hi, lo, cl = fh[i], fl[i], fc[i]
        stop_now = trail if (tp2_done and trail is not None) else (entry if be_done else sl)
        # stop side touch
        stop_hit = lo <= stop_now if sign == 1 else hi >= stop_now
        if stop_hit:
            if not tp1_done:
                r, exit_type = -1.0, "SL"
            elif not tp2_done:
                r, exit_type = 0.5, "BE"
            else:
                exit_r = sign * (stop_now - entry) / sl_dist
                r = 0.5 * TP1_R + 0.15 * TP2_R + 0.35 * exit_r
                exit_type = "TRAIL"
            break
        # TP final (runner)
        if tp2_done and ((sign == 1 and hi >= tpf) or (sign == -1 and lo <= tpf)):
            r = 0.5 * TP1_R + 0.15 * TP2_R + 0.35 * TPF_R
            exit_type = "TPF"
            break
        # TP2
        if tp1_done and not tp2_done and ((sign == 1 and hi >= tp2) or (sign == -1 and lo <= tp2)):
            tp2_done = True
        # TP1 (+BE same level: BE_TRIGGER_R=1.0)
        if not tp1_done and ((sign == 1 and hi >= tp1) or (sign == -1 and lo <= tp1)):
            tp1_done = True
            be_done = True
        # trail after TP2 (ratchet)
        if tp2_done:
            cand = cl - sign * TRAIL_MULT * atr
            trail = cand if trail is None else (max(trail, cand) if sign == 1 else min(trail, cand))
    else:
        # window ended open -> MTM at last close (censored)
        mtm_r = sign * (fc[-1] - entry) / sl_dist
        if tp2_done:
            r = 0.5 * TP1_R + 0.15 * TP2_R + 0.35 * mtm_r
        elif tp1_done:
            r = 0.5 * TP1_R + 0.5 * mtm_r
        else:
            r = mtm_r
        exit_type = "OPEN@6h"

    return {"r": round(r, 3), "exit": exit_type, "mae": round(mae, 2),
            "mfe": round(mfe, 2), "sl_w_atr": round(sl_dist / atr, 2),
            "fallback": fallback}

# ---------- run all ----------
for pool, tag in ((setups, "ENGINE"), (randoms, "RANDOM")):
    for row in pool:
        row["res"] = run_point(row["symbol"], row["side"], row["ts"])
        row["res_open"] = run_point(row["symbol"], row["side"], row["ts"], entry_mode="open")
        row["h4s"] = h4_strength(row["symbol"], row["ts"])
    print(f"{tag} replayed", flush=True)

def summarize(rows, key="res"):
    ok = [r for r in rows if r.get(key)]
    if not ok:
        return "n=0"
    rs = [r[key]["r"] for r in ok]
    ex = Counter(r[key]["exit"] for r in ok)
    decided = [r for r in ok if r[key]["exit"] in ("SL", "BE", "TRAIL", "TPF")]
    tp1r = sum(1 for r in ok if r[key]["exit"] in ("BE", "TRAIL", "TPF", "OPEN@6h") or r[key]["exit"] == "BE")
    win = sum(1 for r in decided if r[key]["r"] > 0)
    return (f"n={len(ok)} meanR={np.mean(rs):+.3f} medR={np.median(rs):+.3f} "
            f"TP1-first={tp1r/len(ok):.0%} win(decided)={win}/{len(decided)}"
            f" exits={dict(ex.most_common())}")

print("\n=== (1) REAL-LADDER replay ===", flush=True)
print(f"ENGINE: {summarize(setups)}", flush=True)
print(f"RANDOM: {summarize(randoms)}", flush=True)
print(f"ENGINE @bar-OPEN (timing probe): {summarize(setups, 'res_open')}", flush=True)

print("\n=== (2) MAE/MFE (ATR units) ===", flush=True)
def mm(rows, label):
    ok = [r["res"] for r in rows if r.get("res")]
    if not ok:
        return
    mae = sorted(x["mae"] for x in ok)
    mfe = sorted(x["mfe"] for x in ok)
    n = len(ok)
    print(f"{label:<28} n={n:<4} MAE p25/50/75={mae[n//4]:.1f}/{mae[n//2]:.1f}/{mae[3*n//4]:.1f} "
          f"MFE p50={mfe[n//2]:.1f}  MAE<1: {sum(1 for x in mae if x<1)/n:.0%}  MFE>2: {sum(1 for x in mfe if x>2)/n:.0%}")
mm(setups, "ALL ENGINE")
mm([r for r in setups if r["symbol"] in ("XAUUSD.", "USDJPY.")], "XAU+JPY (0-win pair)")
mm([r for r in setups if r["symbol"] not in ("XAUUSD.", "USDJPY.")], "EUR/GBP/AUD")
mm([r for r in setups if r.get("res") and r["res"]["r"] > 0], "winners (R>0)")
mm([r for r in setups if r.get("res") and r["res"]["r"] <= 0], "losers  (R<=0)")
mm(randoms, "RANDOM")

print("\n=== (3) H4 trend-strength bins (24h |move|/ATR_H4) ===", flush=True)
for lo, hi, tag in ((0, 1, "range<1"), (1, 2, "mild 1-2"), (2, 99, "strong>=2")):
    grp = [r for r in setups if r.get("h4s") is not None and lo <= r["h4s"] < hi]
    if grp:
        ok = [r for r in grp if r.get("res")]
        rs = [r["res"]["r"] for r in ok]
        tp1 = sum(1 for r in ok if r["res"]["exit"] in ("BE", "TRAIL", "TPF"))
        print(f"{tag:<11} n={len(ok):<4} meanR={np.mean(rs):+.3f}  TP1-first={tp1/len(ok):.0%}")
    else:
        print(f"{tag:<11} n=0")

print("\n=== (4) entry-structure features: winners vs losers ===", flush=True)
for label, cond in (("winners", lambda r: r["res"]["r"] > 0),
                    ("losers", lambda r: r["res"]["r"] <= 0)):
    grp = [r for r in setups if r.get("res") and cond(r)]
    if not grp:
        continue
    sw = sorted(r["res"]["sl_w_atr"] for r in grp)
    # entry percentile in last-20-bar range recompute cheap proxy: use MFE/MAE ratio
    ratio = sorted(r["res"]["mfe"] / max(r["res"]["mae"], 0.05) for r in grp)
    n = len(grp)
    print(f"{label:<8} n={n:<4} SLwidth/ATR p50={sw[n//2]:.2f}  MFE/MAE p50={ratio[n//2]:.2f}")

fb = sum(1 for r in setups + randoms if r.get("res") and r["res"]["fallback"])
tot = sum(1 for r in setups + randoms if r.get("res"))
print(f"\nstructural-SL fallback used: {fb}/{tot}", flush=True)

out = ROOT / "scripts" / "b6_real_ladder.json"
out.write_text(json.dumps([
    {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}
    for r in setups + randoms], indent=1, ensure_ascii=False), encoding="utf-8")
print(f"artifact -> {out}", flush=True)
mt5.shutdown()
