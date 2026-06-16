"""Tests for LLM decision service (OpenAI + Ollama)."""

import os
import sys
import json
import pytest
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.decision.ollama_decider import call_llm, is_trade_valid, prescreen_evidence

# Valid evidence that passes prescreen (bullish aligned)
VALID_EVIDENCE = {
    "symbol": "EURUSD", "regime": "TRENDING_UP", "current_price": 1.085, "atr14": 0.0012,
    "indicators": {"rsi14": 38.0, "ema9": 1.0848, "ema21": 1.0840},
    "structure": {"bos": [{"type": "BOS_UP", "bar": 45}], "choch": [{"type": "CHOCH_UP", "bar": 40}]},
    "liquidity": {"sweeps": [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.30, "wick_atr_ratio": 0.5, "bar": 48}]},
    "orderblocks": [{"type": "BULLISH_OB", "price": 1.0842}],
    "fvg": [{"type": "BULLISH_FVG", "top": 1.0855, "bottom": 1.0845}],
}


class TestPrescreen:
    def test_valid_setup_passes(self):
        """Valid aligned setup should pass prescreen."""
        assert prescreen_evidence(VALID_EVIDENCE) is None

    def test_counter_trend_sell_refused(self):
        """SELL in uptrend with bullish structure should be refused."""
        e = {
            "symbol": "EURUSD", "regime": "TRENDING_UP", "current_price": 1.087, "atr14": 0.0012,
            "indicators": {"rsi14": 60.0},
            "structure": {"bos": [{"type": "BOS_UP", "bar": 43}], "choch": [{"type": "CHOCH_UP", "bar": 36}]},
            "liquidity": {"sweeps": [{"type": "SWEEP_HIGH", "strong": True, "rejection_pct": 0.21, "wick_atr_ratio": 0.33, "bar": 49}]},
            "orderblocks": [], "fvg": [],
        }
        result = prescreen_evidence(e)
        assert result is not None
        assert result["decision"] == "REFUSE"

    def test_counter_trend_buy_refused(self):
        """BUY in downtrend with bearish structure should be refused."""
        e = {
            "symbol": "EURUSD", "regime": "TRENDING_DOWN", "current_price": 1.080, "atr14": 0.0012,
            "indicators": {"rsi14": 30.0},
            "structure": {"bos": [{"type": "BOS_DOWN", "bar": 43}], "choch": [{"type": "CHOCH_DOWN", "bar": 36}]},
            "liquidity": {"sweeps": [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 49}]},
            "orderblocks": [], "fvg": [],
        }
        result = prescreen_evidence(e)
        assert result is not None
        assert result["decision"] == "REFUSE"

    def test_no_structure_refused(self):
        """No BOS or CHOCH should be refused."""
        e = {
            "symbol": "EURUSD", "regime": "RANGING", "current_price": 1.085, "atr14": 0.0008,
            "indicators": {"rsi14": 50.0},
            "structure": {"bos": [], "choch": []},
            "liquidity": {"sweeps": [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.22, "wick_atr_ratio": 0.35, "bar": 48}]},
            "orderblocks": [], "fvg": [],
        }
        result = prescreen_evidence(e)
        assert result is not None
        assert "No BOS" in result["justification"]

    def test_rsi_overbought_refused(self):
        """RSI > 70 for BUY should be refused."""
        e = {
            "symbol": "EURUSD", "regime": "TRENDING_UP", "current_price": 1.090, "atr14": 0.0012,
            "indicators": {"rsi14": 78.0},
            "structure": {"bos": [{"type": "BOS_UP", "bar": 45}], "choch": [{"type": "CHOCH_UP", "bar": 40}]},
            "liquidity": {"sweeps": [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.25, "wick_atr_ratio": 0.4, "bar": 48}]},
            "orderblocks": [], "fvg": [],
        }
        result = prescreen_evidence(e)
        assert result is not None
        assert "overbought" in result["justification"]

    def test_weak_sweep_refused(self):
        """Weak (not strong) sweep should be refused."""
        e = {
            "symbol": "EURUSD", "regime": "TRENDING_UP", "current_price": 1.085, "atr14": 0.0012,
            "indicators": {"rsi14": 40.0},
            "structure": {"bos": [{"type": "BOS_UP", "bar": 45}], "choch": [{"type": "CHOCH_UP", "bar": 40}]},
            "liquidity": {"sweeps": [{"type": "SWEEP_LOW", "strong": False, "rejection_pct": 0.08, "wick_atr_ratio": 0.15, "bar": 48}]},
            "orderblocks": [], "fvg": [],
        }
        result = prescreen_evidence(e)
        assert result is not None
        assert "weak" in result["justification"].lower()


class TestOllamaDecisions:
    def test_allow_decision_valid(self, ollama_allow):
        """ALLOW decision with high confidence should be valid."""
        result = call_llm(VALID_EVIDENCE)
        assert result is not None
        assert result["decision"] == "ALLOW"
        assert result["confidence"] == 0.85

        valid, reason = is_trade_valid(result, "BUY", "SWEEP_LOW", "NEUTRAL")
        assert valid, f"Should be valid but got: {reason}"

    def test_refuse_decision_valid(self, ollama_refuse):
        """REFUSE decision should block the trade."""
        result = call_llm(VALID_EVIDENCE)
        assert result is not None
        assert result["decision"] == "REFUSE"

        valid, reason = is_trade_valid(result, "BUY", "SWEEP_LOW", "NEUTRAL")
        assert not valid
        assert "LLM_REFUSE" in reason

    def test_refuse_on_ollama_down(self, ollama_down):
        """When Ollama is unreachable, should block trade and alert."""
        result = call_llm(VALID_EVIDENCE)
        assert result is None

        # LLM required — unavailable should block the trade
        valid, reason = is_trade_valid(result, "BUY", "SWEEP_LOW", "NEUTRAL")
        assert not valid
        assert "LLM_UNAVAILABLE" in reason

    def test_refuse_on_low_confidence(self, ollama_low_confidence):
        """ALLOW with confidence < 0.70 should be refused."""
        result = call_llm(VALID_EVIDENCE)
        assert result is not None
        assert result["confidence"] == 0.3

        valid, reason = is_trade_valid(result, "BUY", "SWEEP_LOW", "NEUTRAL")
        assert not valid
        assert "LOW_CONFIDENCE" in reason


class TestOpenAIProvider:
    def test_openai_allow_decision(self):
        """OpenAI provider should parse JSON response correctly."""
        try:
            import respx
        except ImportError:
            pytest.skip("respx not installed")

        body = {
            "decision": "ALLOW", "confidence": 0.82,
            "justification": "Strong confluence with sweep + structure",
        }
        openai_response = {
            "choices": [{"message": {"content": json.dumps(body)}}],
        }

        old_provider = os.environ.get("LLM_PROVIDER")
        old_key = os.environ.get("OPENAI_API_KEY")
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_MODEL"] = "gpt-4o-mini"
        try:
            with respx.mock:
                respx.post("https://api.openai.com/v1/chat/completions").mock(
                    return_value=httpx.Response(200, json=openai_response)
                )
                result = call_llm(VALID_EVIDENCE)
            assert result is not None
            assert result["decision"] == "ALLOW"
            assert result["confidence"] == 0.82
        finally:
            if old_provider is not None:
                os.environ["LLM_PROVIDER"] = old_provider
            else:
                os.environ.pop("LLM_PROVIDER", None)
            if old_key is not None:
                os.environ["OPENAI_API_KEY"] = old_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_openai_refuse_decision(self):
        """OpenAI REFUSE should block trade."""
        try:
            import respx
        except ImportError:
            pytest.skip("respx not installed")

        body = {
            "decision": "REFUSE", "confidence": 0.90,
            "justification": "Counter-trend setup, RSI exhausted",
        }
        openai_response = {
            "choices": [{"message": {"content": json.dumps(body)}}],
        }

        old_provider = os.environ.get("LLM_PROVIDER")
        old_key = os.environ.get("OPENAI_API_KEY")
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "test-key"
        try:
            with respx.mock:
                respx.post("https://api.openai.com/v1/chat/completions").mock(
                    return_value=httpx.Response(200, json=openai_response)
                )
                result = call_llm(VALID_EVIDENCE)
            assert result is not None
            assert result["decision"] == "REFUSE"
            valid, reason = is_trade_valid(result, "BUY", "SWEEP_LOW", "NEUTRAL")
            assert not valid
            assert "LLM_REFUSE" in reason
        finally:
            if old_provider is not None:
                os.environ["LLM_PROVIDER"] = old_provider
            else:
                os.environ.pop("LLM_PROVIDER", None)
            if old_key is not None:
                os.environ["OPENAI_API_KEY"] = old_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)


class TestHardBlocks:
    def test_sweep_direction_hard_block(self):
        """Sweep direction mismatch is a hard block regardless of LLM."""
        allow_decision = {
            "decision": "ALLOW", "confidence": 0.95,
            "justification": "test",
        }
        # SWEEP_HIGH should only allow SELL
        valid, reason = is_trade_valid(allow_decision, "BUY", "SWEEP_HIGH", "NEUTRAL")
        assert not valid
        assert "SWEEP_DIRECTION_BLOCK" in reason

        # SWEEP_LOW should only allow BUY
        valid, reason = is_trade_valid(allow_decision, "SELL", "SWEEP_LOW", "NEUTRAL")
        assert not valid
        assert "SWEEP_DIRECTION_BLOCK" in reason

    def test_htf_bias_hard_block(self):
        """HTF bias mismatch is a hard block regardless of LLM."""
        allow_decision = {
            "decision": "ALLOW", "confidence": 0.95,
            "justification": "test",
        }
        # BEARISH blocks BUY
        valid, reason = is_trade_valid(allow_decision, "BUY", "SWEEP_LOW", "BEARISH")
        assert not valid
        assert "HTF_BIAS_BLOCK" in reason

        # BULLISH blocks SELL
        valid, reason = is_trade_valid(allow_decision, "SELL", "SWEEP_HIGH", "BULLISH")
        assert not valid
        assert "HTF_BIAS_BLOCK" in reason

    def test_htf_skip_in_soft_mode(self):
        """skip_htf=True bypasses HTF block (engine already gated via score)."""
        allow_decision = {
            "decision": "ALLOW", "confidence": 0.95, "justification": "test",
        }
        valid, reason = is_trade_valid(
            allow_decision, "BUY", "SWEEP_LOW", "BEARISH", skip_htf=True,
        )
        assert valid
        assert reason == "VALID"

    def test_min_conf_override_raises_threshold(self):
        """min_conf_override replaces MIN_LLM_CONFIDENCE for the call."""
        decision = {
            "decision": "ALLOW", "confidence": 0.80, "justification": "test",
        }
        # Default min_conf 0.70 → passes.
        os.environ["MIN_LLM_CONFIDENCE"] = "0.70"
        valid, _ = is_trade_valid(decision, "BUY", "SWEEP_LOW", "NEUTRAL")
        assert valid
        # Override to 0.85 → fails despite 0.80 confidence.
        valid, reason = is_trade_valid(
            decision, "BUY", "SWEEP_LOW", "NEUTRAL", min_conf_override=0.85,
        )
        assert not valid
        assert "LOW_CONFIDENCE" in reason


class TestDecisionCache:
    def test_cache_round_trip(self, ollama_allow):
        """Second call with same cache_key returns cached result without HTTP call."""
        from services.decision.ollama_decider import call_llm, clear_decision_cache
        clear_decision_cache()
        os.environ["LLM_PROVIDER"] = "ollama"
        os.environ["LLM_ENABLED"] = "true"

        key = ("EURUSD", "BUY", 100)
        # First call hits the (mocked) provider.
        r1 = call_llm(VALID_EVIDENCE, side="BUY", cache_key=key)
        assert r1 is not None
        assert r1.get("_cached") is not True

        # Second call returns cached value.
        r2 = call_llm(VALID_EVIDENCE, side="BUY", cache_key=key)
        assert r2 is not None
        assert r2["_cached"] is True
        assert r2["decision"] == r1["decision"]

    def test_cache_miss_on_different_sweep_bar(self, ollama_allow):
        """Different sweep_bar → no cache hit, fresh call."""
        from services.decision.ollama_decider import call_llm, clear_decision_cache
        clear_decision_cache()
        os.environ["LLM_PROVIDER"] = "ollama"
        os.environ["LLM_ENABLED"] = "true"

        r1 = call_llm(VALID_EVIDENCE, side="BUY", cache_key=("EURUSD", "BUY", 100))
        r2 = call_llm(VALID_EVIDENCE, side="BUY", cache_key=("EURUSD", "BUY", 101))
        assert r1 is not None and r2 is not None
        # Both fresh (no _cached marker).
        assert not r1.get("_cached")
        assert not r2.get("_cached")
