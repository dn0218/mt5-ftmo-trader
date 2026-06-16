"""MT5 reconnection loop with retry."""

from __future__ import annotations
import os
import time
import logging
import MetaTrader5 as mt5
from services.notifications.telegram import notify

logger = logging.getLogger(__name__)


def ensure_connected(max_attempts: int = 0) -> bool:
    """Ensure MT5 is initialised and logged in. Retries indefinitely if max_attempts=0."""
    attempt = 0
    while True:
        attempt += 1
        if max_attempts > 0 and attempt > max_attempts:
            return False
        try:
            if not mt5.initialize():
                logger.warning("MT5 init failed (attempt %d): %s", attempt, mt5.last_error())
                if attempt == 1:
                    notify(
                        "⚠️ MT5 not detected — please open terminal and log in.\n"
                        "Retrying every 60s..."
                    )
                time.sleep(60)
                continue

            login = int(os.getenv("MT5_LOGIN", "0"))
            password = os.getenv("MT5_PASSWORD", "")
            server = os.getenv("MT5_SERVER", "")

            if not mt5.login(login=login, password=password, server=server):
                logger.warning("MT5 login failed (attempt %d): %s", attempt, mt5.last_error())
                time.sleep(60)
                continue

            logger.info("MT5 connected: %s account #%d", server, login)
            return True
        except Exception as e:
            logger.exception("MT5 connection error: %s", e)
            time.sleep(60)
