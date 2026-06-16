"""FTMO-calibrated risk manager with circuit breakers."""

from __future__ import annotations
import math
import os
import time
import logging
import MetaTrader5 as mt5
from services.risk.state_store import read_state, write_state
from services.notifications.telegram import notify
from services.logging.audit_logger import log_event
from core.mt5.symbols import is_gold

logger = logging.getLogger(__name__)

_RISK_STATE_PATH = os.path.join("storage", "db", "risk_state.json")

_DEFAULT_STATE = {
    "day_start_equity": 0.0,
    "day_start_balance": 0.0,
    "initial_balance": 0.0,
    "consecutive_losses": 0,
    "trades_today": 0,
    "sl_cooldowns": {},
    "today": "",
    "halted_today": False,
}


class RiskManager:
    def __init__(self):
        self._state_path = _RISK_STATE_PATH

    def _state(self) -> dict:
        return read_state(self._state_path, _DEFAULT_STATE)

    def _save(self, state: dict) -> None:
        write_state(self._state_path, state)

    def init_day(self) -> None:
        """Initialize or reset daily tracking. Call on startup and at midnight CET."""
        acct = mt5.account_info()
        if not acct:
            return
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state = self._state()
        if state.get("today") != today:
            state["day_start_equity"] = acct.equity
            state["day_start_balance"] = acct.balance
            state["today"] = today
            state["consecutive_losses"] = 0
            state["trades_today"] = 0
            state["halted_today"] = False
            state["sl_cooldowns"] = {}
            if state.get("initial_balance", 0) <= 0:
                state["initial_balance"] = acct.balance
            self._save(state)
            logger.info("Risk day init: equity=%.2f balance=%.2f", acct.equity, acct.balance)

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        """Check all circuit breakers. Returns (allowed, reason)."""
        state = self._state()

        # Check halted
        if state.get("halted_today"):
            return False, "HALTED_TODAY"

        # Daily drawdown check (equity-based, includes floating P&L)
        ok, reason = self._check_daily_dd(state)
        if not ok:
            return False, reason

        # Total drawdown check
        ok, reason = self._check_total_dd(state)
        if not ok:
            return False, reason

        # Consecutive losses
        max_consec = int(os.getenv("CONSECUTIVE_LOSS_HALT", "3"))
        if state.get("consecutive_losses", 0) >= max_consec:
            notify(f"🚨 {max_consec} consecutive losses — halted for today")
            state["halted_today"] = True
            self._save(state)
            return False, "CONSECUTIVE_LOSS_HALT"

        # Max trades per day
        max_daily = int(os.getenv("MAX_TRADES_PER_DAY", "20"))
        if state.get("trades_today", 0) >= max_daily:
            return False, f"MAX_TRADES_PER_DAY ({max_daily})"

        # Max concurrent positions
        max_pos = int(os.getenv("MAX_CONCURRENT_POSITIONS", "2"))
        open_positions = mt5.positions_get()
        if open_positions and len(open_positions) >= max_pos:
            return False, "MAX_POSITIONS"

        # SL cooldown per symbol
        cooldown = int(os.getenv("SL_COOLDOWN_SECONDS", "3600"))
        last_sl = state.get("sl_cooldowns", {}).get(symbol, 0)
        if time.time() - last_sl < cooldown:
            remaining = int(cooldown - (time.time() - last_sl))
            return False, f"SL_COOLDOWN ({remaining}s remaining)"

        # Spread check
        ok, reason = self._check_spread(symbol)
        if not ok:
            return False, reason

        return True, "OK"

    def _check_daily_dd(self, state: dict) -> tuple[bool, str]:
        """Check daily drawdown using equity (includes floating P&L)."""
        acct = mt5.account_info()
        if not acct:
            return False, "NO_ACCOUNT_INFO"
        day_start = state.get("day_start_equity", acct.equity)
        if day_start <= 0:
            return True, "OK"
        dd_pct = (day_start - acct.equity) / day_start * 100
        limit = float(os.getenv("MAX_DAILY_DRAWDOWN", "2.0"))
        if dd_pct >= limit:
            notify(f"⚠️ DAILY DD {dd_pct:.1f}% ≥ {limit}% limit — HALTED for today")
            log_event("DAILY_DD_HALT", {"dd_pct": dd_pct, "limit": limit})
            state["halted_today"] = True
            self._save(state)
            return False, "DAILY_DD_EXCEEDED"
        return True, "OK"

    def _check_total_dd(self, state: dict) -> tuple[bool, str]:
        """Check total drawdown against initial balance."""
        acct = mt5.account_info()
        if not acct:
            return False, "NO_ACCOUNT_INFO"
        initial = state.get("initial_balance", acct.balance)
        if initial <= 0:
            return True, "OK"
        dd_pct = (initial - acct.equity) / initial * 100
        limit = float(os.getenv("MAX_TOTAL_DRAWDOWN", "6.0"))
        if dd_pct >= limit:
            notify(f"🚨 TOTAL DD {dd_pct:.1f}% ≥ {limit}% limit — ALL TRADING HALTED")
            log_event("TOTAL_DD_HALT", {"dd_pct": dd_pct, "limit": limit})
            state["halted_today"] = True
            self._save(state)
            return False, "TOTAL_DD_EXCEEDED"
        return True, "OK"

    def _check_spread(self, symbol: str) -> tuple[bool, str]:
        """Check if spread is within limits."""
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if not tick or not info or info.point <= 0:
            return False, "NO_TICK_DATA"
        spread_points = (tick.ask - tick.bid) / info.point
        max_spread = float(os.getenv("MAX_SPREAD_POINTS", "25"))
        if spread_points > max_spread:
            return False, f"SPREAD_TOO_HIGH ({spread_points:.0f} > {max_spread})"
        return True, "OK"

    def validate_sl_tp(self, symbol: str, side: str, entry: float,
                       sl: float, tp: float,
                       min_rr_override: float | None = None) -> tuple[bool, str]:
        """Validate SL/TP distances and risk/reward ratio.

        ``min_rr_override`` lets the engine pass a score-tiered RR floor
        (e.g. 2.5 for score 4, 3.0 for score 5). When None, falls back to
        ``RISK_REWARD_MIN`` env var.
        """
        info = mt5.symbol_info(symbol)
        if not info:
            return False, "NO_SYMBOL_INFO"

        # Min distance from broker
        min_dist = info.trade_stops_level * info.point
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)

        if sl_dist < min_dist:
            return False, f"SL_TOO_CLOSE (dist={sl_dist:.5f} < min={min_dist:.5f})"
        if tp_dist < min_dist:
            return False, f"TP_TOO_CLOSE (dist={tp_dist:.5f} < min={min_dist:.5f})"

        # SL pip limits (per-symbol)
        pip = info.point * 10 if info.digits in (3, 5) else info.point
        sl_pips = sl_dist / pip if pip > 0 else 0

        sym_key = symbol.upper().replace(".", "").replace("_", "")
        min_sl = float(os.getenv(f"MIN_SL_PIPS_{sym_key}", os.getenv("MIN_SL_PIPS", "15")))
        max_sl = float(os.getenv(f"MAX_SL_PIPS_{sym_key}", os.getenv("MAX_SL_PIPS", "150")))

        if sl_pips < min_sl:
            return False, f"SL_TOO_TIGHT ({sl_pips:.1f} pips < {min_sl})"
        if sl_pips > max_sl:
            return False, f"SL_TOO_WIDE ({sl_pips:.1f} pips > {max_sl})"

        # Risk/Reward ratio (float precision safe). Score-tiered override wins
        # over RISK_REWARD_MIN when provided by the engine.
        rr_min = (min_rr_override
                  if min_rr_override is not None
                  else float(os.getenv("RISK_REWARD_MIN", "2.0")))
        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if rr < rr_min - 1e-4:
            return False, f"RR_TOO_LOW ({rr:.2f} < {rr_min})"

        return True, "OK"

    def calculate_volume(self, symbol: str, entry: float, sl: float) -> float | None:
        """Calculate position size using quarter-Kelly, normalised to broker step."""
        info = mt5.symbol_info(symbol)
        acct = mt5.account_info()
        if not info or not acct:
            return None

        equity = acct.equity
        risk_pct = float(os.getenv("RISK_PER_TRADE", "0.5"))
        risk_amount = equity * risk_pct / 100.0

        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return None

        # Pip size
        pip = info.point * 10 if info.digits in (3, 5) else info.point
        if pip <= 0:
            return None
        sl_pips = sl_dist / pip
        if sl_pips <= 0:
            return None

        # Pip value in account currency
        pip_value = info.trade_tick_value / info.trade_tick_size * pip if info.trade_tick_size > 0 else 0
        if pip_value <= 0:
            return None

        raw_vol = risk_amount / (sl_pips * pip_value)

        # Normalise using broker volume_step (NOT round(x, 4))
        step = info.volume_step
        if step <= 0:
            return None
        steps = math.floor(raw_vol / step)
        step_str = f"{step:.10f}".rstrip("0")
        decimals = len(step_str.split(".")[-1]) if "." in step_str else 0
        vol = round(steps * step, decimals)

        # Clamp to broker limits
        vol = max(info.volume_min, min(info.volume_max, vol))
        if vol < info.volume_min:
            return None

        return vol

    def record_loss(self, symbol: str, is_sl: bool = True) -> None:
        """Record a loss. Increment consecutive losses and set SL cooldown."""
        state = self._state()
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        if is_sl:
            if "sl_cooldowns" not in state:
                state["sl_cooldowns"] = {}
            state["sl_cooldowns"][symbol] = time.time()
        self._save(state)
        log_event("LOSS_RECORDED", {"symbol": symbol, "consecutive": state["consecutive_losses"]})

    def record_trade_opened(self) -> None:
        """Increment daily trade counter."""
        state = self._state()
        state["trades_today"] = state.get("trades_today", 0) + 1
        self._save(state)

    def record_win(self) -> None:
        """Record a win. Reset consecutive losses."""
        state = self._state()
        state["consecutive_losses"] = 0
        self._save(state)
