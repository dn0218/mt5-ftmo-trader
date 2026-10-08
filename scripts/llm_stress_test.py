"""LLM Stress Test — send hundreds of realistic trade setups to Ollama.

Compares LLM output vs expected correct decision, flags mismatches.
Generates a full report at the end.
"""

from __future__ import annotations
import json
import os
import sys
import time
import logging

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.decision.ollama_decider import call_ollama, _extract_json
from services.notifications.telegram import notify

logging.basicConfig(level=logging.WARNING)

# ── Scenario generators ─────────────────────────────────────────────────────

def _base(symbol="EURUSD", regime="TRENDING_UP", price=1.0850, atr=0.0012):
    return {
        "symbol": symbol,
        "regime": regime,
        "current_price": price,
        "atr14": atr,
        "indicators": {},
        "structure": {"bos": [], "choch": []},
        "liquidity": {"sweeps": []},
        "orderblocks": [],
        "fvg": [],
    }


def generate_scenarios() -> list[dict]:
    """Generate hundreds of realistic trade scenarios."""
    scenarios = []

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 1: STRONG VALID SETUPS (should ALLOW with high confidence)
    # ════════════════════════════════════════════════════════════════════════

    # 1. Perfect bullish EURUSD setup
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 38.0, "ema9": 1.0848, "ema21": 1.0840, "ema50": 1.0820}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45, "price": 1.0840}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40, "price": 1.0830}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.30, "wick_atr_ratio": 0.5, "bar": 48}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0842}]
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 1.0855, "bottom": 1.0845}]
    scenarios.append({"name": "Perfect bullish EURUSD — full confluence", "evidence": e, "expected": "ALLOW", "reason": "All 5 confluence factors present, strong trend"})

    # 2. Perfect bearish EURUSD setup
    e = _base("EURUSD", "TRENDING_DOWN", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 62.0, "ema9": 1.0845, "ema21": 1.0860, "ema50": 1.0880}
    e["structure"]["bos"] = [{"type": "BOS_DOWN", "bar": 45, "price": 1.0860}]
    e["structure"]["choch"] = [{"type": "CHOCH_DOWN", "bar": 40, "price": 1.0870}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.28, "wick_atr_ratio": 0.45, "bar": 48}]
    e["orderblocks"] = [{"type": "BEARISH_OB", "price": 1.0858}]
    e["fvg"] = [{"type": "BEARISH_FVG", "top": 1.0860, "bottom": 1.0850}]
    scenarios.append({"name": "Perfect bearish EURUSD — full confluence", "evidence": e, "expected": "ALLOW", "reason": "All factors bearish aligned"})

    # 3. Perfect bullish XAUUSD (gold)
    e = _base("XAUUSD", "TRENDING_UP", 2350.00, 12.5)
    e["indicators"] = {"rsi14": 40.0, "ema9": 2348.0, "ema21": 2340.0, "ema50": 2320.0}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 42, "price": 2340.0}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 38, "price": 2335.0}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.35, "wick_atr_ratio": 0.6, "bar": 47}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 2342.0}]
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 2352.0, "bottom": 2346.0}]
    scenarios.append({"name": "Perfect bullish XAUUSD — gold uptrend", "evidence": e, "expected": "ALLOW", "reason": "Strong gold bullish setup with all confirmations"})

    # 4. Perfect bearish XAUUSD
    e = _base("XAUUSD", "TRENDING_DOWN", 2350.00, 12.5)
    e["indicators"] = {"rsi14": 58.0, "ema9": 2348.0, "ema21": 2360.0, "ema50": 2380.0}
    e["structure"]["bos"] = [{"type": "BOS_DOWN", "bar": 42, "price": 2360.0}]
    e["structure"]["choch"] = [{"type": "CHOCH_DOWN", "bar": 38, "price": 2365.0}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.32, "wick_atr_ratio": 0.5, "bar": 47}]
    e["orderblocks"] = [{"type": "BEARISH_OB", "price": 2358.0}]
    e["fvg"] = [{"type": "BEARISH_FVG", "top": 2355.0, "bottom": 2345.0}]
    scenarios.append({"name": "Perfect bearish XAUUSD — gold downtrend", "evidence": e, "expected": "ALLOW", "reason": "Strong gold bearish setup"})

    # 5-8. Strong setups with 4/5 factors (missing one each)
    for missing in ["no_ob", "no_fvg", "no_rsi_confirm", "no_bos"]:
        e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
        e["indicators"] = {"rsi14": 38.0, "ema9": 1.0848, "ema21": 1.0840}
        e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45, "price": 1.0840}]
        e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40, "price": 1.0830}]
        e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
        e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0842}]
        e["fvg"] = [{"type": "BULLISH_FVG", "top": 1.0855, "bottom": 1.0845}]

        if missing == "no_ob":
            e["orderblocks"] = []
        elif missing == "no_fvg":
            e["fvg"] = []
        elif missing == "no_rsi_confirm":
            e["indicators"]["rsi14"] = 52.0  # neutral RSI
        elif missing == "no_bos":
            e["structure"]["bos"] = []

        scenarios.append({
            "name": f"Bullish EURUSD — 4/5 factors ({missing})",
            "evidence": e, "expected": "ALLOW",
            "reason": f"Still strong setup even without {missing}"
        })

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 2: COUNTER-TREND (should REFUSE)
    # ════════════════════════════════════════════════════════════════════════

    # 9. BUY in strong downtrend
    e = _base("EURUSD", "TRENDING_DOWN", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 25.0, "ema9": 1.0845, "ema21": 1.0870, "ema50": 1.0900}
    e["structure"]["bos"] = [{"type": "BOS_DOWN", "bar": 42}, {"type": "BOS_DOWN", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_DOWN", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.22, "wick_atr_ratio": 0.35, "bar": 48}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0842}]
    scenarios.append({"name": "BUY in strong downtrend — counter-trend trap", "evidence": e, "expected": "REFUSE", "reason": "Multiple BOS_DOWN, price below all EMAs, counter-trend BUY is high risk"})

    # 10. SELL in strong uptrend
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 72.0, "ema9": 1.0855, "ema21": 1.0830, "ema50": 1.0800}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 42}, {"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
    e["orderblocks"] = [{"type": "BEARISH_OB", "price": 1.0858}]
    scenarios.append({"name": "SELL in strong uptrend — counter-trend trap", "evidence": e, "expected": "REFUSE", "reason": "Multiple BOS_UP, price above all EMAs, counter-trend SELL is high risk"})

    # 11. BUY setup but structure is all bearish
    e = _base("EURUSD", "TRENDING_DOWN", 1.0800, 0.0015)
    e["indicators"] = {"rsi14": 30.0, "ema9": 1.0795, "ema21": 1.0820, "ema50": 1.0850}
    e["structure"]["bos"] = [{"type": "BOS_DOWN", "bar": 40}, {"type": "BOS_DOWN", "bar": 44}, {"type": "BOS_DOWN", "bar": 47}]
    e["structure"]["choch"] = [{"type": "CHOCH_DOWN", "bar": 38}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.21, "wick_atr_ratio": 0.32, "bar": 49}]
    scenarios.append({"name": "Sweep low in waterfall decline — catching knives", "evidence": e, "expected": "REFUSE", "reason": "3 consecutive BOS_DOWN, strong bearish momentum, sweep is just continuation not reversal"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 3: WEAK/INVALID SETUPS (should REFUSE)
    # ════════════════════════════════════════════════════════════════════════

    # 12. Weak sweep — not strong
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 40.0, "ema9": 1.0848, "ema21": 1.0840}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": False, "rejection_pct": 0.08, "wick_atr_ratio": 0.15, "bar": 48}]
    scenarios.append({"name": "Weak sweep — low rejection, not strong", "evidence": e, "expected": "REFUSE", "reason": "Sweep rejection only 8%, wick_atr_ratio 0.15 — noise not real sweep"})

    # 13. No structure at all
    e = _base("EURUSD", "RANGING", 1.0850, 0.0008)
    e["indicators"] = {"rsi14": 50.0, "ema9": 1.0849, "ema21": 1.0851}
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.22, "wick_atr_ratio": 0.35, "bar": 48}]
    scenarios.append({"name": "No BOS/CHOCH — structureless market", "evidence": e, "expected": "REFUSE", "reason": "No structural confirmation, ranging market, EMAs flat"})

    # 14. RSI overbought for BUY
    e = _base("EURUSD", "TRENDING_UP", 1.0900, 0.0012)
    e["indicators"] = {"rsi14": 78.0, "ema9": 1.0895, "ema21": 1.0870}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
    scenarios.append({"name": "RSI 78 overbought — BUY at top", "evidence": e, "expected": "REFUSE", "reason": "RSI 78 is deeply overbought, high reversal risk"})

    # 15. RSI oversold for SELL
    e = _base("EURUSD", "TRENDING_DOWN", 1.0750, 0.0012)
    e["indicators"] = {"rsi14": 18.0, "ema9": 1.0755, "ema21": 1.0780}
    e["structure"]["bos"] = [{"type": "BOS_DOWN", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_DOWN", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
    scenarios.append({"name": "RSI 18 oversold — SELL at bottom", "evidence": e, "expected": "REFUSE", "reason": "RSI 18 is deeply oversold, bounce risk extremely high"})

    # 16. Contradicting signals — bullish structure but bearish sweep
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 55.0, "ema9": 1.0848, "ema21": 1.0840}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.28, "wick_atr_ratio": 0.45, "bar": 48}]
    e["orderblocks"] = [{"type": "BEARISH_OB", "price": 1.0858}]
    scenarios.append({"name": "Contradicting — bullish structure + bearish sweep", "evidence": e, "expected": "REFUSE", "reason": "BOS_UP/CHOCH_UP but SWEEP_HIGH suggests reversal, mixed signals"})

    # 17. Very low ATR — dead market
    e = _base("EURUSD", "RANGING", 1.0850, 0.0003)
    e["indicators"] = {"rsi14": 50.0, "ema9": 1.0850, "ema21": 1.0850}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
    scenarios.append({"name": "Dead market — ATR 0.0003, ranging", "evidence": e, "expected": "REFUSE", "reason": "ATR extremely low (3 pips), no real movement, spread will eat profits"})

    # 18. Volatile regime with no clear direction
    e = _base("EURUSD", "VOLATILE", 1.0850, 0.0025)
    e["indicators"] = {"rsi14": 50.0, "ema9": 1.0855, "ema21": 1.0845}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 42}, {"type": "BOS_DOWN", "bar": 46}]
    e["structure"]["choch"] = [{"type": "CHOCH_DOWN", "bar": 44}]
    e["liquidity"]["sweeps"] = [
        {"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.22, "wick_atr_ratio": 0.35, "bar": 47},
        {"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.20, "wick_atr_ratio": 0.30, "bar": 48},
    ]
    scenarios.append({"name": "Volatile whipsaw — both sides swept", "evidence": e, "expected": "REFUSE", "reason": "Both highs and lows swept, BOS in both directions, choppy price action"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 4: EDGE CASES (mixed expectations)
    # ════════════════════════════════════════════════════════════════════════

    # 19. CHOCH reversal — just happened
    e = _base("EURUSD", "TRENDING_DOWN", 1.0830, 0.0012)
    e["indicators"] = {"rsi14": 35.0, "ema9": 1.0828, "ema21": 1.0840}
    e["structure"]["bos"] = [{"type": "BOS_DOWN", "bar": 35}, {"type": "BOS_DOWN", "bar": 40}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 48}]  # fresh CHOCH reversal!
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.35, "wick_atr_ratio": 0.6, "bar": 47}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0822}]
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 1.0835, "bottom": 1.0825}]
    scenarios.append({"name": "Fresh CHOCH reversal in downtrend — early BUY", "evidence": e, "expected": "ALLOW", "reason": "CHOCH_UP just printed, strong sweep low with high rejection, OB+FVG present — classic reversal entry"})

    # 20. Old structure, fresh sweep
    e = _base("EURUSD", "TRENDING_UP", 1.0880, 0.0012)
    e["indicators"] = {"rsi14": 55.0, "ema9": 1.0878, "ema21": 1.0865}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 10}]  # very old
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 8}]  # very old
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.30, "wick_atr_ratio": 0.5, "bar": 48}]
    scenarios.append({"name": "Old structure (bar 8-10) + fresh sweep", "evidence": e, "expected": "ALLOW", "reason": "Structure is old but trend is intact (EMA alignment), fresh sweep is valid"})

    # 21. Multiple timeframe confluence
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 42.0, "ema9": 1.0848, "ema21": 1.0840, "ema50": 1.0820, "ema200": 1.0780}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 43}, {"type": "BOS_UP", "bar": 46}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.40, "wick_atr_ratio": 0.7, "bar": 48}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0838}]
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 1.0852, "bottom": 1.0843}]
    scenarios.append({"name": "Maximum confluence — all EMAs aligned, huge sweep rejection", "evidence": e, "expected": "ALLOW", "reason": "Perfect storm: all EMAs bullish aligned, 40% rejection sweep, OB+FVG overlap"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 5: GOLD-SPECIFIC (XAUUSD has different dynamics)
    # ════════════════════════════════════════════════════════════════════════

    # 22. Gold ranging between support/resistance
    e = _base("XAUUSD", "RANGING", 2340.00, 8.0)
    e["indicators"] = {"rsi14": 48.0, "ema9": 2340.0, "ema21": 2342.0}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 30}]
    e["structure"]["choch"] = []
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.22, "wick_atr_ratio": 0.35, "bar": 48}]
    scenarios.append({"name": "Gold ranging — sweep at range bottom", "evidence": e, "expected": "ALLOW", "reason": "Range sweep at support, can trade range bounce"})

    # 23. Gold spike — news-driven move
    e = _base("XAUUSD", "VOLATILE", 2380.00, 25.0)
    e["indicators"] = {"rsi14": 82.0, "ema9": 2375.0, "ema21": 2350.0}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 46}, {"type": "BOS_UP", "bar": 48}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 44}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.30, "wick_atr_ratio": 0.5, "bar": 47}]
    scenarios.append({"name": "Gold spike — ATR 25, RSI 82, volatile", "evidence": e, "expected": "REFUSE", "reason": "ATR is 2x normal, RSI 82 overbought, chasing a spike = high risk of reversal"})

    # 24. Gold pullback to OB in uptrend
    e = _base("XAUUSD", "TRENDING_UP", 2355.00, 12.0)
    e["indicators"] = {"rsi14": 42.0, "ema9": 2356.0, "ema21": 2350.0, "ema50": 2335.0}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 40}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 35}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.28, "wick_atr_ratio": 0.45, "bar": 47}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 2348.0}]
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 2358.0, "bottom": 2350.0}]
    scenarios.append({"name": "Gold pullback to OB — clean retest", "evidence": e, "expected": "ALLOW", "reason": "Healthy pullback to OB+FVG in uptrend, RSI not overbought"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 6: RSI EDGE CASES
    # ════════════════════════════════════════════════════════════════════════

    for rsi_val, side_hint, expected, reason in [
        (32.0, "BUY", "ALLOW", "RSI 32 in buy zone (30-45)"),
        (44.0, "BUY", "ALLOW", "RSI 44 in buy zone (30-45)"),
        (48.0, "BUY", "ALLOW", "RSI 48 neutral — still acceptable for BUY with other confluence"),
        (56.0, "SELL", "ALLOW", "RSI 56 in sell zone (55-70)"),
        (68.0, "SELL", "ALLOW", "RSI 68 in sell zone (55-70)"),
        (75.0, "BUY", "REFUSE", "RSI 75 overbought — BUY here is chasing"),
        (22.0, "SELL", "REFUSE", "RSI 22 oversold — SELL here is chasing"),
        (50.0, "BUY", "ALLOW", "RSI 50 neutral — acceptable with strong structure"),
    ]:
        e = _base("EURUSD", "TRENDING_UP" if side_hint == "BUY" else "TRENDING_DOWN", 1.0850, 0.0012)
        e["indicators"] = {"rsi14": rsi_val, "ema9": 1.0848, "ema21": 1.0840 if side_hint == "BUY" else 1.0860}
        e["structure"]["bos"] = [{"type": f"BOS_{'UP' if side_hint == 'BUY' else 'DOWN'}", "bar": 45}]
        e["structure"]["choch"] = [{"type": f"CHOCH_{'UP' if side_hint == 'BUY' else 'DOWN'}", "bar": 40}]
        sweep_type = "SWEEP_LOW" if side_hint == "BUY" else "SWEEP_HIGH"
        e["liquidity"]["sweeps"] = [{"type": sweep_type, "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
        e["orderblocks"] = [{"type": f"{'BULLISH' if side_hint == 'BUY' else 'BEARISH'}_OB", "price": 1.0842}]
        scenarios.append({"name": f"RSI {rsi_val} — {side_hint} setup", "evidence": e, "expected": expected, "reason": reason})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 7: STRUCTURE QUALITY VARIATIONS
    # ════════════════════════════════════════════════════════════════════════

    # Multiple BOS in same direction = strong trend
    e = _base("EURUSD", "TRENDING_UP", 1.0870, 0.0012)
    e["indicators"] = {"rsi14": 40.0, "ema9": 1.0868, "ema21": 1.0855}
    e["structure"]["bos"] = [
        {"type": "BOS_UP", "bar": 30}, {"type": "BOS_UP", "bar": 36},
        {"type": "BOS_UP", "bar": 42}, {"type": "BOS_UP", "bar": 47},
    ]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 28}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.30, "wick_atr_ratio": 0.5, "bar": 48}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0862}]
    scenarios.append({"name": "4x BOS_UP — very strong trend continuation", "evidence": e, "expected": "ALLOW", "reason": "4 consecutive BOS_UP = very strong bullish momentum"})

    # Structure conflict — BOS up then BOS down
    e = _base("EURUSD", "RANGING", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 50.0, "ema9": 1.0850, "ema21": 1.0850}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 38}, {"type": "BOS_DOWN", "bar": 44}]
    e["structure"]["choch"] = [{"type": "CHOCH_DOWN", "bar": 42}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.22, "wick_atr_ratio": 0.35, "bar": 48}]
    scenarios.append({"name": "BOS conflict — UP then DOWN, now sweep low", "evidence": e, "expected": "REFUSE", "reason": "Structure is confused, BOS in both directions, ranging EMAs"})

    # CHOCH without BOS — early reversal signal
    e = _base("EURUSD", "TRENDING_DOWN", 1.0820, 0.0012)
    e["indicators"] = {"rsi14": 33.0, "ema9": 1.0818, "ema21": 1.0835}
    e["structure"]["bos"] = []
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 48}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.35, "wick_atr_ratio": 0.55, "bar": 47}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0812}]
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 1.0825, "bottom": 1.0815}]
    scenarios.append({"name": "CHOCH_UP without BOS — early reversal", "evidence": e, "expected": "ALLOW", "reason": "Strong sweep + CHOCH is valid early reversal signal, OB+FVG present"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 8: SWEEP QUALITY
    # ════════════════════════════════════════════════════════════════════════

    for rej, wick, strong, exp, label in [
        (0.40, 0.70, True, "ALLOW", "Monster sweep — 40% rejection"),
        (0.25, 0.40, True, "ALLOW", "Good sweep — 25% rejection"),
        (0.20, 0.30, True, "ALLOW", "Minimum strong sweep — borderline"),
        (0.15, 0.25, False, "REFUSE", "Weak sweep — below threshold"),
        (0.08, 0.12, False, "REFUSE", "Very weak sweep — noise"),
        (0.50, 0.90, True, "ALLOW", "Extreme rejection — V-reversal"),
    ]:
        e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
        e["indicators"] = {"rsi14": 38.0, "ema9": 1.0848, "ema21": 1.0840}
        e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
        e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
        e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": strong, "rejection_pct": rej, "wick_atr_ratio": wick, "bar": 48}]
        e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0842}]
        scenarios.append({"name": f"Sweep quality: {label}", "evidence": e, "expected": exp, "reason": f"rejection={rej}, wick_atr={wick}, strong={strong}"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 9: REGIME VARIATIONS
    # ════════════════════════════════════════════════════════════════════════

    for regime, exp, reason in [
        ("TRENDING_UP", "ALLOW", "Trending regime is ideal for BUY"),
        ("TRENDING_DOWN", "REFUSE", "Trending down but BUY setup = counter-trend"),
        ("RANGING", "ALLOW", "Ranging with sweep at boundary can work"),
        ("VOLATILE", "REFUSE", "Volatile regime = unpredictable, avoid"),
    ]:
        e = _base("EURUSD", regime, 1.0850, 0.0012 if regime != "VOLATILE" else 0.0025)
        e["indicators"] = {"rsi14": 38.0, "ema9": 1.0848, "ema21": 1.0840}
        e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
        e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
        e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
        e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0842}]
        scenarios.append({"name": f"Regime: {regime} — BUY setup", "evidence": e, "expected": exp, "reason": reason})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 10: EMPTY / MINIMAL EVIDENCE
    # ════════════════════════════════════════════════════════════════════════

    # Completely empty
    e = _base("EURUSD", "RANGING", 1.0850, 0.0010)
    e["indicators"] = {"rsi14": 50.0}
    scenarios.append({"name": "Empty evidence — no signals at all", "evidence": e, "expected": "REFUSE", "reason": "No structure, no sweeps, no OB/FVG — nothing to trade"})

    # Only sweep, nothing else
    e = _base("EURUSD", "RANGING", 1.0850, 0.0010)
    e["indicators"] = {"rsi14": 50.0}
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
    scenarios.append({"name": "Only sweep — no structure or zones", "evidence": e, "expected": "REFUSE", "reason": "Sweep alone without structural confirmation is unreliable"})

    # Only structure, no sweep
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 40.0, "ema9": 1.0848, "ema21": 1.0840}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    scenarios.append({"name": "Structure only — no sweep", "evidence": e, "expected": "REFUSE", "reason": "No liquidity sweep means no clear entry trigger"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 11: PRICE VS EMA POSITIONS
    # ════════════════════════════════════════════════════════════════════════

    # Price far above EMAs — overextended BUY
    e = _base("EURUSD", "TRENDING_UP", 1.0900, 0.0012)
    e["indicators"] = {"rsi14": 65.0, "ema9": 1.0880, "ema21": 1.0860, "ema50": 1.0830}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.22, "wick_atr_ratio": 0.35, "bar": 48}]
    scenarios.append({"name": "Price 40 pips above EMA50 — overextended", "evidence": e, "expected": "REFUSE", "reason": "Price too far from mean, pullback likely before continuation"})

    # Price at EMA — mean reversion entry
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 42.0, "ema9": 1.0850, "ema21": 1.0845}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.28, "wick_atr_ratio": 0.45, "bar": 48}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0845}]
    scenarios.append({"name": "Price at EMA9 — optimal mean reversion BUY", "evidence": e, "expected": "ALLOW", "reason": "Price pulled back to EMA9 = ideal entry, not overextended"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 12: MULTIPLE SWEEPS
    # ════════════════════════════════════════════════════════════════════════

    # Double sweep low — extra conviction
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 36.0, "ema9": 1.0848, "ema21": 1.0840}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [
        {"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 44},
        {"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.30, "wick_atr_ratio": 0.5, "bar": 48},
    ]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0842}]
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 1.0855, "bottom": 1.0845}]
    scenarios.append({"name": "Double sweep low — extra conviction", "evidence": e, "expected": "ALLOW", "reason": "Two sweep lows = liquidity taken twice, strong reversal signal"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 13: REALISTIC LOSING SETUPS FROM TODAY
    # ════════════════════════════════════════════════════════════════════════

    # Recreate what today's losing SELL trades likely looked like
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 55.0, "ema9": 1.0848, "ema21": 1.0835, "ema50": 1.0810}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 40}, {"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 38}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.22, "wick_atr_ratio": 0.35, "bar": 48}]
    e["orderblocks"] = [{"type": "BEARISH_OB", "price": 1.0855}]
    scenarios.append({"name": "TODAY'S LOSS TYPE — SELL in bullish trend (1)", "evidence": e, "expected": "REFUSE", "reason": "This is what lost $50 today: SELL against BOS_UP trend with bullish EMA alignment"})

    e = _base("EURUSD", "TRENDING_UP", 1.0860, 0.0012)
    e["indicators"] = {"rsi14": 58.0, "ema9": 1.0858, "ema21": 1.0845, "ema50": 1.0820}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 42}, {"type": "BOS_UP", "bar": 46}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.20, "wick_atr_ratio": 0.32, "bar": 48}]
    scenarios.append({"name": "TODAY'S LOSS TYPE — SELL in bullish trend (2)", "evidence": e, "expected": "REFUSE", "reason": "Counter-trend SELL against sustained uptrend, all EMAs bullish"})

    e = _base("EURUSD", "TRENDING_UP", 1.0870, 0.0012)
    e["indicators"] = {"rsi14": 60.0, "ema9": 1.0868, "ema21": 1.0852, "ema50": 1.0830}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 38}, {"type": "BOS_UP", "bar": 43}, {"type": "BOS_UP", "bar": 47}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 36}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.21, "wick_atr_ratio": 0.33, "bar": 49}]
    scenarios.append({"name": "TODAY'S LOSS TYPE — SELL in bullish trend (3)", "evidence": e, "expected": "REFUSE", "reason": "3x BOS_UP = very strong uptrend, SELL is suicide"})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 14: BATCH — vary one parameter at a time
    # ════════════════════════════════════════════════════════════════════════

    # Vary ATR for gold
    for atr_val, exp, label in [
        (5.0, "ALLOW", "Low ATR gold = calm market"),
        (12.0, "ALLOW", "Normal ATR gold"),
        (20.0, "ALLOW", "Elevated ATR gold — still tradeable"),
        (35.0, "REFUSE", "Extreme ATR gold — news spike"),
        (50.0, "REFUSE", "Crisis ATR gold — untradeable"),
    ]:
        e = _base("XAUUSD", "TRENDING_UP", 2350.0, atr_val)
        e["indicators"] = {"rsi14": 42.0, "ema9": 2348.0, "ema21": 2340.0}
        e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
        e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
        e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
        e["orderblocks"] = [{"type": "BULLISH_OB", "price": 2342.0}]
        scenarios.append({"name": f"Gold ATR={atr_val}: {label}", "evidence": e, "expected": exp, "reason": label})

    # ════════════════════════════════════════════════════════════════════════
    # CATEGORY 15: FVG + OB OVERLAP vs NO OVERLAP
    # ════════════════════════════════════════════════════════════════════════

    # OB and FVG overlap at same price
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 38.0, "ema9": 1.0848, "ema21": 1.0840}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0842}]
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 1.0848, "bottom": 1.0840}]  # overlaps OB
    scenarios.append({"name": "OB + FVG overlap — high probability zone", "evidence": e, "expected": "ALLOW", "reason": "OB at 1.0842 inside FVG 1.0840-1.0848 = confluence entry zone"})

    # OB and FVG far apart
    e = _base("EURUSD", "TRENDING_UP", 1.0850, 0.0012)
    e["indicators"] = {"rsi14": 38.0, "ema9": 1.0848, "ema21": 1.0840}
    e["structure"]["bos"] = [{"type": "BOS_UP", "bar": 45}]
    e["structure"]["choch"] = [{"type": "CHOCH_UP", "bar": 40}]
    e["liquidity"]["sweeps"] = [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]
    e["orderblocks"] = [{"type": "BULLISH_OB", "price": 1.0810}]  # far below
    e["fvg"] = [{"type": "BULLISH_FVG", "top": 1.0870, "bottom": 1.0860}]  # far above
    scenarios.append({"name": "OB + FVG far apart — no overlap", "evidence": e, "expected": "ALLOW", "reason": "Still valid setup, just no OB/FVG overlap — slight lower conviction but other factors present"})

    return scenarios


# ── Main runner ──────────────────────────────────────────────────────────────

def run_stress_test():
    scenarios = generate_scenarios()
    total = len(scenarios)
    print(f"\n{'='*80}")
    print(f"  LLM STRESS TEST — {total} scenarios")
    print(f"{'='*80}\n")

    notify(f"🧪 <b>LLM STRESS TEST STARTED</b>\nRunning {total} trade scenarios against qwen3:4b...")

    results = []
    matches = 0
    mismatches = 0
    errors = 0
    allow_count = 0
    refuse_count = 0
    llm_failures = 0

    for i, scenario in enumerate(scenarios):
        name = scenario["name"]
        evidence = scenario["evidence"]
        expected = scenario["expected"]
        my_reason = scenario["reason"]

        print(f"\n[{i+1}/{total}] {name}")
        print(f"  Expected: {expected} — {my_reason}")

        start = time.time()
        result = call_ollama(evidence)
        elapsed = time.time() - start

        if result is None:
            print(f"  LLM: ❌ UNAVAILABLE ({elapsed:.1f}s)")
            llm_failures += 1
            results.append({
                "id": i+1, "name": name, "expected": expected,
                "llm_decision": "ERROR", "llm_confidence": None,
                "llm_justification": "UNAVAILABLE",
                "match": False, "my_reason": my_reason, "elapsed": elapsed,
            })
            errors += 1
            continue

        llm_dec = result.get("decision", "?")
        llm_conf = result.get("confidence", 0)
        llm_just = result.get("justification", "")

        match = llm_dec == expected
        symbol = "✅" if match else "❌"
        if match:
            matches += 1
        else:
            mismatches += 1

        if llm_dec == "ALLOW":
            allow_count += 1
        else:
            refuse_count += 1

        print(f"  LLM:      {symbol} {llm_dec} (conf={llm_conf:.2f}) — {llm_just[:80]}")
        if not match:
            print(f"  ⚠️  MISMATCH! Expected {expected}, got {llm_dec}")

        results.append({
            "id": i+1, "name": name, "expected": expected,
            "llm_decision": llm_dec, "llm_confidence": llm_conf,
            "llm_justification": llm_just,
            "match": match, "my_reason": my_reason, "elapsed": elapsed,
        })

    # ── Report ───────────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*80}")
    print(f"  Total scenarios: {total}")
    print(f"  Matches:    {matches} ({matches/total*100:.0f}%)")
    print(f"  Mismatches: {mismatches} ({mismatches/total*100:.0f}%)")
    print(f"  LLM errors: {errors}")
    print(f"  LLM ALLOW:  {allow_count} | LLM REFUSE: {refuse_count}")
    print()

    # Mismatches detail
    mismatch_list = [r for r in results if not r["match"] and r["llm_decision"] != "ERROR"]
    if mismatch_list:
        print(f"\n{'='*80}")
        print(f"  MISMATCHES (LLM disagrees with expected)")
        print(f"{'='*80}")

        false_allows = [r for r in mismatch_list if r["llm_decision"] == "ALLOW" and r["expected"] == "REFUSE"]
        false_refuses = [r for r in mismatch_list if r["llm_decision"] == "REFUSE" and r["expected"] == "ALLOW"]

        if false_allows:
            print(f"\n  🔴 FALSE ALLOWS ({len(false_allows)}) — LLM said ALLOW but should REFUSE:")
            for r in false_allows:
                print(f"    [{r['id']}] {r['name']}")
                print(f"         LLM: {r['llm_decision']} conf={r['llm_confidence']:.2f} — {r['llm_justification'][:80]}")
                print(f"         WHY REFUSE: {r['my_reason']}")

        if false_refuses:
            print(f"\n  🟡 FALSE REFUSES ({len(false_refuses)}) — LLM said REFUSE but should ALLOW:")
            for r in false_refuses:
                print(f"    [{r['id']}] {r['name']}")
                print(f"         LLM: {r['llm_decision']} conf={r['llm_confidence']:.2f} — {r['llm_justification'][:80]}")
                print(f"         WHY ALLOW: {r['my_reason']}")

    # Confidence distribution
    confs = [r["llm_confidence"] for r in results if r["llm_confidence"] is not None]
    if confs:
        print(f"\n  Confidence: min={min(confs):.2f} max={max(confs):.2f} avg={sum(confs)/len(confs):.2f}")
        low_conf = [r for r in results if r["llm_confidence"] is not None and r["llm_confidence"] < 0.70]
        print(f"  Below 0.70 threshold: {len(low_conf)} ({len(low_conf)/len(confs)*100:.0f}%)")

    # Save full results
    report_path = os.path.join("storage", "logs", "llm_stress_test.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Full results saved to: {report_path}")

    # Telegram summary
    tg_msg = (
        f"🧪 <b>LLM STRESS TEST COMPLETE</b>\n\n"
        f"Scenarios: {total}\n"
        f"✅ Matches: {matches} ({matches/total*100:.0f}%)\n"
        f"❌ Mismatches: {mismatches} ({mismatches/total*100:.0f}%)\n"
        f"⚠️ LLM errors: {errors}\n\n"
        f"🔴 False ALLOWs: {len([r for r in mismatch_list if r['llm_decision']=='ALLOW'])}\n"
        f"🟡 False REFUSEs: {len([r for r in mismatch_list if r['llm_decision']=='REFUSE'])}\n\n"
        f"ALLOW rate: {allow_count}/{total} | REFUSE rate: {refuse_count}/{total}"
    )
    if confs:
        tg_msg += f"\nAvg confidence: {sum(confs)/len(confs):.2f}"
    notify(tg_msg)

    return results


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    run_stress_test()
