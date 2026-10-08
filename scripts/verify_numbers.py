"""verify_numbers.py — full-chain verification of critical trading numbers.

Any key number entering analysis must trace back to raw MT5 data through an
independent, reproducible chain. Three sections:
  1. Live pip conversion chain (source of truth: MT5 raw)
  2. Regression: 9/21 XAU SL_TOO_WIDE events under corrected conversion
  3. Symbol normalisation coverage

Testability: section 1 is split into data collection (conversion_rows,
MT5-dependent) and rendering (format_table, pure). tests/test_verify_output.py
locks the arithmetic MIN_$ == MIN_pips x pip and the 4-decimal display format
with synthetic rows — display-layer regressions can no longer hide.
"""
import sys, os, re, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
from collections import Counter
from dataclasses import dataclass

from core.mt5.symbols import pip_size, normalise_symbol

# NOTE: load_dotenv lives INSIDE main(), not at module import — importing
# this module from tests must never mutate os.environ (it once broke respx
# mocks by injecting OPENAI_BASE_URL from .env during test collection).


@dataclass
class ConversionRow:
    symbol: str
    digits: int
    point: float
    pip: float
    rule: str
    min_pips: float
    max_pips: float


def conversion_rows(mt5, symbols=None) -> list[ConversionRow]:
    """Collect one ConversionRow per symbol from live MT5 data."""
    symbols = symbols or [s.strip() for s in os.getenv("SYMBOLS", "EURUSD.,XAUUSD.").split(",") if s.strip()]
    rows = []
    for sym in symbols:
        info = mt5.symbol_info(sym)
        if not info:
            continue
        p = pip_size(sym, info)
        rule = "x10" if p == info.point * 10 else "raw"
        key = normalise_symbol(sym).replace(".", "").replace("_", "")
        mn = float(os.getenv(f"MIN_SL_PIPS_{key}", os.getenv("MIN_SL_PIPS", "15")))
        mx = float(os.getenv(f"MAX_SL_PIPS_{key}", os.getenv("MAX_SL_PIPS", "150")))
        rows.append(ConversionRow(sym, info.digits, info.point, p, rule, mn, mx))
    return rows


def format_table(rows: list[ConversionRow]) -> str:
    """Render the chain table. MIN_$/MAX_$ MUST equal pips x pip exactly —
    the display is asserted in tests, not just the arithmetic."""
    out = (f"{'symbol':<10}{'digits':>6}{'point':>10}{'pip':>10}{'rule':>5}"
           f"{'MIN_pips':>9}{'MIN_$':>9}{'MAX_pips':>9}{'MAX_$':>9}\n")
    for r in rows:
        out += (f"{r.symbol:<10}{r.digits:>6}{r.point:>10.5f}{r.pip:>10.5f}{r.rule:>5}"
                f"{r.min_pips:>9.0f}{r.min_pips*r.pip:>9.4f}"
                f"{r.max_pips:>9.0f}{r.max_pips*r.pip:>9.4f}\n")
    return out


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    print("=== 1. Live pip conversion chain (source of truth: MT5 raw) ===")
    import MetaTrader5 as mt5
    mt5.initialize()
    mt5.login(int(os.getenv("MT5_LOGIN", "0")), password=os.getenv("MT5_PASSWORD", ""),
              server=os.getenv("MT5_SERVER", ""))
    print(format_table(conversion_rows(mt5)), end="")
    mt5.shutdown()

    print("\n=== 2. Regression: 9/21 SL_TOO_WIDE events under CORRECT conversion ===")
    sl_re = re.compile(r"SL_TOO_(TIGHT|WIDE) \(([\d.]+) pips")
    audit = Path(os.path.join(os.path.dirname(__file__), "..", "storage", "logs", "audit.jsonl"))
    old_events = []
    for line in audit.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") != "SLTP_INVALID" or not r["ts"].startswith("2026-09-21"):
            continue
        m = sl_re.search(str(r.get("data", {}).get("reason", "")))
        if m and r["data"].get("symbol") == "XAUUSD.":
            old_events.append(float(m.group(2)))
    verdicts = Counter()
    for pips_old in old_events:
        new_pips = pips_old / 10   # old XAU pip was $0.01, correct is $0.10
        if new_pips < 60:    verdicts["would_fail_TIGHT (<60)"] += 1
        elif new_pips > 300: verdicts["would_fail_WIDE (>300)"] += 1
        else:                verdicts["PASS under fixed window"] += 1
    print(f"  XAU SL_TOO_WIDE events 9/21 (pre-dedup): {len(old_events)}")
    for k, v in verdicts.items():
        print(f"  {k}: {v}")

    print("\n=== 3. Symbol normalisation coverage ===")
    for raw in ["EURUSD.", "EURUSD.r", "XAUUSD.pro", "GBPUSD_raw", "US30", "BTCUSD"]:
        n = normalise_symbol(raw).replace(".", "").replace("_", "")
        pip_rule = "x10" if n in {"EURUSD","GBPUSD","AUDUSD","NZDUSD","USDCAD","USDCHF","USDJPY","XAUUSD","XAGUSD"} else "raw"
        print(f"  {raw:<12} -> {n:<8} pip rule: {pip_rule}")


if __name__ == "__main__":
    main()
