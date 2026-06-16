"""Broker suffix normalisation and currency pair utilities."""

from __future__ import annotations

# Common broker suffixes to strip
_SUFFIXES = (".r", ".e", ".z", ".m", ".i", ".s", "_SB", ".pro", ".raw")


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
