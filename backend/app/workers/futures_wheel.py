"""Futures Wheel builder via SpaceXAI (xAI) with fundamentals-only domains."""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from backend.app.kalshi.market_filter import FUNDAMENTAL_DOMAINS, MarketFilter

logger = logging.getLogger(__name__)

WHEEL_SYSTEM = """You are NexusPMT Futures Wheel engine for a professional macro/intelligence desk.
Build 1st, 2nd, and 3rd-order consequences from the intel provided.
Rules:
- Fundamentals only: geopolitics, economics, energy, climate, tech, crypto_macro, politics, trade.
- NEVER produce sports, entertainment, or gambling-odds scenarios.
- Prefer causally grounded, settleable claims useful for prediction markets.
- Reply with JSON only matching the schema.
"""


class WheelNode(BaseModel):
    order: int = Field(ge=1, le=3)
    title: str
    domain: str
    direction: str = "uncertain"
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    kalshi_keywords: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("domain")
    @classmethod
    def domain_ok(cls, v: str) -> str:
        d = v.strip().lower().replace(" ", "_")
        if d not in FUNDAMENTAL_DOMAINS:
            raise ValueError(f"non-fundamental domain: {v}")
        return d


class FuturesWheelResult(BaseModel):
    query: str
    nodes: list[WheelNode]
    raw_model: str | None = None


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


class FuturesWheelEngine:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        market_filter: MarketFilter,
    ) -> None:
        self.model = model
        self.market_filter = market_filter
        self._client = AsyncOpenAI(api_key=api_key or "missing", base_url=base_url)

    async def build(self, query: str, intel_snippets: list[str]) -> FuturesWheelResult:
        user = {
            "query": query,
            "intel": intel_snippets[:40],
            "schema": {
                "nodes": [
                    {
                        "order": "1|2|3",
                        "title": "string",
                        "domain": sorted(FUNDAMENTAL_DOMAINS),
                        "direction": "up|down|uncertain",
                        "confidence": "0-1",
                        "kalshi_keywords": ["string"],
                        "evidence_refs": ["string"],
                    }
                ]
            },
        }
        if not self._client.api_key or self._client.api_key == "missing":
            return self._fallback(query, intel_snippets)

        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": WHEEL_SYSTEM},
                    {"role": "user", "content": json.dumps(user)},
                ],
                temperature=0.3,
            )
            content = resp.choices[0].message.content or "{}"
            data = _extract_json(content)
            nodes_raw = data.get("nodes") or []
            nodes: list[WheelNode] = []
            for n in nodes_raw:
                try:
                    node = WheelNode.model_validate(n)
                    if self.market_filter.allow_wheel_domain(node.domain):
                        nodes.append(node)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("drop wheel node: %s", exc)
            return FuturesWheelResult(query=query, nodes=nodes, raw_model=self.model)
        except Exception:
            logger.exception("Futures wheel LLM failed; using fallback")
            return self._fallback(query, intel_snippets)

    def _fallback(self, query: str, intel_snippets: list[str]) -> FuturesWheelResult:
        hint = intel_snippets[0][:120] if intel_snippets else query
        nodes = [
            WheelNode(
                order=1,
                title=f"Direct macro response to: {query[:80]}",
                domain="economics",
                direction="uncertain",
                confidence=0.4,
                kalshi_keywords=["fed", "rates", "inflation"],
                evidence_refs=[hint],
            ),
            WheelNode(
                order=2,
                title="Secondary market volatility and risk premia shift",
                domain="economics",
                direction="up",
                confidence=0.35,
                kalshi_keywords=["recession", "volatility"],
                evidence_refs=[],
            ),
            WheelNode(
                order=3,
                title="Longer-horizon policy and trade regime adjustment",
                domain="politics",
                direction="uncertain",
                confidence=0.3,
                kalshi_keywords=["election", "tariffs", "legislation"],
                evidence_refs=[],
            ),
        ]
        return FuturesWheelResult(query=query, nodes=nodes, raw_model="fallback")
