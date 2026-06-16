"""Technical indicators: EMA, RSI, ATR, OBV."""

from __future__ import annotations
import pandas as pd
import numpy as np


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=span, adjust=False).mean()


def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA9, EMA21, EMA50, EMA200 columns."""
    for span in (9, 21, 50, 200):
        df[f"ema{span}"] = ema(df["close"], span)
    return df


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    direction = np.sign(df["close"].diff()).fillna(0)
    vol = df.get("tick_volume", df.get("volume", pd.Series(0, index=df.index)))
    return (direction * vol).cumsum()


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all standard indicators to dataframe."""
    df = add_emas(df)
    df["rsi14"] = rsi(df["close"], 14)
    df["atr14"] = atr(df, 14)
    df["obv"] = obv(df)
    return df
