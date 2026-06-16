"""Build compact evidence payload for LLM consumption."""

from __future__ import annotations
import os
import pandas as pd
from core.detection.structure import detect_swing_points, detect_bos, detect_choch
from core.detection.liquidity import detect_sweeps
from core.detection.orderblocks import detect_orderblocks
from core.detection.fvg import detect_fvg
from core.detection.regime import detect_regime
from core.indicators.indicators import atr, rsi, add_all_indicators


def _forward_targets(price: float, swings: list, obs: list, fvgs: list) -> dict:
    """Forward-looking exit candidates for both sides.

    For BUY runs: opposing swing highs ABOVE price, bearish OBs above, bullish FVGs above.
    For SELL runs: opposing swing lows BELOW price, bullish OBs below, bearish FVGs below.
    """
    up_swings = sorted(
        [s["price"] for s in swings if s.get("type") == "SWING_HIGH" and s.get("price", 0) > price]
    )[:3]
    down_swings = sorted(
        [s["price"] for s in swings if s.get("type") == "SWING_LOW" and s.get("price", 0) < price],
        reverse=True,
    )[:3]
    up_obs = sorted(
        [o["bottom"] for o in obs if o.get("type") == "bearish" and o.get("bottom", 0) > price]
    )[:3]
    down_obs = sorted(
        [o["top"] for o in obs if o.get("type") == "bullish" and o.get("top", 0) < price],
        reverse=True,
    )[:3]
    up_fvgs = sorted(
        [(f["bottom"] + f["top"]) / 2 for f in fvgs if f.get("type") == "BULLISH" and f.get("bottom", 0) > price]
    )[:3]
    down_fvgs = sorted(
        [(f["bottom"] + f["top"]) / 2 for f in fvgs if f.get("type") == "BEARISH" and f.get("top", 0) < price],
        reverse=True,
    )[:3]
    return {
        "up": {"swings": up_swings, "obs": up_obs, "fvgs": up_fvgs},
        "down": {"swings": down_swings, "obs": down_obs, "fvgs": down_fvgs},
    }


def build_evidence(df: pd.DataFrame, symbol: str) -> dict:
    """Aggregate all signals into compact JSON for LLM.

    Token budget: ~3000 chars max. Cap events at EVIDENCE_EVENTS_LIMIT.
    """
    events_limit = int(os.getenv("EVIDENCE_EVENTS_LIMIT", "15"))
    candles_limit = int(os.getenv("EVIDENCE_CANDLES_LIMIT", "48"))

    # Add indicators
    df = add_all_indicators(df.copy())

    atr_series = df["atr14"]
    rsi_val = df["rsi14"].iloc[-1] if "rsi14" in df.columns else 50.0

    # Detect structures
    swings = detect_swing_points(df)
    bos_list = detect_bos(swings)
    choch_list = detect_choch(swings)
    sweeps = detect_sweeps(df, swings, atr_series)
    obs = detect_orderblocks(df, atr_series)
    fvgs = detect_fvg(df, atr_series)
    regime = detect_regime(df)

    # Recent candle data (compact)
    recent = df.tail(candles_limit)
    candles = []
    for _, row in recent.iterrows():
        candles.append({
            "t": str(row["time"])[:16] if "time" in row.index else "",
            "o": round(float(row["open"]), 5),
            "h": round(float(row["high"]), 5),
            "l": round(float(row["low"]), 5),
            "c": round(float(row["close"]), 5),
        })

    evidence = {
        "symbol": symbol,
        "regime": regime,
        "current_price": round(float(df["close"].iloc[-1]), 5),
        "atr14": round(float(atr_series.iloc[-1]), 6) if not pd.isna(atr_series.iloc[-1]) else 0.0,
        "indicators": {
            "rsi14": round(float(rsi_val), 2) if not pd.isna(rsi_val) else 50.0,
            "ema9": round(float(df["ema9"].iloc[-1]), 5) if "ema9" in df.columns else 0.0,
            "ema21": round(float(df["ema21"].iloc[-1]), 5) if "ema21" in df.columns else 0.0,
            "ema50": round(float(df["ema50"].iloc[-1]), 5) if "ema50" in df.columns else 0.0,
        },
        "structure": {
            "swings": swings[-events_limit:],
            "bos": bos_list[-events_limit:],
            "choch": choch_list[-events_limit:],
        },
        "liquidity": {
            "sweeps": sweeps[-events_limit:],
        },
        "orderblocks": obs[-events_limit:],
        "fvg": fvgs[-events_limit:],
        "candles": candles[-candles_limit:],
        "targets": _forward_targets(
            float(df["close"].iloc[-1]), swings, obs, fvgs,
        ),
    }

    return evidence
