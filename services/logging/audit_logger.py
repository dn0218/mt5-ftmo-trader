"""JSONL audit logger — one event per line."""

from __future__ import annotations
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOG_LOCK = threading.Lock()
_LOG_PATH = os.path.join("storage", "logs", "audit.jsonl")


def log_event(event_type: str, data: dict | None = None, path: str | None = None) -> None:
    """Append a JSON event to the audit log."""
    log_path = path or _LOG_PATH
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
    }
    if data:
        entry["data"] = data

    with _LOG_LOCK:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def log_decision_debug(data: dict) -> None:
    """Log LLM decision debug info."""
    debug_path = os.path.join("storage", "logs", "decision_debug.jsonl")
    log_event("LLM_DECISION", data, path=debug_path)
