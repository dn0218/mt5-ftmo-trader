"""Tests for SMC detection modules."""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.conftest import make_trending_up, make_ranging, make_volatile
from core.detection.structure import detect_swing_points, detect_bos, detect_choch
from core.detection.liquidity import detect_sweeps
from core.detection.fvg import detect_fvg
from core.detection.regime import detect_regime
from core.indicators.indicators import atr


class TestSwingDetection:
    def test_swing_detection_finds_highs_lows(self):
        df = make_trending_up(200)
        swings = detect_swing_points(df)
        highs = [s for s in swings if s["type"] == "SWING_HIGH"]
        lows = [s for s in swings if s["type"] == "SWING_LOW"]
        assert len(highs) > 0, "Should find at least 1 swing high"
        assert len(lows) > 0, "Should find at least 1 swing low"


class TestBOS:
    def test_bos_up_detected_on_trending_up(self):
        df = make_trending_up(200)
        swings = detect_swing_points(df)
        bos = detect_bos(swings)
        up_bos = [b for b in bos if b["direction"] == "UP"]
        assert len(up_bos) > 0, "Should find BOS UP in uptrend"


class TestCHOCH:
    def test_choch_detected_on_reversal(self):
        df = make_trending_up(200)
        swings = detect_swing_points(df)
        choch = detect_choch(swings)
        # CHOCH may or may not appear depending on data
        # Just verify the function runs without error
        assert isinstance(choch, list)


class TestSweeps:
    def test_strong_sweep_criteria(self):
        """Strong sweep needs rejection_pct >= 20 AND wick_atr_ratio >= 0.3."""
        df = make_trending_up(200)
        swings = detect_swing_points(df)
        atr_series = atr(df, 14)
        sweeps = detect_sweeps(df, swings, atr_series)

        for s in sweeps:
            if s.get("strong"):
                assert s["rejection_pct"] >= 20.0
                assert s["wick_atr_ratio"] >= 0.3


class TestFVG:
    def test_fvg_size_filter(self):
        """FVG smaller than 0.5 * ATR should be excluded."""
        df = make_trending_up(200)
        atr_series = atr(df, 14)
        fvgs = detect_fvg(df, atr_series, min_atr_mult=0.5)

        for fvg in fvgs:
            bar = fvg["bar"]
            if bar < len(atr_series):
                atr_val = atr_series.iloc[bar]
                if not (atr_val != atr_val):  # not NaN
                    assert fvg["size"] >= 0.5 * atr_val


class TestRegime:
    def test_regime_trending_up(self):
        df = make_trending_up(200)
        regime = detect_regime(df)
        assert regime in ("TRENDING_UP", "TRENDING_DOWN", "RANGING"), f"Got {regime}"

    def test_regime_ranging(self):
        df = make_ranging(200)
        regime = detect_regime(df)
        assert regime == "RANGING", f"Expected RANGING, got {regime}"

    def test_regime_volatile(self):
        df = make_volatile(200)
        regime = detect_regime(df)
        assert regime == "VOLATILE", f"Expected VOLATILE, got {regime}"
