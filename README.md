# MT5 FTMO Trader

> An autonomous, FTMO-compliant MetaTrader 5 trading bot that combines fast rule-based Smart Money Concepts (SMC) detection with an optional LLM veto, hard risk circuit breakers, and real-time Telegram monitoring.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![MetaTrader5](https://img.shields.io/badge/MetaTrader-5-0A0A0A)
![OpenAI](https://img.shields.io/badge/LLM-gpt--4o--mini-412991?logo=openai&logoColor=white)
![Ollama](https://img.shields.io/badge/LLM-Ollama%20qwen3:4b-000000)
![Tests](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)

A production-grade algorithmic trading system for MetaTrader 5. A sub-millisecond Python engine detects SMC structure (BOS, CHOCH, sweeps, order blocks, FVGs), filters by HTF bias / regime / session kill-zones, and scores confluence 0–5. Setups scoring ≥3 are passed to an **optional LLM veto** (OpenAI `gpt-4o-mini` or local Ollama `qwen3:4b`) that can only *refuse* or *confirm* — never originate — trades. Approved trades go through an FTMO-calibrated risk manager and a multi-target exit ladder.

> ⚠️ **Disclaimer:** Trading carries substantial risk of loss. This is provided for research/educational purposes, is **not** financial advice, and ships with `DRY_RUN=true`. Validate thoroughly on a demo account before risking capital.

## Features

- 📐 **SMC detection** — swing structure, BOS, CHOCH, liquidity sweeps (with strength metrics), order blocks, fair value gaps.
- 🎯 **Confluence scoring** — 5 weighted factors; only setups scoring ≥3 reach the LLM.
- 🤖 **LLM veto** — OpenAI or local Ollama; confirms/refuses pre-qualified signals (rule-prefilter + LLM-veto pattern). If the LLM is unreachable, the trade is blocked (safety first).
- 🛡️ **FTMO circuit breakers** — 2% daily / 6% total drawdown hard cutoffs (tracking floating equity), quarter-Kelly 0.5% sizing, max-2 concurrent, 3-consecutive-loss halt.
- 📈 **Multi-target exit ladder** — TP1 (50% @ 1R), breakeven, TP2 (30% @ structural), ATR runner trail; score-tiered RR floor capped at 5R.
- 🕒 **Session / regime / news filters** — London & NY kill-zones, trending-only regime, ForexFactory news blackout (no API key).
- 📲 **Telegram monitoring** — every trade, fill, SL, halt and daily summary pushed to your phone.
- ✅ **Tested** — pytest suite with MT5 and Ollama fully mocked (no DLL/GPU needed to run tests).

## Tech Stack

| Area | Technologies |
|---|---|
| **Language** | Python 3.12 |
| **Broker** | MetaTrader5 SDK |
| **Data** | Pandas, NumPy |
| **LLM** | OpenAI REST or Ollama REST (via `httpx` — no `openai` pkg), Pydantic schemas |
| **Notify** | Telegram Bot API |
| **Testing** | pytest, respx (HTTP mocking) |

## Architecture

```
MT5 M15 bars
   ↓  FAST PYTHON ENGINE (<1ms)
core/detection   SMC: structure, liquidity, orderblocks, fvg, regime, trendlines
core/indicators  EMA 9/21/50/200, RSI14, ATR14, OBV
core/scoring     confluence (0–5, ≥3 to proceed)
core/evidence    compact JSON payload for the LLM
core/decision    Pydantic LLMDecision / TradePlan schemas
   ↓  OPTIONAL LLM VETO (1–5s)
services/decision        OpenAI / Ollama decider
services/risk            FTMO sizing + circuit breakers + atomic state
services/trade_management breakeven / partial TP / invalidation
services/mt5_exec        FOK→IOC→RETURN fill fallback + post-fill verify
services/notifications   Telegram · services/logging  JSONL audit
services/calendar        ForexFactory news filter · services/analytics  FTMO tracker
```

## Quick start

**Prerequisites:** Python 3.12, MetaTrader 5 terminal + account, (optional) Ollama for local LLM, OpenAI API key, Telegram bot token.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

copy .env.example .env            # then fill in MT5 / OpenAI / Telegram secrets
python scripts/check_health.py    # verifies all 8 components
python scripts/smoke_test.py      # runs detection on live MT5 data
pytest tests/ -q                  # run the test suite

python main.py                    # start the bot (DRY_RUN=true by default)
```

Flip `DRY_RUN=false` in `.env` **only** after the health check passes and you've validated behaviour on a demo account.

## Secrets

`.env` is **gitignored** and never committed — only `.env.example` (with placeholders) is tracked. Provide your own MT5 credentials, OpenAI key and Telegram token.

## License

No license file is included yet. Add one before any public/open-source use.
