"""Diagnose XAUUSD availability on the connected MT5 account.

Lists every symbol whose name contains XAU/GOLD, prints trade mode + volume
limits, and tells you which ones are actually tradable.
"""
from __future__ import annotations
import os
import sys

# Load .env
from pathlib import Path
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import MetaTrader5 as mt5

TRADE_MODE = {
    0: "DISABLED",
    1: "LONG_ONLY",
    2: "SHORT_ONLY",
    3: "CLOSE_ONLY",
    4: "FULL",
}

if not mt5.initialize():
    print(f"initialize() failed: {mt5.last_error()}")
    sys.exit(1)

login = int(os.environ["MT5_LOGIN"])
pwd = os.environ["MT5_PASSWORD"]
server = os.environ["MT5_SERVER"]
if not mt5.login(login, password=pwd, server=server):
    print(f"login() failed: {mt5.last_error()}")
    mt5.shutdown()
    sys.exit(1)

acct = mt5.account_info()
print(f"Connected: #{acct.login} | {acct.server} | balance={acct.balance} {acct.currency}")
print(f"Trade allowed (account): {bool(acct.trade_allowed)}  Margin mode: {acct.margin_mode}")
print()

symbols = mt5.symbols_get() or []
print(f"Total broker symbols: {len(symbols)}")

matching = [s for s in symbols if "XAU" in s.name.upper() or "GOLD" in s.name.upper()]
print(f"Gold-related symbols: {len(matching)}")
print()

if not matching:
    print("⚠️ No XAU/GOLD symbol found on this broker. Account profile likely excludes metals.")
    mt5.shutdown()
    sys.exit(0)

print(f"{'NAME':<20} {'VISIBLE':<8} {'TRADE_MODE':<12} {'VOL_MIN':<8} {'VOL_MAX':<8} {'PATH'}")
print("-" * 100)
for s in matching:
    info = mt5.symbol_info(s.name)
    tm = TRADE_MODE.get(getattr(info, "trade_mode", -1), str(getattr(info, "trade_mode", "?")))
    vmin = getattr(info, "volume_min", "?")
    vmax = getattr(info, "volume_max", "?")
    visible = bool(getattr(info, "visible", False))
    path = getattr(info, "path", "?")
    print(f"{s.name:<20} {str(visible):<8} {tm:<12} {str(vmin):<8} {str(vmax):<8} {path}")

# Try forcing XAUUSD into Market Watch
print()
for name in ["XAUUSD"] + [s.name for s in matching if s.name != "XAUUSD"][:3]:
    sel = mt5.symbol_select(name, True)
    info = mt5.symbol_info(name)
    if info:
        tm = TRADE_MODE.get(info.trade_mode, info.trade_mode)
        print(f"symbol_select({name!r}) → {sel}; trade_mode={tm}; tick={mt5.symbol_info_tick(name)}")
    else:
        print(f"symbol_select({name!r}) → {sel}; no symbol_info (not on broker)")

mt5.shutdown()
