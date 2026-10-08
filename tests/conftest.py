"""Test fixtures — MT5 mocked, Telegram disabled, OHLCV generators."""

import sys
import os
import json
from unittest.mock import MagicMock
import pytest

# ── Mock MT5 at import time (before any module imports it) ──
_mt5 = MagicMock()
_mt5.TIMEFRAME_M15 = 15
_mt5.TIMEFRAME_H4 = 16408
_mt5.ORDER_TYPE_BUY = 0
_mt5.ORDER_TYPE_SELL = 1
_mt5.ORDER_FILLING_FOK = 0
_mt5.ORDER_FILLING_IOC = 1
_mt5.ORDER_FILLING_RETURN = 2
_mt5.TRADE_RETCODE_DONE = 10009
_mt5.TRADE_RETCODE_REQUOTE = 10004
_mt5.TRADE_ACTION_DEAL = 1
_mt5.TRADE_ACTION_SLTP = 6
_mt5.ORDER_TIME_GTC = 0
_mt5.initialize.return_value = True
_mt5.login.return_value = True
_mt5.account_info.return_value = MagicMock(equity=10000.0, balance=10000.0)

# Symbol info mock
_sym_info = MagicMock()
_sym_info.volume_min = 0.01
_sym_info.volume_max = 100.0
_sym_info.volume_step = 0.01
_sym_info.trade_tick_value = 1.0
_sym_info.trade_tick_size = 0.00001
_sym_info.point = 0.00001
_sym_info.digits = 5
_sym_info.trade_stops_level = 10
_mt5.symbol_info.return_value = _sym_info

# Tick mock
_tick = MagicMock()
_tick.ask = 1.08550
_tick.bid = 1.08540
_mt5.symbol_info_tick.return_value = _tick

_mt5.positions_get.return_value = []

sys.modules["MetaTrader5"] = _mt5

# ── Silence Telegram during tests ──
os.environ["TELEGRAM_ENABLED"] = "false"
os.environ["DRY_RUN"] = "true"
# Isolate the audit log: synthetic events must NEVER append to the
# production storage/logs/audit.jsonl (ticket=99 phantom TRADE_ADOPTED
# events from test runs polluted funnel stats across three analysis rounds).
import tempfile
_AUDIT_ISOLATION = tempfile.mkdtemp(prefix="audit_iso_")
os.environ["AUDIT_LOG_OVERRIDE"] = os.path.join(_AUDIT_ISOLATION, "audit.jsonl")
# Use Ollama provider in tests (mocked via respx)
os.environ["LLM_PROVIDER"] = "ollama"
os.environ["LLM_ENABLED"] = "true"
os.environ["OLLAMA_ENABLED"] = "true"
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
os.environ["OLLAMA_MODEL"] = "qwen3:4b"
os.environ["OLLAMA_NUM_CTX"] = "4096"
os.environ["MIN_LLM_CONFIDENCE"] = "0.70"
os.environ["RISK_PER_TRADE"] = "0.5"
os.environ["MAX_DAILY_DRAWDOWN"] = "2.0"
os.environ["MAX_TOTAL_DRAWDOWN"] = "6.0"
os.environ["RISK_REWARD_MIN"] = "2.0"
os.environ["MAX_SPREAD_POINTS"] = "25"
os.environ["CONSECUTIVE_LOSS_HALT"] = "3"
os.environ["MAX_CONCURRENT_POSITIONS"] = "2"
os.environ["MIN_SL_PIPS"] = "15"
os.environ["MAX_SL_PIPS"] = "150"
os.environ["PRECHECK_REQUIRE_BOS"] = "true"
os.environ["PRECHECK_REQUIRE_CHOCH"] = "true"
os.environ["PRECHECK_REQUIRE_SWEEP"] = "true"
os.environ["PRECHECK_REQUIRE_STRONG_SWEEP"] = "true"
os.environ["SWEEP_DIRECTION_FILTER_ENABLED"] = "true"
os.environ["HTF_FILTER_ENABLED"] = "true"

# ── OHLCV generators ──
import pandas as pd
import numpy as np


def make_trending_up(n: int = 200) -> pd.DataFrame:
    np.random.seed(42)
    close = 1.08 + np.cumsum(np.random.normal(0.0002, 0.001, n))
    high = close + np.abs(np.random.normal(0, 0.0005, n))
    low = close - np.abs(np.random.normal(0, 0.0005, n))
    opn = close - np.random.normal(0, 0.0003, n)
    times = pd.date_range("2024-01-01", periods=n, freq="15min")
    return pd.DataFrame({
        "time": times, "open": opn, "high": high,
        "low": low, "close": close, "tick_volume": 100,
    })


def make_ranging(n: int = 200) -> pd.DataFrame:
    np.random.seed(42)
    t = np.linspace(0, 4 * np.pi, n)
    close = 1.08 + 0.005 * np.sin(t)
    high = close + 0.0003
    low = close - 0.0003
    times = pd.date_range("2024-01-01", periods=n, freq="15min")
    return pd.DataFrame({
        "time": times, "open": close, "high": high,
        "low": low, "close": close, "tick_volume": 50,
    })


def make_volatile(n: int = 200) -> pd.DataFrame:
    df = make_ranging(n)
    df.loc[df.index[-5:], "high"] *= 1.02
    df.loc[df.index[-5:], "low"] *= 0.98
    return df


# ── Ollama fixtures ──
import httpx

try:
    import respx
    HAS_RESPX = True
except ImportError:
    HAS_RESPX = False


@pytest.fixture
def ollama_allow():
    if not HAS_RESPX:
        pytest.skip("respx not installed")
    body = {
        "decision": "ALLOW", "confidence": 0.85,
        "justification": "Strong sweep + CHOCH + OB present",
        "violations": [], "conditions_met": ["SWEEP", "CHOCH", "OB"],
        "conditions_failed": [], "strategy_version": "v1",
        "trade": {"symbol": "EURUSD", "side": "BUY",
                  "volume": 0.01, "sl": 1.0820, "tp": 1.0920},
    }
    with respx.mock:
        respx.post("http://localhost:11434/api/chat").mock(
            return_value=httpx.Response(200, json={"message": {"content": json.dumps(body)}})
        )
        yield body


@pytest.fixture
def ollama_refuse():
    if not HAS_RESPX:
        pytest.skip("respx not installed")
    body = {
        "decision": "REFUSE", "confidence": 0.3,
        "justification": "No strong sweep present",
        "violations": ["NO_STRONG_SWEEP"], "conditions_met": [],
        "conditions_failed": ["STRONG_SWEEP"], "strategy_version": "v1",
    }
    with respx.mock:
        respx.post("http://localhost:11434/api/chat").mock(
            return_value=httpx.Response(200, json={"message": {"content": json.dumps(body)}})
        )
        yield body


@pytest.fixture
def ollama_down():
    if not HAS_RESPX:
        pytest.skip("respx not installed")
    with respx.mock:
        respx.post("http://localhost:11434/api/chat").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        yield


@pytest.fixture
def ollama_low_confidence():
    if not HAS_RESPX:
        pytest.skip("respx not installed")
    body = {
        "decision": "ALLOW", "confidence": 0.3,
        "justification": "Weak setup",
        "violations": [], "conditions_met": [],
        "conditions_failed": [], "strategy_version": "v1",
    }
    with respx.mock:
        respx.post("http://localhost:11434/api/chat").mock(
            return_value=httpx.Response(200, json={"message": {"content": json.dumps(body)}})
        )
        yield body
