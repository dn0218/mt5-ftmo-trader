"""Pip calculation utilities."""

from __future__ import annotations
import MetaTrader5 as mt5


def get_pip_size(symbol: str) -> float:
    """Get pip size for a symbol."""
    info = mt5.symbol_info(symbol)
    if not info:
        return 0.0001  # default forex
    return info.point * 10 if info.digits in (3, 5) else info.point


def price_to_pips(symbol: str, price_distance: float) -> float:
    """Convert price distance to pips."""
    pip = get_pip_size(symbol)
    return price_distance / pip if pip > 0 else 0.0


def pips_to_price(symbol: str, pips: float) -> float:
    """Convert pips to price distance."""
    return pips * get_pip_size(symbol)
