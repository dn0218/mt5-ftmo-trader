"""Breakout strategy boundary tests (5 concrete cases, no philosophy)."""
import numpy as np
import pandas as pd
import pytest

from core.detection.breakout import evaluate_breakout, h4_direction, spread_ok


def make_m15(closes, highs=None, lows=None):
    n = len(closes)
    highs = highs if highs is not None else [c + 0.3 for c in closes]
    lows = lows if lows is not None else [c - 0.3 for c in closes]
    return pd.DataFrame({
        "time": range(n),
        "open": list(closes),
        "high": highs,
        "low": lows,
        "close": list(closes),
    })


def make_h4(up=True, n=60):
    drift = 0.5 if up else -0.5
    closes = [100 + i * drift for i in range(n)]
    return pd.DataFrame({
        "time": range(n),
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
    })


BASE = 40 * [100.0]


def test_no_breakout_returns_none():
    """Flat closes below any breakout -> no signal."""
    closes = BASE + [100.05, 100.1, 100.15, 100.2, 100.25, 100.3, 100.35]
    df = make_m15(closes)
    assert evaluate_breakout(df, make_h4(up=True)) is None


def test_breakout_with_retest_confirms_buy():
    """20-bar high broken, next bar closes back near the level -> BUY."""
    closes = BASE + [101.0, 100.35, 100.4, 100.45]
    # high of the 20-bar window before breakout bar is ~100.3
    df = make_m15(closes)
    sig = evaluate_breakout(df, make_h4(up=True))
    assert sig is not None and sig["side"] == "BUY"
    assert sig["entry"] == pytest.approx(100.35)   # first retest bar confirms
    assert sig["sl"] < sig["entry"] < sig["tp1"] < sig["tp2"]
    assert "PULLBACK_CONFIRM" in sig["factors"]


def test_pullback_not_yet_confirmed_returns_none():
    """Breakout happened but every later close still far above the level."""
    closes = BASE + [101.0, 101.5, 102.0, 102.5]
    df = make_m15(closes)
    assert evaluate_breakout(df, make_h4(up=True)) is None


def test_h4_direction_gates_side():
    """H4 downtrend blocks the BUY breakout entirely."""
    closes = BASE + [101.0, 100.35, 100.4, 100.45]
    df = make_m15(closes)
    assert evaluate_breakout(df, make_h4(up=False)) is None
    assert h4_direction(make_h4(up=False)) == "DOWN"


def test_spread_filter():
    assert spread_ok("EURUSD.", 20, 30) is True
    assert spread_ok("EURUSD.", 45, 30) is False
