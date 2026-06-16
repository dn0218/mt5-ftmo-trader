"""Order block detection — impulse-based."""

from __future__ import annotations
import pandas as pd
import numpy as np
from core.indicators.indicators import atr


def detect_orderblocks(df: pd.DataFrame, atr_series: pd.Series | None = None) -> list[dict]:
    """Detect impulse order blocks — last opposing candle before a strong move."""
    if atr_series is None:
        atr_series = atr(df, 14)

    obs = []
    close_vals = df["close"].values
    open_vals = df["open"].values
    high_vals = df["high"].values
    low_vals = df["low"].values

    for i in range(2, len(df) - 1):
        atr_val = atr_series.iloc[i] if i < len(atr_series) and not np.isnan(atr_series.iloc[i]) else 0
        if atr_val <= 0:
            continue

        # Bullish OB: bearish candle followed by strong bullish impulse (>1.5x ATR)
        current_bearish = close_vals[i] < open_vals[i]
        next_move = close_vals[i + 1] - open_vals[i + 1] if i + 1 < len(df) else 0
        if current_bearish and next_move > 1.5 * atr_val:
            obs.append({
                "type": "bullish",
                "bar": i,
                "price": float(low_vals[i]),
                "top": float(high_vals[i]),
                "bottom": float(low_vals[i]),
                "time": df["time"].iloc[i] if "time" in df.columns else i,
            })

        # Bearish OB: bullish candle followed by strong bearish impulse
        current_bullish = close_vals[i] > open_vals[i]
        if current_bullish and next_move < -1.5 * atr_val:
            obs.append({
                "type": "bearish",
                "bar": i,
                "price": float(high_vals[i]),
                "top": float(high_vals[i]),
                "bottom": float(low_vals[i]),
                "time": df["time"].iloc[i] if "time" in df.columns else i,
            })

    return obs
