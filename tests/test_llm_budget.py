"""A3: LLM daily budget counter must survive process restarts.

Scenario matrix:
  1. same-day restart      -> count continues from risk_state.json
  2. next-day rollover     -> count resets to 0
  3. cap reached           -> fail-closed REFUSE without API call
  4. cap=0                 -> guard disabled entirely
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest

import services.decision.ollama_decider as decider


@pytest.fixture
def budget_env(tmp_path, monkeypatch):
    """Isolated budget state file + fixed env."""
    state_file = tmp_path / "risk_state.json"
    monkeypatch.setattr(decider, "_BUDGET_STATE_PATH", str(state_file))
    monkeypatch.setenv("LLM_DAILY_CALL_CAP", "3")
    # reset in-memory
    decider._budget_state.update({"date": None, "count": 0})
    return state_file


def _persisted(state_file):
    return json.loads(state_file.read_text(encoding="utf-8"))["llm_budget"]


def test_restart_continues_count(budget_env):
    """Process A counts 2 calls, dies. Process B (fresh in-memory) must adopt
    the persisted count and continue from it, not restart at 0."""
    state_file = budget_env
    today = decider.time.strftime("%Y-%m-%d", decider.time.gmtime())
    # process A: two calls
    assert decider._budget_check_and_count() is True
    assert decider._budget_check_and_count() is True
    assert _persisted(state_file) == {"date": today, "count": 2}
    # process B: simulate restart by wiping in-memory only
    decider._budget_state.update({"date": None, "count": 0})
    assert decider._budget_check_and_count() is True          # call #3
    assert decider._budget_state["count"] == 3
    assert decider._budget_check_and_count() is False         # cap=3 hit


def test_next_day_rollover(budget_env):
    """Persisted count from YESTERDAY must not block today's calls."""
    state_file = budget_env
    yesterday = "2000-01-01"
    state_file.write_text(json.dumps({"llm_budget": {"date": yesterday, "count": 999}}),
                          encoding="utf-8")
    assert decider._budget_check_and_count() is True
    assert decider._budget_state["count"] == 1


def test_cap_zero_disables(budget_env, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_CALL_CAP", "0")
    for _ in range(10):
        assert decider._budget_check_and_count() is True


def test_cap_refuse_is_fail_closed(budget_env, monkeypatch):
    """At cap, call_llm returns a prescreen-style REFUSE (never None, never ALLOW,
    and the HTTP layer is never touched). Prescreen is neutralised so the
    budget guard is the only gate in play."""
    state_file = budget_env
    monkeypatch.setattr(decider, "prescreen_evidence", lambda *a, **k: None)
    for _ in range(3):
        decider._budget_check_and_count()
    decider._budget_state.update({"date": None, "count": 0})  # fresh process
    result = decider.call_llm(
        {"symbol": "EURUSD."}, side="BUY", cache_key=None,
    )
    assert result is not None
    assert result["decision"] == "REFUSE"
    assert "LLM_DAILY_CALL_CAP" in result["justification"]
