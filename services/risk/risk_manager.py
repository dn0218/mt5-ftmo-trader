"""FTMO-calibrated risk manager with circuit breakers."""

from __future__ import annotations
import json
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

    def _total_open_risk(self, open_positions) -> float:
        """Aggregate worst-case $ risk if every open position hits its SL."""
        total = 0.0
        for p in open_positions or []:
            try:
                info = mt5.symbol_info(p.symbol)
                if not info or not p.sl:
                    continue
                sl_dist = abs(p.price_open - p.sl)
                if sl_dist > 0 and info.trade_tick_size > 0:
                    total += p.volume * (sl_dist / info.trade_tick_size) \
                        * info.trade_tick_value
            except Exception:
                continue
        return total

    def _open_risk_headroom(self) -> float:
        """$ headroom under MAX_TOTAL_OPEN_RISK_PCT for a NEW position."""
        limit_pct = float(os.getenv("MAX_TOTAL_OPEN_RISK_PCT", "2.5"))
        acct = mt5.account_info()
        if not acct:
            return 0.0
        open_risk = self._total_open_risk(mt5.positions_get())
        return acct.equity * limit_pct / 100 - open_risk

    def _storage_full(self) -> bool:
        """True when storage_guard flagged >950 MB (STORAGE_FULL).

        Fail-open by design: if the state file is missing or unreadable
        the guard simply hasn't run yet — blocking all trading because a
        bookkeeping file is missing would be worse than the disk risk it
        protects against. The guard re-writes the flag daily.
        """
        path = os.getenv(
            "STORAGE_STATE_PATH",
            os.path.join("storage", "db", "storage_state.json"),
        )
        try:
            with open(path, encoding="utf-8") as f:
                return bool(json.load(f).get("storage_full"))
        except (OSError, ValueError):
            return False

    # ── Demo 验证期硬护栏（2026-10 高风险切换配套）──────────────────
    # Demo 账户总回撤 ≥ 10% 即停（配置限 15%，验证期留 5% 观察余量）。
    # 锁存在 risk_state.json 的 demo_dd_halt 键，不随午夜 init_day
    # 重置 —— 恢复需人工删除该键，对应"触发后不再自动恢复"约束。
    DEMO_DD_HALT_PCT = 10.0

    def _is_demo_account(self) -> bool:
        try:
            acct = mt5.account_info()
            return bool(acct) and acct.trade_mode == getattr(
                mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        except Exception:
            return False

    def _get_total_dd_pct(self) -> float:
        """Total drawdown % vs initial balance (0.0 when unknowable)."""
        acct = mt5.account_info()
        if not acct:
            return 0.0
        initial = self._state().get("initial_balance", 0.0) or acct.balance
        if initial <= 0:
            return 0.0
        return (initial - acct.equity) / initial * 100

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        """Check all circuit breakers. Returns (allowed, reason)."""
        state = self._state()

        # Demo 验证期硬护栏：总回撤 ≥ 10% 时停止一切新开仓
        if not state.get("demo_dd_halt"):
            dd_pct = self._get_total_dd_pct()
            if dd_pct >= self.DEMO_DD_HALT_PCT and self._is_demo_account():
                state["demo_dd_halt"] = True
                state["halted_today"] = True
                self._save(state)
                notify(f"🛑 DEMO_RISK_LIMIT_HIT: total DD {dd_pct:.1f}% ≥ "
                       f"{self.DEMO_DD_HALT_PCT:.0f}% — demo validation "
                       "halted, manual reset required")
                log_event("DEMO_RISK_LIMIT_HIT", {
                    "dd_pct": round(dd_pct, 2),
                    "guard_pct": self.DEMO_DD_HALT_PCT,
                    "manual_reset": "delete demo_dd_halt in risk_state.json",
                }, stream="lifecycle")
        if state.get("demo_dd_halt"):
            return False, "DEMO_DRAWDOWN_HALT"

        # Check halted
        if state.get("halted_today"):
            return False, "HALTED_TODAY"

        # Storage capacity guard (>950 MB → no NEW trades; exits untouched)
        if self._storage_full():
            return False, "STORAGE_FULL (storage/ > 950 MB — free disk space)"

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

        # Pre-open daily-DD guard: would THIS trade's full risk, added to
        # today's equity drawdown (floating included), breach the limit?
        # Prevents stacking concurrent full-risk losses past the breaker.
        ok, reason = self._check_daily_dd_risk(state, open_positions, symbol)
        if not ok:
            return False, reason

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

    def _check_daily_dd_risk(self, state: dict, open_positions, symbol: str) -> tuple[bool, str]:
        """Pre-open guard: prospective trade risk + open risk + today's DD must
        stay under the daily limit. Blocks the Nth concurrent trade whose
        combined worst-case would breach MAX_DAILY_DRAWDOWN in one scan."""
        limit_pct = float(os.getenv("MAX_DAILY_DRAWDOWN", "2.0"))
        acct = mt5.account_info()
        if not acct:
            return True, "OK"
        day_start = state.get("day_start_equity", acct.equity)
        if day_start <= 0:
            return True, "OK"
        dd_now = day_start - acct.equity          # floating P&L included (equity-based)
        open_risk = 0.0
        for p in open_positions or []:
            info = mt5.symbol_info(p.symbol)
            if not info:
                continue
            pip = 10 * info.point if info.digits in (3, 5) else info.point
            sl_dist = abs(p.price_open - p.sl) if p.sl else 0.0
            if p.volume and sl_dist > 0 and info.trade_tick_size > 0:
                open_risk += p.volume * (sl_dist / info.trade_tick_size) * info.trade_tick_value
        new_risk = self._prospective_risk(symbol)
        headroom = day_start * limit_pct / 100
        if dd_now + open_risk + new_risk > headroom:
            log_event("DAILY_DD_WOULD_BREACH", {
                "symbol": symbol,
                "dd_now": round(dd_now, 2),
                "open_risk": round(open_risk, 2),
                "new_risk": round(new_risk, 2),
                "headroom": round(headroom, 2),
                "limit_pct": limit_pct,
            }, stream="lifecycle")
            return False, ("DAILY_DD_WOULD_BREACH "
                           f"(dd={dd_now:.0f} + open_risk={open_risk:.0f} "
                           f"+ new={new_risk:.0f} > {headroom:.0f})")
        return True, "OK"

    def _prospective_risk(self, symbol: str) -> float:
        """Risk in account currency of a minimal-size trade on this symbol,
        using the configured RISK_PER_TRADE budget as the per-trade bound."""
        try:
            risk_pct = float(os.getenv("RISK_PER_TRADE", "0.5"))
            acct = mt5.account_info()
            if acct:
                return acct.equity * risk_pct / 100
        except Exception:
            pass
        return 0.0

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
            log_event("DAILY_DD_HALT", {"dd_pct": dd_pct, "limit": limit}, stream="lifecycle")
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
            log_event("TOTAL_DD_HALT", {"dd_pct": dd_pct, "limit": limit}, stream="lifecycle")
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
        # Per-symbol override: MAX_SPREAD_POINTS_XAUUSD etc. (suffix stripped)
        sym_key = symbol.upper().replace(".", "").replace("_", "")
        max_spread = float(os.getenv(f"MAX_SPREAD_POINTS_{sym_key}",
                                     os.getenv("MAX_SPREAD_POINTS", "25")))
        if spread_points > max_spread:
            return False, f"SPREAD_TOO_HIGH ({spread_points:.0f} > {max_spread})"
        return True, "OK"

    def validate_sl_tp(self, symbol: str, side: str, entry: float,
                       sl: float, tp: float,
                       min_rr_override: float | None = None,
                       atr: float | None = None,
                       atr_prev: float | None = None) -> tuple[bool, str]:
        """Validate SL/TP distances and risk/reward ratio.

        ``min_rr_override`` lets the engine pass a score-tiered RR floor
        (e.g. 2.5 for score 4, 3.0 for score 5). When None, falls back to
        ``RISK_REWARD_MIN`` env var.

        ATR-multiple mode (SL_LIMITS_MODE=atr): MIN/MAX become
        MIN/MAX_SL_ATR_MULT_{SYM} x ``atr`` (engine passes the EMA5-smoothed
        ATR14). Jump guard: if ATR jumped > SL_ATR_JUMP_PCT vs ``atr_prev``,\n        the PRE-jump ATR is used for the threshold (a spike must not loosen\n        the gate for the bar that contains it). Fail-closed: ATR missing or\n        non-positive in atr mode -> reject (never silently fall back to\n        fixed pips, which would re-introduce the low-vol blind reject).
        """
        info = mt5.symbol_info(symbol)
        if not info:
            return False, "NO_SYMBOL_INFO"

        # Min distance from broker
        min_dist = info.trade_stops_level * info.point
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)

        # Side sanity: SL/TP must sit on the LOSING side of the entry. A SL
        # beyond the entry means the signal is stale (price ran through the
        # structural level since the signal bar) — broker would 10016 it.
        if side == "BUY" and (sl >= entry or tp <= entry):
            return False, f"SL_WRONG_SIDE (BUY: sl={sl:.5f} >= entry={entry:.5f})"
        if side == "SELL" and (sl <= entry or tp >= entry):
            return False, f"SL_WRONG_SIDE (SELL: sl={sl:.5f} <= entry={entry:.5f})"

        if sl_dist < min_dist:
            return False, f"SL_TOO_CLOSE (dist={sl_dist:.5f} < min={min_dist:.5f})"
        if tp_dist < min_dist:
            return False, f"TP_TOO_CLOSE (dist={tp_dist:.5f} < min={min_dist:.5f})"

        # SL pip limits (per-symbol). pip_size() is the single source of
        # truth (core/mt5/symbols.py) — FX and XAU/XAG scale point*10,
        # everything else uses point as-is (index/crypto CFDs).
        from core.mt5.symbols import pip_size
        pip = pip_size(symbol, info)
        sl_pips = sl_dist / pip if pip > 0 else 0

        sym_key = symbol.upper().replace(".", "").replace("_", "")

        if os.getenv("SL_LIMITS_MODE", "fixed") == "atr":
            # Fail-closed: no valid ATR -> reject (no fixed-pips fallback)
            if atr is None or atr <= 0:
                return False, "ATR_UNAVAILABLE (SL_LIMITS_MODE=atr requires ATR)"
            basis = atr
            jump_note = ""
            jump_pct = float(os.getenv("SL_ATR_JUMP_PCT", "20"))
            if atr_prev and atr_prev > 0:
                jump = abs(atr - atr_prev) / atr_prev * 100
                if jump > jump_pct:
                    basis = atr_prev        # use pre-jump ATR for the threshold
                    jump_note = f" [jump {jump:.0f}%>p99 thr, using pre-jump ATR]"
            min_mult = float(os.getenv(f"MIN_SL_ATR_MULT_{sym_key}",
                                       os.getenv("MIN_SL_ATR_MULT", "0.5")))
            max_mult = float(os.getenv(f"MAX_SL_ATR_MULT_{sym_key}",
                                       os.getenv("MAX_SL_ATR_MULT", "5.0")))
            min_sl = min_mult * basis / pip if pip > 0 else 0
            max_sl = max_mult * basis / pip if pip > 0 else 0
            if sl_pips < min_sl - 1e-4:      # float-precision safe boundary
                return False, (f"SL_TOO_TIGHT ({sl_pips:.1f} pips < {min_sl:.1f}"
                               f" = {min_mult}xATR{jump_note})")
            if sl_pips > max_sl + 1e-4:
                return False, (f"SL_TOO_WIDE ({sl_pips:.1f} pips > {max_sl:.1f}"
                               f" = {max_mult}xATR{jump_note})")
        else:
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

        # Total-open-risk cap: shrink the budget to remaining headroom
        # (de-risks sizing instead of rejecting when near the cap).
        try:
            headroom = self._open_risk_headroom()
            if headroom <= 0:
                return None
            risk_amount = min(risk_amount, headroom)
        except Exception:
            pass

        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return None

        # Pip size — single source of truth in core/mt5/symbols.py::pip_size.
        # (FX 5/3-digit and XAU/XAG scale point*10; index/crypto use point.)
        from core.mt5.symbols import pip_size
        pip = pip_size(symbol, info)
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
        log_event("LOSS_RECORDED", {"symbol": symbol, "consecutive": state["consecutive_losses"]}, stream="lifecycle")

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
