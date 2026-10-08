"""B5: Is the LLM layer worth keeping? Three-way test on real production calls.

Groups (all deduped by symbol+side+15m bar, live audit only, 9/14-9/22):
  REFUSE : TRADE_REJECTED reason startswith LLM_REFUSE   (model said no)
  ALLOW  : LOW_CONFIDENCE + SLTP_INVALID + DRY_RUN_TRADE (model said yes;
           downstream gates may still have killed it - that's fine, we want
           the model's binary call vs outcome)
  RANDOM : 200 random (symbol, side, running-hour) points - methodology baseline

Replay semantics (identical to b2 pack): entry = signal-bar close,
SL = 1 x ATR14(M15), TP = 2 x SL, first touch wins, 6h window.

Also: confidence-binned win rate on the ALLOW group (conf parsed from
LOW_CONFIDENCE reason strings - real production confidences).

Run-day census included (cross-day analyses must verify both days ran).
"""
from __future__ import annotations
import os
import sys
import json
import random
import re
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
    mt5.shutdown()
    sys.exit("login failed")

SYMBOLS = ["EURUSD.", "GBPUSD.", "AUDUSD.", "XAUUSD.", "USDJPY."]

# ---------- run-day census ----------
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
print("=== run-day census (scan hours present) ===", flush=True)
for d in sorted(run_hours):
    print(f"  {d}: {len(run_hours[d])}h  {sorted(run_hours[d])[:3]}...{sorted(run_hours[d])[-1:]}", flush=True)

# ---------- collect groups ----------
refuse, allow = [], []
for line in (ROOT / "storage/logs/audit.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
    need = ('"TRADE_REJECTED"' in line or '"SLTP_INVALID"' in line
            or '"DRY_RUN_TRADE"' in line)
    if not need:
        continue
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    ts = r.get("ts", "")
    if not ("2026-09-14" <= ts[:10] <= "2026-09-22"):
        continue
    d = r.get("data") or {}
    sym, side = d.get("symbol"), d.get("side")
    if not sym or not side:
        continue
    ev, reason = r.get("event"), d.get("reason", "")
    t = datetime.fromisoformat(ts)
    rec = {"ts": t, "symbol": sym, "side": side}
    if ev == "TRADE_REJECTED" and reason.startswith("LLM_REFUSE"):
        rec["group"] = "REFUSE"
    elif ev == "TRADE_REJECTED" and reason.startswith("LOW_CONFIDENCE"):
        m = re.search(r"\(([\d.]+) < ([\d.]+)\)", reason)
        rec.update(group="ALLOW", conf=float(m.group(1)) if m else None)
    elif ev in ("SLTP_INVALID", "DRY_RUN_TRADE"):
        rec.update(group="ALLOW", conf=None)
    else:
        continue
    (refuse if rec["group"] == "REFUSE" else allow).append(rec)

def dedupe(rows):
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: x["ts"]):
        bar = r["ts"].replace(minute=(r["ts"].minute // 15) * 15, second=0, microsecond=0)
        key = (r["symbol"], r["side"], bar)
        if key in seen:
            continue
        seen.add(key)
        r["bar"] = bar
        out.append(r)
    return out

refuse, allow = dedupe(refuse), dedupe(allow)
print(f"\ndistinct setups: REFUSE={len(refuse)} ALLOW={len(allow)}", flush=True)

# ---------- replay ----------
def replay(symbol, side, ts, look_h=6):
    raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M15,
                               ts - timedelta(hours=12), ts)
    if raw is None or len(raw) < 15:
        return None
    trs, prev = [], float(raw[0]["close"])
    for b in raw[1:]:
        h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
        trs.append(max(h - l, abs(h - prev), abs(l - prev)))
        prev = c
    atr = float(np.mean(trs[-14:]))
    if atr <= 0:
        return None
    e = float(raw[-1]["close"])
    sl_d, tp_d = atr, 2 * atr
    bars = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M15, ts, ts + timedelta(hours=look_h))
    if bars is None or len(bars) < 4:
        return None
    if side == "BUY":
        slp, tpp = e - sl_d, e + tp_d
        i_sl = next((i for i, b in enumerate(bars) if float(b["low"]) <= slp), None)
        i_tp = next((i for i, b in enumerate(bars) if float(b["high"]) >= tpp), None)
    else:
        slp, tpp = e + sl_d, e - tp_d
        i_sl = next((i for i, b in enumerate(bars) if float(b["high"]) >= slp), None)
        i_tp = next((i for i, b in enumerate(bars) if float(b["low"]) <= tpp), None)
    if i_tp is not None and (i_sl is None or i_tp < i_sl):
        return "win"
    if i_sl is not None and (i_tp is None or i_sl < i_tp):
        return "lose"
    return "censored"

for r in refuse + allow:
    r["verdict"] = replay(r["symbol"], r["side"], r["ts"])

# ---------- random control ----------
rng = random.Random(42)
slots = []
for d, hours in run_hours.items():
    for h in hours:
        slots.append((d, h))
randoms = []
for _ in range(200):
    d, h = rng.choice(slots)
    ts = datetime.fromisoformat(f"{d}T{h:02d}:{rng.randrange(0, 60, 1):02d}:00+00:00")
    randoms.append({"ts": ts, "symbol": rng.choice(SYMBOLS),
                    "side": rng.choice(["BUY", "SELL"])})
for r in randoms:
    r["verdict"] = replay(r["symbol"], r["side"], r["ts"])

def rate(rows):
    v = [r["verdict"] for r in rows]
    c = Counter(x for x in v if x)
    dec = c["win"] + c["lose"]
    wr = c["win"] / dec if dec else float("nan")
    return f"win={c['win']} lose={c['lose']} censored={c.get('censored', 0) if 'censored' in c else v.count('censored')} -> win-rate={wr:.1%}"

print("\n=== THREE-WAY OUTCOME (same replay semantics) ===", flush=True)
print(f"RANDOM control (n=200): {rate(randoms)}", flush=True)
rf = [r for r in refuse if r["verdict"]]
al = [r for r in allow if r["verdict"]]
print(f"REFUSE (n={len(rf)} replayed of {len(refuse)}): {rate(refuse)}", flush=True)
print(f"ALLOW  (n={len(al)} replayed of {len(allow)}): {rate(allow)}", flush=True)

# ---------- calibration bins on production confidences ----------
print("\n=== confidence-binned win rate (production ALLOW confs, LOW_CONF events) ===", flush=True)
withconf = [r for r in allow if r.get("conf") is not None and r["verdict"] in ("win", "lose")]
bins = [(0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80)]
for lo, hi in bins:
    grp = [r for r in withconf if lo <= r["conf"] < hi]
    if not grp:
        print(f"  [{lo:.2f}-{hi:.2f}): n=0")
        continue
    w = sum(1 for r in grp if r["verdict"] == "win")
    print(f"  [{lo:.2f}-{hi:.2f}): n={len(grp)} win-rate={w/len(grp):.0%}", flush=True)

out = ROOT / "scripts" / "b5_llm_value.json"
out.write_text(json.dumps({
    "refuse": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()} for r in refuse],
    "allow": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()} for r in allow],
    "random_win": sum(1 for r in randoms if r["verdict"] == "win"),
    "random_decided": sum(1 for r in randoms if r["verdict"] in ("win", "lose")),
}, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"\nartifact -> {out}", flush=True)
mt5.shutdown()
