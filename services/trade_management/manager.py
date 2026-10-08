"""Trade management: breakeven, partial TP, SL/TP hit detection."""

from __future__ import annotations
import os
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5
from services.risk.state_store import read_state, write_state
from services.notifications.telegram import notify
from services.logging.audit_logger import log_event
from services.trade_management.pips import get_pip_size
from services.analytics.performance import record_trade

logger = logging.getLogger(__name__)

_TRADE_STATE_PATH = os.path.join("storage", "db", "trade_state.json")


def _parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":")
        return int(h), int(m)
    except Exception:
        return default


def _is_eos_guard_active() -> bool:
    """True when within EOS_BE_GUARD_MINUTES of TRADING_END_UTC."""
    end_h, end_m = _parse_hhmm(os.getenv("TRADING_END_UTC", "20:00"), (20, 0))
    guard = int(os.getenv("EOS_BE_GUARD_MINUTES", "30"))
    now = datetime.now(timezone.utc)
    end_min = end_h * 60 + end_m
    now_min = now.hour * 60 + now.minute
    return end_min - guard <= now_min <= end_min

# MT5 DEAL_REASON enum values (confirmed from MT5 docs)
_DEAL_REASON = {
    0: "MANUAL",   # Client terminal
    1: "MANUAL",   # Mobile terminal
    2: "MANUAL",   # Web terminal
    3: "EA",       # Expert Advisor
    4: "SL",       # Stop Loss triggered
    5: "TP",       # Take Profit triggered
    6: "SO",       # Stop Out
}


class TradeManager:
    """Manages open positions: breakeven, partial TP, SL/TP hit detection."""

    def __init__(self, risk_manager):
        self._lock = threading.RLock()
        self._state_path = _TRADE_STATE_PATH
        self._risk_manager = risk_manager

    def register_trade(self, ticket: int, symbol: str, side: str,
                       volume: float, entry: float, sl: float, tp: float,
                       score: int, confidence: float,
                       tp1: float | None = None,
                       tp2: float | None = None,
                       tp_final: float | None = None,
                       sig_entry: float | None = None) -> None:
        """Register a new trade for management.

        tp1/tp2 are absolute prices for the partial-close ladder.
        tp_final is the broker TP (typically the runner cap); falls back to ``tp``.
        """
        sl_dist = abs(entry - sl)
        if tp1 is None and sl_dist > 0:
            # Fallback ladder: TP1=1R, TP2=midway to broker tp.
            sign = 1 if side == "BUY" else -1
            tp1 = entry + sign * sl_dist
            tp2 = entry + sign * sl_dist * 1.5
        if tp_final is None:
            tp_final = tp

        # ── Re-anchor TP ladder to the FILL price ──────────────────────
        # Signal-side ladder is computed from the confirming-bar close,
        # but the fill slips (0.53 XAU observed 9/28). BE / current_r use
        # the fill, so a signal-anchored TP1 sits at 1.09R -> BE fires at
        # 1R, TP1 waits 30min. Translate the ladder's R-offsets onto the
        # fill so TP1 == 1.00R of actual risk, preserving R-geometry.
        sig_entry = sig_entry if sig_entry is not None else entry
        sig_dist = abs(sig_entry - sl)
        if sig_dist > 0 and abs(entry - sig_entry) > 1e-12:
            sign = 1 if side == "BUY" else -1
            fill_dist = abs(entry - sl)
            r1 = (tp1 - sig_entry) / sig_dist if side == "BUY" else (sig_entry - tp1) / sig_dist
            r2 = (tp2 - sig_entry) / sig_dist if side == "BUY" else (sig_entry - tp2) / sig_dist
            rf = (tp_final - sig_entry) / sig_dist if side == "BUY" else (sig_entry - tp_final) / sig_dist
            tp1 = entry + sign * r1 * fill_dist
            tp2 = entry + sign * r2 * fill_dist
            tp_final = entry + sign * rf * fill_dist
        with self._lock:
            state = read_state(self._state_path, {})
            state[str(ticket)] = {
                "ticket": ticket,
                "symbol": symbol,
                "side": side,
                "volume": volume,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "tp1_price": tp1,
                "tp2_price": tp2,
                "tp_final": tp_final,
                "score": score,
                "confidence": confidence,
                "initial_sl_dist": sl_dist,   # frozen 1R basis — BE moves SL,
                                            # never the R-metric (probe-matrix
                                            # catch: post-BE sl_dist==0 silently
                                            # skipped the whole TP ladder)
                "be_triggered": False,
                "tp1_closed": False,
                "tp2_closed": False,
                "trail_active": False,
                "opened_at": time.time(),
            }
            write_state(self._state_path, state)

    def rebuild_from_mt5(self) -> int:
        """Re-register any live MT5 positions that aren't in trade_state.

        Run on engine start so a restart doesn't orphan positions (no BE / no partials).
        Returns the number of positions adopted.
        """
        try:
            positions = mt5.positions_get() or []
        except Exception:
            return 0
        if not positions:
            return 0

        with self._lock:
            state = read_state(self._state_path, {})
        adopted = 0
        for pos in positions:
            key = str(pos.ticket)
            if key in state:
                continue
            side = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
            entry = float(pos.price_open)
            sl = float(pos.sl) if pos.sl else entry
            tp = float(pos.tp) if pos.tp else entry
            self.register_trade(
                ticket=int(pos.ticket), symbol=pos.symbol, side=side,
                volume=float(pos.volume), entry=entry, sl=sl, tp=tp,
                score=0, confidence=0.0,
                tp1=None, tp2=None, tp_final=tp,
            )
            log_event("TRADE_ADOPTED", {
                "ticket": int(pos.ticket), "symbol": pos.symbol, "side": side,
                "entry": entry, "sl": sl, "tp": tp,
            }, stream="lifecycle")
            adopted += 1
        return adopted

    def check_positions(self) -> None:
        """Check all managed positions for BE, partial TP, SL/TP hits."""
        with self._lock:
            state = read_state(self._state_path, {})

        if state:
            logger.info("Monitor tick: %d managed position(s)", len(state))

        tickets_to_remove = []
        for ticket_str, trade in list(state.items()):
            ticket = trade["ticket"]
            symbol = trade["symbol"]

            # Check if position still exists
            positions = mt5.positions_get(ticket=ticket)
            if not positions or len(positions) == 0:
                # Position closed — look up deal history with retry for MT5 delay.
                # Always remove from state, even if bookkeeping raises, to avoid
                # an infinite loop on stale tickets (the prior bug pattern).
                tickets_to_remove.append(ticket_str)
                try:
                    self._handle_closed_position(trade)
                except Exception:
                    logger.exception("Bookkeeping failed for closed ticket %s", ticket)
                continue

            pos = positions[0]
            entry = trade["entry"]
            sl = trade["sl"]
            side = trade["side"]
            # 1R basis is FROZEN at registration; SL moves (BE/trailing)
            # must never zero it out.
            sl_dist = trade.get("initial_sl_dist") or abs(entry - sl)
            if sl_dist <= 0:
                continue

            # Use the close-side tick (BUY closes on bid, SELL closes on ask) —
            # this is the price the broker uses to trigger TP/SL, so it's what
            # we must compare against. pos.price_current can drift on spread.
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                current_price = tick.bid if side == "BUY" else tick.ask
            else:
                current_price = pos.price_current

            # Resolve TP ladder — fall back if state predates the multi-TP fields.
            sign = 1 if side == "BUY" else -1
            tp1_price = trade.get("tp1_price") or (entry + sign * sl_dist)
            tp2_price = trade.get("tp2_price") or (entry + sign * sl_dist * 1.5)

            def _crossed(target: float) -> bool:
                if side == "BUY":
                    return current_price >= target
                return current_price <= target

            current_r = (current_price - entry) / sl_dist if side == "BUY" \
                else (entry - current_price) / sl_dist

            logger.info(
                "MONITOR %s %s entry=%.5f price=%.5f R=%.2f tp1=%.5f tp2=%.5f "
                "be=%s tp1_closed=%s tp2_closed=%s",
                symbol, side, entry, current_price, current_r,
                tp1_price, tp2_price,
                trade.get("be_triggered"), trade.get("tp1_closed"),
                trade.get("tp2_closed"),
            )

            # ── Breakeven trigger at 1R (configurable via BE_TRIGGER_R) ──
            be_trigger = float(os.getenv("BE_TRIGGER_R", "0.5"))
            if current_r >= be_trigger and not trade.get("be_triggered"):
                self._modify_sl(ticket, symbol, entry, pos.tp)
                with self._lock:
                    state = read_state(self._state_path, {})
                    if ticket_str in state:
                        state[ticket_str]["be_triggered"] = True
                        state[ticket_str]["sl"] = entry
                        write_state(self._state_path, state)

            # ── End-of-session BE guard ──
            # Within EOS_BE_GUARD_MINUTES of TRADING_END_UTC, force-BE any +R
            # position so overnight reversals can't drag a winner back to SL
            # (this is exactly how 5/6 GBPUSD lost while bot was offline).
            elif _is_eos_guard_active() and current_r > 0 and not trade.get("be_triggered"):
                self._modify_sl(ticket, symbol, entry, pos.tp, label="EOS_BE")
                with self._lock:
                    state = read_state(self._state_path, {})
                    if ticket_str in state:
                        state[ticket_str]["be_triggered"] = True
                        state[ticket_str]["sl"] = entry
                        write_state(self._state_path, state)

            # ── TP1: close 50% when price crosses absolute TP1 level ──
            if _crossed(tp1_price) and not trade.get("tp1_closed"):
                pct = int(os.getenv("TP1_CLOSE_PCT", "50"))
                closed = self._partial_close(ticket, symbol, pos.volume, pct, "TP1")
                if closed:
                    with self._lock:
                        state = read_state(self._state_path, {})
                        if ticket_str in state:
                            state[ticket_str]["tp1_closed"] = True
                            write_state(self._state_path, state)

            # ── TP2: close 30% at absolute TP2 (only after TP1) ──
            if _crossed(tp2_price) and trade.get("tp1_closed") and not trade.get("tp2_closed"):
                # Re-fetch position — TP1 reduced volume.
                pos2_list = mt5.positions_get(ticket=ticket)
                if pos2_list:
                    pct = int(os.getenv("TP2_CLOSE_PCT", "30"))
                    closed = self._partial_close(ticket, symbol, pos2_list[0].volume, pct, "TP2")
                    if closed:
                        with self._lock:
                            state = read_state(self._state_path, {})
                            if ticket_str in state:
                                state[ticket_str]["tp2_closed"] = True
                                write_state(self._state_path, state)

            # ── Trailing stop on the runner (only after TP2 closes) ──
            if trade.get("tp2_closed"):
                self._maybe_trail(ticket_str, ticket, symbol, side, pos)

        # Remove closed positions
        if tickets_to_remove:
            with self._lock:
                state = read_state(self._state_path, {})
                for t in tickets_to_remove:
                    state.pop(t, None)
                write_state(self._state_path, state)

    @staticmethod
    def _pip_value(symbol: str) -> float | None:
        """$ value of 1.0 price unit for 1 lot (tick_value/tick_size ratio)."""
        try:
            info = mt5.symbol_info(symbol)
            if info and info.trade_tick_size > 0:
                return info.trade_tick_value / info.trade_tick_size
        except Exception:
            pass
        return None

    def _handle_closed_position(self, trade: dict) -> None:
        """Handle a position that was closed — detect SL/TP hit with retry."""
        ticket = trade["ticket"]
        reason = "UNKNOWN"
        pnl = 0.0
        exit_price = 0.0

        # Retry up to 5x with 2s delay; combine by-position direct lookup with
        # a wide time window to survive both fast queries (MT5 lag) and stale state.
        for attempt in range(5):
            if attempt > 0:
                time.sleep(2.0)

            deals = None
            # Preferred: ask MT5 directly for deals tied to this position.
            try:
                deals = mt5.history_deals_get(position=ticket)
            except Exception:
                deals = None

            # Fallback: scan a 30-day window if the by-position lookup is unsupported / empty.
            if not deals:
                now = datetime.now(timezone.utc)
                deals = mt5.history_deals_get(now - timedelta(days=30), now)

            if not deals:
                continue

            matching = [d for d in deals
                        if getattr(d, "position_id", None) == ticket
                        and getattr(d, "entry", None) == 1]
            if not matching:
                continue

            matching.sort(key=lambda d: d.time)
            deal = matching[-1]
            # A position managed by the TP ladder (TP1/TP2 partial closes)
            # produces MULTIPLE close deals. P&L must aggregate every leg —
            # including commission and swap — or a TP1-winner that BEs out
            # on the runner reports as a loss (observed 9/28: TP1 +26.26
            # + BE -0.04 recorded as -0.04).
            pnl = sum(
                float(getattr(d, "profit", 0.0) or 0.0)
                + float(getattr(d, "commission", 0.0) or 0.0)
                + float(getattr(d, "swap", 0.0) or 0.0)
                for d in matching
            )
            legs = len(matching)
            exit_price = float(getattr(deal, "price", 0.0) or 0.0)
            reason = _DEAL_REASON.get(getattr(deal, "reason", -1), "UNKNOWN")
            break

        if reason == "UNKNOWN":
            log_event("EXIT_LOOKUP_FAILED", {
                "ticket": ticket, "symbol": trade.get("symbol", "?"),
            })

        # Net R multiple: aggregate P&L over the frozen initial risk.
        # 1R basis = initial_sl_dist * full volume (frozen at registration);
        # with commission/swap folded into pnl this yields the true per-trade R.
        sl_dist0 = trade.get("initial_sl_dist")
        r_mult = None
        if sl_dist0 and sl_dist0 > 0:
            pip_val = self._pip_value(trade["symbol"])
            if pip_val:
                risk_per_unit = sl_dist0 * pip_val   # $ risk per 1.0 lot
                one_r = risk_per_unit * trade["volume"]
                if one_r > 0:
                    r_mult = round(pnl / one_r, 3)

        # Record to analytics
        record_trade(
            ticket=ticket,
            symbol=trade["symbol"],
            side=trade["side"],
            volume=trade["volume"],
            entry=trade["entry"],
            exit_price=exit_price,
            pnl=pnl,
            reason=reason,
            r_multiple=r_mult,
        )

        # Update risk state
        if pnl < -0.01:
            self._risk_manager.record_loss(trade["symbol"], is_sl=(reason == "SL"))
            emoji = "🛑"
            label = reason if reason in ("SL", "SO") else "LOSS"
        elif reason == "TP":
            self._risk_manager.record_win()
            emoji = "🎯"
            label = "TP"
        elif reason in ("MANUAL", "EA"):
            self._risk_manager.record_win() if pnl > 0 else self._risk_manager.record_loss(trade["symbol"])
            emoji = "🎯" if pnl >= 0 else "🛑"
            label = "MANUAL"
        else:
            # Breakeven or zero P&L — but with a TP-ladder aggregate this can
            # still be a net winner (TP1 banked, runner BE'd): book it as one.
            if pnl > 0.01:
                self._risk_manager.record_win()
                emoji = "🎯"
                label = "TP_PARTIAL_BE"
            else:
                emoji = "➡️"
                label = "BE"

        notify(
            f"{emoji} <b>{label} HIT</b>: {trade['symbol']} {trade['side']}\n"
            f"Entry: {trade['entry']} → Exit: {exit_price}\n"
            f"P&L: ${pnl:+.2f}"
        )
        log_event("POSITION_CLOSED", {
            "ticket": ticket, "symbol": trade["symbol"],
            "reason": reason, "pnl": pnl, "exit_price": exit_price,
        }, stream="lifecycle")

    def _modify_sl(self, ticket: int, symbol: str, new_sl: float,
                   tp: float | None = None, *, label: str = "BREAKEVEN") -> bool:
        """Modify SL on an open position. tp=None preserves existing tp.

        Used both for breakeven (label=BREAKEVEN) and trailing (label=TRAIL).
        """
        try:
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                return False
            pos = positions[0]
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "symbol": symbol,
                "sl": new_sl,
                "tp": tp if tp is not None else pos.tp,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                if label == "BREAKEVEN":
                    notify(f"🔄 BE triggered: {symbol} SL → {new_sl:.5f}")
                elif label == "EOS_BE":
                    notify(f"🌙 EOS BE: {symbol} SL → {new_sl:.5f} (session close)")
                else:
                    notify(f"📈 Trailing: {symbol} SL → {new_sl:.5f}")
                log_event(label, {"ticket": ticket, "symbol": symbol, "new_sl": new_sl}, stream="lifecycle")
                return True
        except Exception as e:
            logger.warning("Failed to modify SL for %s: %s", ticket, e)
        return False

    def _maybe_trail(self, ticket_str: str, ticket: int, symbol: str,
                     side: str, pos) -> None:
        """Trail the runner after TP2 by ATR multiple — only ratchets forward."""
        if os.getenv("TRAIL_ENABLED", "true") != "true":
            return
        try:
            mult = float(os.getenv("TRAIL_ATR_MULT", "1.5"))
            # Pull recent M15 ATR via MT5; cheap because monitor runs every 60s.
            tf = getattr(mt5, "TIMEFRAME_M15", 15)
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, 30)
            if rates is None or len(rates) < 15:
                return
            import numpy as np
            highs = np.array([r["high"] for r in rates])
            lows = np.array([r["low"] for r in rates])
            closes = np.array([r["close"] for r in rates])
            tr = np.maximum.reduce([
                highs[1:] - lows[1:],
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ])
            atr_val = float(tr[-14:].mean()) if len(tr) >= 14 else float(tr.mean())
            if atr_val <= 0:
                return
            current = pos.price_current
            current_sl = pos.sl
            if side == "BUY":
                new_sl = current - mult * atr_val
                if new_sl <= current_sl:  # only ratchet up
                    return
            else:
                new_sl = current + mult * atr_val
                if new_sl >= current_sl or current_sl <= 0:
                    return
            ok = self._modify_sl(ticket, symbol, new_sl, label="TRAIL")
            if ok:
                with self._lock:
                    state = read_state(self._state_path, {})
                    if ticket_str in state:
                        state[ticket_str]["trail_active"] = True
                        state[ticket_str]["sl"] = new_sl
                        write_state(self._state_path, state)
        except Exception as e:
            logger.warning("Trail failed for %s: %s", ticket, e)

    def _partial_close(self, ticket: int, symbol: str, volume: float,
                       pct: int, label: str) -> bool:
        """Partially close a position. Returns True on success."""
        try:
            info = mt5.symbol_info(symbol)
            if not info:
                return False

            step = info.volume_step
            raw_vol = volume * pct / 100
            import math
            close_vol = math.floor(raw_vol / step) * step
            close_vol = max(info.volume_min, close_vol)
            close_vol = min(volume, close_vol)

            # Don't partial close if remaining would be below min
            remaining = volume - close_vol
            if remaining > 0 and remaining < info.volume_min:
                close_vol = volume  # close all instead

            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                return False
            pos = positions[0]
            is_buy = pos.type == mt5.ORDER_TYPE_BUY
            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                return False

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": close_vol,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": ticket,
                "price": tick.bid if is_buy else tick.ask,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                notify(
                    f"🎯 <b>{label}</b>: {symbol} — {pct}% closed ({close_vol} lots)\n"
                    f"Fill price: {result.price:.5f}"
                )
                log_event("PARTIAL_CLOSE", {
                    "ticket": ticket, "symbol": symbol,
                    "label": label, "volume_closed": close_vol,
                }, stream="lifecycle")
                return True
        except Exception as e:
            logger.warning("Partial close failed for %s: %s", ticket, e)
        return False
