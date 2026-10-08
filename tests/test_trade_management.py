"""Tests for the trade manager: BE, partial TP via absolute prices, trailing, rebuild."""

import os
import sys
import json
import tempfile
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import MetaTrader5 as mt5
from services.trade_management.manager import TradeManager


@pytest.fixture
def fresh_state(monkeypatch, tmp_path):
    """Point trade_state.json at a tmp file for each test."""
    state_file = tmp_path / "trade_state.json"
    state_file.write_text("{}")
    monkeypatch.setattr(
        "services.trade_management.manager._TRADE_STATE_PATH", str(state_file)
    )
    yield str(state_file)


@pytest.fixture
def manager(fresh_state):
    risk = MagicMock()
    tm = TradeManager(risk)
    tm._state_path = fresh_state
    return tm


def _pos(ticket=1, symbol="EURUSD", side="BUY", entry=1.0850, sl=1.0800,
         tp=1.0950, current=1.0850, volume=0.10):
    p = MagicMock()
    p.ticket = ticket
    p.symbol = symbol
    p.type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    p.price_open = entry
    p.sl = sl
    p.tp = tp
    p.price_current = current
    p.volume = volume
    return p


def _tick_at(price: float):
    """Build a tick whose bid AND ask both equal ``price``.

    Manager uses bid for BUY (close-side) and ask for SELL — mirroring price on
    both keeps tests independent of side.
    """
    t = MagicMock()
    t.bid = price
    t.ask = price
    return t


class TestRegisterTrade:
    def test_register_stores_multi_tp(self, manager, fresh_state):
        manager.register_trade(
            ticket=1, symbol="EURUSD", side="BUY", volume=0.10,
            entry=1.0850, sl=1.0800, tp=1.0950,
            score=4, confidence=0.8,
            tp1=1.0900, tp2=1.0925, tp_final=1.0950,
        )
        state = json.loads(open(fresh_state).read())
        assert "1" in state
        assert state["1"]["tp1_price"] == 1.0900
        assert state["1"]["tp2_price"] == 1.0925
        assert state["1"]["tp_final"] == 1.0950
        assert state["1"]["be_triggered"] is False
        assert state["1"]["tp1_closed"] is False

    def test_register_falls_back_to_synthesized_ladder(self, manager, fresh_state):
        manager.register_trade(
            ticket=1, symbol="EURUSD", side="BUY", volume=0.10,
            entry=1.0850, sl=1.0800, tp=1.0950,
            score=3, confidence=0.7,
        )
        state = json.loads(open(fresh_state).read())
        # tp1 = entry + 1R = 1.0900
        assert abs(state["1"]["tp1_price"] - 1.0900) < 1e-6


class TestTP1FiresOnPriceCross:
    def test_tp1_partial_close_fires_when_price_crosses(self, manager, fresh_state):
        manager.register_trade(
            ticket=1, symbol="EURUSD", side="BUY", volume=0.10,
            entry=1.0850, sl=1.0800, tp=1.0950,
            score=3, confidence=0.7,
            tp1=1.0900, tp2=1.0925, tp_final=1.0950,
        )
        # Position at price above tp1 → TP1 should fire.
        with patch.object(mt5, "positions_get", return_value=[_pos(current=1.0905)]), \
             patch.object(mt5, "symbol_info_tick", return_value=_tick_at(1.0905)), \
             patch.object(manager, "_partial_close", return_value=True) as pc, \
             patch.object(manager, "_modify_sl", return_value=True):
            manager.check_positions()

        # TP1 fired (called with label TP1 and 50% pct).
        assert pc.called
        assert any(call.args[3] == 50 and call.args[4] == "TP1"
                   for call in pc.call_args_list)
        state = json.loads(open(fresh_state).read())
        assert state["1"]["tp1_closed"] is True
        # BE should also have been triggered (current_r >= 1).
        assert state["1"]["be_triggered"] is True

    def test_tp1_does_not_fire_below_threshold(self, manager, fresh_state):
        manager.register_trade(
            ticket=1, symbol="EURUSD", side="BUY", volume=0.10,
            entry=1.0850, sl=1.0800, tp=1.0950,
            score=3, confidence=0.7,
            tp1=1.0900, tp2=1.0925, tp_final=1.0950,
        )
        # Below tp1 — no partial close.
        with patch.object(mt5, "positions_get", return_value=[_pos(current=1.0870)]), \
             patch.object(mt5, "symbol_info_tick", return_value=_tick_at(1.0870)), \
             patch.object(manager, "_partial_close", return_value=True) as pc, \
             patch.object(manager, "_modify_sl", return_value=True):
            manager.check_positions()
        assert not pc.called
        state = json.loads(open(fresh_state).read())
        assert state["1"]["tp1_closed"] is False

    def test_be_fires_independently_at_half_r_before_tp1(self, manager, fresh_state):
        """Path B/C gap found in review: BE triggers at 0.5R on its own,
        WITHOUT TP1. Price at +0.55R (between BE_TRIGGER_R=0.5 and TP1=1R)
        must move SL to entry and must NOT partial-close."""
        manager.register_trade(
            ticket=1, symbol="EURUSD", side="BUY", volume=0.10,
            entry=1.0850, sl=1.0800, tp=1.0950,
            score=3, confidence=0.7,
            tp1=1.0900, tp2=1.0925, tp_final=1.0950,
        )
        # +0.55R = 1.0850 + 0.55*0.005 = 1.08775
        with patch.object(mt5, "positions_get", return_value=[_pos(current=1.0878)]), \
             patch.object(mt5, "symbol_info_tick", return_value=_tick_at(1.0878)), \
             patch.object(manager, "_partial_close", return_value=True) as pc, \
             patch.object(manager, "_modify_sl", return_value=True) as ms:
            manager.check_positions()
        assert ms.called, "BE must fire at 0.5R without TP1"
        assert not pc.called, "TP1 must NOT fire at +0.55R"
        state = json.loads(open(fresh_state).read())
        assert state["1"]["be_triggered"] is True
        assert state["1"]["sl"] == 1.0850
        assert state["1"]["tp1_closed"] is False


class TestProbeMatrix:
    """Boundary probe matrix (single probes prove single scenarios; matrices
    prove behaviour). Probe points are COMPUTED from live config/geometry —
    never hardcoded — so an upstream change to BE_TRIGGER_R or ladder
    geometry moves the probes with it. (A hardcoded probe silently degrades
    from boundary-detection to regression-detection and keeps passing while
    behaviour has already changed.)"""

    GEOM = dict(entry=1.0850, sl=1.0800, tp1=1.0900, tp2=1.0950, tp_final=1.1000)
    _D = GEOM["entry"] - GEOM["sl"]                       # 1R in price
    BE_R = float(os.getenv("BE_TRIGGER_R", "0.5"))        # follows config
    TP1_R = (GEOM["tp1"] - GEOM["entry"]) / _D            # ladder geometry
    TP2_R = (GEOM["tp2"] - GEOM["entry"]) / _D

    def _register(self, manager):
        g = self.GEOM
        manager.register_trade(
            ticket=1, symbol="EURUSD", side="BUY", volume=0.10,
            entry=g["entry"], sl=g["sl"], tp=g["tp_final"],
            score=3, confidence=0.7,
            tp1=g["tp1"], tp2=g["tp2"], tp_final=g["tp_final"],
        )

    def _price(self, r: float) -> float:
        return round(self.GEOM["entry"] + r * self._D, 5)

    def _probe(self, manager, price):
        with patch.object(mt5, "positions_get", return_value=[_pos(current=price)]), \
             patch.object(mt5, "symbol_info_tick", return_value=_tick_at(price)), \
             patch.object(manager, "_partial_close", return_value=True) as pc, \
             patch.object(manager, "_modify_sl", return_value=True) as ms:
            manager.check_positions()
        return pc, ms

    @pytest.mark.parametrize("r,expect_be", [
        (BE_R - 0.01, False),   # just below boundary
        (BE_R, True),           # exactly on boundary (>= semantics)
        (BE_R + 0.05, True),    # near-above (edge jitter zone)
        (BE_R + 0.15, True),    # far-above (unambiguous post-trigger)
    ])
    def test_be_boundary(self, manager, fresh_state, r, expect_be):
        self._register(manager)
        _, ms = self._probe(manager, self._price(r))
        state = json.loads(open(fresh_state).read())
        assert state["1"]["be_triggered"] is expect_be, f"BE at {r}R"
        assert ms.called == expect_be

    @pytest.mark.parametrize("r,expect_tp1", [
        (TP1_R - 0.01, False), (TP1_R, True), (TP1_R + 0.01, True),
    ])
    def test_tp1_boundary(self, manager, fresh_state, r, expect_tp1):
        self._register(manager)
        pc, _ = self._probe(manager, self._price(r))
        state = json.loads(open(fresh_state).read())
        assert state["1"]["tp1_closed"] is expect_tp1, f"TP1 at {r}R"

    @pytest.mark.parametrize("r,expect_tp2", [
        (TP2_R - 0.01, False), (TP2_R, True), (TP2_R + 0.01, True),
    ])
    def test_tp2_boundary_after_tp1(self, manager, fresh_state, r, expect_tp2):
        self._register(manager)
        self._probe(manager, self._price(self.TP1_R + 0.01))   # TP1 first (gating)
        self._probe(manager, self._price(r))
        state = json.loads(open(fresh_state).read())
        assert state["1"]["tp2_closed"] is expect_tp2, f"TP2 at {r}R"

    def test_probes_follow_config_change(self, manager, fresh_state, monkeypatch):
        """Meta-probe: proves the parameterisation works. With BE_TRIGGER_R=0.6
        the OLD hardcoded probes (0.49/0.50/0.55) would all sit below the new
        boundary and 'pass' vacuously — these probes must follow the config."""
        monkeypatch.setenv("BE_TRIGGER_R", "0.6")
        self._register(manager)
        _, ms = self._probe(manager, self._price(0.59))       # below new boundary
        state = json.loads(open(fresh_state).read())
        assert state["1"]["be_triggered"] is False, "0.59R must not trigger BE at 0.6"
        _, ms = self._probe(manager, self._price(0.60))       # on new boundary
        state = json.loads(open(fresh_state).read())
        assert state["1"]["be_triggered"] is True, "0.60R must trigger BE at 0.6"


class TestTP2GatedOnTP1:
    def test_tp2_does_not_fire_before_tp1(self, manager, fresh_state):
        manager.register_trade(
            ticket=1, symbol="EURUSD", side="BUY", volume=0.10,
            entry=1.0850, sl=1.0800, tp=1.0950,
            score=3, confidence=0.7,
            tp1=1.0900, tp2=1.0925, tp_final=1.0950,
        )
        # Force tp1_closed False, even though price already past tp2 (rare/unrealistic).
        # Manager must respect ordering.
        with patch.object(mt5, "positions_get", return_value=[_pos(current=1.0930)]), \
             patch.object(mt5, "symbol_info_tick", return_value=_tick_at(1.0930)), \
             patch.object(manager, "_partial_close", return_value=False) as pc, \
             patch.object(manager, "_modify_sl", return_value=True):
            # First call closes TP1.
            manager.check_positions()
        # TP1 should have been attempted (returned False), TP2 must still wait.
        assert any(call.args[4] == "TP1" for call in pc.call_args_list)


class TestSellSide:
    def test_sell_tp1_fires_on_price_drop(self, manager, fresh_state):
        manager.register_trade(
            ticket=2, symbol="EURUSD", side="SELL", volume=0.10,
            entry=1.0850, sl=1.0900, tp=1.0750,
            score=3, confidence=0.7,
            tp1=1.0800, tp2=1.0775, tp_final=1.0750,
        )
        with patch.object(mt5, "positions_get",
                          return_value=[_pos(ticket=2, side="SELL", entry=1.0850,
                                             sl=1.0900, tp=1.0750, current=1.0795)]), \
             patch.object(mt5, "symbol_info_tick", return_value=_tick_at(1.0795)), \
             patch.object(manager, "_partial_close", return_value=True) as pc, \
             patch.object(manager, "_modify_sl", return_value=True):
            manager.check_positions()
        assert any(call.args[4] == "TP1" for call in pc.call_args_list)


class TestRebuildFromMT5:
    def test_adopts_unknown_position(self, manager, fresh_state):
        # State empty, MT5 has one position → rebuild should adopt it.
        with patch.object(mt5, "positions_get",
                          return_value=[_pos(ticket=99, entry=1.0850, sl=1.0800, tp=1.0950)]):
            adopted = manager.rebuild_from_mt5()
        assert adopted == 1
        state = json.loads(open(fresh_state).read())
        assert "99" in state
        assert state["99"]["entry"] == 1.0850

    def test_does_not_duplicate_existing_position(self, manager, fresh_state):
        manager.register_trade(
            ticket=99, symbol="EURUSD", side="BUY", volume=0.10,
            entry=1.0850, sl=1.0800, tp=1.0950,
            score=3, confidence=0.7,
        )
        with patch.object(mt5, "positions_get",
                          return_value=[_pos(ticket=99)]):
            adopted = manager.rebuild_from_mt5()
        assert adopted == 0

    def test_no_positions_returns_zero(self, manager):
        with patch.object(mt5, "positions_get", return_value=[]):
            assert manager.rebuild_from_mt5() == 0
