"""Weekly BOT_START/BOT_STOP orphan-patrol.

Detects, from audit.jsonl alone (no live process checks):
  R1  BOT_START whose session_id has no BOT_STOP, followed by a new BOT_START
      while still unpaired  -> crash / hard-kill / multi-instance episode
  R2  two BOT_STARTs with different pid but same ppid -> bat/ps1 loop stacking
      (the exact 2026-09-23 failure signature)
  R3  duplicate BOT_STOP for one session_id -> double-write / exit-path bug

Also reports open sessions (START without STOP yet, still alive = normal).

Exit code 0 = clean, 1 = violations found (so it can gate CI / task scheduler).
Run: python scripts/patrol_orphans.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

AUDIT = Path("storage/logs/audit.jsonl")
WINDOW_DAYS = 7          # only look at the trailing week (old orphans age out)
cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).date()

starts = []          # (ts, session_id, pid, ppid)
stops = []           # (ts, session_id, pid)
for line in AUDIT.read_text(encoding="utf-8", errors="replace").splitlines():
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    ev, d = r.get("event"), r.get("data") or {}
    ts = r.get("ts", "")
    try:
        if datetime.fromisoformat(ts).date() < cutoff:
            continue
    except ValueError:
        pass
    if ev == "BOT_START":
        starts.append((ts, d.get("session_id") or d.get("pid"),
                       d.get("pid"), d.get("ppid")))
    elif ev == "BOT_STOP":
        stops.append((ts, d.get("session_id") or d.get("pid"),
                      d.get("pid")))

stop_keys = {s[1] for s in stops}
violations = []

# R3: duplicate STOP per session key
seen_stop = set()
for _, key, _pid in stops:
    if key in seen_stop:
        violations.append(("R3", f"duplicate BOT_STOP for session/pid {key}"))
    seen_stop.add(key)

# R1: new START while another session still unpaired (set-based: the real
# incident shape was ACCUMULATION - five STARTs, zero STOPs)
open_keys = {}       # key -> ts (unpaired yet)
for ts, key, pid, ppid in starts:
    if open_keys:
        violations.append(
            ("R1", f"{ts}: BOT_START (session {key}) while "
                   f"{len(open_keys)} session(s) unpaired: "
                   f"{sorted(map(str, open_keys))} -> crash/hard-kill/multi-instance"))
    open_keys[key] = ts
    if key in stop_keys:          # STOP already seen earlier in the file
        open_keys.pop(key, None)

# R2: same ppid spawning different pids (loop stacking)
from collections import defaultdict
ppid_kids = defaultdict(set)
for _, key, pid, ppid in starts:
    if pid is not None and ppid is not None:
        ppid_kids[ppid].add(pid)
for ppid, pids in ppid_kids.items():
    if len(pids) > 1:
        violations.append(
            ("R2", f"ppid {ppid} spawned multiple bot pids {sorted(pids)} "
                   f"-> wrapper loop stacking"))

open_sessions = [k for k in {s[1] for s in starts} if k not in stop_keys]

print(f"scanned {len(starts)} BOT_START / {len(stops)} BOT_STOP events")
print(f"open sessions (no STOP yet, may be alive): {open_sessions or 'none'}")
if violations:
    print(f"\n{len(violations)} VIOLATIONS:")
    for rule, msg in violations:
        print(f"  [{rule}] {msg}")
    sys.exit(1)
print("no R1/R2/R3 violations")
