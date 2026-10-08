"""Lock down pip_size/normalise_symbol behaviour — the conversion that
silently locked XAUUSD for a week must never regress."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from core.mt5.symbols import pip_size, normalise_symbol


class _Info:
    def __init__(self, point, digits):
        self.point = point
        self.digits = digits


@pytest.mark.parametrize("raw,point,digits,expected", [
    # FX majors, 5-digit, broker trailing dot
    ("EURUSD.", 0.00001, 5, 0.0001),
    ("GBPUSD.", 0.00001, 5, 0.0001),
    ("AUDUSD.", 0.00001, 5, 0.0001),
    # JPY 3-digit
    ("USDJPY.", 0.001, 3, 0.01),
    # Gold 2-digit: pip = point*10 = $0.10 (the bug that locked XAU)
    ("XAUUSD.", 0.01, 2, 0.1),
    # broker-suffixed variants
    ("XAUUSD.r", 0.01, 2, 0.1),
    ("EURUSD.pro", 0.00001, 5, 0.0001),
    # non-whitelisted: point as pip (index/crypto CFD semantics)
    ("US30", 0.1, 1, 0.1),
    ("BTCUSD", 0.01, 2, 0.01),
])
def test_pip_size(raw, point, digits, expected):
    assert pip_size(raw, _Info(point, digits)) == pytest.approx(expected)


def test_pip_size_none_info():
    assert pip_size("EURUSD.", None) == 0.0
    assert pip_size("EURUSD.", _Info(0, 5)) == 0.0


@pytest.mark.parametrize("raw,expected", [
    ("EURUSD.", "EURUSD"),
    ("XAUUSD.r", "XAUUSD"),
    ("EURUSD.pro", "EURUSD"),
    ("US30", "US30"),
])
def test_normalise_strips_to_whitelist_form(raw, expected):
    assert normalise_symbol(raw).replace(".", "").replace("_", "") == expected
