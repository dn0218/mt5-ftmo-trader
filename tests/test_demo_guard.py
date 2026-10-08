"""Demo validation hard guard: total DD >= 10% on a demo account halts
all new entries with DEMO_DRAWDOWN_HALT, latches (survives midnight reset),
and never fires on live accounts."""

import os
import sys
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import MetaTrader5 as mt5

# Real MT5 constant (mock auto-attribute would break the == comparison)
mt5.ACCOUNT_TRADE_MODE_DEMO = 0

from services.risk.risk_manager import RiskManager
from services.risk.state_store import read_state, write_state


@pytest.fixture(autouse=True)
def snapshot_prod_state():
    """Snapshot & restore the production risk_state.json around each test."""
    rm = RiskManager()
    saved = read_state(rm._state_path, {})
    yield
    if saved:
        write_state(rm._state_path, saved)
    else:
        try:
            os.remove(rm._state_path)
        except OSError:
            pass


def _set_account(equity: float, demo: bool):
    mt5.account_info.return_value = MagicMock(
        equity=equity, balance=10000.0,
        trade_mode=0 if demo else 1)


def _write_state(day_start: float = 10000.0, **overrides):
    rm = RiskManager()
    base = {
        "day_start_equity": day_start,
        "day_start_balance": 10000.0,
        "today": "2026-10-06",
        "consecutive_losses": 0,
        "trades_today": 0,
        "halted_today": False,
        "sl_cooldowns": {},
        "initial_balance": 10000.0,
    }
    base.update(overrides)
    write_state(rm._state_path, base)
    return rm


class TestDemoDrawdownHalt:
    def test_demo_total_dd_10pct_halts(self):
        """Demo account, total DD 10.5% (daily DD isolated to 0) ->
        DEMO_DRAWDOWN_HALT fires before any other breaker."""
        _set_account(8950.0, demo=True)
        rm = _write_state(day_start=8950.0)  # daily DD = 0
        can, reason = rm.can_trade("EURUSD")
        assert not can
        assert reason == "DEMO_DRAWDOWN_HALT"
        # Audit-latched in state
        assert rm._state().get("demo_dd_halt") is True

    def test_halt_latches_across_midnight(self):
        """Once latched, the flag survives even with recovered equity."""
        _set_account(9900.0, demo=True)
        rm = _write_state(day_start=9900.0, demo_dd_halt=True)
        can, reason = rm.can_trade("EURUSD")
        assert not can
        assert reason == "DEMO_DRAWDOWN_HALT"

    def test_live_account_not_blocked_by_demo_guard(self):
        """Same -10.5% total DD on a LIVE account: the demo guard stays
        silent (falls through to the regular configured breakers)."""
        _set_account(8950.0, demo=False)
        rm = _write_state(day_start=8950.0)  # daily DD = 0, total DD 10.5%
        can, reason = rm.can_trade("EURUSD")
        assert not can  # test-env MAX_TOTAL_DRAWDOWN=6 fires instead
        assert "DEMO" not in reason

    def test_demo_below_threshold_trades_normally(self):
        """Demo account, DD 5% (< 10% guard) -> guard silent, trade OK."""
        _set_account(9500.0, demo=True)
        rm = _write_state(day_start=9500.0)  # daily DD = 0, total DD 5%
        can, reason = rm.can_trade("EURUSD")
        assert can
        assert reason == "OK"
        assert not rm._state().get("demo_dd_halt")

    def test_manual_reset_clears_halt(self):
        """Deleting the latch key is the documented manual recovery path."""
        _set_account(9900.0, demo=True)
        rm = _write_state(day_start=9900.0)
        state = rm._state()
        state.pop("demo_dd_halt", None)
        state["halted_today"] = False
        rm._save(state)
        can, reason = rm.can_trade("EURUSD")
        assert can
        assert reason == "OK"
