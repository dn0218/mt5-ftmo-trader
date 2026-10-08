"""JSONL audit logger — one event per line, dual-stream.

Stream A "operations": the default. High-volume scan noise (SCAN_START,
SCAN_SKIP, SPREAD_TOO_HIGH, ...). Rotates daily when >2 MB, 30-day archive
retention. Budget ≤60 MB.

Stream B "lifecycle": trade-lifecycle events (TRADE_REGISTERED, SL/TP/BE,
DD_HALT, BOT_START/STOP, ...). Kept forever, compressed monthly into
storage/audit_lifecycle/{YYYY-MM}.jsonl.gz. Budget ≤50 MB per half year.

Backward compatibility:
  - log_event("X")                     → operations (unchanged default)
  - log_event("X", stream="lifecycle") → lifecycle stream
The AUDIT_LOG_OVERRIDE test-isolation mechanism is untouched: when the
override is active BOTH streams are redirected to the override file
(single file, "stream" field recorded in the event itself).
"""

from __future__ import annotations
import gzip
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOG_LOCK = threading.Lock()
_LOG_PATH = os.path.join("storage", "logs", "audit.jsonl")
_LIFECYCLE_PATH = os.path.join("storage", "logs", "audit_lifecycle.jsonl")
_LIFECYCLE_ARCHIVE_DIR = Path("storage") / "audit_lifecycle"

# Daily rotation for the operations stream (unchanged behaviour): at first
# write past local midnight, move the current audit.jsonl to
# audit_YYYYMMDD.jsonl and start fresh. Only the newest
# AUDIT_RETENTION_DAYS archives are kept (default 30).
_ROTATION_LOCK = threading.Lock()
_LAST_ROTATED_DAY: str | None = None

# Test isolation: pytest sets AUDIT_LOG_OVERRIDE in tests/conftest.py so
# synthetic events NEVER land in the production audit trail. (They once
# did — ticket=99 TRADE_ADOPTED phantoms from test runs corrupted funnel
# stats for three separate analysis rounds.)
_TEST_OVERRIDE = os.environ.get("AUDIT_LOG_OVERRIDE")

# Lifecycle archival state (monthly gzip). Guarded by _ROTATION_LOCK so a
# concurrent log_event can't double-archive the same month.
_LAST_ARCHIVED_MONTH: str | None = None

# Events that ALWAYS land in the lifecycle stream, whatever the caller says.
# Belt-and-braces: callers mark critical events explicitly, but if a new
# call site forgets, classification by event name keeps the permanent record
# complete. Test isolation (AUDIT_LOG_OVERRIDE) is NOT affected — override
# still wins and sends everything to the single test file.
_LIFECYCLE_EVENTS = {
    "TRADE_ADOPTED", "TRADE_REJECTED", "TRADE_REGISTERED", "TRADE_REGISTRATION_FAILED",
    "SL_HIT", "TP_HIT", "TP1", "TP2", "PARTIAL_CLOSE", "BREAKEVEN", "EOS_BE", "TRAIL",
    "POSITION_CLOSED", "ORDER_FILLED", "ORDER_FAILED", "EXIT_LOOKUP_FAILED",
    "DD_HALT", "DAILY_DD_HALT", "DAILY_DD_WOULD_BREACH", "HALTED_TODAY",
    "LOSS_RECORDED", "WIN_RECORDED",
    "BOT_START", "BOT_STOP", "BOT_RESTART",
    "STORAGE_STATUS", "STORAGE_CRITICAL", "STORAGE_FULL",
    "SNAPSHOT_SAVED", "SNAPSHOT_FAILED",
    "ENGINE_CRASH",
}


def _maybe_rotate(log_path: str) -> None:
    """Rotate once per calendar day if size > AUDIT_ROTATE_MIN_MB."""
    global _LAST_ROTATED_DAY
    try:
        today = datetime.now().strftime("%Y%m%d")
        if _LAST_ROTATED_DAY == today:
            return
        p = Path(log_path)
        if not p.exists() or p.stat().st_size < 1:
            _LAST_ROTATED_DAY = today
            return
        with _ROTATION_LOCK:
            if _LAST_ROTATED_DAY == today:
                return
            # rotate only when the file has meaningful size (>2 MB)
            if p.stat().st_size < 2 * 1024 * 1024:
                _LAST_ROTATED_DAY = today
                return
            archive = p.with_name(f"audit_{_LAST_ROTATED_DAY or today}.jsonl")
            p.replace(archive)
            # prune archives beyond retention
            keep = int(os.getenv("AUDIT_RETENTION_DAYS", "30"))
            olds = sorted(p.parent.glob("audit_2*.jsonl"))
            for old in olds[:-keep] if keep > 0 else olds:
                try:
                    old.unlink()
                except OSError:
                    pass
            _LAST_ROTATED_DAY = today
    except Exception:
        # rotation must NEVER break event logging
        pass


def _maybe_archive_lifecycle() -> None:
    """On the 1st of a new month, gzip last month's lifecycle stream.

    Failure-safe by design: every step is wrapped — if archival fails the
    plain .jsonl keeps growing (disk cost, not data loss) and the next
    write re-attempts. Never raises into the logging path.
    """
    global _LAST_ARCHIVED_MONTH
    try:
        now = datetime.now()
        this_month = now.strftime("%Y-%m")
        if _LAST_ARCHIVED_MONTH == this_month:
            return
        with _ROTATION_LOCK:
            if _LAST_ARCHIVED_MONTH == this_month:
                return
            p = Path(_LIFECYCLE_PATH)
            # Archive only when the month has rolled over AND there is data.
            if p.exists() and p.stat().st_size > 0:
                # Archive name = the month the events were written in.
                # mtime is a good enough proxy for "the month this chunk
                # belongs to" because the stream archives-and-empties on
                # every month boundary.
                file_month = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m")
                if file_month != this_month:
                    dest_dir = _LIFECYCLE_ARCHIVE_DIR
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / f"{file_month}.jsonl.gz"
                    # append-mode merge: if dest already exists (e.g. re-run
                    # after a crash between gzip and unlink), concatenate
                    # instead of overwriting — data preservation first.
                    with open(p, "rb") as src, gzip.open(dest, "at", encoding="utf-8") as out:
                        for line in src:
                            out.write(line.decode("utf-8", errors="replace"))
                    p.unlink()
            _LAST_ARCHIVED_MONTH = this_month
    except Exception:
        # archival must NEVER break event logging
        pass


def log_event(event_type: str, data: dict | None = None, path: str | None = None,
              stream: str = "operations") -> None:
    """Append a JSON event to the audit log.

    A ``stream`` field marks which trail the event belongs to. Default
    "operations" keeps every existing call site byte-identical in behaviour.

    A ``source`` field marks event origin: pytest sets AUDIT_SOURCE=test,
    one-off scripts should set AUDIT_SOURCE=script; anything else is
    production runtime. Future pollution becomes traceable and filterable
    regardless of whether the path override was in effect.
    """
    # Intended stream decides the file (and the recorded label) BEFORE any
    # override/path redirect, so test-isolation files still show which
    # stream the event belonged to.
    lifecycle = stream == "lifecycle" or event_type in _LIFECYCLE_EVENTS
    # Test override wins for BOTH streams (single file) — the test-isolation
    # contract is untouched. The "stream" field stays in the payload so
    # tests can still assert stream routing.
    log_path = path or _TEST_OVERRIDE or (
        _LIFECYCLE_PATH if lifecycle else _LOG_PATH
    )
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "source": os.environ.get("AUDIT_SOURCE", "runtime"),
        "stream": "lifecycle" if lifecycle else "operations",
    }
    if data:
        entry["data"] = data

    with _LOG_LOCK:
        if log_path == _LIFECYCLE_PATH:
            _maybe_archive_lifecycle()
        elif log_path == _LOG_PATH:
            _maybe_rotate(log_path)
        # path override: plain append, no rotation/archive side effects
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def log_decision_debug(data: dict) -> None:
    """Log LLM decision debug info."""
    debug_path = os.path.join("storage", "logs", "decision_debug.jsonl")
    log_event("LLM_DECISION", data, path=debug_path)


def read_lifecycle_events(since: str | None = None) -> list[dict]:
    """Helper for analysis scripts: merged lifecycle read.

    Reads the live lifecycle stream plus every monthly .jsonl.gz archive
    (oldest first). ``since`` is an inclusive ISO date (YYYY-MM-DD) filter
    on the event timestamp. Standard library only.
    """
    out: list[dict] = []
    sources: list[Path] = []
    if _LIFECYCLE_ARCHIVE_DIR.is_dir():
        sources.extend(sorted(_LIFECYCLE_ARCHIVE_DIR.glob("*.jsonl.gz")))
    live = Path(_LIFECYCLE_PATH)
    if live.exists():
        sources.append(live)
    for src in sources:
        try:
            opener = gzip.open if src.suffix == ".gz" else open
            with opener(src, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since and rec.get("ts", "") < since:
                        continue
                    out.append(rec)
        except OSError:
            continue
    return out
