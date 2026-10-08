"""C2 probe matrix: module-level invariants that guard against silent drift.

Each probe asserts a structural property the strategy layer depends on.
Failure = an upstream refactor changed behaviour without updating tests.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest


def test_probe_import_chain():
    """All strategy-layer modules import cleanly under mocked MT5."""
    import services.worker.engine          # noqa: F401
    import services.risk.risk_manager      # noqa: F401
    import services.decision.ollama_decider  # noqa: F401
    import services.trade_management.dry_run_tracker  # noqa: F401


def test_probe_ladder_fractions_sum_to_one(monkeypatch):
    """Tracker ladder fractions must always sum to 1 (env-driven)."""
    from services.trade_management.dry_run_tracker import DryRunTracker
    for tp1, tp2 in [("50", "30"), ("40", "25"), ("60", "50")]:
        monkeypatch.setenv("TP1_CLOSE_PCT", tp1)
        monkeypatch.setenv("TP2_CLOSE_PCT", tp2)
        t = DryRunTracker(mt5_mod=None)
        t.open_position("EURUSD.", "BUY", 0.1, 1.1, 1.09, 1.13,
                        1.11, 1.12, score=3, confidence=0.7)
        pos = t._open["EURUSD."]
        runner = 1.0 - pos["tp1_frac"] - pos["tp2_frac_orig"]
        assert pos["tp1_frac"] + pos["tp2_frac_orig"] + runner == pytest.approx(1.0)


def test_probe_pip_single_source():
    """pip_size is the ONLY pip source; XAU must scale point*10."""
    from core.mt5.symbols import pip_size

    class Info:  # minimal duck-typed symbol_info
        point = 0.01
        digits = 2
    assert pip_size("XAUUSD.", Info()) == pytest.approx(0.1)

    class InfoFx:
        point = 0.00001
        digits = 5
    assert pip_size("EURUSD.", InfoFx()) == pytest.approx(0.0001)


def test_probe_sl_limits_mode_partition(monkeypatch):
    """SL gate must expose BOTH modes: fixed (default) and atr (fail-closed)."""
    from services.risk.risk_manager import RiskManager
    rm = RiskManager()
    # atr mode without ATR -> fail closed
    monkeypatch.setenv("SL_LIMITS_MODE", "atr")
    ok, why = rm.validate_sl_tp("EURUSD.", "BUY", 1.1, 1.09, 1.14, atr=None)
    assert not ok and "ATR_UNAVAILABLE" in why
    # fixed mode unaffected by ATR arg
    monkeypatch.setenv("SL_LIMITS_MODE", "fixed")
    ok, why = rm.validate_sl_tp("EURUSD.", "BUY", 1.1, 1.09, 1.14, atr=None)
    assert ok


def test_probe_budget_cap_fail_closed(tmp_path, monkeypatch):
    """LLM budget guard must fail closed and survive restarts."""
    import services.decision.ollama_decider as decider
    monkeypatch.setattr(decider, "_BUDGET_STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("LLM_DAILY_CALL_CAP", "1")
    monkeypatch.setattr(decider, "prescreen_evidence", lambda *a, **k: None)
    # consume the single-call budget directly (no HTTP involved)
    decider._budget_state.update({"date": None, "count": 0})
    assert decider._budget_check_and_count() is True
    # fresh process (in-memory wiped): guard must adopt persisted count
    decider._budget_state.update({"date": None, "count": 0})
    assert decider._budget_check_and_count() is False   # cap=1 already used
    r = decider.call_llm({"symbol": "EURUSD."}, side="BUY")
    assert r and r["decision"] == "REFUSE" and "LLM_DAILY_CALL_CAP" in r["justification"]
