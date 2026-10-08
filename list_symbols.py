"""List broker symbols matching common trade names. Safe to delete."""
import sys
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

import MetaTrader5 as mt5
import os

if not mt5.initialize():
    print("MT5 init failed:", mt5.last_error()); sys.exit(1)

login = int(os.getenv("MT5_LOGIN", "0"))
if not mt5.login(login=login, password=os.getenv("MT5_PASSWORD", ""), server=os.getenv("MT5_SERVER", "")):
    print("login failed:", mt5.last_error()); sys.exit(1)

print("Account:", mt5.account_info().server)

all_syms = mt5.symbols_get()
print(f"Total symbols visible: {len(all_syms)}")

import re
patterns = ["EURUSD", "XAUUSD", "GOLD", "GBPUSD", "USDJPY"]
seen = set()
for s in all_syms:
    n = s.name.upper()
    for p in patterns:
        if p in n and n not in seen:
            seen.add(n)
            # show only tradable + first 40 matches
            print(f"  {s.name:30s} visible={s.visible} path={s.path}")
            break

# Check if exact EURUSD exists and has data
for probe in ["EURUSD", "EURUSD.i", "EURUSD.a", "EURUSD.pro", "XAUUSD", "XAUUSD.i", "GOLD"]:
    info = mt5.symbol_info(probe)
    if info:
        tick = mt5.symbol_info_tick(probe)
        print(f"\nEXISTS: {probe}  bid={getattr(tick,'bid',None)}  visible={info.visible}")
