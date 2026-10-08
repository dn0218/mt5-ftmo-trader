"""Merged audit reader — one call for all analysis scripts.

Reads BOTH audit shapes, oldest → newest:
  - legacy single file:  storage/logs/audit.jsonl (+ rotated audit_*.jsonl)
  - lifecycle stream:    storage/logs/audit_lifecycle.jsonl
                         + storage/audit_lifecycle/{YYYY-MM}.jsonl.gz

Backward compatible: when the new files don't exist (pre-migration data,
fresh clone), it silently reads whatever IS there. ``--since YYYY-MM-DD``
style filtering via ``since=`` (inclusive, compares ISO ts prefix).

Standard library only.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "storage" / "logs"
LIFECYCLE_DIR = ROOT / "storage" / "audit_lifecycle"


def _read_jsonl(path: Path, since: str | None, out: list) -> None:
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
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
        pass


def iter_events(since: str | None = None,
                streams: tuple[str, ...] = ("operations", "lifecycle"),
                root: Path | None = None) -> list[dict]:
    """Merged, chronologically ordered read of audit events.

    since:  inclusive ISO timestamp prefix (e.g. "2026-10-01"); events
            whose ts sorts before it are skipped.
    streams: which streams to include. ("operations",) reproduces the old
            single-file behaviour minus lifecycle events.
    """
    logs = (root or ROOT) / "storage" / "logs"
    lc_dir = (root or ROOT) / "storage" / "audit_lifecycle"
    out: list[dict] = []

    if "operations" in streams:
        # rotated archives first (oldest), then the live file
        for p in sorted(logs.glob("audit_2*.jsonl")):
            _read_jsonl(p, since, out)
        _read_jsonl(logs / "audit.jsonl", since, out)

    if "lifecycle" in streams:
        for p in sorted(lc_dir.glob("*.jsonl.gz")):
            _read_jsonl(p, since, out)
        _read_jsonl(logs / "audit_lifecycle.jsonl", since, out)

    out.sort(key=lambda r: r.get("ts", ""))
    return out


def parse_since(argv: list[str]) -> str | None:
    """Extract --since YYYY-MM-DD from a raw argv list (scripts glue)."""
    try:
        i = argv.index("--since")
        if i + 1 < len(argv):
            return argv[i + 1]
    except ValueError:
        pass
    return None


if __name__ == "__main__":
    import sys
    since = parse_since(sys.argv)
    events = iter_events(since=since)
    print(f"{len(events)} events (since={since})")
    for e in events[-10:]:
        print(f"{e.get('ts', '?')[:19]} {e.get('event', '?'):24} "
              f"[{e.get('stream', '?')}]")
