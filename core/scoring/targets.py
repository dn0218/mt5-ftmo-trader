"""Structural multi-target exit builder.

Replaces the fixed `tp = entry ± sl_dist * RR_MIN` rule with a tiered ladder:
    TP1 = 1R (always — anchors the de-risk partial)
    TP2 = first forward structural target with RR ≥ score-tiered floor
    TP_final = farthest viable target, capped at HARD_RR_CAP (default 2.5R)

The default 2.5R cap keeps trades finishable inside a single session — a 2.5R
move on M15 is typically reachable within 1 trading day on majors (~2× daily
ATR), whereas the old 5R cap routinely required multi-day continuations.

Score-tiered RR floor (configurable via env), auto-clamped so TP2 never
crowds TP_final (always leaves ≥0.25R headroom under the cap):
    score 3 → RR_FLOOR_SCORE_3 (default 2.0)
    score 4 → RR_FLOOR_SCORE_4 (default 2.5, clamped under cap)
    score 5+ → RR_FLOOR_SCORE_5 (default 3.0, clamped under cap)
"""

from __future__ import annotations

import os


def _read_hard_cap() -> float:
    return float(os.getenv("HARD_RR_CAP", "2.5"))


HARD_RR_CAP = _read_hard_cap()


def rr_floor_for_score(score: int) -> float:
    """RR floor for the TP2 given confluence score.

    Clamped to HARD_RR_CAP − 0.25 so TP_final always has room above TP2.
    """
    if score >= 5:
        f = float(os.getenv("RR_FLOOR_SCORE_5", "3.0"))
    elif score == 4:
        f = float(os.getenv("RR_FLOOR_SCORE_4", "2.5"))
    else:
        f = float(os.getenv("RISK_REWARD_MIN", "2.0"))
    cap = _read_hard_cap()
    return min(f, cap - 0.25)


def _r_of(price: float, entry: float, sl_dist: float, side: str) -> float:
    if sl_dist <= 0:
        return 0.0
    if side == "BUY":
        return (price - entry) / sl_dist
    return (entry - price) / sl_dist


def _forward_targets_for_side(evidence: dict, side: str) -> list[float]:
    """Pull forward target prices from evidence, in proximity order."""
    targets = (evidence.get("targets") or {})
    bucket = targets.get("up" if side == "BUY" else "down", {})
    out: list[float] = []
    for key in ("swings", "obs", "fvgs"):
        for p in bucket.get(key, []) or []:
            try:
                out.append(float(p))
            except (TypeError, ValueError):
                continue
    return out


def build_targets(side: str, entry: float, sl: float,
                  evidence: dict, score: int,
                  hard_cap: float | None = None) -> dict:
    """Pick TP1 / TP2 / TP_final for a setup.

    Returns dict with keys: tp1, tp2, tp_final, tp2_source.
    Falls back to fixed RR ladder when no structural targets are usable.
    """
    if hard_cap is None:
        hard_cap = _read_hard_cap()
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        # Degenerate — return entry to be safe; engine should reject upstream.
        return {"tp1": entry, "tp2": entry, "tp_final": entry, "tp2_source": "INVALID_SL"}

    sign = 1 if side == "BUY" else -1
    floor = rr_floor_for_score(score)

    tp1 = entry + sign * sl_dist  # always 1R

    # Candidate forward targets (already filtered by side in evidence).
    forward = _forward_targets_for_side(evidence, side)
    # Convert to R, filter to those past the floor, dedup near-equal ones.
    candidates: list[tuple[float, float]] = []  # (rr, price)
    for p in forward:
        rr = _r_of(p, entry, sl_dist, side)
        if rr <= 1.0:  # don't double-up with TP1
            continue
        if rr > hard_cap:  # past cap → useful as ceiling, but not as TP2
            continue
        candidates.append((rr, p))
    candidates.sort()

    # TP2: first candidate at or above the score floor.
    tp2 = None
    tp2_source = "STRUCTURAL"
    for rr, p in candidates:
        if rr + 1e-4 >= floor:
            tp2 = p
            break
    if tp2 is None:
        # No structural target hits the floor — synthesize at the floor.
        tp2 = entry + sign * sl_dist * floor
        tp2_source = "RR_FLOOR_FALLBACK"

    # TP_final: farthest viable target up to cap; otherwise the cap itself.
    if candidates:
        max_rr, max_p = candidates[-1]
        # Only use as final if it's at least 0.25R further than TP2.
        tp2_rr = _r_of(tp2, entry, sl_dist, side)
        if max_rr >= tp2_rr + 0.25:
            tp_final = max_p
        else:
            tp_final = entry + sign * sl_dist * hard_cap
    else:
        tp_final = entry + sign * sl_dist * hard_cap

    # Safety clamps: TP1 ≤ TP2 ≤ TP_final in trade direction.
    if side == "BUY":
        tp2 = max(tp2, tp1 + sl_dist * 0.25)
        tp_final = max(tp_final, tp2 + sl_dist * 0.25)
    else:
        tp2 = min(tp2, tp1 - sl_dist * 0.25)
        tp_final = min(tp_final, tp2 - sl_dist * 0.25)

    return {
        "tp1": float(tp1),
        "tp2": float(tp2),
        "tp_final": float(tp_final),
        "tp2_source": tp2_source,
    }


def apply_llm_plan(structural: dict, llm_decision: dict | None,
                   side: str, entry: float, sl: float, score: int) -> dict:
    """Validate LLM-proposed tp_plan; merge into structural targets if valid.

    Sanity checks (any failure → fall back to structural):
      - tp_plan present with all three numeric fields
      - all targets on the profit side of entry
      - all targets do not cross SL
      - tp_final RR ≤ HARD_RR_CAP and ≥ score floor
      - tp1 ≤ tp2 ≤ tp_final in trade direction
    Always returns the structural dict on any failure or when the flag is off.
    """
    if not llm_decision:
        return structural
    plan = (llm_decision.get("trade") or llm_decision.get("tp_plan") or {})
    try:
        tp1 = float(plan.get("tp1"))
        tp2 = float(plan.get("tp2"))
        tp_final = float(plan.get("tp_final"))
    except (TypeError, ValueError):
        return structural

    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return structural
    sign = 1 if side == "BUY" else -1

    # Profit-side and ordering checks
    if side == "BUY":
        if not (entry < tp1 <= tp2 <= tp_final):
            return structural
        if min(tp1, tp2, tp_final) <= sl:
            return structural
    else:
        if not (entry > tp1 >= tp2 >= tp_final):
            return structural
        if max(tp1, tp2, tp_final) >= sl:
            return structural

    final_rr = abs(tp_final - entry) / sl_dist
    if final_rr > _read_hard_cap() + 1e-4:
        return structural
    if final_rr + 1e-4 < rr_floor_for_score(score):
        return structural

    return {
        "tp1": tp1, "tp2": tp2, "tp_final": tp_final,
        "tp2_source": "LLM",
    }
