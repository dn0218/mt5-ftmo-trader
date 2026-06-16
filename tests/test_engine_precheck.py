"""Tests for engine precheck and hard blocks."""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.decision.ollama_decider import is_trade_valid


def _make_evidence(**overrides):
    ev = {
        "structure": {"bos": [], "choch": [], "swings": []},
        "liquidity": {"sweeps": []},
        "orderblocks": [],
        "fvg": [],
    }
    ev.update(overrides)
    return ev


class TestPrecheck:
    def test_precheck_passes_with_full_confluence(self):
        """Precheck should pass when BOS, CHOCH, and strong sweep all present."""
        from services.worker.engine import TradingEngine
        engine = TradingEngine()
        ev = _make_evidence(
            structure={
                "bos": [{"direction": "UP"}],
                "choch": [{"direction": "UP"}],
                "swings": [],
            },
            liquidity={
                "sweeps": [{"type": "SWEEP_LOW", "strong": True}],
            },
        )
        assert engine._precheck(ev) is True

    def test_precheck_fails_no_sweep(self):
        """Precheck should fail without any sweep."""
        from services.worker.engine import TradingEngine
        engine = TradingEngine()
        ev = _make_evidence(
            structure={
                "bos": [{"direction": "UP"}],
                "choch": [{"direction": "UP"}],
                "swings": [],
            },
            liquidity={"sweeps": []},
        )
        assert engine._precheck(ev) is False

    def test_precheck_fails_no_strong_sweep(self):
        """Precheck should fail without a strong sweep."""
        from services.worker.engine import TradingEngine
        engine = TradingEngine()
        ev = _make_evidence(
            structure={
                "bos": [{"direction": "UP"}],
                "choch": [{"direction": "UP"}],
                "swings": [],
            },
            liquidity={
                "sweeps": [{"type": "SWEEP_LOW", "strong": False}],
            },
        )
        assert engine._precheck(ev) is False

    def test_precheck_fails_no_bos(self):
        """Precheck should fail without BOS."""
        from services.worker.engine import TradingEngine
        engine = TradingEngine()
        ev = _make_evidence(
            structure={"bos": [], "choch": [{"direction": "UP"}], "swings": []},
            liquidity={"sweeps": [{"type": "SWEEP_LOW", "strong": True}]},
        )
        assert engine._precheck(ev) is False


class TestHardBlocks:
    def test_htf_bearish_blocks_buy(self):
        """HTF BEARISH bias should block BUY trades."""
        valid, reason = is_trade_valid(None, "BUY", None, "BEARISH")
        assert not valid
        assert "HTF_BIAS_BLOCK" in reason

    def test_sweep_direction_blocks_sell_on_sweep_low(self):
        """SWEEP_LOW should only allow BUY, not SELL."""
        valid, reason = is_trade_valid(None, "SELL", "SWEEP_LOW", "NEUTRAL")
        assert not valid
        assert "SWEEP_DIRECTION_BLOCK" in reason
