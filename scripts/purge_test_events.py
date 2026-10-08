"""One-shot purge of synthetic test events from the production audit trail.

Fingerprints (all established empirically):
  - ticket == 99                      (mock order fill)
  - bare symbol names                 (real bot uses 'EURUSD.' with suffix)
  - constant dd_pct 2.1 / 6.5         (mock risk events)
Prints what it removes; writes audit.jsonl.clean then replaces the original
(backup kept as audit.jsonl.bak).
"""
import json, shutil
from pathlib import Path

p = Path("storage/logs/audit.jsonl")
bak = p.with_suffix(".jsonl.bak")
lines = p.read_text(encoding="utf-8", errors="replace").splitlines()

kept, dropped = [], 0
for line in lines:
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        kept.append(line)
        continue
    d = r.get("data", {}) or {}
    sym = str(d.get("symbol", ""))
    synthetic = (
        d.get("ticket") == 99
        or sym in ("EURUSD", "USDJPY", "GBPUSD", "XAUUSD", "AUDUSD")  # bare = test
        or (r.get("event", "").endswith("DD_HALT") and d.get("dd_pct") in (2.1, 6.5))
    )
    if synthetic:
        dropped += 1
    else:
        kept.append(line)

shutil.copy2(p, bak)
p.write_text("\n".join(kept) + "\n", encoding="utf-8")
print(f"kept {len(kept)}, dropped {dropped}, backup -> {bak}")
