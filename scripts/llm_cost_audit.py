"""Exact real-API LLM call counts per day (cached=false only) vs clean baseline."""
import json
from pathlib import Path
from collections import Counter

real_per_day = Counter()
cached_per_day = Counter()
for line in Path("storage/logs/audit.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    if r.get("event") != "LLM_CALLED":
        continue
    d = r.get("data") or {}
    day = r.get("ts", "")[:10]
    if d.get("cached"):
        cached_per_day[day] += 1
    else:
        real_per_day[day] += 1

clean_days = [d for d in real_per_day if d < "2026-09-23"]
base = sum(real_per_day[d] for d in clean_days) / max(1, len(clean_days))
print(f"real API calls/day (clean baseline, avg of {len(clean_days)} days): {base:.0f}")
print(f"{'day':<12}{'real':>7}{'cached':>8}{'extra':>8}")
for day in sorted(real_per_day):
    extra = max(0, int(real_per_day[day] - base))
    print(f"{day:<12}{real_per_day[day]:>7}{cached_per_day[day]:>8}{extra:>8}")
tot_extra = max(0, int(real_per_day.get('2026-09-23', 0) - base))
print(f"\n9/23 extra real calls vs baseline: {tot_extra}")
print("glm-5.3 pricing: check zhipu console; e.g. ¥0.1/10k tokens x ~2k tokens/call")
