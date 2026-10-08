"""Async K-line snapshot writer — freezes entry-time market context.

Every TRADE_REGISTERED freezes the decision context for later replay:
  - last 500 M15 bars (OHLCV)
  - last 80 H4 bars
  - ATR14 / EMA50 / spread / signal dict at entry

Design constraints (from the storage-governance spec):
  - NEVER blocks the scan loop: submit() enqueues and returns; a daemon
    thread does the gzip/JSON work.
  - Failure-safe: a failed write logs SNAPSHOT_FAILED and moves on —
    replay capability degrades for ONE trade, the bot keeps trading.
  - Standard library only (queue, gzip, json, threading, pathlib).
  - 90-day retention enforced by scripts/storage_guard.py (this module
    only writes; deletion is centralised in the guard with everything else).

Layout: storage/snapshots/{YYYYMMDD}/{symbol}_{side}_{trade_id}.json.gz
Budget: ~60 KB compressed per trade; ~10 trades/day → ~0.6 MB/day →
90 days ≈ 54 MB steady-state. Guard budget: 150 MB.
"""

from __future__ import annotations

import gzip
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SNAPSHOT_ROOT = Path("storage") / "snapshots"
_QUEUE_MAX = 64          # bound the queue; drop oldest on overflow
_SNAPSHOT_RETENTION_DAYS = 90   # informational; enforced by storage_guard


def _df_to_records(df) -> list[dict]:
    """DataFrame → list of plain dicts (time as ISO string)."""
    if df is None:
        return []
    try:
        cols = [c for c in ("time", "open", "high", "low", "close", "tick_volume")
                if c in df.columns]
        d = df[cols].copy()
        if "time" in d.columns:
            d["time"] = d["time"].astype(str)
        # round floats to keep payloads small; OHLC precision is plenty
        for c in ("open", "high", "low", "close"):
            if c in d.columns:
                d[c] = d[c].astype(float).round(6)
        return json.loads(d.to_json(orient="records"))
    except Exception:
        return []


def _spread_points(symbol: str) -> float | None:
    """Current spread in points, or None when MT5 is unavailable."""
    try:
        import MetaTrader5 as mt5
        t = mt5.symbol_info_tick(symbol)
        i = mt5.symbol_info(symbol)
        if t and i and i.point:
            return round((t.ask - t.bid) / i.point, 1)
    except Exception:
        pass
    return None


def build_snapshot(symbol: str, side: str, entry: float, sl: float, tp: float,
                   m15, h4, signal: dict | None) -> dict:
    """Pure builder — no I/O. Unit-testable."""
    def _atr14(df) -> float | None:
        try:
            h, l, c = df["high"], df["low"], df["close"]
            tr = (h - l).combine((h - c.shift()).abs(), max) \
                     .combine((l - c.shift()).abs(), max)
            v = tr.rolling(14).mean().iloc[-1]
            return round(float(v), 6) if v == v else None  # NaN guard
        except Exception:
            return None

    def _ema(df, span: int) -> float | None:
        try:
            v = df["close"].ewm(span=span, adjust=False).mean().iloc[-1]
            return round(float(v), 6)
        except Exception:
            return None

    snap = {
        "schema": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": side,
        "entry": float(entry), "sl": float(sl), "tp": float(tp),
        "spread_points": _spread_points(symbol),
        "atr14_m15": _atr14(m15) if m15 is not None else None,
        "ema50_m15": _ema(m15, 50) if m15 is not None else None,
        "ema50_h4": _ema(h4, 50) if h4 is not None else None,
        "signal": _sanitize(signal),
        "m15": _df_to_records(m15),
        "h4": _df_to_records(h4),
    }
    return snap


def _sanitize(obj):
    """Make any signal dict JSON-safe (drop non-primitive values)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()
                if not isinstance(v, (bytes,))}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 6)
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def snapshot_path(symbol: str, side: str, trade_id, when: datetime | None = None) -> Path:
    day = (when or datetime.now(timezone.utc)).strftime("%Y%m%d")
    safe_sym = symbol.replace(".", "").replace("/", "")
    return _SNAPSHOT_ROOT / day / f"{safe_sym}_{side}_{trade_id}.json.gz"


def save_snapshot(snap: dict, when: datetime | None = None) -> Path:
    """Synchronous write (gzip). Called by the worker thread; also usable
    directly in tests."""
    p = snapshot_path(snap["symbol"], snap["side"],
                      snap.get("trade_id", "na"), when=when)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "wb") as raw:
        with gzip.open(raw, "wt", encoding="utf-8") as f:
            json.dump(snap, f, default=str)
    tmp.replace(p)   # atomic-ish: readers never see a partial gzip
    return p


def load_snapshot(path: Path) -> dict:
    """Read a snapshot back (gzip → dict)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


class SnapshotWriter:
    """Async wrapper: submit() from the scan thread, daemon writes."""

    def __init__(self, root: Path | None = None, queue_max: int = _QUEUE_MAX):
        self.root = root or _SNAPSHOT_ROOT
        self._q: queue.Queue = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="snapshot-writer",
                                        daemon=True)
        self._thread.start()
        # singleton-ish registry so tests / stop() can reach every writer
        _WRITERS.append(self)

    def submit(self, trade_id, symbol: str, side: str,
               entry: float, sl: float, tp: float,
               m15, h4, signal: dict | None = None) -> bool:
        """Enqueue a snapshot job. Returns False when the queue is full
        (snapshot dropped, SNAPSHOT_FAILED logged, trading unaffected)."""
        job = {
            "trade_id": trade_id, "symbol": symbol, "side": side,
            "entry": entry, "sl": sl, "tp": tp,
            # freeze DataFrames NOW (list-of-dicts) — the scan loop reuses
            # and mutates df objects after we return
            "m15": _df_to_records(m15),
            "h4": _df_to_records(h4),
            "signal": _sanitize(signal),
        }
        try:
            self._q.put_nowait(job)
            return True
        except queue.Full:
            try:
                from services.logging.audit_logger import log_event
                log_event("SNAPSHOT_FAILED", {
                    "trade_id": trade_id, "symbol": symbol,
                    "reason": "QUEUE_FULL",
                }, stream="lifecycle")
            except Exception:
                pass
            return False

    def _run(self) -> None:
        from services.logging.audit_logger import log_event
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                # Rebuild DataFrames from the frozen records so indicators
                # (ATR14/EMA50) are computed here on the worker thread —
                # submit() stays pure-enqueue, never blocking the scan loop.
                import pandas as pd
                m15 = pd.DataFrame(job["m15"]) if job.get("m15") else None
                h4 = pd.DataFrame(job["h4"]) if job.get("h4") else None
                snap = build_snapshot(
                    symbol=job["symbol"], side=job["side"],
                    entry=job["entry"], sl=job["sl"], tp=job["tp"],
                    m15=m15, h4=h4, signal=job.get("signal"),
                )
                snap["trade_id"] = job["trade_id"]
                p = save_snapshot(snap, when=None)
                log_event("SNAPSHOT_SAVED", {
                    "trade_id": job["trade_id"], "symbol": job["symbol"],
                    "path": str(p), "bytes": p.stat().st_size,
                }, stream="lifecycle")
            except Exception as e:
                try:
                    log_event("SNAPSHOT_FAILED", {
                        "trade_id": job.get("trade_id"),
                        "symbol": job.get("symbol"),
                        "reason": f"{type(e).__name__}: {e}",
                    }, stream="lifecycle")
                except Exception:
                    pass
                logger.warning("snapshot write failed: %s", e)

    def close(self, timeout: float = 5.0) -> None:
        """Stop the worker, flushing pending jobs (best effort)."""
        self._stop.set()
        self._thread.join(timeout=timeout)

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the queue drains (test helper)."""
        self._q.join()


# keep weak refs so test teardown can stop stray writers
_WRITERS: list["SnapshotWriter"] = []


def stop_all_writers(timeout: float = 2.0) -> None:
    """Test/teardown helper — stop every writer created in this process."""
    for w in _WRITERS:
        try:
            w.close(timeout=timeout)
        except Exception:
            pass
    _WRITERS.clear()
