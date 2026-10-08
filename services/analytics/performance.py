"""FTMO progress and performance tracker."""

from __future__ import annotations
import os
from datetime import datetime, timezone
from services.risk.state_store import read_state, write_state

_PERF_PATH = os.path.join("storage", "db", "performance.json")

_DEFAULT = {
    "start_balance": 0.0,
    "trades": [],
    "daily_pnl": {},
}


def get_performance() -> dict:
    state = read_state(_PERF_PATH, _DEFAULT)
    # Pre-existing files may be missing keys; setdefault keeps callers crash-free.
    state.setdefault("start_balance", 0.0)
    state.setdefault("trades", [])
    state.setdefault("daily_pnl", {})
    return state


def _ensure_start_balance(state: dict) -> None:
    """Lazy-init start_balance from MT5 the first time we record anything."""
    if state.get("start_balance", 0.0) > 0:
        return
    try:
        import MetaTrader5 as mt5
        acct = mt5.account_info()
        if acct and getattr(acct, "balance", 0) > 0:
            state["start_balance"] = float(acct.balance)
    except Exception:
        pass


def record_trade(ticket: int, symbol: str, side: str, volume: float,
                 entry: float, exit_price: float, pnl: float, reason: str,
                 r_multiple: float | None = None) -> None:
    """Record a completed trade.

    r_multiple: net P&L in units of the position's initial 1R (risk).
    None when the risk basis is unknown (legacy state)."""
    state = get_performance()
    _ensure_start_balance(state)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state["trades"].append({
        "ticket": ticket,
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "entry": entry,
        "exit": exit_price,
        "pnl": pnl,
        "reason": reason,
        "r_multiple": r_multiple,
        "date": today,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    # Update daily PnL
    daily = state["daily_pnl"].get(today, 0.0)
    state["daily_pnl"][today] = daily + pnl
    write_state(_PERF_PATH, state)


def get_daily_summary(start_balance: float) -> dict:
    """Get daily trading summary."""
    state = get_performance()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in state["trades"] if t.get("date") == today]
    total_pnl = sum(t["pnl"] for t in today_trades)
    wins = sum(1 for t in today_trades if t["pnl"] > 0)
    losses = sum(1 for t in today_trades if t["pnl"] < 0)
    total_all_pnl = sum(t["pnl"] for t in state["trades"])

    return {
        "date": today,
        "trades": len(today_trades),
        "wins": wins,
        "losses": losses,
        "pnl": total_pnl,
        "total_pnl": total_all_pnl,
        "ftmo_progress": (total_all_pnl / start_balance * 100) if start_balance > 0 else 0,
    }
