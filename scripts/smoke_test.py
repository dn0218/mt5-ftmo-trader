"""Smoke test — fetch real MT5 data and run all detectors."""

from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    import MetaTrader5 as mt5
    from core.mt5.data import MT5Client
    from core.mt5.reconnect import ensure_connected
    from core.detection.structure import detect_swing_points, detect_bos, detect_choch
    from core.detection.liquidity import detect_sweeps
    from core.detection.orderblocks import detect_orderblocks
    from core.detection.fvg import detect_fvg
    from core.detection.regime import detect_regime
    from core.indicators.indicators import atr, add_all_indicators
    from services.notifications.telegram import notify

    if not ensure_connected(max_attempts=3):
        print("❌ Cannot connect to MT5")
        return 1

    symbol = "EURUSD"
    df = MT5Client.get_bars(symbol, mt5.TIMEFRAME_M15, 200)
    if df is None:
        print(f"❌ No data for {symbol}")
        return 1

    print(f"✅ Got {len(df)} bars for {symbol}")

    df = add_all_indicators(df)
    atr_series = df["atr14"]

    swings = detect_swing_points(df)
    bos = detect_bos(swings)
    choch = detect_choch(swings)
    sweeps = detect_sweeps(df, swings, atr_series)
    obs = detect_orderblocks(df, atr_series)
    fvgs = detect_fvg(df, atr_series)
    regime = detect_regime(df)

    print(f"  Swing points: {len(swings)}")
    print(f"  BOS: {len(bos)}")
    print(f"  CHOCH: {len(choch)}")
    print(f"  Sweeps: {len(sweeps)} (strong: {sum(1 for s in sweeps if s.get('strong'))})")
    print(f"  Order blocks: {len(obs)}")
    print(f"  FVGs: {len(fvgs)}")
    print(f"  Regime: {regime}")

    # Basic assertions
    assert len(swings) > 0, "Should find at least 1 swing point"
    sh = sum(1 for s in swings if s["type"] == "SWING_HIGH")
    sl = sum(1 for s in swings if s["type"] == "SWING_LOW")
    assert sh > 0, "Should find at least 1 swing high"
    assert sl > 0, "Should find at least 1 swing low"

    print("\n✅ SMOKE TEST PASSED")
    notify("🧪 Smoke test passed — all detectors working on live data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
