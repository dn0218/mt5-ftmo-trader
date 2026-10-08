"""B3: is the 3-40% LLM allow-rate swing internal (bug/cache) or external?

Approach: cluster LLM_REFUSE justifications per day. If refusal reasons
shift with market regime (e.g. 'choppy/range' vs 'countertrend' vs
'no structure'), the swing tracks the market, not a code defect.
Also: cache-hit share per day (cache replays OLD verdicts by design) and
UNAVAILABLE/timeout counts (API degradation).
"""
import json, re
from pathlib import Path
from collections import Counter, defaultdict

BUCKETS = [
    ("counter_trend", r"countertrend|counter-trend|against.*trend|opposed|htf|higher.?timeframe"),
    ("no_structure",  r"no bos|no choch|no structure|no sweep|weak sweep|no strong"),
    ("range_chop",    r"range|choppy|consolidat|sideways|low momentum|thin"),
    ("risk_reward",   r"rr|risk.?reward|reward.*risk|poor ratio"),
    ("spread_timing", r"spread|news|timing|session"),
]

rows = []
from scripts.audit_reader import iter_events, parse_since

_since = parse_since(sys.argv) or "2026-09-14"
for r in iter_events(since=_since):
        if '"LLM_REFUSE' not in json.dumps(r):
            continue
        ts = r.get("ts", "")
        if not ("2026-09-14" <= ts[:10] <= "2026-09-22"):
            continue
        d = r.get("data") or {}
        just = (d.get("reason") or "").lower()
        bucket = next((name for name, pat in BUCKETS if re.search(pat, just)), "other")
        rows.append((ts[:10], bucket, d.get("symbol"), just[:90]))

per_day = defaultdict(Counter)
for day, b, _, _ in rows:
    per_day[day][b] += 1

print("=== LLM_REFUSE justification buckets per day ===")
days = sorted(per_day)
print(f"{'day':<12}" + "".join(f"{n:>16}" for n, _ in BUCKETS) + f"{'other':>8}")
for day in days:
    c = per_day[day]
    print(f"{day:<12}" + "".join(f"{c.get(n,0):>16}" for n, _ in BUCKETS) + f"{c.get('other',0):>8}")

print("\nsamples of 'other':")
for day, b, sym, just in [r for r in rows if r[1] == "other"][:8]:
    print(f"  {day} {sym:<9} {just}")
