"""Swing point detection, BOS (Break of Structure), and CHOCH (Change of Character)."""

from __future__ import annotations
import pandas as pd


def detect_swing_points(df: pd.DataFrame, lookback: int = 5) -> list[dict]:
    """Detect swing highs and swing lows using lookback window."""
    swings = []
    highs = df["high"].values
    lows = df["low"].values

    for i in range(lookback, len(df) - lookback):
        # Swing high: highest in window
        if highs[i] == max(highs[i - lookback : i + lookback + 1]):
            swings.append({
                "type": "SWING_HIGH",
                "bar": i,
                "price": float(highs[i]),
                "time": df["time"].iloc[i] if "time" in df.columns else i,
            })
        # Swing low: lowest in window
        if lows[i] == min(lows[i - lookback : i + lookback + 1]):
            swings.append({
                "type": "SWING_LOW",
                "bar": i,
                "price": float(lows[i]),
                "time": df["time"].iloc[i] if "time" in df.columns else i,
            })

    return sorted(swings, key=lambda s: s["bar"])


def detect_bos(swings: list[dict]) -> list[dict]:
    """Detect Break of Structure — price breaks previous swing in same direction."""
    bos_list = []
    highs = [s for s in swings if s["type"] == "SWING_HIGH"]
    lows = [s for s in swings if s["type"] == "SWING_LOW"]

    # BOS UP: new swing high breaks previous swing high
    for i in range(1, len(highs)):
        if highs[i]["price"] > highs[i - 1]["price"]:
            bos_list.append({
                "type": "BOS",
                "direction": "UP",
                "bar": highs[i]["bar"],
                "price": highs[i]["price"],
                "broken_level": highs[i - 1]["price"],
            })

    # BOS DOWN: new swing low breaks previous swing low
    for i in range(1, len(lows)):
        if lows[i]["price"] < lows[i - 1]["price"]:
            bos_list.append({
                "type": "BOS",
                "direction": "DOWN",
                "bar": lows[i]["bar"],
                "price": lows[i]["price"],
                "broken_level": lows[i - 1]["price"],
            })

    return sorted(bos_list, key=lambda b: b["bar"])


def detect_choch(swings: list[dict]) -> list[dict]:
    """Detect Change of Character — structure breaks in opposite direction."""
    choch_list = []
    highs = [s for s in swings if s["type"] == "SWING_HIGH"]
    lows = [s for s in swings if s["type"] == "SWING_LOW"]

    # CHOCH UP: after series of lower lows, a swing high breaks prev swing high
    # Simplified: swing low makes higher low after lower lows, then high breaks
    for i in range(2, len(highs)):
        # Previous trend was down (lower highs)
        if highs[i - 1]["price"] < highs[i - 2]["price"]:
            # Current breaks above previous (reversal)
            if highs[i]["price"] > highs[i - 1]["price"]:
                choch_list.append({
                    "type": "CHOCH",
                    "direction": "UP",
                    "bar": highs[i]["bar"],
                    "price": highs[i]["price"],
                    "broken_level": highs[i - 1]["price"],
                })

    # CHOCH DOWN: after higher highs, breaks below previous swing low
    for i in range(2, len(lows)):
        if lows[i - 1]["price"] > lows[i - 2]["price"]:
            if lows[i]["price"] < lows[i - 1]["price"]:
                choch_list.append({
                    "type": "CHOCH",
                    "direction": "DOWN",
                    "bar": lows[i]["bar"],
                    "price": lows[i]["price"],
                    "broken_level": lows[i - 1]["price"],
                })

    return sorted(choch_list, key=lambda c: c["bar"])
