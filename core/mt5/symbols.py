"""Broker suffix normalisation and currency pair utilities."""

from __future__ import annotations

# Common broker suffixes to strip
_SUFFIXES = (".r", ".e", ".z", ".m", ".i", ".s", "_SB", ".pro", ".raw", "_raw", "_ecn", ".ecn")

# Instrument classes for pip semantics. point*10 is correct for FX (5/3
# digits) and XAUUSD (2 digits, point=0.01 -> pip=$0.10). Indices/crypto
# CFDs (also often 2 digits) must NOT be scaled — their point IS the pip.
# Kept explicit so extending SYMBOLS to new asset classes stays deliberate.
_PIP_TEN = frozenset({"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD",
                      "USDCHF", "USDJPY", "XAUUSD", "XAGUSD"})


def pip_size(symbol: str, info) -> float:
    """Single source of truth for pip size.

    ``info`` is a MetaTrader5 symbol_info object (or any object with
    ``point`` and ``digits``). Returns the size of one pip in price units:
    0.0001 for 5-digit FX, 0.01 for 3-digit JPY pairs, 0.10 for XAUUSD.
    Unlisted symbols fall back to ``info.point`` (no scaling) so index/crypto
    CFDs keep point-as-pip semantics.
    """
    if info is None or not getattr(info, "point", 0):
        return 0.0
    # Strip suffixes AND any residual separators ('EURUSD.' -> 'EURUSD'):
    # broker trailing-dot suffixes aren't in _SUFFIXES, and a leftover dot
    # breaks the whitelist membership test below.
    clean = normalise_symbol(symbol).replace(".", "").replace("_", "")
    if clean in _PIP_TEN:
        return info.point * 10
    return info.point


def normalise_symbol(raw: str) -> str:
    """Strip broker suffixes: 'EURUSD.r' -> 'EURUSD'."""
    upper = raw.upper()
    for suf in _SUFFIXES:
        if upper.endswith(suf.upper()):
            return upper[: -len(suf)]
    return upper


def normalise_symbol(raw: str) -> str:
    """Strip broker suffixes: 'EURUSD.r' -> 'EURUSD'."""
    upper = raw.upper()
    for suf in _SUFFIXES:
        if upper.endswith(suf.upper()):
            return upper[: -len(suf)]
    return upper


def get_currencies(symbol: str) -> tuple[str, str]:
    """Extract base and quote currency from a symbol like EURUSD -> (EUR, USD)."""
    clean = normalise_symbol(symbol)
    # Gold, Silver special cases
    if clean.startswith("XAU"):
        return ("XAU", clean[3:6] if len(clean) >= 6 else "USD")
    if clean.startswith("XAG"):
        return ("XAG", clean[3:6] if len(clean) >= 6 else "USD")
    if len(clean) >= 6:
        return (clean[:3], clean[3:6])
    return (clean, "USD")


def is_gold(symbol: str) -> bool:
    """Check if symbol is gold (XAUUSD or variants)."""
    return normalise_symbol(symbol).startswith("XAU")
