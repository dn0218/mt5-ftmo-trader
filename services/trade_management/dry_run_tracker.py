"""Dry-run virtual position lifecycle tracker — mirrors manager.py exactly.

P0 instrumentation: exit events make 'do adopted signals win?' answerable.

Ladder semantics (line-verified against services/trade_management/manager.py):
  1. BE trigger at 0.5R (BE_TRIGGER_R): SL -> entry, ratchets once
  2. TP1 (absolute level): close 50% (TP1_CLOSE_PCT)
  3. TP2 (absolute level, only after TP1): close 30% of remaining (TP2_CLOSE_PCT)
  4. Runner (20%) trails by TRAIL_ATR_MULT x ATR14 after TP2, ratchet-only
  5. Same-bar SL+TP double-touch -> conservative SL-first
  6. 8h timeout -> MTM close, but tracking CONTINUES on paper to the real
     SL/TP outcome (censored data must not be silently mixed into aggregates;
     the final event carries both the 8h snapshot and the true exit).

R accounting matches the live fractions:
  full SL before anything:      -1R x 100%            -> -1.0
  SL after BE:                   0R x 100%            ->  0.0
  TP1 then SL@BE:                0.5x1R + 0.5x0R      -> +0.5
  TP1+TP2 then trail exit at r:  with default TP1=50%/TP2=30%-of-remaining,
  fractions of ORIGINAL volume are 50% / 15% / 35% runner:
    R = 0.5x1 + 0.15x r2 + 0.35x r   (r2 = TP2 R, r = runner exit R)
"""
from __future__ import annotations
import os
from datetime import datetime, timezone

from services.logging.audit_logger import log_event

_LIFETIME_BARS = 8 * 4   # 8h snapshot boundary (tracking continues past it)


class DryRunTracker:
    def __init__(self, mt5_mod):
        self._mt5 = mt5_mod
        self._open: dict[str, dict] = {}

    # ── lifecycle ─────────────────────────────────────────
    def open_position(self, symbol: str, side: str, volume: float,
                      entry: float, sl: float, tp_final: float,
                      tp1: float | None, tp2: float | None,
                      score: int, confidence: float) -> None:
        if symbol in self._open:
            return  # one virtual position per symbol
        sign = 1 if side == "BUY" else -1
        sl_dist = abs(entry - sl)
        # Mirror manager.py fractions EXACTLY (env-driven, same vars):
        #   TP1 closes TP1_CLOSE_PCT% of ORIGINAL volume
        #   TP2 closes TP2_CLOSE_PCT% of the REMAINING volume
        #   runner keeps the rest  (= 50/15/35 with default 50/30)
        tp1_frac = float(os.getenv("TP1_CLOSE_PCT", "50")) / 100.0
        tp2_frac = float(os.getenv("TP2_CLOSE_PCT", "30")) / 100.0
        self._open[symbol] = {
            "side": side, "entry": entry, "sl": sl, "sl_dist": sl_dist,
            "tp1": tp1 if tp1 is not None else entry + sign * sl_dist,
            "tp2": tp2 if tp2 is not None else tp_final,
            "tp_final": tp_final, "volume": volume,
            "tp1_frac": tp1_frac,
            "tp2_frac_orig": (1.0 - tp1_frac) * tp2_frac,
            "score": score, "confidence": confidence,
            "be_triggered": False, "tp1_done": False, "tp2_done": False,
            "trail_sl": None,
            "opened_utc": datetime.now(timezone.utc),
            "bars_elapsed": 0, "snapshot_taken": False,
        }
        log_event("DRY_RUN_POSITION_OPEN", {
            "symbol": symbol, "side": side, "entry": entry, "sl": sl,
            "tp1": self._open[symbol]["tp1"], "tp2": self._open[symbol]["tp2"],
            "volume": volume, "score": score, "confidence": confidence,
        })

    def tick(self, symbol: str, bar_high: float, bar_low: float,
             bar_close: float | None = None, atr: float | None = None) -> None:
        """Advance one closed M15 bar. bar_close/atr feed the runner trail."""
        pos = self._open.get(symbol)
        if pos is None:
            return
        pos["bars_elapsed"] += 1
        sign = 1 if pos["side"] == "BUY" else -1

        def hit(level):   # TP side touch
            return bar_high >= level if sign == 1 else bar_low <= level
        def stop(level):  # SL side touch
            return bar_low <= level if sign == 1 else bar_high >= level

        sl_now = pos["trail_sl"] if pos["tp2_done"] and pos["trail_sl"] else pos["sl"]

        # 1) stop check first (conservative on same-bar double touch)
        if stop(sl_now):
            self._close_stop(pos, symbol, sl_now)
            return
        # 2) BE trigger at BE_TRIGGER_R (mid-bar approximation: use the
        #    favourable extreme; manager uses tick price at 60s cadence)
        be_r = float(os.getenv("BE_TRIGGER_R", "0.5"))
        fav = (bar_high - pos["entry"]) if sign == 1 else (pos["entry"] - bar_low)
        if not pos["be_triggered"] and fav / pos["sl_dist"] >= be_r:
            pos["be_triggered"] = True
            pos["sl"] = pos["entry"]
            log_event("DRY_RUN_BE", {"symbol": symbol, "r": be_r})
        # 3) TP1
        if not pos["tp1_done"] and hit(pos["tp1"]):
            pos["tp1_done"] = True
            log_event("DRY_RUN_TP1", {
                "symbol": symbol, "closed_frac": pos["tp1_frac"],
                "bars_elapsed": pos["bars_elapsed"]})
        # 4) TP2 (only after TP1)
        if pos["tp1_done"] and not pos["tp2_done"] and hit(pos["tp2"]):
            pos["tp2_done"] = True
            log_event("DRY_RUN_TP2", {
                "symbol": symbol, "closed_frac": pos["tp2_frac_orig"],
                "bars_elapsed": pos["bars_elapsed"]})
        # 5) trail the runner after TP2 (ratchet-only, TRAIL_ATR_MULT x ATR)
        if pos["tp2_done"] and atr and atr > 0:
            mult = float(os.getenv("TRAIL_ATR_MULT", "1.5"))
            ref_close = bar_close if bar_close is not None else (bar_high + bar_low) / 2
            cand = ref_close - sign * mult * atr
            prev = pos.get("trail_sl")
            better = cand if prev is None else (
                max(cand, prev) if sign == 1 else min(cand, prev))
            if prev is None or better != prev:
                pos["trail_sl"] = better
        # 6) broker-side TP protects the runner (tp_final) — mirror live
        if pos["tp1_done"] and hit(pos["tp_final"]):
            self.close_final(symbol, pos["tp_final"])
            return
        # 7) timeout snapshot — tracking continues to the real exit
        if pos["bars_elapsed"] == _LIFETIME_BARS and not pos["snapshot_taken"]:
            pos["snapshot_taken"] = True
            mtm = (bar_high + bar_low) / 2
            log_event("DRY_RUN_TIMEOUT_SNAPSHOT", {
                "symbol": symbol, "r_multiple": round(self._r(pos, mtm), 3),
                "note": "censored — tracking continues to real exit"})

    # ── helpers ───────────────────────────────────────────
    def _r(self, pos: dict, exit_price: float) -> float:
        sign = 1 if pos["side"] == "BUY" else -1
        exit_r = sign * (exit_price - pos["entry"]) / pos["sl_dist"]
        # Ladder R mirrors manager's ACTUAL fractions (env-driven):
        #   full SL before anything:      -1R
        #   TP1 then SL@BE:               tp1_frac * 1R
        #   TP1+TP2, runner exits at r:   tp1_frac*1 + tp2_frac_orig*r2 + runner*r
        runner_frac = 1.0 - pos["tp1_frac"] - pos["tp2_frac_orig"]
        if pos["tp2_done"]:
            tp2_r = sign * (pos["tp2"] - pos["entry"]) / pos["sl_dist"]
            return (pos["tp1_frac"] * 1.0 + pos["tp2_frac_orig"] * tp2_r
                    + runner_frac * exit_r)
        if pos["tp1_done"]:
            return pos["tp1_frac"] * 1.0 + (1.0 - pos["tp1_frac"]) * exit_r
        return exit_r

    def _close_stop(self, pos: dict, symbol: str, sl_level: float) -> None:
        self._open.pop(symbol)
        log_event("DRY_RUN_SL_HIT", {
            "symbol": symbol, "side": pos["side"],
            "r_multiple": round(self._r(pos, sl_level), 3),
            "bars_elapsed": pos["bars_elapsed"],
            "be_triggered": pos["be_triggered"], "tp1_taken": pos["tp1_done"],
            "tp2_taken": pos["tp2_done"],
            "score": pos["score"], "confidence": pos["confidence"],
            "opened_at": pos["opened_utc"].isoformat(),
        })

    def close_final(self, symbol: str, exit_price: float) -> None:
        """TP final reached (or manual close) — close the runner."""
        pos = self._open.pop(symbol)
        log_event("DRY_RUN_TP_FINAL", {
            "symbol": symbol, "side": pos["side"],
            "r_multiple": round(self._r(pos, exit_price), 3),
            "bars_elapsed": pos["bars_elapsed"],
            "tp1_taken": True, "tp2_taken": pos["tp2_done"],
            "score": pos["score"], "confidence": pos["confidence"],
            "opened_at": pos["opened_utc"].isoformat(),
        })

    def snapshot(self) -> list[dict]:
        return list(self._open.values())
