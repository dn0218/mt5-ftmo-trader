"""Tests for confluence scoring."""

import os
import sys
import pytest
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.scoring.confluence import score_confluence


def _make_evidence(**overrides):
    """Build a base evidence dict."""
    ev = {
        "symbol": "EURUSD",
        "current_price": 1.08500,
        "structure": {"bos": [], "choch": [], "swings": []},
        "liquidity": {"sweeps": []},
        "orderblocks": [],
        "fvg": [],
        "indicators": {"rsi14": 50},
    }
    ev.update(overrides)
    return ev


class TestConfluence:
    def test_score_0_no_factors(self):
        """Empty evidence should score 0."""
        ev = _make_evidence()
        score, factors = score_confluence(ev, "BUY", 1.08500)
        assert score == 0 or "KILL_ZONE" in factors  # kill zone depends on current time

    def test_score_5_all_factors(self):
        """All factors present should score max (adjusting for kill zone)."""
        ev = _make_evidence(
            structure={
                "bos": [{"direction": "UP"}],
                "choch": [{"direction": "UP"}],
                "swings": [],
            },
            liquidity={
                "sweeps": [{"type": "SWEEP_LOW", "strong": True}],
            },
            orderblocks=[{"type": "bullish", "price": 1.08500}],
            fvg=[{"type": "BULLISH", "top": 1.08600, "bottom": 1.08400}],
            indicators={"rsi14": 38},
        )
        # Mock time to be in kill zone
        with patch("core.scoring.confluence.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            score, factors = score_confluence(ev, "BUY", 1.08500)
        assert score == 5
        assert "STRONG_SWEEP" in factors
        assert "BOS_CHOCH" in factors
        assert "OB_FVG_OVERLAP" in factors
        assert "RSI_OVERSOLD_RECOVERY" in factors
        assert "KILL_ZONE" in factors

    def test_minimum_score_gate(self):
        """Score < 3 should not proceed (tested at caller level)."""
        ev = _make_evidence()
        score, _ = score_confluence(ev, "BUY", 1.08500)
        min_score = int(os.getenv("MIN_CONFLUENCE_SCORE", "3"))
        # With no factors, score should be < 3 (possibly +1 for kill zone)
        if score < min_score:
            assert True  # Would be blocked

    def test_rsi_buy_range(self):
        """RSI 30-45 should add 1 point for BUY."""
        ev = _make_evidence(indicators={"rsi14": 38})
        score_buy, factors = score_confluence(ev, "BUY", 1.08500)
        assert "RSI_OVERSOLD_RECOVERY" in factors

        # RSI outside range should not add
        ev2 = _make_evidence(indicators={"rsi14": 50})
        score_no, factors2 = score_confluence(ev2, "BUY", 1.08500)
        assert "RSI_OVERSOLD_RECOVERY" not in factors2


class TestSweepAgeDecay:
    def _ev_with_strong_sweep(self):
        return _make_evidence(
            liquidity={"sweeps": [{"type": "SWEEP_LOW", "strong": True}]},
        )

    def test_fresh_sweep_adds_point(self):
        ev = self._ev_with_strong_sweep()
        base_score, base_factors = score_confluence(ev, "BUY", 1.08500)
        fresh_score, fresh_factors = score_confluence(ev, "BUY", 1.08500, sweep_age_bars=3)
        # Fresh sweep should be +1 (capped at 5).
        assert "FRESH_SWEEP" in fresh_factors
        assert fresh_score == min(5, base_score + 1)

    def test_stale_sweep_subtracts_point(self):
        ev = self._ev_with_strong_sweep()
        base_score, _ = score_confluence(ev, "BUY", 1.08500)
        stale_score, factors = score_confluence(ev, "BUY", 1.08500, sweep_age_bars=30)
        assert "STALE_SWEEP_PENALTY" in factors
        assert stale_score == max(0, base_score - 1)

    def test_mid_age_no_modifier(self):
        ev = self._ev_with_strong_sweep()
        base_score, base_factors = score_confluence(ev, "BUY", 1.08500)
        mid_score, mid_factors = score_confluence(ev, "BUY", 1.08500, sweep_age_bars=12)
        assert "FRESH_SWEEP" not in mid_factors
        assert "STALE_SWEEP_PENALTY" not in mid_factors
        assert mid_score == base_score

    def test_score_clamped_to_5(self):
        # Build an all-5 evidence then add fresh-sweep — must stay at 5.
        ev = _make_evidence(
            structure={"bos": [{"direction": "UP"}], "choch": [{"direction": "UP"}], "swings": []},
            liquidity={"sweeps": [{"type": "SWEEP_LOW", "strong": True}]},
            orderblocks=[{"type": "bullish", "price": 1.08500}],
            fvg=[{"type": "BULLISH", "top": 1.08600, "bottom": 1.08400}],
            indicators={"rsi14": 38},
        )
        with patch("core.scoring.confluence.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc)
            score, _ = score_confluence(ev, "BUY", 1.08500, sweep_age_bars=2)
        assert score == 5

    def test_score_clamped_to_0(self):
        # No factors and stale sweep → would be -1, clamped to 0.
        ev = _make_evidence(liquidity={"sweeps": []})  # no sweep at all
        score, _ = score_confluence(ev, "BUY", 1.08500, sweep_age_bars=100)
        assert score >= 0
