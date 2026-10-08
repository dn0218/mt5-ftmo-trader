"""Main entry point for the MT5 FTMO Trading Bot.

Runs forever with automatic crash recovery:
- If engine crashes, waits 30s and restarts
- Sends Telegram on every crash + recovery
- Telegram command bot runs alongside for /status, /pause, etc.
"""

from __future__ import annotations
import os
import sys
import time
import logging
import logging.handlers
import traceback
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

# Create log dirs
Path("storage/logs").mkdir(parents=True, exist_ok=True)
Path("storage/db").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.TimedRotatingFileHandler(
            "storage/logs/bot.log", when="midnight", backupCount=14,
            encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


class _SecretScrubber(logging.Filter):
    """Mask secrets (Telegram bot token etc.) in every log line.

    httpx logs full request URLs — including the bot token — at INFO level,
    which previously leaked the credential into bot.log on every call.
    """

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            if s in msg:
                record.msg = msg.replace(s, "***")
                record.args = None
        return True


_scrubber = _SecretScrubber([os.getenv("TELEGRAM_BOT_TOKEN", "")])
for _h in logging.getLogger().handlers:
    _h.addFilter(_scrubber)


def run_engine():
    """Single engine run. Returns normally on KeyboardInterrupt, raises on crash."""
    from config import Config
    from core.mt5.reconnect import ensure_connected
    from services.notifications.telegram import notify
    from services.notifications.telegram_bot import set_engine, start_command_bot
    from services.worker.engine import TradingEngine

    config = Config.from_env()

    notify("🔗 Connecting to MT5...")
    if not ensure_connected(max_attempts=60):
        notify("🔴 BLOCKED: Cannot connect to MT5 after 60 attempts")
        return

    import MetaTrader5 as mt5
    acct = mt5.account_info()
    if acct:
        notify(
            f"🔗 MT5 connected: {config.mt5_server} account #{config.mt5_login}\n"
            f"Balance: ${acct.balance:.2f} | Equity: ${acct.equity:.2f}"
        )

    mode = "DRY RUN" if config.dry_run else "LIVE"
    notify(
        f"{'📊' if config.dry_run else '🚀'} Engine starting in {mode} mode\n"
        f"Symbols: {', '.join(config.symbols)}\n"
        f"LLM: {'ON' if config.llm_enabled else 'OFF'} ({config.llm_provider}: {config.llm_model})\n"
        f"Telegram commands: /help for list"
    )

    engine = TradingEngine()

    # Register engine for Telegram commands and start command bot
    set_engine(engine)
    start_command_bot()

    engine.run_forever()


def main():
    """Run forever with crash recovery."""
    from services.notifications.telegram import notify

    crash_count = 0
    max_backoff = 300  # 5 min max

    while True:
        try:
            import os
            import uuid
            from services.logging.audit_logger import log_event
            # Process-lifecycle audit. session_id pairs BOT_START/BOT_STOP even
            # across crashes (crash = STOP missing for that session_id, which is
            # distinct from multi-instance = a NEW BOT_START while another
            # session_id is still unpaired).
            session_id = uuid.uuid4().hex[:12]
            log_event("BOT_START", {"session_id": session_id,
                                    "pid": os.getpid(), "ppid": os.getppid(),
                                    "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini")},
                      stream="lifecycle")
            try:
                run_engine()
            finally:
                log_event("BOT_STOP", {"session_id": session_id,
                                       "pid": os.getpid()},
                          stream="lifecycle")
            # Clean exit (KeyboardInterrupt caught inside run_engine)
        
            break
        except KeyboardInterrupt:
            notify("⏹️ Engine stopped by user (Ctrl+C)")
            break
        except Exception:
            crash_count += 1
            tb = traceback.format_exc()
            logger.error("ENGINE CRASH #%d:\n%s", crash_count, tb)

            # Backoff: 30s, 60s, 120s, ... up to max_backoff
            wait = min(30 * (2 ** (crash_count - 1)), max_backoff)

            try:
                notify(
                    f"💥 <b>ENGINE CRASH #{crash_count}</b>\n"
                    f"<code>{tb[-500:]}</code>\n"
                    f"Restarting in {wait}s..."
                )
            except Exception:
                pass

            time.sleep(wait)

            try:
                notify(f"🔄 Restarting engine (attempt #{crash_count + 1})...")
            except Exception:
                pass


if __name__ == "__main__":
    main()
