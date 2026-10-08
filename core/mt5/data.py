"""MT5 data client: bars, positions, tick info."""

from __future__ import annotations
import logging
import pandas as pd
import MetaTrader5 as mt5

logger = logging.getLogger(__name__)


class MT5Client:
    """Wrapper around MT5 Python API for data retrieval."""

    @staticmethod
    def get_bars(symbol: str, timeframe: int, count: int = 500) -> pd.DataFrame | None:
        """Fetch OHLCV bars, sorted oldest-first."""
        # Ensure the symbol is selected in MarketWatch — suffixed broker symbols
        # (e.g. "EURUSD.") return no bars from copy_rates unless selected.
        try:
            info = mt5.symbol_info(symbol)
            if info is not None and not info.visible:
                mt5.symbol_select(symbol, True)
        except Exception:
            pass

        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            logger.warning("No bars returned for %s tf=%s", symbol, timeframe)
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.sort_values("time").reset_index(drop=True)
        return df

    @staticmethod
    def get_positions(symbol: str | None = None) -> list:
        """Get open positions, optionally filtered by symbol."""
        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()
        return list(positions) if positions else []

    @staticmethod
    def get_tick(symbol: str):
        """Get latest tick for symbol."""
        return mt5.symbol_info_tick(symbol)

    @staticmethod
    def get_symbol_info(symbol: str):
        """Get symbol info (volumes, tick sizes, etc.)."""
        return mt5.symbol_info(symbol)

    @staticmethod
    def get_account_info():
        """Get account info."""
        return mt5.account_info()
