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

Default: replay the author's historical SAMPLES (bare names mapped via
resolve_symbol). --live: replay this bot's own last-48h rejections from
storage/logs/audit.jsonl.
"""
from __future__ import annotations
import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_path = ROOT / ".env"
if env_path.exists():
    # encoding mandatory: Windows default is GBK on zh-CN systems
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
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


def resolve_symbol(name: str) -> str:
    """Map a bare symbol (author's broker) to this broker's suffixed name."""
    if mt5.symbol_info(name) is not None:
        return name
    wanted = name.upper()
    for s in (mt5.symbols_get(wanted + ".*") or []):
        return s.name  # first suffix variant (e.g. 'USDJPY.')
    for s in (mt5.symbols_get() or []):
        if s.name.upper().rstrip(".") == wanted:
            return s.name
    return name  # give up; replay() will report NO_DATA


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


LIVE_RE = re.compile(r"(?:LLM_REFUSE|LOW_CONFIDENCE|HTF_OPPOSED)", re.I)


def load_live_samples() -> list:
    """Pull the last 48h of replayable rejections from audit.jsonl.

    Dedupes by (symbol, side, M15 bar): the engine re-logs the same cached
    rejection every scan (~60s) while the sweep bar is unchanged, so raw
    events overstate signal count ~11x. One replay per cluster.
    """
    from scripts.audit_reader import iter_events
    out, seen = [], set()
    hours = 48
    for i, a in enumerate(sys.argv):
        if a == "--hours" and i + 1 < len(sys.argv):
            hours = int(sys.argv[i + 1])
        if a == "--since" and i + 1 < len(sys.argv):
            pass  # handled by iter_events callers; kept for CLI symmetry
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for rec in iter_events():
        ev = rec.get("event", "")
        if ev not in ("TRADE_REJECTED", "SLTP_INVALID"):
            continue
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        d = rec.get("data", {})
        sym, side = d.get("symbol"), d.get("side")
        if not sym or not side:
            continue  # pre-patch SLTP_INVALID rows lack 'side'
        bar = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
        key = (sym, side, bar)
        if key in seen:
            continue
        seen.add(key)
        reason = (d.get("reason") or "")[:60]
        klass = ("SLTP_INVALID_" if ev == "SLTP_INVALID" else "REJECT_") + sym.rstrip(".")
        out.append((klass, ts, sym, side, reason))
    return out


def summarize(samples: list, verdicts: dict) -> None:
    """Print W/L/F per (symbol, side) and per reason-class — with honest
    sample-size caveats. 48h snapshots are ONE market state; do not treat
    W>>L as proof the filter is good until n is in the hundreds across
    different regimes."""
    from collections import Counter
    per_ss, per_class = Counter(), Counter()
    for s in samples:
        k, _ts, sym, side, _reason = s if len(s) == 5 else (*s, "")
        v = verdicts.get((sym, side, _ts), "?")
        per_ss[(k, v)] += 1
        per_class[v] += 1
    print("\nVERDICT BREAKDOWN (unique signals):")
    print(f"  overall: W:{per_class['WOULD_WIN']} L:{per_class['WOULD_LOSE']} "
          f"F:{per_class['FLAT']} other:{per_class['?']}")
    by_klass = {}
    for (k, v), n in per_ss.items():
        by_klass.setdefault(k, Counter())[v] = n
    for k, c in sorted(by_klass.items()):
        print(f"  {k:<30} W:{c['WOULD_WIN']} L:{c['WOULD_LOSE']} "
              f"F:{c['FLAT']} other:{c['?']}")
    n = sum(per_class.values())
    if n < 200:
        print(f"\n  ⚠ n={n} < 200 and spans ≤48h ≈ one market state — "
              "NOT statistically robust. Track weekly.")


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

samples = [(k, t, s, sd) for (k, t, s, sd, _r) in load_live_samples()] if "--live" in sys.argv else SAMPLES
if not samples:
    print("No replayable rejection events in the last 48h.")
    mt5.shutdown()
    sys.exit(0)

mode = "LIVE (last 48h from audit.jsonl)" if "--live" in sys.argv else "AUTHOR HISTORICAL SAMPLES"
if "--hours" in sys.argv:
    mode += f" window={sys.argv[sys.argv.index('--hours')+1]}h"
print(f"Replaying {len(samples)} samples - {mode}")
print(f"{'CLASS':<35} {'TS':<26} {'SYM':<8} {'SIDE':<5} {'VERDICT':<12} {'H4':<5}  ENTRY -> SL/TP")
print("-" * 130)

class_stats = {}
samples = [(k, t, s, sd) for (k, t, s, sd, _r) in load_live_samples()] if "--live" in sys.argv else SAMPLES
if not samples:
    print("No replayable rejection events in the last 48h.")
    mt5.shutdown()
    sys.exit(0)

_sample_keys = []
class_stats_flat = []
for klass, ts, sym, side in samples:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    r = replay(resolve_symbol(sym), side, ts)
    v = r.get("verdict", "?")
    _sample_keys.append((sym, side, ts))
    class_stats_flat.append(v)
    class_stats.setdefault(klass, []).append(v)
    ts_iso = ts.isoformat()
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

if "--live" in sys.argv:
    vmap = dict(zip(_sample_keys, class_stats_flat))
    summarize(samples, vmap)
print()
print("Interpretation:")
print("  W (WOULD_WIN)  = filter blocked a winner (cost)")
print("  L (WOULD_LOSE) = filter blocked a loser  (correct save)")
print("  F (FLAT)       = no resolution in 6h    (neutral)")

mt5.shutdown()
