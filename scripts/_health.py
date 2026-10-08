"""Agent health: audit stream stats since last BOT_START + error scan in bot.log."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

audit = Path("storage/logs/audit.jsonl")
lines = audit.read_text(encoding="utf-8", errors="replace").splitlines()

# find last BOT_START
start_idx = max(i for i, l in enumerate(lines) if '"BOT_START"' in l)
start = json.loads(lines[start_idx])
print(f"last BOT_START: {start['ts']}  session={start['data'].get('session_id')} pid={start['data'].get('pid')}")

since = []
for l in lines[start_idx + 1:]:
    try:
        since.append(json.loads(l))
    except json.JSONDecodeError:
        continue

ev = Counter(r.get("event") for r in since)
print(f"\nevents since restart: {len(since)}")
for k, v in ev.most_common(15):
    print(f"  {k:<26}{v:>6}")

# scan cadence: per-minute SCAN_START in last hour
now = datetime.now(timezone.utc)
recent = [r for r in since if r.get("event") == "SCAN_START"
          and (now - datetime.fromisoformat(r["ts"])).total_seconds() < 3600]
mins = Counter(r["ts"][11:16] for r in recent)
print(f"\nSCAN_START last 60min: {len(recent)} (expect ~60 rounds x 5 symbols=300)")
per_min = Counter(mins.values())
print(f"symbols-per-minute distribution: {dict(sorted(per_min.items()))}")

# errors / anomalies
errs = [r for r in since if r.get("event") in
        ("ORDER_FAILED", "TRADE_REGISTRATION_FAILED", "VOLUME_CALC_FAILED") 
        or "ERROR" in str(r.get("event", "")).upper()]
print(f"\nerror events: {len(errs)}")
for r in errs[:5]:
    print("  ", r["ts"][11:19], r["event"], str(r.get("data", {}))[:90])

# dry-run trades adopted under breakout
dr = [r for r in since if r.get("event") in ("DRY_RUN_TRADE", "DRY_RUN_POSITION_OPEN",
                                             "DRY_RUN_TP1", "DRY_RUN_SL_HIT", "DRY_RUN_BE",
                                             "DRY_RUN_TP2", "DRY_RUN_TP_FINAL")]
print(f"\ndry-run lifecycle events: {len(dr)}")
for r in dr[:8]:
    print("  ", r["ts"][11:19], r["event"], str(r.get("data", {}))[:100])

# skip reasons today (what gates are doing)
skips = Counter((r.get("data") or {}).get("reason", "?") for r in since
                if r.get("event") == "SCAN_SKIP")
print(f"\nSCAN_SKIP reasons (top 6):")
for k, v in skips.most_common(6):
    print(f"  {k:<32}{v:>6}")

# bot.log errors
blog = Path("storage/logs/bot.log")
if blog.exists():
    txt = blog.read_text(encoding="utf-8", errors="replace").splitlines()
    err_lines = [l for l in txt[-2000:] if "ERROR" in l or "CRITICAL" in l or "Traceback" in l]
    print(f"\nbot.log: {len(txt)} lines total, {len(err_lines)} error lines in last 2000")
    for l in err_lines[-5:]:
        print("  ", l[:130])
else:
    print("\nbot.log: not found")
