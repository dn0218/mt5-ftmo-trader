"""ForexFactory news filter — no API key needed."""

from __future__ import annotations
import os
import time
import logging
import httpx
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_CACHE: dict = {"events": [], "ts": 0.0}


def _events() -> list:
    """Fetch and cache ForexFactory calendar events (1h cache)."""
    if time.time() - _CACHE["ts"] < 3600:
        return _CACHE["events"]
    try:
        r = httpx.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=10.0,
            follow_redirects=True,
        )
        if r.status_code == 200:
            _CACHE["events"] = r.json()
            _CACHE["ts"] = time.time()
    except Exception as e:
        logger.warning("ForexFactory fetch failed (non-fatal): %s", e)
    return _CACHE["events"]


def is_news_blackout(symbol: str) -> bool:
    """Check if we're in a news blackout period for this symbol."""
    if os.getenv("NEWS_FILTER_ENABLED", "true") != "true":
        return False
    sym = symbol.upper().replace("_", "").replace(".", "")
    currencies = {sym[:3], sym[3:6]}
    pre = int(os.getenv("NEWS_BLACKOUT_PRE_MINUTES", "30"))
    post = int(os.getenv("NEWS_BLACKOUT_POST_MINUTES", "15"))
    now = datetime.now(timezone.utc)
    for ev in _events():
        if ev.get("impact", "").upper() != "HIGH":
            continue
        country = ev.get("country", "").upper()
        if country not in currencies:
            continue
        try:
            date_str = ev.get("date", "")
            et = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if (et - timedelta(minutes=pre)) <= now <= (et + timedelta(minutes=post)):
            return True
    return False
