"""Output-format + arithmetic tests for verify_numbers rendering layer.

Born from a real incident: a %.2f display truncation made EURUSD MIN_$
read 0.0001 (== pip) instead of 0.0015 (== 15 x pip) — and 88 tests
didn't catch it because they only covered computation, not presentation.
These tests lock BOTH the arithmetic and the rendered format.
"""
import re, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from verify_numbers import ConversionRow, format_table


def _rows():
    return [
        ConversionRow("EURUSD.", 5, 0.00001, 0.0001, "x10", 15, 150),
        ConversionRow("USDJPY.", 3, 0.001, 0.01, "x10", 15, 150),
        ConversionRow("XAUUSD.", 2, 0.01, 0.1, "x10", 60, 300),
        ConversionRow("US30", 1, 0.1, 0.1, "raw", 15, 150),
    ]


def test_min_dollar_equals_pips_times_pip():
    """Arithmetic invariant: every rendered MIN_$/MAX_$ == pips x pip."""
    out = format_table(_rows())
    for line in out.splitlines()[1:]:
        parts = line.split()
        min_pips, min_d = float(parts[5]), float(parts[6])
        max_pips, max_d = float(parts[7]), float(parts[8])
        pip = float(parts[3])
        assert abs(min_d - min_pips * pip) < 1e-9, line
        assert abs(max_d - max_pips * pip) < 1e-9, line


def test_dollar_columns_have_4_decimals():
    """Display invariant: MIN_$/MAX_$ render with exactly 4 decimals so
    FX-scale values (0.0015) are never truncated into ambiguity."""
    out = format_table(_rows())
    for line in out.splitlines()[1:]:
        m = re.match(r"\S+\s+\d+\s+\d+\.\d+\s+\d+\.\d+\s+\w+\s+\d+\s+(\d+\.\d{4})\s+\d+\s+(\d+\.\d{4})", line)
        assert m, f"format regression: {line!r}"


def test_eurusd_min_dollar_not_pip_value():
    """The exact incident: EURUSD MIN_$ must be 0.0015, not the pip 0.0001."""
    out = format_table(_rows())
    eurusd = next(l for l in out.splitlines() if l.startswith("EURUSD"))
    assert "0.0015" in eurusd
    # and it must differ from the pip column value
    pip_col = float(eurusd.split()[3])
    assert float(eurusd.split()[6]) != pip_col or pip_col == 0.0015
