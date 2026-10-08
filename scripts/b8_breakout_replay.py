"""B8: replay the breakout strategy over 9/14-9/22 using the b6 ladder.

Walks each symbol's M15 bars bar-by-bar (as the engine would see them),
calls evaluate_breakout on the growing window, and replays accepted
signals with the b6 real-ladder semantics (TP1=1R 50% + BE, TP2=2R 15%,
runner trail 1.5 ATR, 6h window). Reports frequency, meanR, TP1-first,
win rate. One signal per (symbol, entry bar); re-arms after exit window.
"""
from __future__ import annotations
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
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
from core.detection.breakout import evaluate_breakout

if not mt5.initialize():
    sys.exit("mt5 init failed")
if not mt5.login(int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"],
                 server=os.environ["MT5_SERVER"]):
    mt5.shutdown()
    sys.exit("login failed")

SYMBOLS = ["EURUSD.", "GBPUSD.", "AUDUSD.", "XAUUSD.", "USDJPY."]
T0 = datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc)
KILL = [(7 * 60, 9 * 60), (13 * 60, 15 * 60)]   # engine kill zones (UTC)
TRAIL = 1.5
FWD = 24                                        # bars = 6h

def in_kill_zone(ts):
    m = ts.hour * 60 + ts.minute
    return any(a <= m <= b for a, b in KILL)

trades = []
for sym in SYMBOLS:
    m15 = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M15, T0 - timedelta(hours=30), T1)
    h4 = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H4, T0 - timedelta(days=10), T1)
    if m15 is None or h4 is None or len(m15) < 100 or len(h4) < 60:
        print(f"{sym}: insufficient history")
        continue
    mdf = pd.DataFrame(m15)
    hdf = pd.DataFrame(h4)
    h4_times = list(hdf["time"].values)

    i = 80
    cooldown_end = -1
    while i < len(mdf):
        bar_time = mdf["time"].iloc[i]
        bt = datetime.fromtimestamp(int(bar_time), tz=timezone.utc)
        if bt < T0:
            i += 1
            continue
        if i <= cooldown_end or not in_kill_zone(bt):
            i += 1
            continue
        window = mdf.iloc[: i + 1]
        h4_mask = [k for k, t in enumerate(h4_times) if t < bar_time]
        h4_closed = hdf.iloc[h4_mask] if h4_mask else None
        sig = evaluate_breakout(window, h4_closed)
        if sig is None or sig["bar"] != len(window) - 1:
            i += 1
            continue

        # ---- replay ladder from entry bar forward (b6 semantics) ----
        entry, sl = sig["entry"], sig["sl"]
        sign = 1 if sig["side"] == "BUY" else -1
        risk = abs(entry - sl)
        atr = sig["atr"]
        tp1, tp2 = sig["tp1"], sig["tp2"]
        trail = None
        tp1_done = tp2_done = False
        r = exit_type = None
        for j in range(i, min(i + FWD, len(mdf))):
            hi = float(mdf["high"].iloc[j]); lo = float(mdf["low"].iloc[j])
            stop = trail if (tp2_done and trail is not None) else (entry if tp1_done else sl)
            stop_hit = lo <= stop if sign == 1 else hi >= stop
            if stop_hit:
                if not tp1_done:
                    r, exit_type = -1.0, "SL"
                elif not tp2_done:
                    r, exit_type = 0.5, "BE"
                else:
                    er = sign * (stop - entry) / risk
                    r = 0.5 * 1.0 + 0.15 * 2.0 + 0.35 * er
                    exit_type = "TRAIL"
                break
            if not tp1_done and ((sign == 1 and hi >= tp1) or (sign == -1 and lo <= tp1)):
                tp1_done = True
            if tp1_done and not tp2_done and ((sign == 1 and hi >= tp2) or (sign == -1 and lo <= tp2)):
                tp2_done = True
            if tp2_done:
                cl = float(mdf["close"].iloc[j])
                cand = cl - sign * TRAIL * atr
                trail = cand if trail is None else (max(trail, cand) if sign == 1 else min(trail, cand))
        if r is None:
            cl = float(mdf["close"].iloc[min(i + FWD, len(mdf)) - 1])
            mtm = sign * (cl - entry) / risk
            r = (0.5 + 0.15 * 2.0 + 0.35 * mtm) if tp2_done else ((0.5 + 0.5 * mtm) if tp1_done else mtm)
            exit_type = "OPEN@6h"
        trades.append({"symbol": sym, "side": sig["side"], "ts": bt.isoformat(),
                       "r": round(r, 3), "exit": exit_type, "score": sig["score"]})
        cooldown_end = i + 4          # 1h cooldown per symbol after a signal
        i += 1

print(f"\nsignals: {len(trades)} over 6 running days "
      f"= {len(trades)/6:.1f}/day", flush=True)
if trades:
    rs = [t["r"] for t in trades]
    decided = [t for t in trades if t["exit"] != "OPEN@6h"]
    wins = sum(1 for t in decided if t["r"] > 0)
    tp1first = sum(1 for t in trades if t["exit"] in ("BE", "TRAIL", "OPEN@6h") )
    tp1_rate = sum(1 for t in trades if t["exit"] in ("BE", "TRAIL")) + sum(
        1 for t in trades if t["exit"] == "OPEN@6h")
    print(f"meanR = {np.mean(rs):+.3f}   medianR = {np.median(rs):+.3f}")
    print(f"win rate (decided) = {wins}/{len(decided)} = {wins/len(decided):.0%}" if decided else "no decided")
    print(f"TP1-first rate = {tp1first/len(trades):.0%}")
    print(f"exits: {dict(Counter(t['exit'] for t in trades))}")
    print(f"by symbol: {dict(Counter(t['symbol'] for t in trades))}")
    print(f"by side: {dict(Counter(t['side'] for t in trades))}")
    bad = sorted(trades, key=lambda t: t["r"])[:5]
    print("worst 5:", [(t['symbol'], t['side'], t['r']) for t in bad])

out = ROOT / "scripts" / "b8_breakout_replay.json"
out.write_text(json.dumps(trades, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"artifact -> {out}", flush=True)
mt5.shutdown()
