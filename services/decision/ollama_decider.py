"""LLM decision service — veto filter for pre-qualified setups.

Supports OpenAI API (gpt-4o-mini) and Ollama (local) via httpx.
Provider selected by LLM_PROVIDER env var ("openai" or "ollama").
"""

from __future__ import annotations
import json
import os
import re
import time
import logging
import httpx
from pydantic import ValidationError
from core.decision.schema import LLMDecision
from services.logging.audit_logger import log_decision_debug

logger = logging.getLogger(__name__)


def _get_provider() -> str:
    """Get configured LLM provider."""
    return os.getenv("LLM_PROVIDER", "openai").lower()


def _call_openai_api(messages: list[dict]) -> str | None:
    """Call OpenAI Chat Completions API via httpx. Returns content or None."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.error("OPENAI_API_KEY not set")
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    timeout = float(os.getenv("LLM_TIMEOUT", "30"))

    # OPENAI_BASE_URL: point to any OpenAI-compatible endpoint.
    # Defaults to the official API so behaviour is unchanged when unset.
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

    # LLM_MAX_TOKENS: reasoning models (GLM/qwen-thinking etc.) burn tokens on
    # chain-of-thought before the visible content — 200 starves them and yields
    # an empty message. Default 2000 leaves room for reasoning + JSON body.
    max_tokens = int(os.getenv("LLM_MAX_TOKENS", "2000"))

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": float(os.getenv("LLM_TEMPERATURE", os.getenv("OLLAMA_TEMPERATURE", "0.1"))),
        "response_format": {"type": "json_object"},
    }

    resp = httpx.post(
        f"{base_url}/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_ollama_api(messages: list[dict]) -> str | None:
    """Call Ollama API. Returns content or None."""
    url = f"{os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')}/api/chat"
    model = os.getenv("OLLAMA_MODEL", "qwen3:4b")
    timeout = float(os.getenv("OLLAMA_TIMEOUT", "120"))

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {
            "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0.1")),
            "num_predict": 300,
            "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "4096")),
        },
    }

    resp = httpx.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    msg = resp.json().get("message", {})
    content = msg.get("content", "")
    thinking = msg.get("thinking", "")
    return content if content.strip() else thinking

SYSTEM_PROMPT_VETO = """Rate this pre-qualified trade setup. Respond with JSON containing exactly these keys:
- "decision": "ALLOW" or "REFUSE"
- "confidence": a number between 0.0 and 1.0 reflecting how strong the setup is
- "justification": one sentence reason

Confidence guide: 0.9+ = excellent, 0.8 = good, 0.7 = acceptable, below 0.7 = weak.
REFUSE only if you see a clear problem (conflicting signals, exhausted move, bad R:R).
Most setups should ALLOW since they passed pre-screening."""


SYSTEM_PROMPT_PLANNER = """Rate this pre-qualified trade setup AND propose multi-target exits. JSON keys:
- "decision": "ALLOW" or "REFUSE"
- "confidence": 0.0..1.0
- "justification": one sentence reason
- "tp_plan": object {"tp1": float, "tp2": float, "tp_final": float} — absolute prices in trade direction.

Guidance for tp_plan:
- TP1 should be ~1R from entry (de-risk partial).
- TP2 should target the next opposing structural level (swing / OB / FVG) at 2R-3R.
- tp_final should target the farthest viable liquidity, capped at 5R.
- All prices must be on the profit side of entry. The engine will validate and fall back if invalid.

REFUSE only on clear problems. Most setups should ALLOW."""


def _system_prompt() -> str:
    if os.getenv("LLM_PLAN_TARGETS", "false").lower() == "true":
        return SYSTEM_PROMPT_PLANNER
    return SYSTEM_PROMPT_VETO


# Backwards-compatible alias used in tests / external callers.
SYSTEM_PROMPT = SYSTEM_PROMPT_VETO


def _build_user_message(evidence: dict, side: str | None = None) -> str:
    """Build trade summary with pre-analyzed warnings for LLM."""
    symbol = evidence.get("symbol", "?")
    regime = evidence.get("regime", "?")
    price = evidence.get("current_price", 0)
    atr = evidence.get("atr14", 0)

    ind = evidence.get("indicators", {})
    rsi = ind.get("rsi14", 50)
    if not isinstance(rsi, (int, float)):
        rsi = 50

    struct = evidence.get("structure", {})
    bos_list = struct.get("bos", [])[-5:]
    choch_list = struct.get("choch", [])[-5:]
    bos_types = [b.get("type", "") for b in bos_list]
    choch_types = [c.get("type", "") for c in choch_list]

    sweeps = evidence.get("liquidity", {}).get("sweeps", [])[-3:]
    strong_sweeps = [s for s in sweeps if s.get("strong")]
    if side not in ("BUY", "SELL"):
        has_sweep_low = any(s.get("type") == "SWEEP_LOW" for s in sweeps)
        side = "BUY" if has_sweep_low else "SELL" if sweeps else "?"

    obs = evidence.get("orderblocks", [])[-3:]
    fvgs = evidence.get("fvg", [])[-3:]

    # ── Pre-analyze warnings ──
    warnings = []

    # Counter-trend check
    has_bos_up = any("UP" in b for b in bos_types)
    has_bos_down = any("DOWN" in b for b in bos_types)
    has_choch_up = any("UP" in c for c in choch_types)
    has_choch_down = any("DOWN" in c for c in choch_types)

    if side == "SELL":
        if "TRENDING_UP" in regime:
            warnings.append("SELL against TRENDING_UP regime")
        if has_bos_up and not has_bos_down:
            warnings.append("SELL but all BOS are UP (counter-trend)")
        if has_choch_up and not has_choch_down:
            warnings.append("SELL but CHOCH is UP (counter-trend)")
    elif side == "BUY":
        if "TRENDING_DOWN" in regime:
            warnings.append("BUY against TRENDING_DOWN regime")
        if has_bos_down and not has_bos_up:
            warnings.append("BUY but all BOS are DOWN (counter-trend)")
        if has_choch_down and not has_choch_up:
            warnings.append("BUY but CHOCH is DOWN (counter-trend)")

    # RSI extremes
    if side == "BUY" and rsi > 70:
        warnings.append(f"RSI={rsi:.0f} overbought for BUY")
    elif side == "SELL" and rsi < 30:
        warnings.append(f"RSI={rsi:.0f} oversold for SELL")

    # No structure
    if not bos_list and not choch_list:
        warnings.append("No BOS or CHOCH — no structural confirmation")

    # Weak sweep
    if sweeps and not strong_sweeps:
        warnings.append("Sweep is weak (not strong)")

    # No sweep at all
    if not sweeps:
        warnings.append("No liquidity sweep")

    # Volatile ATR
    typical_atr = 15.0 if "XAU" in symbol else 0.0012
    if atr > typical_atr * 2.5:
        warnings.append(f"ATR={atr} abnormally high (volatile spike)")

    # ── Build message ──
    bos_str = ", ".join(bos_types) if bos_types else "none"
    choch_str = ", ".join(choch_types) if choch_types else "none"
    sweep_parts = []
    for s in sweeps:
        rej = s.get("rejection_pct", 0)
        strong = "strong" if s.get("strong") else "weak"
        sweep_parts.append(f"{s.get('type','?')}({strong}, {rej:.0%} rejection)")
    sweep_str = ", ".join(sweep_parts) if sweep_parts else "none"
    ob_str = ", ".join(o.get("type", "?") for o in obs) if obs else "none"
    fvg_str = ", ".join(f.get("type", "?") for f in fvgs) if fvgs else "none"

    msg = (
        f"{symbol} {side}: price={price}, regime={regime}, RSI={rsi}, ATR={atr}. "
        f"BOS=[{bos_str}]. CHOCH=[{choch_str}]. Sweeps=[{sweep_str}]. OB=[{ob_str}]. FVG=[{fvg_str}]."
    )

    if warnings:
        msg += f"\n\nWARNINGS: {'; '.join(warnings)}"
    else:
        msg += "\n\nNo warnings detected. All signals align."

    return msg


def _extract_json(text: str) -> dict | None:
    """Extract decision JSON from LLM response, handling markdown blocks and thinking tags."""
    if not text or not text.strip():
        return None

    # Strip <think>...</think> tags (qwen3 thinking mode)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # Try direct parse first
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "decision" in obj:
            return obj
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(1))
            if isinstance(obj, dict) and "decision" in obj:
                return obj
        except json.JSONDecodeError:
            pass

    # Brace-matching: find all valid JSON objects by tracking { } depth
    candidates = []
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth = 0
            start = i
            in_str = False
            escape = False
            for j in range(i, len(text)):
                c = text[j]
                if escape:
                    escape = False
                    continue
                if c == '\\' and in_str:
                    escape = True
                    continue
                if c == '"' and not escape:
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start:j+1])
                        i = j
                        break
        i += 1

    # Return the first candidate that parses and has "decision"
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and "decision" in obj:
                return obj
        except json.JSONDecodeError:
            continue

    # Last resort: find truncated JSON with "decision" and try to repair it
    # This handles cases where thinking got cut off mid-JSON
    for match in re.finditer(r'\{\s*"decision"', text):
        fragment = text[match.start():]
        # Try progressively closing the JSON
        repaired = _repair_truncated_json(fragment)
        if repaired:
            return repaired

    return None


def _repair_truncated_json(fragment: str) -> dict | None:
    """Attempt to repair a truncated JSON object containing a decision."""
    # First try parsing as-is
    try:
        obj = json.loads(fragment)
        if isinstance(obj, dict) and "decision" in obj:
            return obj
    except json.JSONDecodeError:
        pass

    # Close any open strings, arrays, and braces
    # Strip trailing incomplete key/value
    cleaned = fragment.rstrip()
    # Remove trailing comma or incomplete value
    cleaned = re.sub(r',\s*"[^"]*"?\s*:?\s*"?[^"{}[\]]*$', '', cleaned)
    cleaned = re.sub(r',\s*$', '', cleaned)

    # Count unclosed structures and close them
    depth_brace = 0
    depth_bracket = 0
    in_str = False
    escape = False
    for c in cleaned:
        if escape:
            escape = False
            continue
        if c == '\\' and in_str:
            escape = True
            continue
        if c == '"' and not escape:
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == '{': depth_brace += 1
        elif c == '}': depth_brace -= 1
        elif c == '[': depth_bracket += 1
        elif c == ']': depth_bracket -= 1

    # If we're inside a string, close it
    if in_str:
        cleaned += '"'

    # Close open arrays and braces
    cleaned += ']' * max(0, depth_bracket)
    cleaned += '}' * max(0, depth_brace)

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "decision" in obj:
            logger.debug("Repaired truncated JSON successfully")
            return obj
    except json.JSONDecodeError:
        pass

    return None


def prescreen_evidence(evidence: dict, side: str | None = None) -> dict | None:
    """Fast Python-based pre-screening. Returns REFUSE dict or None if passed.

    ``side`` is the engine's chosen direction. When omitted, falls back to
    deriving direction from sweep type (legacy behaviour, kept for tests
    and any caller that hasn't yet been updated).
    """
    ind = evidence.get("indicators", {})
    rsi = ind.get("rsi14", 50)
    if not isinstance(rsi, (int, float)):
        rsi = 50

    struct = evidence.get("structure", {})
    bos_list = struct.get("bos", [])
    choch_list = struct.get("choch", [])
    bos_types = [b.get("type", "") for b in bos_list]
    choch_types = [c.get("type", "") for c in choch_list]

    sweeps = evidence.get("liquidity", {}).get("sweeps", [])
    strong_sweeps = [s for s in sweeps if s.get("strong")]
    if side not in ("BUY", "SELL"):
        has_sweep_low = any(s.get("type") == "SWEEP_LOW" for s in sweeps)
        side = "BUY" if has_sweep_low else "SELL" if sweeps else None

    regime = evidence.get("regime", "")
    atr = evidence.get("atr14", 0)
    symbol = evidence.get("symbol", "")

    has_bos_up = any("UP" in b for b in bos_types)
    has_bos_down = any("DOWN" in b for b in bos_types)
    has_choch_up = any("UP" in c for c in choch_types)
    has_choch_down = any("DOWN" in c for c in choch_types)

    def refuse(reason):
        return {"decision": "REFUSE", "confidence": 0.99, "justification": reason}

    # Counter-trend checks — only the *structural* ones. Regime-only refusals
    # were wrong for SMC sweep entries: a sweep+BOS SELL is *defined* to fire
    # during a local M15 up-leg (price spikes high, sweeps liquidity, reverses).
    # Treating that up-leg as TRENDING_UP and refusing kills the very setup the
    # engine is built to take. H4 HTF bias already gates trend direction at the
    # right level. Structural BOS/CHOCH disagreement (no opposing reversal
    # evidence) is still a valid refuse.
    if side == "SELL":
        if has_bos_up and not has_bos_down:
            return refuse("SELL but structure is bullish (BOS_UP)")
        if has_choch_up and not has_choch_down:
            return refuse("SELL but CHOCH is bullish (CHOCH_UP)")
    elif side == "BUY":
        if has_bos_down and not has_bos_up:
            return refuse("BUY but structure is bearish (BOS_DOWN)")
        if has_choch_down and not has_choch_up:
            return refuse("BUY but CHOCH is bearish (CHOCH_DOWN)")

    # RSI extremes
    if side == "BUY" and rsi > 70:
        return refuse(f"RSI={rsi:.0f} overbought for BUY")
    if side == "SELL" and rsi < 30:
        return refuse(f"RSI={rsi:.0f} oversold for SELL")

    # No structure
    if not bos_list and not choch_list:
        return refuse("No BOS or CHOCH — no structural confirmation")

    # Weak sweep only
    if sweeps and not strong_sweeps:
        return refuse("Sweep is weak (not strong)")

    # No sweep
    if not sweeps:
        return refuse("No liquidity sweep detected")

    # Volatile ATR (high)
    typical_atr = 15.0 if "XAU" in symbol else 0.0015
    if atr > typical_atr * 2.0:
        return refuse(f"ATR={atr} abnormally high (volatile spike)")

    # Dead market ATR (too low)
    min_atr = 5.0 if "XAU" in symbol else 0.0005
    if atr < min_atr:
        return refuse(f"ATR={atr} too low — dead market, spread will eat profits")

    # Volatile regime
    if regime == "VOLATILE":
        return refuse("VOLATILE regime — unpredictable, avoid")

    # Overextended price (price too far from EMA50)
    ema50 = ind.get("ema50")
    if ema50 and isinstance(ema50, (int, float)) and ema50 > 0:
        price = evidence.get("current_price", 0)
        if price and atr > 0:
            dist_from_ema50 = abs(price - ema50) / atr
            if dist_from_ema50 > 5.0:  # More than 5x ATR from EMA50
                return refuse(f"Price overextended ({dist_from_ema50:.1f}x ATR from EMA50)")

    return None  # Passed pre-screening


# In-memory decision cache keyed by (symbol, side, sweep_bar) so repeat scans
# on a stable setup don't re-pay the LLM round-trip. Bounded to last 64 entries.
_DECISION_CACHE: dict[tuple[str, str, int], dict] = {}
_DECISION_CACHE_ORDER: list[tuple[str, str, int]] = []
_DECISION_CACHE_MAX = 64


def _cache_get(key: tuple[str, str, int]) -> dict | None:
    entry = _DECISION_CACHE.get(key)
    if entry is None:
        return None
    # Return a copy with a _cached marker so the caller can log it.
    out = dict(entry)
    out["_cached"] = True
    return out


def _cache_put(key: tuple[str, str, int], value: dict) -> None:
    if key in _DECISION_CACHE:
        return
    _DECISION_CACHE[key] = value
    _DECISION_CACHE_ORDER.append(key)
    while len(_DECISION_CACHE_ORDER) > _DECISION_CACHE_MAX:
        old = _DECISION_CACHE_ORDER.pop(0)
        _DECISION_CACHE.pop(old, None)


def clear_decision_cache() -> None:
    """Test helper — wipe the cache."""
    _DECISION_CACHE.clear()
    _DECISION_CACHE_ORDER.clear()


# --- LLM daily budget guard: cap real (non-cached) API calls per UTC day ---
# Fail-closed: over cap -> prescreen-style REFUSE, never an API call.
# Set LLM_DAILY_CALL_CAP=0 to disable. Guard lives AFTER cache lookup, so
# cached hits are free.
# Counter is PERSISTED to risk_state.json (storage/db/risk_state.json) on
# every increment, so a restart / crash does NOT reset the day's budget.
# Multi-instance note: read-modify-write is best-effort cross-process
# (state_store lock is per-process); the cap still bounds each copy.
_BUDGET_DEFAULT = 1000
_budget_state = {"date": None, "count": 0}
_BUDGET_STATE_PATH = os.path.join("storage", "db", "risk_state.json")


def _budget_check_and_count() -> bool:
    cap = int(os.getenv("LLM_DAILY_CALL_CAP", str(_BUDGET_DEFAULT)))
    if cap <= 0:
        return True
    today = time.strftime("%Y-%m-%d", time.gmtime())

    from services.risk.state_store import read_state, write_state
    persisted = read_state(_BUDGET_STATE_PATH, {}).get("llm_budget", {})
    # Adopt persisted counter unless the in-memory one is ahead (same-day
    # process already counted higher) or the persisted date is stale.
    if persisted.get("date") == today:
        if int(persisted.get("count", 0)) > _budget_state.get("count", 0) \
                or _budget_state.get("date") != today:
            _budget_state["date"] = today
            _budget_state["count"] = int(persisted.get("count", 0))
    if _budget_state["date"] != today:
        _budget_state["date"] = today
        _budget_state["count"] = 0

    if _budget_state["count"] >= cap:
        return False
    _budget_state["count"] += 1
    _persist_budget(_BUDGET_STATE_PATH, today, _budget_state["count"])
    return True


def _persist_budget(path: str, date: str, count: int) -> None:
    try:
        from services.risk.state_store import read_state, write_state
        state = read_state(path, {})
        state["llm_budget"] = {"date": date, "count": count}
        write_state(path, state)
    except Exception:
        # Budget bookkeeping must never break trading; in-memory still holds.
        pass


def call_llm(evidence: dict, side: str | None = None,
             cache_key: tuple[str, str, int] | None = None) -> dict | None:
    """Call LLM (OpenAI or Ollama) for trade decision. Returns None on failure.

    ``side`` is the engine's chosen direction. Pass it through so prescreen
    counter-trend checks judge the engine's actual side instead of re-deriving
    it from the sweep type.

    ``cache_key`` is an optional ``(symbol, side, sweep_bar)`` tuple. When
    provided, the cached decision is returned on subsequent calls until the
    sweep bar advances — this avoids paying OpenAI for the same setup once
    per scan cycle. Prescreen-REFUSE results are NOT cached (cheap to recompute).
    """
    # Fast pre-screening in Python — catches counter-trend, RSI extremes, no structure
    prescreen = prescreen_evidence(evidence, side=side)
    if prescreen is not None:
        logger.info("Prescreen REFUSE: %s", prescreen.get("justification", ""))
        return prescreen

    # Cache lookup before issuing the LLM call.
    if cache_key is not None:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    llm_enabled = os.getenv("LLM_ENABLED", os.getenv("OLLAMA_ENABLED", "true"))
    if llm_enabled != "true":
        return None

    if not _budget_check_and_count():
        logger.warning("LLM daily cap reached - refusing without API call")
        return {"decision": "REFUSE", "confidence": 0.99,
                "justification": "LLM_DAILY_CALL_CAP reached"}

    user_msg = _build_user_message(evidence, side=side)
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": user_msg},
    ]

    provider = _get_provider()

    for attempt in range(2):
        try:
            if provider == "openai":
                content = _call_openai_api(messages)
            else:
                content = _call_ollama_api(messages)

            if not content:
                logger.error("LLM returned empty response")
                return None

            raw = _extract_json(content)
            if raw is None:
                logger.error("LLM no parseable JSON — content: %s", content[:300])
                return None

            # Validate with Pydantic
            decision = LLMDecision(**raw)
            result = decision.model_dump()

            if os.getenv("LLM_DECISION_DEBUG", "false") == "true":
                log_decision_debug({
                    "provider": provider,
                    "raw_content": content[:500],
                    "parsed": result,
                })

            logger.info("LLM [%s]: %s conf=%.2f for %s — %s",
                        provider, result["decision"], result["confidence"],
                        evidence.get("symbol", "?"), result.get("justification", "")[:80])
            if cache_key is not None:
                _cache_put(cache_key, result)
            return result

        except httpx.ConnectError:
            logger.warning("LLM [%s] not reachable (attempt %d)", provider, attempt + 1)
            if attempt == 0:
                time.sleep(3)
                continue
            return None
        except httpx.TimeoutException:
            logger.warning("LLM [%s] timed out (attempt %d)", provider, attempt + 1)
            if attempt == 0:
                time.sleep(3)
                continue
            return None
        except httpx.HTTPStatusError as e:
            logger.error("LLM [%s] HTTP error: %s", provider, e)
            return None
        except (json.JSONDecodeError, ValidationError) as e:
            logger.error("LLM output invalid: %s", e)
            return None
        except Exception as e:
            logger.exception("Unexpected LLM error: %s", e)
            return None

    return None


# Backward compatibility alias
call_ollama = call_llm


def is_trade_valid(decision: dict | None, side: str, sweep_bias: str | None,
                   htf_bias: str | None, symbol: str = "?",
                   skip_htf: bool = False,
                   min_conf_override: float | None = None) -> tuple[bool, str]:
    """Validate trade against LLM decision + hard blocks.

    ``skip_htf``: when True (soft-mode), the HTF bias block here is skipped
    because the engine has already enforced an alignment-aware score gate.

    ``min_conf_override``: when set, replaces ``MIN_LLM_CONFIDENCE`` for this
    call — used to require higher confidence on HTF-opposed setups.
    """
    # Sweep direction hard block — NEVER override
    if sweep_bias:
        if sweep_bias == "SWEEP_LOW" and side != "BUY":
            return False, "SWEEP_DIRECTION_BLOCK (SWEEP_LOW requires BUY)"
        if sweep_bias == "SWEEP_HIGH" and side != "SELL":
            return False, "SWEEP_DIRECTION_BLOCK (SWEEP_HIGH requires SELL)"

    # HTF bias hard block (skipped in soft mode — engine handled it via score gate).
    if htf_bias and not skip_htf:
        if htf_bias == "BEARISH" and side == "BUY":
            return False, "HTF_BIAS_BLOCK (H4 BEARISH blocks BUY)"
        if htf_bias == "BULLISH" and side == "SELL":
            return False, "HTF_BIAS_BLOCK (H4 BULLISH blocks SELL)"

    # LLM is required — block trade if unavailable
    if decision is None:
        from services.notifications.telegram import notify
        provider = _get_provider()
        notify(f"🚨 <b>ALERT: LLM UNAVAILABLE</b>\n{symbol} {side} setup blocked — {provider} not responding")
        return False, "LLM_UNAVAILABLE (trade blocked)"

    # LLM refused
    if decision.get("decision") == "REFUSE":
        return False, f"LLM_REFUSE: {decision.get('justification', 'no reason')}"

    # LLM allowed but low confidence
    if min_conf_override is not None:
        min_conf = float(min_conf_override)
    else:
        min_conf = float(os.getenv("MIN_LLM_CONFIDENCE", "0.70"))
    conf = decision.get("confidence", 0.0)
    if conf < min_conf:
        return False, f"LOW_CONFIDENCE ({conf:.2f} < {min_conf})"

    return True, "VALID"
