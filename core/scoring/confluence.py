"""Confluence scoring: 0-5 base score with optional sweep-age decay.

A fresh sweep (≤5 bars) gets +1 because rejections close to the wick are
materially higher quality than stale ones. Sweeps >20 bars old subtract 1
because the original liquidity grab is no longer "active" — price has had
many bars to develop new structure that may invalidate the entry.
"""

from __future__ import annotations
from datetime import datetime, timezone


def score_confluence(evidence: dict, side: str, current_price: float,
                     sweep_age_bars: int | None = None) -> tuple[int, list[str]]:
    """Score confluence factors. Returns (score, list_of_factors).

    Score range:
      - Base: 0..5 from the five rule-based factors.
      - +1 if sweep_age_bars ≤ 5 (FRESH_SWEEP factor).
      - −1 if sweep_age_bars > 20 (STALE_SWEEP penalty).
      - Final score is clamped to [0, 5] to keep tiers stable.
    """
    score = 0
    factors = []

    # 1. Strong sweep in trade direction
    sweeps = evidence.get("liquidity", {}).get("sweeps", [])
    want_type = "SWEEP_LOW" if side == "BUY" else "SWEEP_HIGH"
    if any(s.get("strong") and s.get("type") == want_type for s in sweeps):
        score += 1
        factors.append("STRONG_SWEEP")

    # 2. BOS + CHOCH confluence
    bos = evidence.get("structure", {}).get("bos", [])
    choch = evidence.get("structure", {}).get("choch", [])
    direction = "UP" if side == "BUY" else "DOWN"
    has_bos = any(b.get("direction") == direction for b in bos)
    has_choch = any(c.get("direction") == direction for c in choch)
    if has_bos and has_choch:
        score += 1
        factors.append("BOS_CHOCH")

    # 3. OB + FVG overlap
    obs = evidence.get("orderblocks", [])
    fvgs = evidence.get("fvg", [])
    ob_type = "bullish" if side == "BUY" else "bearish"
    fvg_type = "BULLISH" if side == "BUY" else "BEARISH"
    # Use wider range for gold
    price_range = 5.0 if "XAU" in evidence.get("symbol", "") else 0.0030
    ob_near = any(
        abs(ob.get("price", 0) - current_price) < price_range
        for ob in obs if ob.get("type", "").lower() == ob_type
    )
    fvg_near = any(
        fvg.get("bottom", 0) <= current_price <= fvg.get("top", 0)
        for fvg in fvgs if fvg.get("type") == fvg_type
    )
    if ob_near and fvg_near:
        score += 1
        factors.append("OB_FVG_OVERLAP")

    # 4. RSI confirmation
    rsi = evidence.get("indicators", {}).get("rsi14", 50)
    if side == "BUY" and 30 <= rsi <= 45:
        score += 1
        factors.append("RSI_OVERSOLD_RECOVERY")
    elif side == "SELL" and 55 <= rsi <= 70:
        score += 1
        factors.append("RSI_OVERBOUGHT_PULLBACK")

    # 5. Kill zone timing (London open or NY open)
    now = datetime.now(timezone.utc)
    mins = now.hour * 60 + now.minute
    london = 7 * 60 <= mins <= 9 * 60
    ny_open = 13 * 60 <= mins <= 15 * 60
    if london or ny_open:
        score += 1
        factors.append("KILL_ZONE")

    # Sweep age modifier (optional; engine passes this when known).
    if sweep_age_bars is not None:
        if sweep_age_bars <= 5:
            score += 1
            factors.append("FRESH_SWEEP")
        elif sweep_age_bars > 20:
            score -= 1
            factors.append("STALE_SWEEP_PENALTY")

    # Clamp to [0, 5] so tier thresholds stay meaningful.
    score = max(0, min(5, score))
    return score, factors
