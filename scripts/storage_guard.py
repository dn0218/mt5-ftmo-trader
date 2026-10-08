"""Storage guard — 1 GB hard-cap enforcement for storage/.

Daily (04:00 UTC via APScheduler in the engine, or manual:
``python scripts/storage_guard.py``).

Deletion policy (whitelist — anything not listed is NEVER touched):
  1. operations audit archives  storage/logs/audit_*.jsonl    > 30 days
  2. K-line snapshots           storage/snapshots/**          > 90 days
  3. lifecycle audit archives   storage/audit_lifecycle/*.gz  > 180 days
  4. nothing else. performance.json, trade_state.json, risk_state.json,
     bot.log (has its own TimedRotatingFileHandler) are untouchable.

Thresholds (on storage/ total):
  > 800 MB → delete per policy above, oldest first, until ≤ 800 MB
             (lifecycle archives only when > 180 days old)
  still > 800 MB → STOP deleting, STORAGE_CRITICAL + Telegram alert
  > 950 MB → new trades blocked: risk_manager reads storage_state.json
             (STORAGE_FULL flag), writes STORAGE_FULL event

Every run writes a STORAGE_STATUS lifecycle event with the size breakdown.
Failure-safe: any deletion error is swallowed and the file kept — prefer
keeping data over breaking the run. Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "storage"

WARN_BYTES = 800 * 1024 * 1024      # 800 MB — start pruning
CRITICAL_BYTES = 950 * 1024 * 1024  # 950 MB — block new trades
STATE_PATH = STORAGE / "db" / "storage_state.json"

# Whitelist of deletable patterns (relative to storage/). Nothing outside
# this list is ever removed, no matter how big storage/ gets.
OPS_ARCHIVE_GLOB = "logs/audit_*.jsonl"        # rotated operations stream
SNAPSHOT_DAYS = 90                              # K-line snapshots
LIFECYCLE_DAYS = 180                            # compressed lifecycle months
OPS_DAYS = 30                                   # operations archives


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _category_sizes() -> dict[str, int]:
    return {
        "operations_archives": _dir_size(STORAGE / "logs") ,
        "snapshots": _dir_size(STORAGE / "snapshots"),
        "lifecycle_archives": _dir_size(STORAGE / "audit_lifecycle"),
        "db": _dir_size(STORAGE / "db"),
        "other": 0,  # filled by caller after total is known
    }


def _age_days(p: Path, now: datetime) -> float:
    try:
        return (now - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)).days
    except OSError:
        return 0.0


def _delete(p: Path) -> bool:
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return True
    except OSError:
        return False


def prune_operations_archives(now: datetime, dry: bool = False) -> int:
    """Delete rotated operations archives older than OPS_DAYS. Returns bytes freed."""
    freed = 0
    for p in sorted((STORAGE / "logs").glob("audit_*.jsonl")):
        if _age_days(p, now) > OPS_DAYS:
            sz = p.stat().st_size if p.exists() else 0
            if dry or _delete(p):
                freed += 0 if dry else sz
    return freed


def prune_snapshots(now: datetime, dry: bool = False) -> int:
    """Delete snapshot DAY-directories older than SNAPSHOT_DAYS."""
    freed = 0
    root = STORAGE / "snapshots"
    if not root.is_dir():
        return 0
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # unknown naming — never guess, keep it
        if (now - day).days > SNAPSHOT_DAYS:
            sz = _dir_size(day_dir)
            if dry or _delete(day_dir):
                freed += 0 if dry else sz
    return freed


def prune_lifecycle_archives(now: datetime, dry: bool = False) -> int:
    """Delete monthly lifecycle gz archives older than LIFECYCLE_DAYS.

    This is the last-resort tier: lifecycle data is the permanent record,
    so it only goes when > 180 days old AND the 800 MB line is breached."""
    freed = 0
    root = STORAGE / "audit_lifecycle"
    if not root.is_dir():
        return 0
    for p in sorted(root.glob("*.jsonl.gz")):
        try:
            month = datetime.strptime(p.stem.split(".")[0], "%Y-%m").replace(
                day=28, tzinfo=timezone.utc)
        except ValueError:
            continue  # unknown naming — keep
        if (now - month).days > LIFECYCLE_DAYS:
            sz = p.stat().st_size if p.exists() else 0
            if dry or _delete(p):
                freed += 0 if dry else sz
    return freed


def read_storage_flag() -> dict:
    """risk_manager reads this to decide whether new trades are allowed."""
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"storage_full": False}


def write_storage_flag(full: bool, total_bytes: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "storage_full": full,
        "total_bytes": total_bytes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _notify(msg: str) -> None:
    try:
        sys.path.insert(0, str(ROOT))
        from services.notifications.telegram import notify
        notify(msg)
    except Exception:
        pass  # alert failure must never break the guard


def _audit(event: str, data: dict) -> None:
    try:
        sys.path.insert(0, str(ROOT))
        from services.logging.audit_logger import log_event
        log_event(event, data, stream="lifecycle")
    except Exception:
        pass


def run_guard(now: datetime | None = None, dry: bool = False) -> dict:
    """One guard pass. Returns a report dict (also used by tests)."""
    now = now or datetime.now(timezone.utc)
    report: dict = {
        "ts": now.isoformat(),
        "total_before": 0, "total_after": 0,
        "freed": {"operations": 0, "snapshots": 0, "lifecycle": 0},
        "critical": False, "storage_full": False,
        "categories": {},
    }

    total = _dir_size(STORAGE)
    report["total_before"] = total
    cats = _category_sizes()
    cats["other"] = max(0, total - sum(v for k, v in cats.items() if k != "other"))
    report["categories"] = {k: _mb(v) for k, v in cats.items()}
    report["total_mb"] = _mb(total)

    if total <= WARN_BYTES:
        report["storage_full"] = False
        write_storage_flag(False, total) if not dry else None
        _audit("STORAGE_STATUS", _status_payload(report))
        return report

    # ── > 800 MB: tiered pruning, oldest first ──
    report["freed"]["operations"] = prune_operations_archives(now, dry=dry)
    total = _dir_size(STORAGE)
    if total > WARN_BYTES:
        report["freed"]["snapshots"] = prune_snapshots(now, dry=dry)
    total = _dir_size(STORAGE)
    if total > WARN_BYTES:
        report["freed"]["lifecycle"] = prune_lifecycle_archives(now, dry=dry)
    total = _dir_size(STORAGE)
    report["total_after"] = total

    if total > WARN_BYTES:
        report["critical"] = True
        _audit("STORAGE_CRITICAL", {
            "total_mb": _mb(total), "limit_mb": 800,
            "message": "pruning exhausted whitelist; manual intervention needed",
        })
        _notify(f"🚨 <b>STORAGE CRITICAL</b>: storage/ at {_mb(total)} MB "
                f"after pruning — manual cleanup needed (cap 1 GB)")

    full = total > CRITICAL_BYTES
    report["storage_full"] = full
    if full:
        _audit("STORAGE_FULL", {
            "total_mb": _mb(total), "limit_mb": 950,
            "action": "new trades blocked until guard frees space",
        })
        _notify(f"⛔ <b>STORAGE FULL</b>: {_mb(total)} MB > 950 MB — "
                f"new trades blocked")
    if not dry:
        write_storage_flag(full, total)
    _audit("STORAGE_STATUS", _status_payload(report))
    return report


def _mb(b: int) -> float:
    return round(b / (1024 * 1024), 2)


def _status_payload(report: dict) -> dict:
    return {
        "total_mb": report.get("total_mb"),
        "categories_mb": report.get("categories"),
        "freed_mb": {k: _mb(v) for k, v in report.get("freed", {}).items()},
        "critical": report.get("critical", False),
        "storage_full": report.get("storage_full", False),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="storage/ capacity guard")
    ap.add_argument("--dry", action="store_true", help="report only, delete nothing")
    ap.add_argument("--json", action="store_true", help="print report as JSON")
    args = ap.parse_args()

    report = run_guard(dry=args.dry)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"storage/ total: {report['total_mb']} MB "
          f"(before {report['total_before']/(1024*1024):.1f} MB)")
    print("categories:")
    for k, v in report["categories"].items():
        print(f"  {k:<22} {v:>8} MB")
    if any(report["freed"].values()):
        print("freed:", {k: _mb(v) for k, v in report["freed"].items()})
    print(f"critical={report['critical']} storage_full={report['storage_full']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
