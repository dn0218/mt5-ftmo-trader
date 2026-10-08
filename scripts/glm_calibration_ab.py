"""GLM confidence calibration A/B v2: glm-5.3 vs glm-5.3-flash.

Stratified sample of 24 rejected setups (balanced win/lose verdicts from the
MT5 2R/6h replay used in the b2 pack). Same confidence anchors as the
production prompt (0.9 excellent / 0.8 good / 0.7 acceptable / <0.7 weak).
READ-ONLY: never touches engine, audit log, or gates. ~48 relay calls.
"""
from __future__ import annotations
import os
import sys
import json
import time
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import httpx
import numpy as np
import MetaTrader5 as mt5

BASE = os.environ["OPENAI_BASE_URL"].rstrip("/")
KEY = os.environ["OPENAI_API_KEY"]
MODELS = ["glm-5.3", "glm-5.3-flash"]
ART = ROOT / "scripts" / "glm_calibration_ab.json"

events = []
for line in (ROOT / "storage/logs/audit.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
    if '"TRADE_REJECTED"' not in line:
        continue
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    ts = r.get("ts", "")
    if "2026-09-14" <= ts[:10] <= "2026-09-22":
        events.append((ts, r.get("data") or {}))

setups, seen = [], set()
for ts, d in sorted(events):
    t = datetime.fromisoformat(ts)
    bar = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
    key = (d.get("symbol"), d.get("side"), bar)
    if key in seen or not d.get("symbol") or not d.get("side"):
        continue
    seen.add(key)
    setups.append({"ts": t, "symbol": d["symbol"], "side": d["side"], "reason": d.get("reason", "")})
print(f"distinct rejected setups: {len(setups)}", flush=True)

if not mt5.initialize():
    sys.exit("mt5 init failed")
if not mt5.login(int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"],
                 server=os.environ["MT5_SERVER"]):
    mt5.shutdown()
    sys.exit("mt5 login failed")

def replay(s):
    raw = mt5.copy_rates_range(s["symbol"], mt5.TIMEFRAME_M15,
                               s["ts"] - timedelta(hours=12), s["ts"])
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
    sl, tp = 2 * atr, 4 * atr
    bars = mt5.copy_rates_range(s["symbol"], mt5.TIMEFRAME_M15,
                                s["ts"], s["ts"] + timedelta(hours=6))
    if bars is None or len(bars) < 4:
        return None
    if s["side"] == "BUY":
        slp, tpp = e - sl, e + tp
        i_sl = next((i for i, b in enumerate(bars) if float(b["low"]) <= slp), None)
        i_tp = next((i for i, b in enumerate(bars) if float(b["high"]) >= tpp), None)
    else:
        slp, tpp = e + sl, e - tp
        i_sl = next((i for i, b in enumerate(bars) if float(b["high"]) >= slp), None)
        i_tp = next((i for i, b in enumerate(bars) if float(b["low"]) <= tpp), None)
    if i_tp is not None and (i_sl is None or i_tp < i_sl):
        return "win"
    if i_sl is not None and (i_tp is None or i_sl < i_tp):
        return "lose"
    return None

for s in setups:
    s["verdict"] = replay(s)
scored = [s for s in setups if s["verdict"]]
wins = [s for s in scored if s["verdict"] == "win"]
loses = [s for s in scored if s["verdict"] == "lose"]
print(f"replay-able {len(scored)}: win={len(wins)} lose={len(loses)}", flush=True)

def pick(pool, n, tag):
    """spread across symbols/days, deterministic."""
    by_sym = defaultdict(list)
    for s in pool:
        by_sym[s["symbol"]].append(s)
    out, i = [], 0
    while len(out) < min(n, len(pool)):
        for sym in sorted(by_sym):
            if len(out) >= min(n, len(pool)):
                break
            q = by_sym[sym]
            if i < len(q):
                out.append(q[i])
        i += 1
        if i > 200:
            break
    return out

SAMPLE = pick(wins, 12, "win") + pick(loses, 12, "lose")
print(f"sample: {len(SAMPLE)} ({sum(1 for s in SAMPLE if s['verdict']=='win')} win / "
      f"{sum(1 for s in SAMPLE if s['verdict']=='lose')} lose)", flush=True)

PROMPT = """You are evaluating a trade setup for an FTMO-style prop account.
Based only on the evidence below, output STRICT JSON (no prose):
{{"confidence": <float 0.0-1.0>, "decision": "allow"|"refuse"}}
Confidence guide (same as production): 0.9+ = excellent, 0.8 = good, 0.7 = acceptable, below 0.7 = weak.

EVIDENCE:
Symbol {symbol} {side} @ {ts} UTC
Recorded rejection context: {reason}"""

results = defaultdict(list)
for model in MODELS:
    print(f"\n--- {model} ---", flush=True)
    for i, s in enumerate(SAMPLE):
        row = {"ts": s["ts"].isoformat(), "symbol": s["symbol"], "side": s["side"],
               "truth": s["verdict"]}
        try:
            r = httpx.post(f"{BASE}/chat/completions",
                           headers={"Authorization": f"Bearer {KEY}"},
                           json={"model": model,
                                 "messages": [{"role": "user", "content": PROMPT.format(
                                     symbol=s["symbol"], side=s["side"],
                                     ts=f"{s['ts']:%Y-%m-%d %H:%M}", reason=s["reason"])}],
                                 "max_tokens": 2000, "temperature": 0, "think": False},
                           timeout=60)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            content = (msg.get("content") or msg.get("thinking")
                       or msg.get("reasoning_content") or "").strip()
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            m = re.search(r"\{.*\}", content, flags=re.DOTALL)
            obj = json.loads(m.group(0)) if m else {}
            conf = float(obj.get("confidence"))
            row.update(conf=conf, decision=obj.get("decision"))
        except Exception as ex:
            row.update(conf=None, error=str(ex)[:100])
        results[model].append(row)
        ART.write_text(json.dumps(dict(results), indent=1, ensure_ascii=False), encoding="utf-8")
        got = row.get("conf")
        print(f"  {i+1}/{len(SAMPLE)} {s['symbol']:<9}{s['side']:<5}truth={row['truth']:<5}"
              f"conf={got if got is not None else 'ERR'}", flush=True)

def auc(wc, lc):
    pairs = sorted([(c, 1) for c in wc] + [(c, 0) for c in lc])
    confs = [p[0] for p in pairs]
    ranks, i = [], 0
    while i < len(confs):
        j = i
        while j + 1 < len(confs) and confs[j + 1] == confs[i]:
            j += 1
        avg = (i + j) / 2 + 1
        ranks.extend([avg] * (j - i + 1))
        i = j + 1
    n1 = sum(p[1] for p in pairs)
    n0 = len(pairs) - n1
    if not n1 or not n0:
        return None
    sr = sum(r for r, p in zip(ranks, pairs) if p[1] == 1)
    return (sr - n1 * (n1 + 1) / 2) / (n1 * n0)

print("\n=== RESULTS ===", flush=True)
for model, rows in results.items():
    ok = [r for r in rows if r.get("conf") is not None]
    wc = [r["conf"] for r in ok if r["truth"] == "win"]
    lc = [r["conf"] for r in ok if r["truth"] == "lose"]
    print(f"\n{model}: ok={len(ok)}/{len(rows)}")
    if ok:
        cs = [r["conf"] for r in ok]
        print(f"  conf min/med/max = {min(cs):.2f}/{sorted(cs)[len(cs)//2]:.2f}/{max(cs):.2f}")
        if wc:
            print(f"  wins  n={len(wc)}  mean={np.mean(wc):.2f}")
        if lc:
            print(f"  loses n={len(lc)} mean={np.mean(lc):.2f}")
        a = auc(wc, lc) if wc and lc else None
        print(f"  AUC = {a:.3f}" if a is not None else "  AUC = n/a")
        for lo in (0.7, 0.8):
            sub = [r for r in ok if r["conf"] >= lo]
            if sub:
                wr = sum(1 for r in sub if r["truth"] == "win") / len(sub)
                print(f"  conf>={lo}: n={len(sub)} win-rate={wr:.0%}")
        allow = [r for r in ok if r.get("decision") == "allow"]
        if allow:
            wr = sum(1 for r in allow if r["truth"] == "win") / len(allow)
            print(f"  decision=allow: n={len(allow)} win-rate={wr:.0%}")
print(f"\nartifact -> {ART}", flush=True)
mt5.shutdown()
