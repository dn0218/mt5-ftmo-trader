"""Market regime detection: TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE."""

from __future__ import annotations
import pandas as pd
import numpy as np


def detect_regime(df: pd.DataFrame) -> str:
    """Detect current market regime based on EMA spread and volatility."""
    close = df["close"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    atr_series = (df["high"] - df["low"]).rolling(14).mean()

    atr_val = atr_series.iloc[-1]
    if np.isnan(atr_val) or atr_val <= 0:
        return "RANGING"

    ema_spread = abs(ema20.iloc[-1] - ema50.iloc[-1]) / atr_val

    # Volatility spike check
    recent_atr = atr_series.iloc[-5:].mean()
    base_atr = atr_series.iloc[-50:-5].mean()
    if base_atr > 0 and not np.isnan(base_atr) and recent_atr / base_atr > 2.0:
        return "VOLATILE"

    last = close.iloc[-1]
    above_both = last > ema20.iloc[-1] > ema50.iloc[-1]
    below_both = last < ema20.iloc[-1] < ema50.iloc[-1]

    if ema_spread > 0.5:
        if above_both:
            return "TRENDING_UP"
        if below_both:
            return "TRENDING_DOWN"

    return "RANGING"
