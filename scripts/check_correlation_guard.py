"""Inspect _correlation_guard in TradingEngine."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.worker.engine import TradingEngine

# Inspect the _correlation_guard method directly
print(TradingEngine._correlation_guard.__doc__)

# Also check the CORRELATION_GUARD setting in .env
cfg = {}
for l in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
    if "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1)
        cfg[k.strip()] = v.strip()
print(f"\nCORRELATION_GUARD in .env: {cfg.get("CORRELATION_GUARD", "Not set")}")
