"""Strategy store — tracks which setups have been seen to avoid duplicates."""

from __future__ import annotations
import time
import threading


class StrategyStore:
    """Prevents duplicate trades on the same setup within a cooldown window."""

    def __init__(self, cooldown_seconds: int = 3600):
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._cooldown = cooldown_seconds

    def make_key(self, symbol: str, side: str, bar_index: int) -> str:
        return f"{symbol}:{side}:{bar_index}"

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            ts = self._seen.get(key, 0)
            return (time.time() - ts) < self._cooldown

    def record(self, key: str) -> None:
        with self._lock:
            self._seen[key] = time.time()

    def cleanup(self) -> None:
        """Remove expired entries."""
        with self._lock:
            now = time.time()
            self._seen = {k: v for k, v in self._seen.items() if now - v < self._cooldown}
