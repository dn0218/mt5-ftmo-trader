"""DryRunTracker lifecycle tests — verified line-by-line against manager.py:
BE at 0.5R, TP1 50%, TP2 30% of remainder, ratchet trail on the 20% runner,
SL-first on same-bar double touch, censored-timeout separation."""
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


def test_be_at_half_r_then_sl_is_scratch(events):
    """Manager: BE trigger is 0.5R — SL after BE must book 0.0R, not -1R."""
    t = DryRunTracker(mt5_mod=None)
    t.open_position("EURUSD.", "BUY", 0.1, entry=1.1000, sl=1.0950,
                    tp_final=1.1150, tp1=1.1050, tp2=1.1090, score=3, confidence=0.75)
    t.tick("EURUSD.", 1.1026, 1.0990)      # high = +0.52R -> BE triggers
    assert any(ev == "DRY_RUN_BE" for ev, _ in events)
    t.tick("EURUSD.", 1.1010, 1.0990)      # drifts back through entry (SL@BE)
    sl = [d for ev, d in events if ev == "DRY_RUN_SL_HIT"]
    assert sl and sl[0]["r_multiple"] == pytest.approx(0.0)


def test_full_ladder_r_matches_live_fractions(events):
    """TP1(50%) + TP2(30% of REMAINING=15% orig) + runner(35%):
    R = 0.5*1 + 0.15*r2 + 0.35*r_exit — mirrors manager._partial_close exactly."""
    t = DryRunTracker(mt5_mod=None)
    t.open_position("XAUUSD.", "BUY", 0.1, entry=100.0, sl=97.0,
                    tp_final=112.0, tp1=103.0, tp2=106.0, score=4, confidence=0.8)
    t.tick("XAUUSD.", 103.5, 100.2)                  # TP1 (+1R also >= 0.5R BE)
    t.tick("XAUUSD.", 106.4, 103.8)                  # TP2 (2R)
    t.tick("XAUUSD.", 109.0, 105.0, bar_close=108.5, atr=1.0)   # trail forms
    # force runner stop-out at the trailed level on a later bar
    pos = t._open["XAUUSD."]
    trail = pos["trail_sl"]
    assert trail == pytest.approx(108.5 - 1.5 * 1.0)  # TRAIL_ATR_MULT=1.5
    t.tick("XAUUSD.", 108.4, trail - 0.1)            # drops to trail
    sl = [d for ev, d in events if ev == "DRY_RUN_SL_HIT"]
    assert sl, "runner stop should fire"
    tp2_r = (106.0 - 100.0) / 3.0                    # 2.0R
    exit_r = (trail - 100.0) / 3.0
    # manager fractions: TP1=50% orig, TP2=30% of remaining (15% orig), runner=35%
    assert sl[0]["r_multiple"] == pytest.approx(0.5 * 1 + 0.15 * tp2_r + 0.35 * exit_r, abs=1e-3)


def test_path_c_tp1_then_be_return_books_half_r(events):
    """Path C: TP1 (+50% at 1R) then price returns through BE stop.
    Remaining 50% exits at entry -> total R = 0.5*1 + 0.5*0 = +0.5."""
    t = DryRunTracker(mt5_mod=None)
    t.open_position("XAUUSD.", "SELL", 0.1, entry=100.0, sl=102.0,
                    tp_final=94.0, tp1=98.0, tp2=96.0, score=4, confidence=0.8)
    t.tick("XAUUSD.", 100.5, 97.9)     # TP1 touched (low <= 98); BE already at 0.5R
    assert any(ev == "DRY_RUN_TP1" for ev, _ in events)
    t.tick("XAUUSD.", 100.3, 99.5)     # rallies through entry (SL@BE for SELL: high >= 100)
    sl = [d for ev, d in events if ev == "DRY_RUN_SL_HIT"]
    assert sl, "BE stop should fire"
    assert sl[0]["r_multiple"] == pytest.approx(0.5)


def test_sl_before_anything_full_loss(events):
    t = DryRunTracker(mt5_mod=None)
    t.open_position("EURUSD.", "SELL", 0.1, entry=1.1000, sl=1.1050,
                    tp_final=1.0850, tp1=1.0950, tp2=1.0900, score=3, confidence=0.7)
    t.tick("EURUSD.", 1.1052, 1.0990)      # ask-side high >= SL
    sl = [d for ev, d in events if ev == "DRY_RUN_SL_HIT"]
    assert sl and sl[0]["r_multiple"] == pytest.approx(-1.0)


def test_double_touch_same_bar_sl_first(events):
    t = DryRunTracker(mt5_mod=None)
    t.open_position("XAUUSD.", "BUY", 0.1, entry=100.0, sl=98.0,
                    tp_final=106.0, tp1=102.0, tp2=104.0, score=4, confidence=0.8)
    t.tick("XAUUSD.", 106.5, 97.9)         # spans TP2 and SL
    assert any(ev == "DRY_RUN_SL_HIT" for ev, _ in events)
    assert not any(ev == "DRY_RUN_TP2" for ev, _ in events)


def test_fractions_mirror_env_like_manager(monkeypatch, events):
    """Probe: TP1/TP2 fractions must come from the same env vars manager uses.
    With TP1=40%, TP2=25%-of-remaining: orig fractions = 40/15/45."""
    monkeypatch.setenv("TP1_CLOSE_PCT", "40")
    monkeypatch.setenv("TP2_CLOSE_PCT", "25")
    t = DryRunTracker(mt5_mod=None)
    t.open_position("XAUUSD.", "BUY", 0.1, entry=100.0, sl=97.0,
                    tp_final=112.0, tp1=103.0, tp2=106.0, score=4, confidence=0.8)
    t.tick("XAUUSD.", 103.5, 100.2)      # TP1
    t.tick("XAUUSD.", 106.4, 103.8)      # TP2
    t.tick("XAUUSD.", 97.9, 90.0)        # collapse straight through original SL zone
    sl = [d for ev, d in events if ev == "DRY_RUN_SL_HIT"]
    assert sl, "runner stop should fire on collapse"
    # 0.40*1R + 0.15*2R + 0.45*(large negative)... just verify fractions used:
    pos_fractions = (0.40, 0.15, 0.45)
    got = [d["closed_frac"] for ev, d in events if ev in ("DRY_RUN_TP1", "DRY_RUN_TP2")]
    assert got[0] == pytest.approx(pos_fractions[0])
    assert got[1] == pytest.approx(pos_fractions[1])


def test_timeout_snapshot_is_censored_not_final(events):
    """8h snapshot must NOT close the position — real exit comes later."""
    t = DryRunTracker(mt5_mod=None)
    t.open_position("EURUSD.", "BUY", 0.1, entry=1.1000, sl=1.0950,
                    tp_final=1.1150, tp1=1.1050, tp2=1.1090, score=3, confidence=0.75)
    for _ in range(32):
        t.tick("EURUSD.", 1.1010, 1.0990)
    assert any(ev == "DRY_RUN_TIMEOUT_SNAPSHOT" for ev, _ in events)
    assert t.snapshot(), "position must still be open after the 8h snapshot"
    # real exit at hour 10: TP final
    for _ in range(8):
        t.tick("EURUSD.", 1.1160, 1.1100)
    fin = [d for ev, d in events if ev == "DRY_RUN_TP_FINAL"]
    assert fin and fin[0]["r_multiple"] > 1.0
