"""Liquidity sweep detection with strong sweep metrics."""

from __future__ import annotations
import pandas as pd
import numpy as np
from core.indicators.indicators import atr


def detect_sweeps(df: pd.DataFrame, swings: list[dict], atr_series: pd.Series | None = None) -> list[dict]:
    """Detect liquidity sweeps: price wicks beyond swing level then closes back."""
    if atr_series is None:
        atr_series = atr(df, 14)

    sweeps = []
    highs_vals = df["high"].values
    lows_vals = df["low"].values
    close_vals = df["close"].values

    swing_highs = [s for s in swings if s["type"] == "SWING_HIGH"]
    swing_lows = [s for s in swings if s["type"] == "SWING_LOW"]

    # Sweep high: price wicks above a previous swing high, then closes below it
    for sh in swing_highs:
        level = sh["price"]
        bar_idx = sh["bar"]
        # Look at bars after this swing
        for i in range(bar_idx + 1, min(bar_idx + 50, len(df))):
            if highs_vals[i] > level and close_vals[i] < level:
                candle_range = highs_vals[i] - lows_vals[i]
                if candle_range <= 0:
                    continue
                rejection_pct = (highs_vals[i] - close_vals[i]) / candle_range * 100
                wick_size = highs_vals[i] - close_vals[i]
                atr_val = atr_series.iloc[i] if i < len(atr_series) and not np.isnan(atr_series.iloc[i]) else 1.0
                wick_atr_ratio = wick_size / atr_val if atr_val > 0 else 0.0
                strong = rejection_pct >= 20.0 and wick_atr_ratio >= 0.3

                sweeps.append({
                    "type": "SWEEP_HIGH",
                    "bar": i,
                    "price": float(highs_vals[i]),
                    "swept_level": level,
                    "rejection_pct": round(rejection_pct, 2),
                    "wick_atr_ratio": round(wick_atr_ratio, 3),
                    "strong": strong,
                    "time": df["time"].iloc[i] if "time" in df.columns else i,
                })
                break  # One sweep per level

    # Sweep low: price wicks below a previous swing low, then closes above it
    for sl in swing_lows:
        level = sl["price"]
        bar_idx = sl["bar"]
        for i in range(bar_idx + 1, min(bar_idx + 50, len(df))):
            if lows_vals[i] < level and close_vals[i] > level:
                candle_range = highs_vals[i] - lows_vals[i]
                if candle_range <= 0:
                    continue
                rejection_pct = (close_vals[i] - lows_vals[i]) / candle_range * 100
                wick_size = close_vals[i] - lows_vals[i]
                atr_val = atr_series.iloc[i] if i < len(atr_series) and not np.isnan(atr_series.iloc[i]) else 1.0
                wick_atr_ratio = wick_size / atr_val if atr_val > 0 else 0.0
                strong = rejection_pct >= 20.0 and wick_atr_ratio >= 0.3

                sweeps.append({
                    "type": "SWEEP_LOW",
                    "bar": i,
                    "price": float(lows_vals[i]),
                    "swept_level": level,
                    "rejection_pct": round(rejection_pct, 2),
                    "wick_atr_ratio": round(wick_atr_ratio, 3),
                    "strong": strong,
                    "time": df["time"].iloc[i] if "time" in df.columns else i,
                })
                break

    return sorted(sweeps, key=lambda s: s["bar"])
