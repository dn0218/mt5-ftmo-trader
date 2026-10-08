"""Breakout strategy — H4-filtered 20-bar M15 breakout with pullback entry.

Replaces the SMC sweep+BOS pipeline (STRATEGY=breakout). Rules:

  1. H4 direction: last CLOSED H4 close vs EMA50 of closed H4 bars.
  2. Breakout: M15 close beyond the prior 20-bar high (BUY) / low (SELL).
  3. Pullback confirm: after the breakout bar, wait for either
     (a) a close back within ±0.3*ATR of the broken level (retest), or
     (b) the next bar still closing beyond the level (continuation).
     Entry = that confirming bar's close. No instant breakout entries.
  4. SL: closer of (recent 5-bar extreme) and (entry -/+ 1.5*ATR).
  5. TP ladder: TP1 = 1R, TP2 = 2R, tp_final = 3*ATR backstop for the
     runner (execution fractions stay with the trade manager, untouched).

Module is pure pandas — no MT5, no env reads at import time — so it can
be unit-tested and replayed offline.
"""
from __future__ import annotations

import pandas as pd

BREAKOUT_LOOKBACK = 20        # prior bars defining the level
PULLBACK_ATR = 0.3            # retest band around the level, in ATR
MAX_WAIT_BARS = 12            # breakout goes stale after this many bars
SL_LOOKBACK = 5               # bars for the structural SL extreme
SL_ATR_MULT = 1.5             # ATR SL alternative
TPF_ATR = 3.0                 # runner backstop distance in ATR


def _atr14(df: pd.DataFrame) -> pd.Series:
    """ATR14 aligned to input bars (bar 0 uses its own range)."""
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return pd.Series(tr).rolling(14).mean().reset_index(drop=True)


def h4_direction(h4: pd.DataFrame) -> str:
    """"UP" / "DOWN" / "NEUTRAL" from last closed H4 close vs EMA50."""
    if h4 is None or len(h4) < 55:
        return "NEUTRAL"
    closes = h4["close"].iloc[:-1]        # closed bars only
    if len(closes) < 51:
        return "NEUTRAL"
    ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
    last = closes.iloc[-1]
    if last > ema50:
        return "UP"
    if last < ema50:
        return "DOWN"
    return "NEUTRAL"


def evaluate_breakout(m15: pd.DataFrame, h4: pd.DataFrame) -> dict | None:
    """Return a signal dict or None.

    Signal dict keys (superset of what the engine/tracker need):
      side, entry, sl, tp1, tp2, tp_final, level, atr, score, factors, bar
    """
    # ── Drop the forming (in-progress) M15 bar: its close changes tick by
    # tick, so confirming a pullback on it means acting on an unfinished
    # candle. Signals must come from CLOSED bars only. (Observed live
    # 9/30-10/2: fills whose recorded SL could not be reproduced by replay
    # over closed bars — the confirming "close" was the live price.)
    m15 = m15.iloc[:-1].reset_index(drop=True)

    if m15 is None or len(m15) < BREAKOUT_LOOKBACK + 16:
        return None
    direction = h4_direction(h4)
    if direction == "NEUTRAL":
        return None

    close = m15["close"].values.astype(float)
    high = m15["high"].values.astype(float)
    low = m15["low"].values.astype(float)
    atr = _atr14(m15).values
    n = len(close)
    if n < 2 or pd.isna(atr[-1]) or atr[-1] <= 0:
        return None

    want = "BUY" if direction == "UP" else "SELL"

    # --- find the most recent qualifying breakout bar within MAX_WAIT_BARS ---
    signal = None
    for b in range(n - 2, max(n - 2 - MAX_WAIT_BARS, BREAKOUT_LOOKBACK - 1), -1):
        if pd.isna(atr[b]):
            continue
        level = (high[b - BREAKOUT_LOOKBACK:b].max() if want == "BUY"
                 else low[b - BREAKOUT_LOOKBACK:b].min())
        broke = close[b] > level if want == "BUY" else close[b] < level
        prev_ok = (close[b - 1] <= level if want == "BUY" else close[b - 1] >= level)
        if not (broke and prev_ok):
            continue
        # --- pullback confirmation at bar e > b ---
        for e in range(b + 1, n):
            a = atr[e] if not pd.isna(atr[e]) else atr[b]
            if want == "BUY":
                if close[e] <= level + PULLBACK_ATR * a:      # retest or back-under
                    entry_bar = e
                    break
            else:
                if close[e] >= level - PULLBACK_ATR * a:
                    entry_bar = e
                    break
        else:
            continue                                          # no confirmation yet
        entry = float(close[entry_bar])
        a_e = atr[entry_bar] if not pd.isna(atr[entry_bar]) else atr[b]
        if want == "BUY":
            sl_struct = float(low[entry_bar - SL_LOOKBACK:entry_bar + 1].min())
            sl_atr = entry - SL_ATR_MULT * a_e
            sl = max(sl_struct, sl_atr)                       # closer = higher for BUY
        else:
            sl_struct = float(high[entry_bar - SL_LOOKBACK:entry_bar + 1].max())
            sl_atr = entry + SL_ATR_MULT * a_e
            sl = min(sl_struct, sl_atr)                       # closer = lower for SELL
        risk = abs(entry - sl)
        if risk <= 0 or a_e <= 0:
            return None
        sign = 1 if want == "BUY" else -1
        tp1 = entry + sign * 1.0 * risk
        tp2 = entry + sign * 2.0 * risk
        tp_final = entry + sign * TPF_ATR * a_e
        # backstop must be at least beyond TP2 to make sense for the runner
        if abs(tp_final - entry) < 2.0 * risk:
            tp_final = entry + sign * 2.05 * risk

        factors = ["H4_TREND_FILTER", "BREAKOUT_20BAR", "PULLBACK_CONFIRM"]
        score = 3
        age = entry_bar - b
        if age <= 2:
            factors.append("FAST_RETEST")
            score = 4
        # confluence-style score stays in the 2..4 band; LLM layer (if on)
        # applies its own floor independently.
        signal = {
            "side": want, "entry": entry, "sl": float(sl),
            "tp1": float(tp1), "tp2": float(tp2), "tp_final": float(tp_final),
            "level": float(level), "atr": float(a_e),
            "score": score, "factors": factors, "bar": int(entry_bar),
            "breakout_bar": int(b),
        }
        break
    return signal


def spread_ok(symbol: str, spread_points: float, max_points: float) -> bool:
    """Spread filter (points). Caller supplies values from tick/symbol info."""
    return spread_points <= max_points
