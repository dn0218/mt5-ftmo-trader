"""Tests for the merged audit reader (task 4).

Old/new structure compatibility + --since time filtering.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gzip
import json
from pathlib import Path

import pytest

from scripts.audit_reader import iter_events, parse_since


def _line(ev, ts, stream="operations", extra=None):
    rec = {"ts": ts, "event": ev, "source": "runtime", "stream": stream}
    if extra:
        rec["data"] = extra
    return json.dumps(rec)


@pytest.fixture
def new_shape(tmp_path):
    """Both streams + archives present (post-migration layout)."""
    logs = tmp_path / "storage" / "logs"
    arc = tmp_path / "storage" / "audit_lifecycle"
    logs.mkdir(parents=True)
    arc.mkdir(parents=True)

    (logs / "audit.jsonl").write_text(
        _line("SCAN_START", "2026-10-05T07:00:00+00:00") + "\n" +
        _line("SCAN_SKIP", "2026-10-05T07:01:00+00:00") + "\n",
        encoding="utf-8")

    with gzip.open(arc / "2026-09.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write(_line("TRADE_REGISTERED", "2026-09-28T08:11:00+00:00",
                      stream="lifecycle") + "\n")
        f.write(_line("POSITION_CLOSED", "2026-09-28T09:52:00+00:00",
                      stream="lifecycle") + "\n")

    (logs / "audit_lifecycle.jsonl").write_text(
        _line("BOT_START", "2026-10-04T07:12:00+00:00", stream="lifecycle") + "\n",
        encoding="utf-8")
    return tmp_path


def _build_new(tmp_path):
    logs = tmp_path / "storage" / "logs"
    arc = tmp_path / "storage" / "audit_lifecycle"
    logs.mkdir(parents=True, exist_ok=True)
    arc.mkdir(parents=True, exist_ok=True)

    (logs / "audit.jsonl").write_text(
        _line("SCAN_START", "2026-10-05T07:00:00+00:00") + "\n" +
        _line("SCAN_SKIP", "2026-10-05T07:01:00+00:00") + "\n",
        encoding="utf-8")

    with gzip.open(arc / "2026-09.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write(_line("TRADE_REGISTERED", "2026-09-28T08:11:00+00:00",
                      stream="lifecycle") + "\n")
        f.write(_line("POSITION_CLOSED", "2026-09-28T09:52:00+00:00",
                      stream="lifecycle") + "\n")

    (logs / "audit_lifecycle.jsonl").write_text(
        _line("BOT_START", "2026-10-04T07:12:00+00:00", stream="lifecycle") + "\n",
        encoding="utf-8")
    return tmp_path


def _build_old(tmp_path):
    """Legacy layout: single audit.jsonl only."""
    logs = tmp_path / "storage" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "audit.jsonl").write_text(
        _line("SCAN_START", "2026-10-05T07:00:00+00:00") + "\n" +
        _line("TRADE_REGISTERED", "2026-10-04T08:11:00+00:00") + "\n",
        encoding="utf-8")
    return tmp_path


class TestMergedRead:
    def test_new_shape_merges_archive_live_and_streams(self, tmp_path):
        """New layout: gz archives + live lifecycle + ops all merged, sorted."""
        _build_new(tmp_path)
        evs = iter_events(root=tmp_path)
        names = [e["event"] for e in evs]
        assert names == [
            "TRADE_REGISTERED",   # 2026-09-28 (archive)
            "POSITION_CLOSED",    # 2026-09-28 (archive)
            "BOT_START",          # 2026-10-04 (live lifecycle)
            "SCAN_START",         # 2026-10-05 (ops)
            "SCAN_SKIP",          # 2026-10-05 (ops)
        ]

    def test_old_shape_fallback(self, tmp_path):
        """Legacy single-file layout still reads (backward compat)."""
        _build_old(tmp_path)
        evs = iter_events(root=tmp_path)
        assert [e["event"] for e in evs] == ["TRADE_REGISTERED", "SCAN_START"]

    def test_since_filter(self, tmp_path):
        """--since drops older events (inclusive boundary)."""
        _build_new(tmp_path)
        evs = iter_events(since="2026-10-01", root=tmp_path)
        assert [e["event"] for e in evs] == ["BOT_START", "SCAN_START", "SCAN_SKIP"]
        # boundary inclusive: exact ts match kept
        evs2 = iter_events(since="2026-09-28T08:11:00+00:00", root=tmp_path)
        assert evs2[0]["event"] == "TRADE_REGISTERED"

    def test_parse_since_argv(self):
        assert parse_since(["--since", "2026-10-01"]) == "2026-10-01"
        assert parse_since(["x", "--since"]) is None
        assert parse_since([]) is None
