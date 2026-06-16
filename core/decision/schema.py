"""Pydantic schemas for LLM decisions and trade plans."""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class TradePlan(BaseModel):
    symbol: str = ""
    side: str = ""  # BUY or SELL
    volume: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp1_pct: Optional[int] = None
    tp2_pct: Optional[int] = None


class LLMDecision(BaseModel):
    decision: str = Field(..., pattern="^(ALLOW|REFUSE)$")
    confidence: float = Field(..., ge=0.0, le=1.0)
    justification: str = ""
    violations: list[str] = Field(default_factory=list)
    conditions_met: list[str] = Field(default_factory=list)
    conditions_failed: list[str] = Field(default_factory=list)
    strategy_version: str = "v1"
    trade: Optional[TradePlan] = None
