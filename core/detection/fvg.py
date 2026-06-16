"""Fair Value Gap detection with size filter."""

from __future__ import annotations
import pandas as pd
import numpy as np
from core.indicators.indicators import atr


def detect_fvg(df: pd.DataFrame, atr_series: pd.Series | None = None, min_atr_mult: float = 0.5) -> list[dict]:
    """Detect Fair Value Gaps — gaps between candle 1 and candle 3 bodies.

    FVGs smaller than min_atr_mult * ATR14 are filtered out as noise.
    """
    if atr_series is None:
        atr_series = atr(df, 14)

    fvgs = []
    high_vals = df["high"].values
    low_vals = df["low"].values

    for i in range(1, len(df) - 1):
        atr_val = atr_series.iloc[i] if i < len(atr_series) and not np.isnan(atr_series.iloc[i]) else 0
        if atr_val <= 0:
            continue

        # Bullish FVG: candle[i-1].high < candle[i+1].low
        if high_vals[i - 1] < low_vals[i + 1]:
            fvg_size = low_vals[i + 1] - high_vals[i - 1]
            if fvg_size >= min_atr_mult * atr_val:
                fvgs.append({
                    "type": "BULLISH",
                    "bar": i,
                    "top": float(low_vals[i + 1]),
                    "bottom": float(high_vals[i - 1]),
                    "size": float(fvg_size),
                    "time": df["time"].iloc[i] if "time" in df.columns else i,
                })

        # Bearish FVG: candle[i+1].high < candle[i-1].low
        if high_vals[i + 1] < low_vals[i - 1]:
            fvg_size = low_vals[i - 1] - high_vals[i + 1]
            if fvg_size >= min_atr_mult * atr_val:
                fvgs.append({
                    "type": "BEARISH",
                    "bar": i,
                    "top": float(low_vals[i - 1]),
                    "bottom": float(high_vals[i + 1]),
                    "size": float(fvg_size),
                    "time": df["time"].iloc[i] if "time" in df.columns else i,
                })

    return fvgs
