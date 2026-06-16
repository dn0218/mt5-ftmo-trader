"""One-shot diagnostic: pull deal history + M15 bars for a ticket.

Usage: python scripts/inspect_trade.py 8495157158
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv()

import MetaTrader5 as mt5
from core.mt5.reconnect import ensure_connected

REASON = {0: "MANUAL_CLIENT", 1: "MANUAL_MOBILE", 2: "MANUAL_WEB",
          3: "EA", 4: "SL", 5: "TP", 6: "STOP_OUT"}


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/inspect_trade.py <ticket>")
        return
    ticket = int(sys.argv[1])

    if not ensure_connected(max_attempts=5):
        print("Failed to connect to MT5")
        return

    print(f"\n=== Deal history for position {ticket} ===")
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        deals = mt5.history_deals_get(
            datetime.now(timezone.utc) - timedelta(days=30),
            datetime.now(timezone.utc),
        )
        deals = [d for d in (deals or []) if getattr(d, "position_id", None) == ticket]
    if not deals:
        print("  (no deals found)")
        return

    open_deal = close_deal = None
    for d in deals:
        ts = datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat()
        kind = "OPEN" if d.entry == 0 else "CLOSE" if d.entry == 1 else f"E{d.entry}"
        print(f"  {ts}  {kind:6s}  vol={d.volume}  price={d.price}  "
              f"profit={d.profit:+.2f}  reason={REASON.get(d.reason, d.reason)}")
        if d.entry == 0 and open_deal is None:
            open_deal = d
        if d.entry == 1:
            close_deal = d if close_deal is None else close_deal
            close_deal = d  # last CLOSE wins

    if not (open_deal and close_deal):
        print("\nMissing open/close deal — can't compute price travel.")
        return

    symbol = open_deal.symbol
    open_ts = datetime.fromtimestamp(open_deal.time, tz=timezone.utc)
    close_ts = datetime.fromtimestamp(close_deal.time, tz=timezone.utc)
    print(f"\nSymbol: {symbol}  side: {'BUY' if open_deal.type == 0 else 'SELL'}")
    print(f"Open:  {open_ts.isoformat()}  @ {open_deal.price}")
    print(f"Close: {close_ts.isoformat()}  @ {close_deal.price}  "
          f"reason={REASON.get(close_deal.reason, close_deal.reason)}  "
          f"P&L=${close_deal.profit:+.2f}")

    # Pull M1 bars over the trade lifetime to see real high/low travel.
    bars = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, open_ts, close_ts)
    if bars is None or len(bars) == 0:
        print("\nNo M1 bars over the lifetime.")
        return

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    high_max = max(highs)
    low_min = min(lows)
    print(f"\nDuring trade ({len(bars)} M1 bars):")
    print(f"  Highest price: {high_max}")
    print(f"  Lowest  price: {low_min}")
    print(f"  Travel from open: +{high_max - open_deal.price:.5f} / "
          f"{low_min - open_deal.price:.5f}")

    side = "BUY" if open_deal.type == 0 else "SELL"
    sl_dist = abs(close_deal.price - open_deal.price)
    sign = -1 if side == "SELL" else 1
    tp1 = open_deal.price + sign * sl_dist
    if side == "SELL":
        below_tp1 = [b for b in bars if b["low"] <= tp1]
    else:
        below_tp1 = [b for b in bars if b["high"] >= tp1]
    print(f"\nTP1 trigger price (1R): {tp1:.5f}")
    print(f"  M1 bars touching/crossing TP1: {len(below_tp1)} of {len(bars)}")
    if below_tp1:
        first = datetime.fromtimestamp(below_tp1[0]["time"], tz=timezone.utc)
        last = datetime.fromtimestamp(below_tp1[-1]["time"], tz=timezone.utc)
        print(f"  First crossed:  {first.isoformat()}")
        print(f"  Last  crossed:  {last.isoformat()}")
        print(f"  Time below TP1: ~{len(below_tp1)} minutes (M1 resolution)")


if __name__ == "__main__":
    main()
