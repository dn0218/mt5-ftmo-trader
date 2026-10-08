"""LLM-based trade invalidation check.

Supports OpenAI API and Ollama via httpx.
"""

from __future__ import annotations
import json
import os
import logging
import httpx
from services.logging.audit_logger import log_event

logger = logging.getLogger(__name__)

INVALIDATION_PROMPT = """You are monitoring an open forex/gold position. Given the current market evidence and position details, decide if the trade setup has been invalidated.

Respond with JSON:
- "invalidated": true or false
- "reason": brief explanation
- "confidence": 0.0 to 1.0

Only invalidate if the original setup thesis is clearly broken (e.g., structure reversed, key level lost)."""


def check_invalidation(evidence: dict, position_info: dict) -> dict | None:
    """Check if an open position should be invalidated. Returns None on failure."""
    llm_enabled = os.getenv("LLM_ENABLED", os.getenv("OLLAMA_ENABLED", "true"))
    if llm_enabled != "true":
        return None
    if os.getenv("CLOSE_ON_INVALIDATION", "true") != "true":
        return None

    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    timeout = float(os.getenv("LLM_TIMEOUT", os.getenv("OLLAMA_TIMEOUT", "30")))

    user_msg = json.dumps({"evidence": evidence, "position": position_info}, default=str)
    messages = [
        {"role": "system", "content": INVALIDATION_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                return None
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "2000")),
                "temperature": float(os.getenv("LLM_TEMPERATURE", os.getenv("OLLAMA_TEMPERATURE", "0.1"))),
                "response_format": {"type": "json_object"},
            }
            resp = httpx.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=timeout,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
        else:
            url = f"{os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')}/api/chat"
            model = os.getenv("OLLAMA_MODEL", "qwen3:4b")
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0.1")),
                    "num_predict": 200,
                    "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "4096")),
                },
            }
            resp = httpx.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            msg = resp.json().get("message", {})
            text = msg.get("content", "") or msg.get("thinking", "")

        if not text or not text.strip():
            logger.warning("Invalidation: empty LLM response")
            return None

        result = None
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            from services.decision.ollama_decider import _extract_json
            result = _extract_json(text)

        if result is None:
            logger.warning("Invalidation: no parseable JSON")
            return None
        log_event("INVALIDATION_CHECK", result)
        return result
    except Exception as e:
        logger.warning("Invalidation check failed (non-fatal): %s", e)
        return None
