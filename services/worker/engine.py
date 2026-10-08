"""TradingEngine — main scan + monitor loop."""

from __future__ import annotations
import os
import time
import threading
import logging
from datetime import datetime, timezone

import pandas as pd
import MetaTrader5 as mt5

from core.mt5.data import MT5Client
from core.mt5.reconnect import ensure_connected
from core.detection.structure import detect_swing_points, detect_bos, detect_choch
from core.detection.liquidity import detect_sweeps
from core.detection.regime import detect_regime
from core.detection.breakout import evaluate_breakout
from core.evidence.build_evidence import build_evidence
from core.scoring.confluence import score_confluence
from core.scoring.targets import build_targets, rr_floor_for_score, apply_llm_plan
from core.indicators.indicators import atr, ema
from services.risk.risk_manager import RiskManager
from services.decision.ollama_decider import call_llm, is_trade_valid
from services.trade_management.manager import TradeManager
from services.trade_management.dry_run_tracker import DryRunTracker
from services.calendar.news_filter import is_news_blackout
from services.notifications.telegram import notify
from services.logging.audit_logger import log_event
from services.mt5_exec import place_order
from services.worker.strategy_store import StrategyStore
from services.analytics.performance import get_daily_summary

logger = logging.getLogger(__name__)


def _parse_time(t: str) -> tuple[int, int]:
    """Parse 'HH:MM' -> (hour, minute)."""
    parts = t.strip().split(":")
    return int(parts[0]), int(parts[1])


def _structure_sl(swings: list, current_price: float, side: str, atr_val: float) -> float:
    """SL below nearest swing low (BUY) or above nearest swing high (SELL).

    Uses the most recent swing point beyond current price with a 0.5×ATR buffer.
    Falls back to 1.5×ATR if no suitable swing found.
    """
    buffer = atr_val * 0.5
    if side == "BUY":
        candidates = [s for s in swings if s["type"] == "SWING_LOW" and s["price"] < current_price]
        if candidates:
            best = max(candidates, key=lambda s: s["bar"])
            return best["price"] - buffer
        return current_price - atr_val * 1.5
    else:
        candidates = [s for s in swings if s["type"] == "SWING_HIGH" and s["price"] > current_price]
        if candidates:
            best = max(candidates, key=lambda s: s["bar"])
            return best["price"] + buffer
        return current_price + atr_val * 1.5


def _in_session() -> bool:
    """Check if we're within trading hours."""
    now = datetime.now(timezone.utc)
    start_h, start_m = _parse_time(os.getenv("TRADING_START_UTC", "07:00"))
    end_h, end_m = _parse_time(os.getenv("TRADING_END_UTC", "20:00"))
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    now_mins = now.hour * 60 + now.minute
    return start_mins <= now_mins <= end_mins


# Correlated clusters: same-direction positions across these groups stack USD
# (or commodity-FX) exposure. Long XAU + long AUD == double-short USD.
_CORRELATION_GROUPS = [
    {"XAUUSD", "AUDUSD", "NZDUSD"},   # commodity / anti-USD bloc
    {"EURUSD", "GBPUSD"},             # European bloc vs USD
]
_CORR_PREFIX_LEN = 6  # compare on normalised 6-char names (suffix stripped)


def _correlation_guard(symbol: str, side: str) -> tuple[bool, str]:
    """Block a trade whose direction duplicates exposure in a correlated group.

    Guard is disabled when CORRELATION_GUARD=false in env.
    """
    if os.getenv("CORRELATION_GUARD", "true").lower() != "true":
        return True, "OK"
    clean = symbol.upper().replace(".", "").replace("_", "")[:_CORR_PREFIX_LEN]
    group = next((g for g in _CORRELATION_GROUPS if clean in g), None)
    if group is None:
        return True, "OK"
    positions = mt5.positions_get() or []
    for p in positions:
        p_clean = (p.symbol or "").upper().replace(".", "").replace("_", "")[:_CORR_PREFIX_LEN]
        if p_clean not in group or p_clean == clean:
            continue
        p_side = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
        if p_side == side:
            return False, f"CORRELATION_BLOCK ({p.symbol} {p_side} already open in group)"
    return True, "OK"


# Write-time dedup for rejection logs. The LLM decision is cached per sweep
# bar, so while the sweep bar is unchanged every ~60s scan re-rejects the
# same signal identically — previously flooding audit.jsonl ~11x. Log the
# first occurrence; when the cluster rotates, close it with a count record.
_reject_clusters: dict[tuple, dict] = {}


def _log_rejection_deduped(event: str, symbol: str, side: str,
                           bar, reason: str, payload: dict) -> None:
    """Log `event` once per (event, symbol, side, bar, reason) cluster.

    Repeats inside the same cluster are counted in memory; a final
    `<event>_REPEATS` record with the total count is written when the cluster
    rotates (or lost on shutdown if it was the last one — acceptable).
    """
    # Normalise the reason for keying: live values embedded in the reason
    # string (e.g. "SL_TOO_WIDE (592.9 pips > 300.0)") change every scan,
    # which silently defeated dedup and re-logged the same bar each minute.
    reason_key = str(reason).split("(")[0].strip()
    key = (event, symbol, side)
    cur = _reject_clusters.get(key)
    if cur is not None and cur["bar"] == bar and cur["reason"] == reason_key:
        cur["count"] += 1
        return
    if cur is not None and cur["count"] > 1:
        log_event(f"{event}_REPEATS", {
            "symbol": symbol, "side": side, "bar": cur["bar"],
            "reason": cur["reason"], "count": cur["count"],
        })
    log_event(event, payload)
    _reject_clusters[key] = {"bar": bar, "reason": reason_key, "count": 1}


def _in_kill_zone() -> bool:
    """Check if we're in a kill zone."""
    zones = os.getenv("SESSION_KILL_ZONES", "07:00-09:00,13:00-15:00")
    now = datetime.now(timezone.utc)
    now_mins = now.hour * 60 + now.minute
    for zone in zones.split(","):
        parts = zone.strip().split("-")
        if len(parts) != 2:
            continue
        sh, sm = _parse_time(parts[0])
        eh, em = _parse_time(parts[1])
        if sh * 60 + sm <= now_mins <= eh * 60 + em:
            return True
    return False


def _get_htf_bias(symbol: str) -> str:
    """Compute H4 bias: BULLISH, BEARISH, or NEUTRAL."""
    if os.getenv("HTF_FILTER_ENABLED", "true") != "true":
        return "NEUTRAL"

    htf_tf = mt5.TIMEFRAME_H4
    htf_bars = int(os.getenv("HTF_LOOKBACK_BARS", "200"))
    df = MT5Client.get_bars(symbol, htf_tf, htf_bars)
    if df is None or len(df) < 25:
        return "NEUTRAL"

    close = df["close"]
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    last_close = close.iloc[-1]

    # EMA9 slope: compare to 3 bars ago to catch sharp reversals before death cross
    ema9_slope = ema9.iloc[-1] - ema9.iloc[-4]

    if last_close < ema21.iloc[-1] or (last_close < ema9.iloc[-1] and ema9_slope < 0):
        return "BEARISH"
    elif last_close > ema21.iloc[-1] and ema9.iloc[-1] > ema21.iloc[-1] and ema9_slope > 0:
        return "BULLISH"
    return "NEUTRAL"


class TradingEngine:
    """Main trading engine: scan loop + position monitor."""

    def __init__(self):
        self.risk_manager = RiskManager()
        self.trade_manager = TradeManager(self.risk_manager)
        self.dry_run_tracker = DryRunTracker(mt5)
        self.strategy_store = StrategyStore()
        # Async K-line snapshot writer (entry-time context, replay-grade).
        from services.snapshots.snapshot_writer import SnapshotWriter
        self._snapshot_writer = SnapshotWriter()
        self._running = False
        self._paused = False
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Start the engine: scanner + monitor threads."""
        self._running = True

        # Init risk day
        self.risk_manager.init_day()

        # Re-adopt any live MT5 positions that aren't in trade_state (post-restart safety).
        try:
            adopted = self.trade_manager.rebuild_from_mt5()
            if adopted:
                logger.info("Adopted %d existing MT5 positions into trade_state", adopted)
                notify(f"♻️ Adopted {adopted} live MT5 position(s) on engine start")
        except Exception as e:
            logger.exception("rebuild_from_mt5 failed: %s", e)

        # Scanner thread
        scanner = threading.Thread(target=self._scan_loop, name="scanner", daemon=True)
        scanner.start()
        self._threads.append(scanner)

        # Monitor thread
        monitor = threading.Thread(target=self._monitor_loop, name="monitor", daemon=True)
        monitor.start()
        self._threads.append(monitor)

        # Daily summary thread
        daily = threading.Thread(target=self._daily_summary_loop, name="daily_summary", daemon=True)
        daily.start()
        self._threads.append(daily)

        # Storage guard thread — daily 04:00 UTC capacity enforcement
        guard = threading.Thread(target=self._storage_guard_loop, name="storage_guard", daemon=True)
        guard.start()
        self._threads.append(guard)

        logger.info("Trading engine started")

    def stop(self) -> None:
        """Stop the engine and flush the snapshot queue."""
        self._running = False
        for t in self._threads:
            t.join(timeout=10)
        try:
            self._snapshot_writer.close()
        except Exception:
            pass
        logger.info("Trading engine stopped")

    def run_forever(self) -> None:
        """Start and block until interrupted."""
        self.start()
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            notify("⏹️ Engine stopped by user")
        finally:
            self.stop()

    def _scan_loop(self) -> None:
        """Main scanning loop — runs every SCAN_INTERVAL_SECONDS."""
        interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
        symbols = [s.strip() for s in os.getenv("SYMBOLS", "EURUSD").split(",")]

        while self._running:
            try:
                # Check if paused
                if self._paused:
                    time.sleep(interval)
                    continue

                # Check session
                if not _in_session():
                    time.sleep(interval)
                    continue

                # Auto-reconnect if MT5 dropped (1 attempt per cycle, not infinite)
                if mt5.account_info() is None:
                    logger.warning("MT5 disconnected — attempting reconnect")
                    if not ensure_connected(max_attempts=1):
                        logger.warning("MT5 reconnect failed — will retry next cycle")
                        time.sleep(interval)
                        continue
                    logger.info("MT5 reconnected")

                # Reset day if needed
                self.risk_manager.init_day()

                for symbol in symbols:
                    if not self._running:
                        break
                    self._scan_symbol(symbol)
                    # advance dry-run virtual positions one M15 bar when a
                    # fresh bar closes (cheap: only pulls 2 bars per symbol)
                    try:
                        self._advance_dry_run(symbol)
                    except Exception as e:
                        logger.exception("dry_run advance failed for %s: %s", symbol, e)

                self.strategy_store.cleanup()
            except Exception as e:
                logger.exception("Scan loop error: %s", e)
                log_event("SCAN_ERROR", {"error": str(e)})
            time.sleep(interval)

    def _advance_dry_run(self, symbol: str) -> None:
        """Feed the latest CLOSED M15 bar to the virtual position tracker."""
        pos = self.dry_run_tracker.snapshot()
        if not pos:
            return
        bars = MT5Client.get_bars(symbol, "M15", 3)
        if not bars:
            return
        last_closed = bars[-2]   # [-1] is the forming bar
        self.dry_run_tracker.tick(symbol, float(last_closed["high"]), float(last_closed["low"]))

    def _scan_symbol(self, symbol: str) -> None:
        """Scan a single symbol for trade setups."""
        log_event("SCAN_START", {"symbol": symbol})

        if os.getenv("STRATEGY", "breakout") == "breakout":
            return self._scan_symbol_breakout(symbol)

        # 1. Check circuit breakers
        can_trade, reason = self.risk_manager.can_trade(symbol)
        if not can_trade:
            log_event("SCAN_BLOCKED", {"symbol": symbol, "reason": reason})
            return

        # 2. Check kill zone
        if not _in_kill_zone():
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": "NOT_IN_KILL_ZONE"})
            return

        # 3. Fetch M15 bars
        tf = mt5.TIMEFRAME_M15
        lookback = int(os.getenv("LOOKBACK_BARS", "500"))
        df = MT5Client.get_bars(symbol, tf, lookback)
        if df is None or len(df) < 50:
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": "INSUFFICIENT_DATA"})
            return

        # 4. Regime filter
        if os.getenv("REGIME_FILTER_ENABLED", "true") == "true":
            regime = detect_regime(df)
            if regime == "RANGING" and os.getenv("REGIME_ALLOW_RANGING", "false") != "true":
                log_event("SCAN_SKIP", {"symbol": symbol, "reason": f"REGIME_{regime}"})
                return
            if regime == "VOLATILE" and os.getenv("REGIME_ALLOW_VOLATILE", "false") != "true":
                log_event("SCAN_SKIP", {"symbol": symbol, "reason": f"REGIME_{regime}"})
                return

        # 5. Build evidence
        evidence = build_evidence(df, symbol)

        # 6. Precheck (BOS + CHOCH + strong sweep required)
        if not self._precheck(evidence):
            log_event("PRECHECK_FAIL", {"symbol": symbol})
            return
        log_event("PRECHECK_PASS", {"symbol": symbol})

        # 7. Determine trade direction from sweep
        sweeps = evidence.get("liquidity", {}).get("sweeps", [])
        strong_sweeps = [s for s in sweeps if s.get("strong")]
        if not strong_sweeps:
            return

        # Use most recent strong sweep
        latest_sweep = strong_sweeps[-1]

        # 7a. Sweep recency filter — ignore sweeps older than N bars
        max_sweep_age = int(os.getenv("MAX_SWEEP_AGE_BARS", "50"))
        last_bar = len(df) - 1
        if last_bar - latest_sweep.get("bar", 0) > max_sweep_age:
            log_event("SCAN_SKIP", {
                "symbol": symbol, "reason": "SWEEP_TOO_OLD",
                "sweep_bar": latest_sweep.get("bar"), "current_bar": last_bar,
            })
            return
        side = "BUY" if latest_sweep["type"] == "SWEEP_LOW" else "SELL"
        sweep_bias = latest_sweep["type"]

        # 7b. Validate BOS + CHOCH align with trade direction (hard filter)
        direction = "UP" if side == "BUY" else "DOWN"
        bos_list = evidence.get("structure", {}).get("bos", [])
        choch_list = evidence.get("structure", {}).get("choch", [])
        if os.getenv("PRECHECK_REQUIRE_BOS", "true") == "true":
            if not any(b.get("direction") == direction for b in bos_list):
                log_event("SCAN_SKIP", {"symbol": symbol, "reason": f"NO_{direction}_BOS"})
                return
        if os.getenv("PRECHECK_REQUIRE_CHOCH", "true") == "true":
            if not any(c.get("direction") == direction for c in choch_list):
                log_event("SCAN_SKIP", {"symbol": symbol, "reason": f"NO_{direction}_CHOCH"})
                return

        # 8. Score confluence (no gate yet — gate depends on HTF alignment).
        current_price = float(df["close"].iloc[-1])
        base_min_score = int(os.getenv("MIN_CONFLUENCE_SCORE", "3"))
        conf_score, factors = score_confluence(
            evidence, side, current_price, sweep_age_bars=last_bar - latest_sweep.get("bar", 0),
        )

        # 9. Check news blackout
        if is_news_blackout(symbol):
            log_event("NEWS_BLACKOUT", {"symbol": symbol})
            return

        # 10. HTF bias + alignment-aware score gate.
        htf_bias = _get_htf_bias(symbol)
        htf_filter_enabled = os.getenv("HTF_FILTER_ENABLED", "true") == "true"
        htf_mode = os.getenv("HTF_FILTER_MODE", "soft").lower()

        htf_aligned = htf_filter_enabled and (
            (htf_bias == "BULLISH" and side == "BUY") or
            (htf_bias == "BEARISH" and side == "SELL")
        )
        htf_opposed = htf_filter_enabled and (
            (htf_bias == "BEARISH" and side == "BUY") or
            (htf_bias == "BULLISH" and side == "SELL")
        )
        htf_neutral = htf_filter_enabled and htf_bias == "NEUTRAL"

        # 10a. HARD mode: opposed setups are rejected immediately (legacy behavior).
        if htf_opposed and htf_mode == "hard":
            log_event("TRADE_REJECTED", {
                "symbol": symbol, "side": side,
                "reason": f"HTF_BIAS_BLOCK (H4 {htf_bias} blocks {side})",
            })
            return

        # 10b. Determine required score given HTF alignment.
        if htf_aligned:
            required_score = int(os.getenv(
                "MIN_CONFLUENCE_SCORE_HTF_ALIGNED", str(base_min_score)
            ))
        elif htf_opposed:
            # SOFT mode only — opposed setups need elite score; LLM conf also bumped below.
            required_score = int(os.getenv("MIN_CONFLUENCE_SCORE_HTF_OPPOSED", "5"))
        elif htf_neutral:
            required_score = base_min_score + 1
        else:
            required_score = base_min_score

        if conf_score < required_score:
            log_event("CONFLUENCE_LOW", {
                "symbol": symbol, "score": conf_score, "required": required_score,
                "htf_bias": htf_bias, "side": side, "factors": factors,
            })
            return

        # 10c. RSI gate for score-3 — research says RSI confluence lifts WR materially.
        if conf_score == 3 and os.getenv("RSI_GATE_FOR_SCORE_3", "true") == "true":
            has_rsi = any(f.startswith("RSI_") for f in factors)
            if not has_rsi:
                log_event("RSI_GATE_FAIL", {
                    "symbol": symbol, "score": conf_score, "factors": factors,
                })
                return

        # 10d. Skip LLM if same symbol+direction already open (saves $0.0003/scan).
        # We still re-check at step 16 to defend against race conditions.
        existing = mt5.positions_get(symbol=symbol) or []
        want_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        if any(p.type == want_type for p in existing):
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": f"POSITION_EXISTS_{side}"})
            return

        # 11. LLM veto (optional). Cache by sweep bar so stable setups don't re-pay.
        llm_decision = None
        llm_enabled = os.getenv("LLM_ENABLED", os.getenv("OLLAMA_ENABLED", "true"))
        if llm_enabled == "true":
            llm_decision = call_llm(
                evidence, side=side,
                cache_key=(symbol, side, int(latest_sweep.get("bar", 0))),
            )
            log_event("LLM_CALLED", {
                "symbol": symbol,
                "decision": llm_decision.get("decision") if llm_decision else "UNAVAILABLE",
                "confidence": llm_decision.get("confidence") if llm_decision else None,
                "cached": llm_decision.get("_cached", False) if llm_decision else False,
            })

        # 12. Validate trade. In SOFT mode, skip is_trade_valid's HTF block (we
        # already enforced the alignment-aware score gate above). HTF-opposed
        # setups require a higher LLM confidence floor.
        skip_htf_in_valid = (htf_mode == "soft")
        min_conf_override = None
        if htf_opposed:
            min_conf_override = float(os.getenv("MIN_LLM_CONFIDENCE_HTF_OPPOSED", "0.85"))
        valid, valid_reason = is_trade_valid(
            llm_decision, side, sweep_bias, htf_bias, symbol,
            skip_htf=skip_htf_in_valid, min_conf_override=min_conf_override,
        )
        if not valid:
            _log_rejection_deduped(
                "TRADE_REJECTED", symbol, side, latest_sweep["bar"],
                valid_reason,
                {"symbol": symbol, "side": side, "reason": valid_reason},
            )
            return

        # 13. Calculate SL via structure, then build the multi-target ladder.
        # ATR basis for SL limits: EMA5 of ATR14 (smooths single-bar spikes);
        # previous EMA value kept for the jump-guard comparison (p99=20%).
        atr_series = atr(df, 14)
        atr_val = float(atr_series.iloc[-1])
        atr_ema = atr_series.ewm(span=5).mean()
        atr_ema5 = float(atr_ema.iloc[-1])
        atr_prev = float(atr_ema.iloc[-2]) if len(atr_ema) > 1 else None
        swings = evidence.get("structure", {}).get("swings", [])
        sl = _structure_sl(swings, current_price, side, atr_val)

        targets = build_targets(side, current_price, sl, evidence, conf_score)
        # If LLM_PLAN_TARGETS is on and we already called the LLM, let it
        # override the structural ladder when its plan validates.
        if (os.getenv("LLM_PLAN_TARGETS", "false").lower() == "true"
                and llm_decision is not None):
            targets = apply_llm_plan(
                targets, llm_decision, side, current_price, sl, conf_score,
            )
        tp1 = targets["tp1"]
        tp2 = targets["tp2"]
        tp_final = targets["tp_final"]
        tp = tp_final  # broker-side TP — protects the runner if monitor stalls

        # 14. Validate SL/TP — the floor is score-tiered (3→2.0, 4→2.5, 5→3.0).
        rr_floor = rr_floor_for_score(conf_score)
        sl_valid, sl_reason = self.risk_manager.validate_sl_tp(
            symbol, side, current_price, sl, tp, min_rr_override=rr_floor,
            atr=atr_ema5, atr_prev=atr_prev,
        )
        if not sl_valid:
            _log_rejection_deduped(
                "SLTP_INVALID", symbol, side, latest_sweep["bar"],
                sl_reason,
                {"symbol": symbol, "side": side, "reason": sl_reason,
                 "rr_floor": rr_floor, "tp2_source": targets.get("tp2_source"),
                 # full numeric chain for gate calibration (MIN/MAX_SL_PIPS
                 # tuning, SL/ATR distribution, ATR drift tracking)
                 "entry": round(float(current_price), 6),
                 "sl": round(float(sl), 6),
                 "atr14": round(float(atr_val), 6),
                 "sl_dist": round(abs(current_price - sl), 6),
                 "sl_over_atr": round(abs(current_price - sl) / atr_val, 2) if atr_val > 0 else None},
            )
            return

        # 15. Calculate volume
        volume = self.risk_manager.calculate_volume(symbol, current_price, sl)
        if volume is None:
            log_event("VOLUME_CALC_FAILED", {"symbol": symbol})
            return

        # 15b. Correlation guard — block stacking same-direction exposure across
        # correlated symbols (e.g. long XAUUSD + long AUDUSD ≈ double-short USD).
        corr_ok, corr_reason = _correlation_guard(symbol, side)
        if not corr_ok:
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": corr_reason})
            return

        # 16. Duplicate check — both in-memory and live position guard
        strat_key = self.strategy_store.make_key(symbol, side, latest_sweep["bar"])
        if self.strategy_store.is_duplicate(strat_key):
            return
        # Hard guard: block if same symbol+direction already open in MT5
        existing = mt5.positions_get(symbol=symbol) or []
        want_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        if any(p.type == want_type for p in existing):
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": f"POSITION_EXISTS_{side}"})
            return
        self.strategy_store.record(strat_key)

        # 17. Get fresh price
        tick = MT5Client.get_tick(symbol)
        if tick:
            entry = tick.ask if side == "BUY" else tick.bid
        else:
            entry = current_price

        confidence = llm_decision.get("confidence", 0.0) if llm_decision else 0.0
        dry_run = os.getenv("DRY_RUN", "true") == "true"

        if dry_run:
            log_event("DRY_RUN_TRADE", {
                "symbol": symbol, "side": side, "volume": volume,
                "entry": entry, "sl": sl, "tp": tp,
                "tp1": tp1, "tp2": tp2, "tp_final": tp_final,
                "tp2_source": targets.get("tp2_source"),
                "rr_floor": rr_floor,
                "score": conf_score, "factors": factors,
                "confidence": confidence,
            })
            # P0: virtual position lifecycle — without exit events the
            # 'do adopted signals win?' question is unanswerable.
            try:
                self.dry_run_tracker.open_position(
                    symbol, side, volume, entry, sl, tp_final, tp1, tp2,
                    score=conf_score, confidence=confidence,
                )
            except Exception as e:
                logger.exception("dry_run_tracker.open_position failed: %s", e)
            notify(
                f"📊 <b>DRY RUN</b>: {symbol} | {side} | Score:{conf_score}/5 | "
                f"Conf:{confidence:.2f}\n"
                f"Entry: {entry:.5f} | SL: {sl:.5f}\n"
                f"TP1: {tp1:.5f} | TP2: {tp2:.5f} | Final: {tp_final:.5f} "
                f"({targets.get('tp2_source')})\n"
                f"Volume: {volume} lots | NOT placed (DRY_RUN)"
            )
            return

        # 18. Execute
        result = place_order(symbol, side, volume, entry, sl, tp)
        if result:
            self.risk_manager.record_trade_opened()
            try:
                self.trade_manager.register_trade(
                    ticket=result["ticket"],
                    symbol=symbol, side=side, volume=result["volume"],
                    entry=result["price"], sl=sl, tp=tp,
                    score=conf_score, confidence=confidence,
                    tp1=tp1, tp2=tp2, tp_final=tp_final,
                )
                log_event("TRADE_REGISTERED", {
                    "ticket": result["ticket"], "symbol": symbol, "side": side,
                    "entry": result["price"], "sl": sl, "tp": tp,
                    "tp1": tp1, "tp2": tp2, "tp_final": tp_final,
                    "score": conf_score, "confidence": confidence,
                }, stream="lifecycle")
                # Freeze entry-time market context for later replay.
                # Async queue — never blocks the scan loop on disk I/O.
                try:
                    self._snapshot_writer.submit(
                        trade_id=result["ticket"], symbol=symbol, side=side,
                        entry=result["price"], sl=sl, tp=tp_final,
                        m15=df, h4=h4, signal=sig,
                    )
                except Exception as e:
                    logger.warning("snapshot submit failed for %s: %s", symbol, e)
            except Exception as e:
                logger.exception("register_trade failed for ticket %s: %s",
                                 result.get("ticket"), e)
                log_event("TRADE_REGISTRATION_FAILED", {
                    "ticket": result.get("ticket"), "symbol": symbol,
                    "error": str(e),
                })
            notify(
                f"💰 <b>TRADE PLACED</b>\n"
                f"<code>{symbol} | {side} | {result['volume']} lots</code>\n"
                f"Entry: {result['price']:.5f} | SL: {sl:.5f} | TP: {tp:.5f}\n"
                f"TP1: {tp1:.5f} | TP2: {tp2:.5f} | Final: {tp_final:.5f}\n"
                f"Score: {conf_score}/5 | Conf: {confidence:.2f}\n"
                f"Ticket: #{result['ticket']}"
            )

    def _scan_symbol_breakout(self, symbol: str) -> None:
        """STRATEGY=breakout: H4-filtered 20-bar breakout with pullback entry.

        Reuses the existing infra stack untouched: kill zone, risk manager
        (daily loss / trade count / spread), news blackout, LLM confirm,
        SL/TP validation, volume, correlation + duplicate guards, tracker.
        """
        # 1-2. circuit breakers + kill zone (same as SMC path)
        can_trade, reason = self.risk_manager.can_trade(symbol)
        if not can_trade:
            log_event("SCAN_BLOCKED", {"symbol": symbol, "reason": reason})
            return
        if not _in_kill_zone():
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": "NOT_IN_KILL_ZONE"})
            return

        # 3. bars
        df = MT5Client.get_bars(symbol, mt5.TIMEFRAME_M15,
                                int(os.getenv("LOOKBACK_BARS", "500")))
        if df is None or len(df) < 50:
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": "INSUFFICIENT_DATA"})
            return
        h4 = MT5Client.get_bars(symbol, mt5.TIMEFRAME_H4, 80)

        # 4. strategy evaluation (pure function)
        sig = evaluate_breakout(df, h4)
        if sig is None:
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": "NO_BREAKOUT_SETUP"})
            return

        side = sig["side"]
        entry_bar = int(sig["bar"])
        current_price = float(df["close"].iloc[-1])

        # 5. news blackout (existing filter)
        if is_news_blackout(symbol):
            log_event("NEWS_BLACKOUT", {"symbol": symbol})
            return

        # 6. LLM confirm — OFF for breakout by default. The SMC-oriented
        # prompt demands BOS/CHOCH evidence that breakout signals never
        # carry, so every call returns REFUSE (observed 198/198 on 9/25,
        # deadlocking the path at zero trades). Re-enable only with a
        # breakout-specific prompt (LLM_BREAKOUT_ENABLED=true).
        llm_decision = None
        llm_enabled = os.getenv("LLM_ENABLED", os.getenv("OLLAMA_ENABLED", "true"))
        bo_llm = os.getenv("LLM_BREAKOUT_ENABLED", "false")
        if llm_enabled == "true" and bo_llm == "true":
            llm_decision = call_llm(
                {"strategy": "breakout",
                 "side": side,
                 "level": sig["level"],
                 "entry": sig["entry"],
                 "atr": sig["atr"],
                 "factors": sig["factors"],
                 "symbol": symbol},
                side=side,
                cache_key=(symbol, side, entry_bar),
            )
            log_event("LLM_CALLED", {
                "symbol": symbol,
                "decision": llm_decision.get("decision") if llm_decision else "UNAVAILABLE",
                "confidence": llm_decision.get("confidence") if llm_decision else None,
                "cached": llm_decision.get("_cached", False) if llm_decision else False,
            })
        # 6b. Validate. With LLM off for breakout, skip is_trade_valid's
        # "LLM required" gate (decision=None -> LLM_UNAVAILABLE block);
        # the rule gates (news/SL/TP/volume/correlation/dup) still run below.
        if llm_decision is not None:
            valid, valid_reason = is_trade_valid(
                llm_decision, side, None, None, symbol,
            )
            if not valid:
                _log_rejection_deduped(
                    "TRADE_REJECTED", symbol, side, entry_bar,
                    valid_reason,
                    {"symbol": symbol, "side": side, "reason": valid_reason},
                )
                return

        # 7. SL/TP validation (existing gates, ATR context included)
        atr_series = atr(df, 14)
        atr_val = float(atr_series.iloc[-1])
        atr_ema5 = float(atr_series.ewm(span=5).mean().iloc[-1])
        atr_prev = float(atr_series.ewm(span=5).mean().iloc[-2])
        sl_valid, sl_reason = self.risk_manager.validate_sl_tp(
            symbol, side, current_price, sig["sl"], sig["tp_final"],
            min_rr_override=2.0, atr=atr_ema5, atr_prev=atr_prev,
        )
        if not sl_valid:
            _log_rejection_deduped(
                "SLTP_INVALID", symbol, side, entry_bar,
                sl_reason,
                {"symbol": symbol, "side": side, "reason": sl_reason,
                 "entry": round(current_price, 6), "sl": round(sig["sl"], 6),
                 "atr14": round(atr_val, 6)},
            )
            return

        # 8. volume
        volume = self.risk_manager.calculate_volume(symbol, current_price, sig["sl"])
        if volume is None:
            log_event("VOLUME_CALC_FAILED", {"symbol": symbol})
            return

        # 9. correlation guard
        corr_ok, corr_reason = _correlation_guard(symbol, side)
        if not corr_ok:
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": corr_reason})
            return

        # 10. duplicate + live-position guards (existing store)
        strat_key = self.strategy_store.make_key(symbol, side, entry_bar)
        if self.strategy_store.is_duplicate(strat_key):
            return
        existing = mt5.positions_get(symbol=symbol) or []
        want_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        if any(p.type == want_type for p in existing):
            log_event("SCAN_SKIP", {"symbol": symbol, "reason": f"POSITION_EXISTS_{side}"})
            return
        self.strategy_store.record(strat_key)

        # 11. fresh entry price
        tick = MT5Client.get_tick(symbol)
        entry = tick.ask if side == "BUY" and tick else (
            tick.bid if tick else current_price)

        confidence = llm_decision.get("confidence", 0.0) if llm_decision else 0.0
        dry_run = os.getenv("DRY_RUN", "true") == "true"

        if dry_run:
            log_event("DRY_RUN_TRADE", {
                "symbol": symbol, "side": side, "volume": volume,
                "entry": entry, "sl": sig["sl"], "tp": sig["tp_final"],
                "tp1": sig["tp1"], "tp2": sig["tp2"], "tp_final": sig["tp_final"],
                "tp2_source": "BREAKOUT_LADDER", "rr_floor": 2.0,
                "score": sig["score"], "factors": sig["factors"],
                "confidence": confidence, "strategy": "breakout",
            })
            try:
                self.dry_run_tracker.open_position(
                    symbol, side, volume, entry, sig["sl"], sig["tp_final"],
                    sig["tp1"], sig["tp2"],
                    score=sig["score"], confidence=confidence,
                )
            except Exception as e:
                logger.exception("dry_run_tracker.open_position failed: %s", e)
            notify(
                f"📊 <b>DRY RUN [BREAKOUT]</b>: {symbol} | {side} | "
                f"Score:{sig['score']}/4 | Conf:{confidence:.2f}\n"
                f"Entry: {entry:.5f} | SL: {sig['sl']:.5f}\n"
                f"TP1: {sig['tp1']:.5f} | TP2: {sig['tp2']:.5f} | "
                f"Final: {sig['tp_final']:.5f}\n"
                f"Volume: {volume} lots | NOT placed (DRY_RUN)"
            )
            return

        result = place_order(symbol, side, volume, entry, sig["sl"], sig["tp_final"])
        if result:
            self.risk_manager.record_trade_opened()
            try:
                self.trade_manager.register_trade(
                    ticket=result["ticket"],
                    symbol=symbol, side=side, volume=result["volume"],
                    entry=result["price"], sl=sig["sl"], tp=sig["tp_final"],
                    score=sig["score"], confidence=confidence,
                    tp1=sig["tp1"], tp2=sig["tp2"], tp_final=sig["tp_final"],
                    sig_entry=sig["entry"],
                )
                log_event("TRADE_REGISTERED", {
                    "ticket": result["ticket"], "symbol": symbol, "side": side,
                    "entry": result["price"], "sl": sig["sl"], "tp": sig["tp_final"],
                    "tp1": sig["tp1"], "tp2": sig["tp2"], "tp_final": sig["tp_final"],
                    "score": sig["score"],
                    "sig_entry": sig["entry"],
                    "slippage": round(result["price"] - sig["entry"], 6),
                }, stream="lifecycle")
                # Freeze entry-time market context for later replay.
                # Async queue — never blocks the scan loop on disk I/O.
                try:
                    self._snapshot_writer.submit(
                        trade_id=result["ticket"], symbol=symbol, side=side,
                        entry=result["price"], sl=sig["sl"], tp=sig["tp_final"],
                        m15=df, h4=h4, signal=sig,
                    )
                except Exception as e:
                    logger.warning("snapshot submit failed for %s: %s", symbol, e)
            except Exception as e:
                logger.exception("trade_manager.register_trade failed: %s", e)
            notify(
                f"✅ <b>BREAKOUT {side}</b>: {symbol} @ {result['price']:.5f} | "
                f"ticket {result['ticket']}"
            )
        else:
            log_event("ORDER_FAILED", {"symbol": symbol, "side": side, "strategy": "breakout"})

    def _precheck(self, evidence: dict) -> bool:
        """Precheck: require BOS + CHOCH + strong sweep as configured."""
        bos = evidence.get("structure", {}).get("bos", [])
        choch = evidence.get("structure", {}).get("choch", [])
        sweeps = evidence.get("liquidity", {}).get("sweeps", [])
        strong_sweeps = [s for s in sweeps if s.get("strong")]

        if os.getenv("PRECHECK_REQUIRE_BOS", "true") == "true" and not bos:
            return False
        if os.getenv("PRECHECK_REQUIRE_CHOCH", "true") == "true" and not choch:
            return False
        if os.getenv("PRECHECK_REQUIRE_SWEEP", "true") == "true" and not sweeps:
            return False
        if os.getenv("PRECHECK_REQUIRE_STRONG_SWEEP", "true") == "true" and not strong_sweeps:
            return False

        return True

    def _monitor_loop(self) -> None:
        """Monitor open positions for BE, partial TP, SL/TP hits."""
        while self._running:
            try:
                self.trade_manager.check_positions()
            except Exception as e:
                logger.exception("Monitor error: %s", e)
            time.sleep(60)

    def _storage_guard_loop(self) -> None:
        """Run scripts/storage_guard.run_guard once per day at 04:00 UTC.

        Thread-based (not APScheduler) to keep the dependency set unchanged;
        same pattern as _daily_summary_loop. Guard failures never kill the
        loop — a missed day only delays pruning, never loses data.
        """
        last_run_day: str | None = None
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")
                if now.hour == 4 and last_run_day != today:
                    last_run_day = today
                    from scripts.storage_guard import run_guard
                    report = run_guard(now=now)
                    logger.info("storage guard: %.1f MB (critical=%s full=%s)",
                                report.get("total_mb", 0),
                                report.get("critical", False),
                                report.get("storage_full", False))
            except Exception as e:
                logger.warning("storage guard run failed: %s", e)
            time.sleep(60)

    def _daily_summary_loop(self) -> None:
        """Send daily summary at 21:00 UTC."""
        while self._running:
            now = datetime.now(timezone.utc)
            if now.hour == 21 and now.minute == 0:
                try:
                    from services.risk.state_store import read_state
                    risk_state = read_state(
                        os.path.join("storage", "db", "risk_state.json"),
                        {"initial_balance": 0},
                    )
                    acct = mt5.account_info()
                    initial_bal = risk_state.get("initial_balance", 0)
                    if initial_bal <= 0:
                        initial_bal = acct.balance if acct else 10000
                    summary = get_daily_summary(initial_bal)
                    notify(
                        f"📈 <b>DAILY SUMMARY</b> {summary['date']}\n"
                        f"Trades: {summary['trades']} | Wins: {summary['wins']} | "
                        f"Losses: {summary['losses']}\n"
                        f"Net P&L: ${summary['pnl']:+.2f}\n"
                        f"FTMO Progress: {summary['ftmo_progress']:.1f}% / 10.0% target"
                    )
                except Exception as e:
                    logger.exception("Daily summary error: %s", e)
            time.sleep(60)
