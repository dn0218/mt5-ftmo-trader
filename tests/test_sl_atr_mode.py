"""B1: SL limits ATR-multiple mode (SL_LIMITS_MODE=atr).

Covers: per-symbol MIN/MAX boundaries, jump-guard three states
(pre-jump / post-jump / recovery), fail-closed on missing ATR, and the
default fixed-pips mode regression (must be byte-identical behaviour).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest

from services.risk.risk_manager import RiskManager


@pytest.fixture
def rm():
    return RiskManager()


@pytest.fixture
def atr_mode(monkeypatch):
    monkeypatch.setenv("SL_LIMITS_MODE", "atr")
    monkeypatch.setenv("MIN_SL_ATR_MULT_EURUSD", "0.5")
    monkeypatch.setenv("MAX_SL_ATR_MULT_EURUSD", "4.0")
    monkeypatch.setenv("SL_ATR_JUMP_PCT", "20")


def _ok_tp(entry, sl):
    # 3R TP so RR never trips in these tests
    return entry + 3 * abs(entry - sl)


def test_atr_mode_min_boundary(rm, atr_mode):
    # EURUSD 5-digit: pip=0.0001 (point 0.00001). ATR=0.00100 -> 10 pips.
    # MIN=0.5xATR=5 pips: SL at exactly 5 pips passes, 4.9 fails.
    entry, atr = 1.10000, 0.00100
    ok, _ = rm.validate_sl_tp("EURUSD.", "BUY", entry, entry - 0.00050,
                              _ok_tp(entry, entry - 0.00050), atr=atr)
    assert ok
    ok, reason = rm.validate_sl_tp("EURUSD.", "BUY", entry, entry - 0.00049,
                                   _ok_tp(entry, entry - 0.00049), atr=atr)
    assert not ok and "SL_TOO_TIGHT" in reason


def test_atr_mode_max_boundary(rm, atr_mode):
    # MAX=4.0xATR=40 pips: SL at exactly 40 pips passes, 40.1 fails.
    entry, atr = 1.10000, 0.00100
    ok, _ = rm.validate_sl_tp("EURUSD.", "BUY", entry, entry - 0.00400,
                              _ok_tp(entry, entry - 0.00400), atr=atr)
    assert ok
    ok, reason = rm.validate_sl_tp("EURUSD.", "BUY", entry, entry - 0.00401,
                                   _ok_tp(entry, entry - 0.00401), atr=atr)
    assert not ok and "SL_TOO_WIDE" in reason


def test_jump_guard_uses_pre_jump_atr(rm, atr_mode):
    """ATR jumped 50% (>20% p99 threshold): the threshold must be computed
    from the PRE-jump ATR, not the spike."""
    entry = 1.10000
    atr_prev = 0.00100          # pre-jump: MIN=5 pips
    atr_spike = 0.00150         # +50% jump
    # SL at 5.5 pips: passes pre-jump MIN (5.0) but FAILS spike MIN (7.5)
    sl = entry - 0.00055
    ok, reason = rm.validate_sl_tp("EURUSD.", "BUY", entry, sl, _ok_tp(entry, sl),
                                   atr=atr_spike, atr_prev=atr_prev)
    # pre-jump basis -> 5.5 pips >= 5.0 -> allowed (and note recorded)
    assert ok
    # SL at 7.4 pips still under spike-basis MIN(7.5) but over pre-jump MIN:
    sl2 = entry - 0.00074
    ok2, _ = rm.validate_sl_tp("EURUSD.", "BUY", entry, sl2, _ok_tp(entry, sl2),
                               atr=atr_spike, atr_prev=atr_prev)
    assert ok2


def test_jump_guard_recovery(rm, atr_mode):
    """After the spike, a normal bar (vs prev EMA still elevated but jump<20%)
    must use the CURRENT ATR — guard only reacts to the jump bar itself."""
    entry = 1.10000
    # current 0.00126 vs prev 0.00120 = +5% jump -> no guard, current basis
    sl = entry - 0.00055            # 5.5 pips vs MIN 0.5*0.00126=6.3 pips -> TIGHT
    ok, reason = rm.validate_sl_tp("EURUSD.", "BUY", entry, sl, _ok_tp(entry, sl),
                                   atr=0.00126, atr_prev=0.00120)
    assert not ok and "SL_TOO_TIGHT" in reason


def test_atr_missing_fails_closed(rm, atr_mode):
    entry = 1.10000
    sl = entry - 0.00100
    ok, reason = rm.validate_sl_tp("EURUSD.", "BUY", entry, sl, _ok_tp(entry, sl),
                                   atr=None)
    assert not ok and "ATR_UNAVAILABLE" in reason
    ok, reason = rm.validate_sl_tp("EURUSD.", "BUY", entry, sl, _ok_tp(entry, sl),
                                   atr=0.0)
    assert not ok and "ATR_UNAVAILABLE" in reason


def test_fixed_mode_unchanged(rm, monkeypatch):
    """Default mode must behave exactly as before B1 (regression guard)."""
    monkeypatch.delenv("SL_LIMITS_MODE", raising=False)
    entry = 1.10000
    sl = entry - 0.00100        # 10 pips < MIN 15 -> TIGHT regardless of ATR
    ok, reason = rm.validate_sl_tp("EURUSD.", "BUY", entry, sl, _ok_tp(entry, sl),
                                   atr=0.05)   # would pass under atr mode MIN
    assert not ok and "SL_TOO_TIGHT" in reason and "ATR" not in reason
