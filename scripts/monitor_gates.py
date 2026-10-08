"""Gate-rejection telemetry: spread percentiles + SL window hits.

Reads storage/logs/audit.jsonl and aggregates:
  - SCAN_BLOCKED / SPREAD_TOO_HIGH  -> observed spread samples per symbol
    (percentiles p50/p90/max, suggested MAX_SPREAD_POINTS_*)
  - SLTP_INVALID / SL_TOO_TIGHT|WIDE -> which side of the SL window binds
Also compares live ATR14(M15) against each symbol's MIN/MAX_SL_PIPS window.

Run during/after London session:  python scripts/monitor_gates.py
"""
from __future__ import annotations
import json
import math
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_path = ROOT / ".env"
if env_path.exists():
    # encoding mandatory: Windows default is GBK on zh-CN systems
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from scripts.audit_reader import iter_events, parse_since

AUDIT = ROOT / "storage" / "logs" / "audit.jsonl"  # legacy path (fallback)

SPREAD_RE = re.compile(r"SPREAD_TOO_HIGH \(([\d.]+) >")
SL_RE = re.compile(r"SL_TOO_(TIGHT|WIDE) \(([\d.]+) pips")


def pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(p * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def main() -> None:
    spreads: dict[str, list[float]] = {}
    sl_hits: dict[str, dict[str, list[float]]] = {}

    # merged read: legacy audit.jsonl + rotated ops archives + lifecycle
    # streams (new shape); falls back to the old single file when absent.
    since = parse_since(sys.argv)
    for rec in iter_events(since=since):
            data = rec.get("data", {})
            sym = data.get("symbol", "?")
            reason = data.get("reason", "") or ""

            m = SPREAD_RE.search(reason)
            if m:
                spreads.setdefault(sym, []).append(float(m.group(1)))
                continue

            ev = rec.get("event", "")
            if ev == "SLTP_INVALID":
                m = SL_RE.search(reason)
                if m:
                    kind = m.group(1).lower()
                    sl_hits.setdefault(sym, {}).setdefault(kind, []).append(float(m.group(2)))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"=== Gate telemetry  ({now} UTC) ===\n")

    # ── Spread section ──────────────────────────────────────
    if not spreads:
        print("SPREAD: no SPREAD_TOO_HIGH samples yet (either session idle, "
              "or spreads within limit = good)\n")
    else:
        print("SPREAD samples (from SPREAD_TOO_HIGH rejects, points):")
        print(f"  {'symbol':<10} {'n':>4} {'p50':>6} {'p90':>6} {'max':>6}   suggested env")
        for sym in sorted(spreads):
            vals = sorted(spreads[sym])
            p50, p90, mx = pct(vals, .5), pct(vals, .9), vals[-1]
            sug = math.ceil(p90 * 1.1)
            key = sym.upper().replace(".", "").replace("_", "")
            print(f"  {sym:<10} {len(vals):>4} {p50:>6.0f} {p90:>6.0f} {mx:>6.0f}"
                  f"   MAX_SPREAD_POINTS_{key}={sug}")
        print("  (suggested = p90 x 1.1, per user's 'slightly above p90' rule)\n")

    # ── SL window section ───────────────────────────────────
    if sl_hits:
        print("SL WINDOW rejections (pips):")
        for sym in sorted(sl_hits):
            t = sl_hits[sym].get("tight", [])
            w = sl_hits[sym].get("wide", [])
            print(f"  {sym:<10} SL_TOO_TIGHT n={len(t)}"
                  f"{f' median {pct(sorted(t), .5):.0f}' if t else ''}"
                  f"  |  SL_TOO_WIDE n={len(w)}"
                  f"{f' median {pct(sorted(w), .5):.0f}' if w else ''}")
        print("  -> one side dominating means that side of MIN/MAX_SL_PIPS binds;\n"
              "     adjust that side. Both ~0 = window well calibrated.\n")
    else:
        print("SL WINDOW: no SLTP_INVALID samples yet\n")

    # ── Live ATR vs window ──────────────────────────────────
    try:
        import MetaTrader5 as mt5
        import pandas as pd
        if not mt5.initialize():
            return
        if not mt5.login(int(os.getenv("MT5_LOGIN", "0")),
                         password=os.getenv("MT5_PASSWORD", ""),
                         server=os.getenv("MT5_SERVER", "")):
            return
        print("LIVE ATR14(M15) vs SL window (1x and 1.5x ATR):")
        for raw in filter(None, os.getenv("SYMBOLS", "").split(",")):
            sym = raw.strip()
            info = mt5.symbol_info(sym)
            if info is None:
                print(f"  {sym:<10} no symbol_info (check suffix)")
                continue
            mt5.symbol_select(sym, True)
            rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 300)
            if rates is None or len(rates) < 20:
                print(f"  {sym:<10} no bars")
                continue
            df = pd.DataFrame(rates)
            hl = df["high"] - df["low"]
            hc = (df["high"] - df["close"].shift()).abs()
            lc = (df["low"] - df["close"].shift()).abs()
            atr = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean().iloc[-1]

            from core.mt5.symbols import pip_size
            pip = pip_size(sym, info)
            key = sym.upper().replace(".", "").replace("_", "")
            mn = float(os.getenv(f"MIN_SL_PIPS_{key}", os.getenv("MIN_SL_PIPS", "15")))
            mx = float(os.getenv(f"MAX_SL_PIPS_{key}", os.getenv("MAX_SL_PIPS", "150")))
            a1, a15 = atr / pip, 1.5 * atr / pip
            f1 = "OK" if mn <= a1 <= mx else "OUT"
            f15 = "OK" if mn <= a15 <= mx else "OUT"
            print(f"  {sym:<10} ATR={atr:.5f} ({a1:.0f} pips) 1x:{f1:<3} "
                  f" 1.5x={a15:.0f} pips:{f15:<3} window=[{mn:.0f},{mx:.0f}]")
        mt5.shutdown()
    except Exception as e:  # MT5 closed / no env — analytics still valid
        print(f"(live ATR skipped: {e})")


if __name__ == "__main__":
    main()
