"""Two-way Telegram bot — polls for commands, dispatches responses.

Commands:
  /status    — engine status, uptime, next kill zone
  /positions — open positions with P&L
  /balance   — account balance + equity + DD
  /trades    — today's completed trades
  /pause     — pause scanning (positions still monitored)
  /resume    — resume scanning
  /risk      — current risk state (consecutive losses, cooldowns)
  /help      — list available commands
"""

from __future__ import annotations
import os
import time
import threading
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# Module-level reference to the engine — set by main.py
_engine = None
_start_time = None


def set_engine(engine) -> None:
    """Register the engine instance for command handlers."""
    global _engine, _start_time
    _engine = engine
    _start_time = datetime.now(timezone.utc)


def _send(text: str) -> None:
    """Send a message via Telegram."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def _handle_status() -> str:
    now = datetime.now(timezone.utc)
    uptime = now - _start_time if _start_time else "unknown"
    if isinstance(uptime, type(now - now)):
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours}h {mins}m {secs}s"
    else:
        uptime_str = str(uptime)

    running = _engine._running if _engine else False
    paused = getattr(_engine, '_paused', False) if _engine else False
    mode = "DRY RUN" if os.getenv("DRY_RUN", "true") == "true" else "LIVE"

    # Next kill zone
    now_mins = now.hour * 60 + now.minute
    zones = os.getenv("SESSION_KILL_ZONES", "07:00-09:00,13:00-15:00")
    next_kz = "none today"
    for zone in zones.split(","):
        parts = zone.strip().split("-")
        if len(parts) == 2:
            sh, sm = int(parts[0].split(":")[0]), int(parts[0].split(":")[1])
            eh, em = int(parts[1].split(":")[0]), int(parts[1].split(":")[1])
            start_m = sh * 60 + sm
            end_m = eh * 60 + em
            if now_mins < end_m:
                if now_mins >= start_m:
                    next_kz = f"NOW ({zone.strip()}) UTC"
                else:
                    next_kz = f"{zone.strip()} UTC (in {start_m - now_mins}min)"
                break

    return (
        f"<b>STATUS</b>\n"
        f"Mode: {mode}\n"
        f"Engine: {'RUNNING' if running else 'STOPPED'}"
        f"{' (PAUSED)' if paused else ''}\n"
        f"Uptime: {uptime_str}\n"
        f"Next kill zone: {next_kz}\n"
        f"Time: {now.strftime('%H:%M:%S')} UTC"
    )


def _handle_positions() -> str:
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get()
        if not positions or len(positions) == 0:
            return "No open positions."
        lines = ["<b>OPEN POSITIONS</b>\n"]
        for p in positions:
            pnl = p.profit
            emoji = "🟢" if pnl >= 0 else "🔴"
            side = "BUY" if p.type == 0 else "SELL"
            lines.append(
                f"{emoji} {p.symbol} {side} {p.volume} lots\n"
                f"   Entry: {p.price_open:.5f} | Current: {p.price_current:.5f}\n"
                f"   SL: {p.sl:.5f} | TP: {p.tp:.5f}\n"
                f"   P&L: ${pnl:+.2f} | Ticket: #{p.ticket}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading positions: {e}"


def _handle_balance() -> str:
    try:
        import MetaTrader5 as mt5
        acct = mt5.account_info()
        if not acct:
            return "Cannot read account info."
        from services.risk.state_store import read_state
        risk_state = read_state(
            os.path.join("storage", "db", "risk_state.json"),
            {"day_start_equity": acct.equity, "initial_balance": acct.balance},
        )
        day_start = risk_state.get("day_start_equity", acct.equity)
        initial = risk_state.get("initial_balance", acct.balance)
        daily_dd = (day_start - acct.equity) / day_start * 100 if day_start > 0 else 0
        total_dd = (initial - acct.equity) / initial * 100 if initial > 0 else 0
        return (
            f"<b>ACCOUNT</b>\n"
            f"Balance: ${acct.balance:,.2f}\n"
            f"Equity: ${acct.equity:,.2f}\n"
            f"Free margin: ${acct.margin_free:,.2f}\n"
            f"Daily DD: {daily_dd:.2f}% / 2.0% limit\n"
            f"Total DD: {total_dd:.2f}% / 6.0% limit\n"
            f"FTMO daily limit: 5.0% | total: 10.0%"
        )
    except Exception as e:
        return f"Error: {e}"


def _handle_trades() -> str:
    try:
        from services.analytics.performance import get_daily_summary
        import MetaTrader5 as mt5
        acct = mt5.account_info()
        bal = acct.balance if acct else 10000
        s = get_daily_summary(bal)
        if s["trades"] == 0:
            return "No trades today."
        return (
            f"<b>TODAY'S TRADES</b>\n"
            f"Total: {s['trades']} | Wins: {s['wins']} | Losses: {s['losses']}\n"
            f"Net P&L: ${s['pnl']:+.2f}\n"
            f"FTMO Progress: {s['ftmo_progress']:.1f}% / 10.0%"
        )
    except Exception as e:
        return f"Error: {e}"


def _handle_pause() -> str:
    if _engine:
        _engine._paused = True
        return "⏸ Scanner PAUSED. Open positions still monitored.\nSend /resume to continue."
    return "Engine not available."


def _handle_resume() -> str:
    if _engine:
        _engine._paused = False
        return "▶️ Scanner RESUMED."
    return "Engine not available."


def _handle_risk() -> str:
    try:
        from services.risk.state_store import read_state
        state = read_state(
            os.path.join("storage", "db", "risk_state.json"), {}
        )
        consec = state.get("consecutive_losses", 0)
        halted = state.get("halted_today", False)
        cooldowns = state.get("sl_cooldowns", {})
        now = time.time()
        cd_lines = []
        for sym, ts in cooldowns.items():
            remaining = int(3600 - (now - ts))
            if remaining > 0:
                cd_lines.append(f"  {sym}: {remaining // 60}m {remaining % 60}s remaining")
        cd_text = "\n".join(cd_lines) if cd_lines else "  none"
        return (
            f"<b>RISK STATE</b>\n"
            f"Consecutive losses: {consec} / {os.getenv('CONSECUTIVE_LOSS_HALT', '3')}\n"
            f"Halted today: {'YES' if halted else 'no'}\n"
            f"SL cooldowns:\n{cd_text}"
        )
    except Exception as e:
        return f"Error: {e}"


def _handle_help() -> str:
    return (
        "<b>COMMANDS</b>\n"
        "/status — engine status + uptime\n"
        "/positions — open positions with P&L\n"
        "/balance — account balance + DD\n"
        "/trades — today's completed trades\n"
        "/pause — pause scanner\n"
        "/resume — resume scanner\n"
        "/risk — risk state + cooldowns\n"
        "/help — this message"
    )


COMMANDS = {
    "/status": _handle_status,
    "/positions": _handle_positions,
    "/balance": _handle_balance,
    "/trades": _handle_trades,
    "/pause": _handle_pause,
    "/resume": _handle_resume,
    "/risk": _handle_risk,
    "/help": _handle_help,
}


def _poll_loop() -> None:
    """Long-poll Telegram for incoming commands."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram bot polling disabled — no token/chat_id")
        return

    offset = 0
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    logger.info("Telegram command bot started polling")

    while True:
        try:
            with httpx.Client(timeout=35) as client:
                r = client.get(url, params={"offset": offset, "timeout": 30})
            if r.status_code == 409:
                # Another session active — wait for it to expire (~30s long-poll)
                time.sleep(35)
                continue
            if r.status_code != 200:
                time.sleep(5)
                continue

            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                msg_chat_id = str(msg.get("chat", {}).get("id", ""))

                # Only respond to our chat
                if msg_chat_id != chat_id:
                    continue

                # Match command
                cmd = text.split()[0].lower() if text else ""
                # Strip @botname suffix (e.g. /status@mybot)
                cmd = cmd.split("@")[0]

                handler = COMMANDS.get(cmd)
                if handler:
                    try:
                        response = handler()
                        _send(response)
                    except Exception as e:
                        _send(f"Command error: {e}")
                        logger.exception("Command handler error for %s", cmd)

        except Exception as e:
            logger.warning("Telegram poll error (retrying in 5s): %s", e)
            time.sleep(5)


def start_command_bot() -> threading.Thread:
    """Start the Telegram command listener in a daemon thread."""
    t = threading.Thread(target=_poll_loop, name="telegram_bot", daemon=True)
    t.start()
    return t
