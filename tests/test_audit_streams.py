"""Tests for the dual-stream audit logger (task 2).

Dual-stream separation / monthly archival / gz read-back / default-stream
backward compat / lifecycle classification of edge events.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.logging import audit_logger as al


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    """Tests default the override OFF so stream routing is observable;
    individual tests may re-enable it via AUDIT_LOG_OVERRIDE."""
    monkeypatch.delenv("AUDIT_LOG_OVERRIDE", raising=False)
    monkeypatch.setattr(al, "_TEST_OVERRIDE", None)
    # isolate BOTH production paths into tmp
    tmp = Path(pytest.temp_dir) if hasattr(pytest, "temp_dir") else None
    yield


@pytest.fixture
def streams(tmp_path, monkeypatch):
    """Point both stream files + archive dir into tmp."""
    ops = tmp_path / "audit.jsonl"
    lc = tmp_path / "audit_lifecycle.jsonl"
    arc = tmp_path / "audit_lifecycle"
    monkeypatch.setattr(al, "_LOG_PATH", str(ops))
    monkeypatch.setattr(al, "_LIFECYCLE_PATH", str(lc))
    monkeypatch.setattr(al, "_LIFECYCLE_ARCHIVE_DIR", arc)
    monkeypatch.setattr(al, "_LAST_ARCHIVED_MONTH", None)
    monkeypatch.setattr(al, "_LAST_ROTATED_DAY", None)
    return {"ops": ops, "lc": lc, "arc": arc}


def _lines(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]


class TestDualStream:
    def test_streams_separate_files(self, streams):
        """ops events → audit.jsonl; lifecycle events → audit_lifecycle.jsonl."""
        al.log_event("SCAN_START", {"symbol": "EURUSD."})
        al.log_event("TRADE_REGISTERED", {"ticket": 1}, stream="lifecycle")
        al.log_event("SCAN_SKIP", {"symbol": "EURUSD."})

        ops = _lines(streams["ops"])
        lc = _lines(streams["lc"])
        assert [e["event"] for e in ops] == ["SCAN_START", "SCAN_SKIP"]
        assert [e["event"] for e in lc] == ["TRADE_REGISTERED"]
        assert all(e["stream"] == "operations" for e in ops)
        assert all(e["stream"] == "lifecycle" for e in lc)

    def test_default_is_operations_backward_compat(self, streams):
        """log_event(name, data) — the historical 2-arg call — must keep
        landing in the operations file (byte-compatible migration)."""
        al.log_event("SCAN_START", {"symbol": "EURUSD."})
        assert streams["ops"].exists()
        assert not streams["lc"].exists()   # nothing routed to lifecycle
        rec = _lines(streams["ops"])[0]
        assert rec["event"] == "SCAN_START"
        assert rec["stream"] == "operations"

    def test_critical_event_name_forces_lifecycle(self, streams):
        """Even without stream=, critical event NAMES route to lifecycle —
        a forgotten kwarg at a future call site can't break the permanent
        record."""
        al.log_event("POSITION_CLOSED", {"ticket": 5, "pnl": -51.29})
        al.log_event("BOT_START", {"pid": 1})
        al.log_event("STORAGE_CRITICAL", {"total_mb": 810})
        assert streams["ops"].exists() is False or not _lines(streams["ops"])
        names = [e["event"] for e in _lines(streams["lc"])]
        assert names == ["POSITION_CLOSED", "BOT_START", "STORAGE_CRITICAL"]

    def test_monthly_archive_gzip_and_readback(self, streams, monkeypatch):
        """Month rollover gzips last month's lifecycle stream; the merged
        reader returns archive + live events in order."""
        lc = streams["lc"]
        lc.parent.mkdir(parents=True, exist_ok=True)
        # simulate: last month's file exists with events, old mtime
        lc.write_text(
            json.dumps({"ts": "2026-09-15T00:00:00+00:00", "event": "BOT_START",
                        "source": "runtime", "stream": "lifecycle"}) + "\n",
            encoding="utf-8")
        import time as _t
        # mtime firmly in September 2026
        sep_ts = _t.mktime((2026, 9, 15, 12, 0, 0, 0, 0, -1))
        os.utime(lc, (sep_ts, sep_ts))

        # force the "today" the logger sees: October 1st → rollover detected
        import services.logging.audit_logger as mod
        real_dt = mod.datetime

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 1, 4, 0, 0, tzinfo=timezone.utc)

        mod.datetime = FakeDT
        try:
            al.log_event("BOT_STOP", {"pid": 2}, stream="lifecycle")
        finally:
            mod.datetime = real_dt

        # September chunk archived + gzipped, BOT_STOP in the fresh live file
        gz = streams["arc"] / "2026-09.jsonl.gz"
        assert gz.exists(), "monthly archive not created"
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            archived = [json.loads(l) for l in f if l.strip()]
        assert [e["event"] for e in archived] == ["BOT_START"]
        assert _lines(lc) and _lines(lc)[0]["event"] == "BOT_STOP"

        # merged reader over a PROPER storage/ layout mirroring the tmp
        # files above: logs/ holds the live lifecycle file, archive dir
        # holds the gz. (iter_events expects <root>/storage/{logs,...}.)
        from scripts.audit_reader import iter_events
        read_root = streams["arc"].parent.parent          # tmp_path
        fake_storage = read_root / "storage"
        (fake_storage / "logs").mkdir(parents=True, exist_ok=True)
        (fake_storage / "audit_lifecycle").mkdir(parents=True, exist_ok=True)
        shutil.copy2(lc, fake_storage / "logs" / "audit_lifecycle.jsonl")
        shutil.copy2(gz, fake_storage / "audit_lifecycle" / "2026-09.jsonl.gz")
        events = iter_events(root=read_root)
        ev_names = [e["event"] for e in events if e.get("stream") == "lifecycle"]
        assert ev_names == ["BOT_START", "BOT_STOP"]

    def test_override_isolates_both_streams(self, streams, tmp_path, monkeypatch):
        """AUDIT_LOG_OVERRIDE keeps its contract: BOTH streams land in the
        single override file, production files untouched."""
        override = tmp_path / "iso" / "audit.jsonl"
        monkeypatch.setattr(al, "_TEST_OVERRIDE", str(override))

        al.log_event("SCAN_START", {})
        al.log_event("TRADE_REGISTERED", {}, stream="lifecycle")

        assert override.exists()
        recs = _lines(override)
        assert [r["event"] for r in recs] == ["SCAN_START", "TRADE_REGISTERED"]
        # stream routing recorded in the payload, files untouched
        assert [r["stream"] for r in recs] == ["operations", "lifecycle"]
        assert not streams["ops"].exists()
        assert not streams["lc"].exists()
