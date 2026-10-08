"""C3: end-to-end dry-run lifecycle event chain (fixture-simulated).

Asserts the FULL event sequence a real adopted signal must produce:
DRY_RUN_POSITION_OPEN -> DRY_RUN_TP1 -> DRY_RUN_BE -> DRY_RUN_TP2
-> DRY_RUN_SL_HIT(runner), with r_multiple present at every exit.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest

from services.trade_management.dry_run_tracker import DryRunTracker


@pytest.fixture
def events(monkeypatch):
    os.environ["AUDIT_SOURCE"] = "test"
    got = []
    import services.logging.audit_logger as al
    monkeypatch.setattr(al, "log_event", lambda ev, data=None, path=None: got.append((ev, data)))
    import services.trade_management.dry_run_tracker as mod
    mod.log_event = al.log_event
    return got


def test_full_chain_dry_run_lifecycle(events):
    t = DryRunTracker(mt5_mod=None)
    # XAU BUY: entry 100, SL 97 (3.0), TP1 103 (1R), TP2 106 (2R), final 112
    t.open_position("XAUUSD.", "BUY", 0.10, entry=100.0, sl=97.0,
                    tp_final=112.0, tp1=103.0, tp2=106.0,
                    score=4, confidence=0.82)
    t.tick("XAUUSD.", 103.5, 100.2)                        # TP1 + BE
    t.tick("XAUUSD.", 106.4, 103.8)                        # TP2
    t.tick("XAUUSD.", 109.0, 105.0, bar_close=108.5, atr=1.0)   # trail forms
    trail = t._open["XAUUSD."]["trail_sl"]
    assert trail is not None
    t.tick("XAUUSD.", 108.4, trail - 0.1)                  # runner stopped

    seq = [ev for ev, _ in events]
    for expected in ("DRY_RUN_POSITION_OPEN", "DRY_RUN_TP1", "DRY_RUN_BE",
                     "DRY_RUN_TP2", "DRY_RUN_SL_HIT"):
        assert expected in seq, f"missing {expected} in {seq}"
    # every exit event carries r_multiple
    for ev, d in events:
        if ev in ("DRY_RUN_SL_HIT", "DRY_RUN_TP_FINAL", "DRY_RUN_TIMEOUT_SNAPSHOT"):
            assert "r_multiple" in d
    sl = [d for ev, d in events if ev == "DRY_RUN_SL_HIT"][0]
    tp2_r, exit_r = 2.0, (trail - 100.0) / 3.0
    assert sl["r_multiple"] == pytest.approx(
        0.5 * 1 + 0.15 * tp2_r + 0.35 * exit_r, abs=1e-3)
    # OPEN carries the adoption context needed for outcome attribution
    op = [d for ev, d in events if ev == "DRY_RUN_POSITION_OPEN"][0]
    assert {"symbol", "side", "entry", "sl", "score", "confidence"} <= set(op)
