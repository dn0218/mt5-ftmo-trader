"""Multi-instance contamination assessment (baseline-calibrated).

Infers live-instance count from SCAN_START density. A single instance emits
a roughly constant scan rate per hour; N concurrent instances emit ~N x that.
Baseline = per-day MEDIAN of that day's clean hourly counts, so the ratio is
robust to session-level activity differences (Asia vs NY ATR, kill zones).

Outputs:
  1. hour-by-hour instance count with contamination flags
  2. per-day summary (median baseline, max multiple, contaminated hours)
     -> which days' funnel ABSOLUTE counts are unreliable
  3. DRY_RUN_POSITION_OPEN per day (duplicated virtual positions)

Run: python scripts/assess_multiinstance.py   (after zombie cleanup)
"""
import json, os, sys
sys.path.insert(0, ".")
from pathlib import Path
from collections import Counter, defaultdict
from statistics import median

FLAG_THRESHOLD = 1.25   # ratio above this => 2+ instances in that hour

scan_per_hour = Counter()
llm_per_hour = Counter()
drt_per_day = Counter()
for line in Path("storage/logs/audit.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    ev, ts = r.get("event", ""), r.get("ts", "")
    if not ts:
        continue
    hour = ts[:13]
    if ev == "SCAN_START":
        scan_per_hour[hour] += 1
    elif ev == "LLM_CALLED":
        llm_per_hour[hour] += 1
    elif ev == "DRY_RUN_POSITION_OPEN":
        drt_per_day[ts[:10]] += 1

# --- leave-one-day-out GLOBAL baseline from full hours (>=100 scans) ---
# A day's own median fails when most of its hours are contaminated (9/23:
# 11/14 dirty hours dragged the median up -> false negative). The clean days
# dominate the leave-one-out pool, so the baseline stays ~300/h.
FULL_HOUR_MIN = 100   # exclude partial start/end hours (~5 scans)

def day_baseline(exclude_day):
    pool = [scan_per_hour[h] for h in scan_per_hour
            if h[:10] != exclude_day and scan_per_hour[h] >= FULL_HOUR_MIN]
    return median(pool) if pool else 0

first_contaminated = None
per_day_rows = []
print(f"{'hour (UTC)':<17}{'scans':>7}{'base':>6}{'xN':>5}{'LLM':>6}  flag")
for day in sorted({h[:10] for h in scan_per_hour}):
    base = day_baseline(day)
    day_hours = sorted(h for h in scan_per_hour if h[:10] == day)
    day_contaminated = 0
    day_max = 1.0
    for h in day_hours:
        n = scan_per_hour[h]
        ratio = n / base if base else 0
        inst = max(1, round(ratio))
        day_max = max(day_max, ratio)
        flag = ""
        if ratio > FLAG_THRESHOLD:
            flag = f"<-- x{ratio:.1f} MULTI-INSTANCE"
            day_contaminated += 1
            if first_contaminated is None:
                first_contaminated = h
        print(f"{h:<17}{n:>7}{int(base):>6}{inst:>5}{llm_per_hour[h]:>6}  {flag}")
    per_day_rows.append((day, int(base), day_max, day_contaminated, len(day_hours)))

print(f"\nfirst contaminated hour: {first_contaminated or 'none'}")
print(f"\n{'day':<12}{'base/h':>7}{'max xN':>8}{'dirty h':>9}{'/h':>5}  funnel absolutes")
for day, base, mx, dirty, tot in per_day_rows:
    verdict = "UNRELIABLE" if dirty else "ok"
    print(f"{day:<12}{base:>7}{mx:>7.1f}x{dirty:>8}/{tot:<4}  {verdict}")

print("\nDRY_RUN_POSITION_OPEN per day (multiplicity => duplicated virtual positions):")
for day in sorted(drt_per_day):
    print(f"  {day}: {drt_per_day[day]}")

print("""
notes:
  - RATIOS (precheck/scan, llm/precheck, ...) survive uniform amplification;
    ABSOLUTE counts in any flagged hour/day do not, and neither does anything
    guarded by per-instance in-memory dedup. For day-level funnel rows marked
    UNRELIABLE, recompute from unflagged hours only.
""")
