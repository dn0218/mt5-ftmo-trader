"""Tests for FTMO risk manager."""

import os
import sys
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import MetaTrader5 as mt5
from services.risk.risk_manager import RiskManager


@pytest.fixture(autouse=True)
def reset_mt5_mocks():
    """Reset MT5 mocks before each test."""
    mt5.account_info.return_value = MagicMock(equity=10000.0, balance=10000.0)
    mt5.positions_get.return_value = []
    info = MagicMock()
    info.volume_min = 0.01
    info.volume_max = 100.0
    info.volume_step = 0.01
    info.trade_tick_value = 1.0
    info.trade_tick_size = 0.00001
    info.point = 0.00001
    info.digits = 5
    info.trade_stops_level = 10
    mt5.symbol_info.return_value = info
    tick = MagicMock()
    tick.ask = 1.08550
    tick.bid = 1.08540
    mt5.symbol_info_tick.return_value = tick
    yield


class TestVolumeNormalisation:
    def test_volume_normalisation_eurusd(self):
        """0.013 lots should floor to 0.01 with step=0.01."""
        rm = RiskManager()
        info = mt5.symbol_info.return_value
        info.volume_step = 0.01
        info.volume_min = 0.01

        # Set equity so raw calculation yields ~0.013
        mt5.account_info.return_value = MagicMock(equity=10000.0, balance=10000.0)
        vol = rm.calculate_volume("EURUSD", 1.08500, 1.08200)
        assert vol is not None
        # Volume should be a multiple of 0.01
        assert round(vol / 0.01) == vol / 0.01

    def test_volume_normalisation_gold(self):
        """Gold with min_vol=0.01 should not go below."""
        rm = RiskManager()
        info = mt5.symbol_info.return_value
        info.volume_step = 0.01
        info.volume_min = 0.01
        info.point = 0.01
        info.digits = 2
        info.trade_tick_value = 1.0
        info.trade_tick_size = 0.01

        vol = rm.calculate_volume("XAUUSD", 2000.0, 1990.0)
        assert vol is not None
        assert vol >= 0.01


class TestDrawdown:
    def test_daily_drawdown_halts_at_limit(self):
        """Daily DD >= 2% should halt trading."""
        rm = RiskManager()
        # Simulate: started at 10000, now at 9790 (2.1% DD)
        mt5.account_info.return_value = MagicMock(equity=9790.0, balance=10000.0)

        from services.risk.state_store import write_state
        write_state(rm._state_path, {
            "day_start_equity": 10000.0,
            "today": "2024-01-01",
            "consecutive_losses": 0,
            "halted_today": False,
            "sl_cooldowns": {},
            "initial_balance": 10000.0,
        })

        can, reason = rm.can_trade("EURUSD")
        assert not can
        assert "DAILY_DD" in reason

    def test_total_drawdown_halts_at_limit(self):
        """Total DD >= 6% should halt all trading."""
        rm = RiskManager()
        mt5.account_info.return_value = MagicMock(equity=9350.0, balance=10000.0)

        from services.risk.state_store import write_state
        write_state(rm._state_path, {
            "day_start_equity": 9500.0,
            "today": "2024-01-01",
            "consecutive_losses": 0,
            "halted_today": False,
            "sl_cooldowns": {},
            "initial_balance": 10000.0,
        })

        can, reason = rm.can_trade("EURUSD")
        assert not can
        assert "TOTAL_DD" in reason


class TestCircuitBreakers:
    def test_consecutive_loss_halt(self):
        """3 consecutive losses should halt for the day."""
        rm = RiskManager()
        from services.risk.state_store import write_state
        write_state(rm._state_path, {
            "day_start_equity": 10000.0,
            "today": "2024-01-01",
            "consecutive_losses": 3,
            "halted_today": False,
            "sl_cooldowns": {},
            "initial_balance": 10000.0,
        })

        can, reason = rm.can_trade("EURUSD")
        assert not can
        assert "CONSECUTIVE" in reason

    def test_spread_check_blocks_trade(self):
        """Spread > MAX_SPREAD_POINTS should block trading."""
        rm = RiskManager()
        from services.risk.state_store import write_state
        write_state(rm._state_path, {
            "day_start_equity": 10000.0,
            "today": "2024-01-01",
            "consecutive_losses": 0,
            "halted_today": False,
            "sl_cooldowns": {},
            "initial_balance": 10000.0,
        })

        # Set huge spread
        tick = MagicMock()
        tick.ask = 1.09000
        tick.bid = 1.08500  # 500 points spread
        mt5.symbol_info_tick.return_value = tick
        info = mt5.symbol_info.return_value
        info.point = 0.00001

        can, reason = rm.can_trade("EURUSD")
        assert not can
        assert "SPREAD" in reason


class TestRiskReward:
    def test_rr_float_precision(self):
        """RR of 2.0000001 should NOT be rejected (float precision)."""
        rm = RiskManager()
        # SL/TP that gives exactly 2.0 RR (with float noise)
        entry = 1.08500
        sl = 1.08200  # 30 pips
        tp = 1.09100  # 60 pips -> RR = 2.0

        valid, reason = rm.validate_sl_tp("EURUSD", "BUY", entry, sl, tp)
        assert valid, f"Should be valid but got: {reason}"
