"""Atomic state file read/write with module-level threading.Lock."""

from __future__ import annotations
import os
import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def read_state(path: str, default: dict) -> dict:
    """Read state from JSON file. Returns default if missing/corrupt."""
    with _LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default.copy()


def write_state(path: str, data: dict) -> None:
    """Atomically write state to JSON file using os.replace."""
    with _LOCK:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, str(p))
