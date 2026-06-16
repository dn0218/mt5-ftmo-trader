"""Telegram notification service. Never raises — trading loop cannot crash from this."""

import httpx
import os
import logging

logger = logging.getLogger(__name__)


def notify(message: str) -> bool:
    """Send Telegram message. Returns True on success, False otherwise."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id or os.getenv("TELEGRAM_ENABLED", "true") != "true":
        logger.info("[TELEGRAM DISABLED] %s", message)
        return False
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
            )
        return r.status_code == 200
    except Exception as e:
        logger.warning("Telegram failed (non-fatal): %s", e)
        return False
