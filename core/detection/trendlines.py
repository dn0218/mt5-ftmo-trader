"""Trendline detection from swing points."""

from __future__ import annotations


def detect_trendlines(swings: list[dict]) -> list[dict]:
    """Detect basic trendlines connecting swing points."""
    trendlines = []
    highs = [s for s in swings if s["type"] == "SWING_HIGH"]
    lows = [s for s in swings if s["type"] == "SWING_LOW"]

    # Descending trendline from swing highs
    for i in range(len(highs) - 1):
        if highs[i + 1]["price"] < highs[i]["price"]:
            slope = (highs[i + 1]["price"] - highs[i]["price"]) / max(1, highs[i + 1]["bar"] - highs[i]["bar"])
            trendlines.append({
                "type": "DESCENDING",
                "start_bar": highs[i]["bar"],
                "end_bar": highs[i + 1]["bar"],
                "start_price": highs[i]["price"],
                "end_price": highs[i + 1]["price"],
                "slope": slope,
            })

    # Ascending trendline from swing lows
    for i in range(len(lows) - 1):
        if lows[i + 1]["price"] > lows[i]["price"]:
            slope = (lows[i + 1]["price"] - lows[i]["price"]) / max(1, lows[i + 1]["bar"] - lows[i]["bar"])
            trendlines.append({
                "type": "ASCENDING",
                "start_bar": lows[i]["bar"],
                "end_bar": lows[i + 1]["bar"],
                "start_price": lows[i]["price"],
                "end_price": lows[i + 1]["price"],
                "slope": slope,
            })

    return trendlines
