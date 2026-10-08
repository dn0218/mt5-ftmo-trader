"""Tests: TP ladder re-anchor + total-open-risk cap."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeInfo:
    trade_tick_size = 0.01
    trade_tick_value = 1.0
    point = 0.01
    digits = 2
    volume_step = 0.01
    volume_min = 0.01
    volume_max = 100.0


class FakeTick:
    bid = 4144.0
    ask = 4144.5


def test_tp_ladder_reanchored_to_fill(monkeypatch):
    """Slippage between signal close and fill must translate the ladder,
    keeping TP1 at exactly 1R of the FILL geometry."""
    from services.trade_management import manager as tm

    m = tm.TradeManager.__new__(tm.TradeManager)
    m._lock = __import__("threading").Lock()
    m._state_path = "storage/db/trade_state_test.json"
    Path(m._state_path).write_text("{}", encoding="utf-8")

    # 9/28 XAU case: signal close 4155.36, fill 4155.89 (0.53 slip), sl 4167.43
    m.register_trade(
        ticket=1, symbol="XAUUSD.", side="SELL", volume=0.04,
        entry=4155.89, sl=4167.425357, tp=4131.0,
        score=4, confidence=0.9,
        tp1=4143.301786, tp2=4138.0, tp_final=4131.0,
        sig_entry=4155.363571,
    )
    import json
    st = json.loads(Path(m._state_path).read_text(encoding="utf-8"))
    t = st["1"]
    fill_dist = 4167.425357 - 4155.89
    tp1_depth = (4155.89 - t["tp1_price"]) / fill_dist
    assert abs(tp1_depth - 1.0) < 0.01, f"tp1 at {tp1_depth:.3f}R, want 1.0R"
    Path(m._state_path).unlink()
