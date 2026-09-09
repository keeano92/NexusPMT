"""Lightweight Kalshi / edge models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EdgeCandidate(BaseModel):
    ticker: str
    event_ticker: str | None = None
    category: str | None = None
    title: str | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    model_prob: float
    market_prob: float
    edge: float
    side: Literal["yes", "no"]
    liquidity: float | None = None
    action: Literal["buy_yes", "buy_no", "skip"] = "skip"


class OrderIntent(BaseModel):
    ticker: str
    side: Literal["yes", "no"]
    count: int = 1
    yes_price: int | None = None  # cents
    client_order_id: str
    mode: Literal["paper", "live"] = "paper"
    edge: float | None = None
    reason: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
