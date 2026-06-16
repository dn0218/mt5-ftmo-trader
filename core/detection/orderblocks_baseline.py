"""Supply/demand zone detection (baseline order blocks)."""

from __future__ import annotations
import pandas as pd
import numpy as np


def detect_supply_demand_zones(df: pd.DataFrame, min_move_pct: float = 0.3) -> list[dict]:
    """Detect supply and demand zones based on strong directional moves."""
    zones = []
    close_vals = df["close"].values
    open_vals = df["open"].values
    high_vals = df["high"].values
    low_vals = df["low"].values

    for i in range(1, len(df) - 2):
        # Look for a consolidation candle followed by a strong move
        body = abs(close_vals[i] - open_vals[i])
        candle_range = high_vals[i] - low_vals[i]
        if candle_range <= 0:
            continue

        # Next candle strong move
        next_body = abs(close_vals[i + 1] - open_vals[i + 1])
        if next_body < candle_range * 1.5:
            continue

        # Demand zone (bullish)
        if close_vals[i + 1] > open_vals[i + 1]:
            zones.append({
                "type": "DEMAND",
                "bar": i,
                "top": float(high_vals[i]),
                "bottom": float(low_vals[i]),
                "strength": round(next_body / candle_range, 2),
            })

        # Supply zone (bearish)
        if close_vals[i + 1] < open_vals[i + 1]:
            zones.append({
                "type": "SUPPLY",
                "bar": i,
                "top": float(high_vals[i]),
                "bottom": float(low_vals[i]),
                "strength": round(next_body / candle_range, 2),
            })

    return zones
