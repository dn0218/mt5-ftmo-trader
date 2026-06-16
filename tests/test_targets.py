"""Tests for the structural multi-target exit builder."""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.scoring.targets import build_targets, rr_floor_for_score, apply_llm_plan, HARD_RR_CAP


def _ev(up_swings=None, down_swings=None, up_obs=None, down_obs=None,
        up_fvgs=None, down_fvgs=None):
    return {
        "targets": {
            "up": {
                "swings": up_swings or [],
                "obs": up_obs or [],
                "fvgs": up_fvgs or [],
            },
            "down": {
                "swings": down_swings or [],
                "obs": down_obs or [],
                "fvgs": down_fvgs or [],
            },
        }
    }


class TestRRFloor:
    def test_score_3_default_is_2(self):
        os.environ.pop("RISK_REWARD_MIN", None)
        os.environ.pop("HARD_RR_CAP", None)
        os.environ["RISK_REWARD_MIN"] = "2.0"
        # Cap 2.5 − 0.25 headroom = 2.25, so 2.0 floor is unaffected.
        assert rr_floor_for_score(3) == 2.0

    def test_score_4_floor_clamped_under_cap(self):
        os.environ.pop("RR_FLOOR_SCORE_4", None)
        os.environ.pop("HARD_RR_CAP", None)
        # Default 2.5 clamped to cap (2.5) − 0.25 = 2.25.
        assert rr_floor_for_score(4) == 2.25

    def test_score_5_floor_clamped_under_cap(self):
        os.environ.pop("RR_FLOOR_SCORE_5", None)
        os.environ.pop("HARD_RR_CAP", None)
        # Default 3.0 clamped to cap (2.5) − 0.25 = 2.25.
        assert rr_floor_for_score(5) == 2.25

    def test_score_above_5_clamps_to_score_5(self):
        os.environ.pop("RR_FLOOR_SCORE_5", None)
        os.environ.pop("HARD_RR_CAP", None)
        assert rr_floor_for_score(7) == 2.25

    def test_cap_widening_lifts_floor(self):
        os.environ["HARD_RR_CAP"] = "4.0"
        os.environ.pop("RR_FLOOR_SCORE_5", None)
        # With cap 4.0, score 5 floor (3.0) is below cap−0.25=3.75 → not clamped.
        try:
            assert rr_floor_for_score(5) == 3.0
        finally:
            os.environ.pop("HARD_RR_CAP", None)


class TestBuyTargets:
    def test_tp1_is_always_1r(self):
        # entry 1.0850, sl 1.0800 → 50 pips risk → tp1 should be 1.0900
        ev = _ev(up_swings=[1.0950, 1.1000])
        out = build_targets("BUY", 1.0850, 1.0800, ev, score=3)
        assert abs(out["tp1"] - 1.0900) < 1e-6

    def test_tp2_picks_first_structural_at_floor(self):
        # 50 pip risk; floor for score 3 is 2.0 → tp2 must be ≥ 1.0950.
        # Candidates: 1.0925 (0.5R, too close), 1.0960 (2.2R, valid).
        ev = _ev(up_swings=[1.0925, 1.0960, 1.1100])
        out = build_targets("BUY", 1.0850, 1.0800, ev, score=3)
        assert abs(out["tp2"] - 1.0960) < 1e-6
        assert out["tp2_source"] == "STRUCTURAL"

    def test_tp2_falls_back_when_no_structure_above_floor(self):
        # Only 0.5R candidate — should synthesize at 2R floor.
        ev = _ev(up_swings=[1.0875])  # 0.5R, below floor
        out = build_targets("BUY", 1.0850, 1.0800, ev, score=3)
        assert out["tp2_source"] == "RR_FLOOR_FALLBACK"
        # Synthetic tp2 at 2R = 1.0950
        assert abs(out["tp2"] - 1.0950) < 1e-6

    def test_tp_final_capped_at_hard_cap(self):
        # Candidates above HARD_RR_CAP (2.5R) get filtered.
        # entry 1.0850, sl 1.0800 → 1R=0.005 → cap price = 1.0850 + 2.5*0.005 = 1.0975.
        # 5R and 9R targets are past cap, so tp_final clamps to cap.
        os.environ.pop("HARD_RR_CAP", None)
        ev = _ev(up_swings=[1.1100, 1.1300])  # 5R, 9R
        out = build_targets("BUY", 1.0850, 1.0800, ev, score=3)
        assert out["tp_final"] <= 1.0850 + 0.0050 * 2.5 + 1e-6

    def test_score_5_floor_synthesises_when_no_structure(self):
        os.environ.pop("HARD_RR_CAP", None)
        os.environ["RR_FLOOR_SCORE_5"] = "3.0"
        # No structural candidates within range → tp2 synthesises at clamped floor.
        ev = _ev(up_swings=[1.0875])  # 0.5R only
        out = build_targets("BUY", 1.0850, 1.0800, ev, score=5)
        assert out["tp2_source"] == "RR_FLOOR_FALLBACK"
        # Floor 3.0 is clamped to cap (2.5) − 0.25 = 2.25R → tp2 = 1.0850 + 0.005*2.25 = 1.09625
        assert abs(out["tp2"] - 1.09625) < 1e-6


class TestSellTargets:
    def test_tp1_is_1r_below_for_sell(self):
        # entry 1.0850, sl 1.0900 → tp1 = 1.0800
        ev = _ev(down_swings=[1.0750, 1.0700])
        out = build_targets("SELL", 1.0850, 1.0900, ev, score=3)
        assert abs(out["tp1"] - 1.0800) < 1e-6

    def test_tp2_floor_for_sell(self):
        # entry 1.0850, sl 1.0900 → 1R=0.005 (50 pips).
        # 2R floor for score 3 → tp2 must be ≤ 1.0750.
        # Candidates: 1.0735 (2.3R), 1.0700 (3R, past 2.5R cap → filtered).
        os.environ.pop("HARD_RR_CAP", None)
        ev = _ev(down_swings=[1.0735, 1.0700])
        out = build_targets("SELL", 1.0850, 1.0900, ev, score=3)
        # 2.3R candidate is at floor and inside cap → picked.
        assert abs(out["tp2"] - 1.0735) < 1e-6


class TestLLMPlanValidation:
    def test_invalid_when_decision_none(self):
        structural = {"tp1": 1.09, "tp2": 1.095, "tp_final": 1.10, "tp2_source": "STRUCTURAL"}
        assert apply_llm_plan(structural, None, "BUY", 1.085, 1.08, 3) is structural

    def test_invalid_when_targets_below_entry_for_buy(self):
        structural = {"tp1": 1.09, "tp2": 1.095, "tp_final": 1.10, "tp2_source": "STRUCTURAL"}
        bad = {"trade": {"tp1": 1.080, "tp2": 1.095, "tp_final": 1.10}}
        assert apply_llm_plan(structural, bad, "BUY", 1.085, 1.08, 3) is structural

    def test_invalid_when_below_score_floor(self):
        os.environ.pop("HARD_RR_CAP", None)
        structural = {"tp1": 1.09, "tp2": 1.0925, "tp_final": 1.0975, "tp2_source": "STRUCTURAL"}
        # entry=1.085, sl=1.08 → 1R = 0.005. Score 5 floor clamps to 2.25R = 1.09625.
        # tp_final at 1.090 = 1R, way below 2.25R floor.
        bad = {"trade": {"tp1": 1.087, "tp2": 1.089, "tp_final": 1.090}}
        os.environ["RR_FLOOR_SCORE_5"] = "3.0"
        assert apply_llm_plan(structural, bad, "BUY", 1.085, 1.08, 5) is structural

    def test_valid_plan_replaces_structural(self):
        os.environ.pop("HARD_RR_CAP", None)
        # entry=1.085, sl=1.08 → 1R=0.005, cap 2.5R = 1.0975.
        structural = {"tp1": 1.09, "tp2": 1.0925, "tp_final": 1.0975, "tp2_source": "STRUCTURAL"}
        # tp_final=1.0970 = 2.4R (within 2.5R cap, above score-3 floor 2.0R).
        good = {"trade": {"tp1": 1.090, "tp2": 1.094, "tp_final": 1.0970}}
        out = apply_llm_plan(structural, good, "BUY", 1.085, 1.08, 3)
        assert out["tp2_source"] == "LLM"
        assert out["tp_final"] == 1.0970

    def test_invalid_when_runner_past_hard_cap(self):
        os.environ.pop("HARD_RR_CAP", None)
        structural = {"tp1": 1.09, "tp2": 1.0925, "tp_final": 1.0975, "tp2_source": "STRUCTURAL"}
        # 3R is past 2.5R cap (entry 1.085 + 3*0.005 = 1.100).
        bad = {"trade": {"tp1": 1.090, "tp2": 1.0925, "tp_final": 1.100}}
        out = apply_llm_plan(structural, bad, "BUY", 1.085, 1.08, 3)
        assert out is structural
