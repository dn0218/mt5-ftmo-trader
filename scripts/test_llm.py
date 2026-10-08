"""LLM end-to-end connectivity test — run AFTER filling in .env.

Verifies that the configured LLM provider (openai-compatible custom endpoint,
official OpenAI, or local Ollama) responds with valid structured JSON that
the bot can parse into an LLMDecision.

Usage:  python scripts/test_llm.py
Exit 0  = LLM ready, exit 1 = check the error below.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

# Minimal but realistic evidence payload (same shape the engine sends)
FAKE_EVIDENCE = {
    "symbol": "EURUSD",
    "regime": "TRENDING_UP",
    "current_price": 1.0850,
    "atr14": 0.0012,
    "indicators": {"rsi14": 55.2, "ema50": 1.0841},
    "structure": {
        "bos": [{"type": "BOS_UP"}],
        "choch": [{"type": "CHOCH_UP"}],
    },
    "liquidity": {"sweeps": [{"type": "SWEEP_LOW", "strong": True, "rejection_pct": 0.72}]},
    "orderblocks": [{"type": "BULLISH_OB"}],
    "fvg": [{"type": "BULLISH_FVG"}],
}


def main() -> int:
    from services.decision.ollama_decider import call_llm, clear_decision_cache
    from services.decision.ollama_decider import prescreen_evidence

    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    enabled = os.getenv("LLM_ENABLED", "true")
    print(f"Provider:  {provider}")
    print(f"LLM_ENABLED: {enabled}")

    if provider == "openai":
        base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        print(f"Endpoint:  {base}/chat/completions")
        print(f"Model:     {os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}")
        print(f"API key:   {'set (' + str(len(os.getenv('OPENAI_API_KEY',''))) + ' chars)' if os.getenv('OPENAI_API_KEY') else 'MISSING!'}")
    else:
        print(f"Endpoint:  {os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')}/api/chat")
        print(f"Model:     {os.getenv('OLLAMA_MODEL', 'qwen3:4b')}")

    # Step 1: prescreen must PASS (None) so the real LLM call happens
    pre = prescreen_evidence(FAKE_EVIDENCE, side="BUY")
    if pre is not None:
        print(f"\n[setup error] Fake evidence failed prescreen: {pre}")
        return 1
    print("\nPrescreen passed — calling LLM with test payload...")

    # Step 2: real HTTP round-trip
    clear_decision_cache()
    decision = call_llm(FAKE_EVIDENCE, side="BUY", cache_key=("EURUSD", "BUY", 1))

    if decision is None:
        print("LLM call FAILED (returned None).")
        print("Check: base URL reachable? API key valid? model name correct?")
        print("       does the model return JSON? (response_format json_object)")
        return 1

    print(f"\nLLM decision: {decision.get('decision')}  "
          f"confidence={decision.get('confidence')}  cached={decision.get('_cached', False)}")
    print(f"Justification: {decision.get('justification')}")

    ok = decision.get("decision") in ("ALLOW", "REFUSE") and 0 <= float(decision.get("confidence", 0)) <= 1
    print("\nLLM READY" if ok else "\nUNEXPECTED SCHEMA — model output did not match LLMDecision")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
