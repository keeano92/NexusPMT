"""Futures Wheel builder via SpaceXAI (xAI) with fundamentals-only domains."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from backend.app.kalshi.market_filter import FUNDAMENTAL_DOMAINS, MarketFilter

logger = logging.getLogger(__name__)

WHEEL_SYSTEM = """You are NexusPMT Futures Wheel engine for a professional macro/intelligence desk.
Build 1st, 2nd, and 3rd-order consequences from the intel provided.
Rules:
- Fundamentals only domains: geopolitics, economics, energy, climate, tech, crypto_macro, politics, trade.
- NEVER produce sports, entertainment, or gambling-odds scenarios.
- Prefer causally grounded, settleable claims useful for prediction markets.
- Reply with a single JSON object only. No markdown fences. No commentary.
- Required shape:
{"nodes":[{"order":1,"title":"...","domain":"economics","direction":"up","confidence":0.6,"kalshi_keywords":["fed","rates"],"evidence_refs":["..."]}]}
- Include at least 6 nodes: two for order 1, two for order 2, two for order 3.
"""

DOMAIN_ALIASES = {
    "economy": "economics",
    "economic": "economics",
    "macro": "economics",
    "finance": "economics",
    "financial": "economics",
    "markets": "economics",
    "market": "economics",
    "geo": "geopolitics",
    "geopolitical": "geopolitics",
    "conflict": "geopolitics",
    "military": "geopolitics",
    "oil": "energy",
    "gas": "energy",
    "power": "energy",
    "weather": "climate",
    "environment": "climate",
    "ai": "tech",
    "technology": "tech",
    "crypto": "crypto_macro",
    "bitcoin": "crypto_macro",
    "blockchain": "crypto_macro",
    "election": "politics",
    "policy": "politics",
    "government": "politics",
    "tariff": "trade",
    "tariffs": "trade",
    "commerce": "trade",
}


class WheelNode(BaseModel):
    order: int = Field(ge=1, le=3)
    title: str
    domain: str
    direction: str = "uncertain"
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    kalshi_keywords: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("order", mode="before")
    @classmethod
    def order_int(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def confidence_float(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return float(v.strip())
            except ValueError:
                return 0.5
        if isinstance(v, (int, float)) and v > 1:
            return min(1.0, float(v) / 100.0)
        return v

    @field_validator("domain", mode="before")
    @classmethod
    def domain_ok(cls, v: Any) -> str:
        raw = str(v or "").strip().lower().replace(" ", "_").replace("-", "_")
        d = DOMAIN_ALIASES.get(raw, raw)
        if d not in FUNDAMENTAL_DOMAINS:
            # last-chance soft map by substring
            for key, mapped in DOMAIN_ALIASES.items():
                if key in raw:
                    d = mapped
                    break
        if d not in FUNDAMENTAL_DOMAINS:
            raise ValueError(f"non-fundamental domain: {v}")
        return d

    @field_validator("direction", mode="before")
    @classmethod
    def direction_norm(cls, v: Any) -> str:
        s = str(v or "uncertain").strip().lower()
        if s in {"up", "higher", "bullish", "increase", "rising"}:
            return "up"
        if s in {"down", "lower", "bearish", "decrease", "falling"}:
            return "down"
        return "uncertain"

    @field_validator("kalshi_keywords", "evidence_refs", mode="before")
    @classmethod
    def listify(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [p.strip() for p in re.split(r"[,|;]", v) if p.strip()]
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        return []


class FuturesWheelResult(BaseModel):
    query: str
    nodes: list[WheelNode]
    raw_model: str | None = None


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model output")

    # Strip markdown fences if present
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    # Prefer object with nodes
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            repaired = _repair_json(candidate)
            data = json.loads(repaired)
            if isinstance(data, dict):
                return data

    # Bare array of nodes
    a0 = text.find("[")
    a1 = text.rfind("]")
    if a0 >= 0 and a1 > a0:
        arr = json.loads(text[a0 : a1 + 1])
        if isinstance(arr, list):
            return {"nodes": arr}

    raise ValueError("no JSON object/array found in model output")


def _repair_json(text: str) -> str:
    """Best-effort repair for truncated / trailing-comma JSON."""
    t = text.strip()
    # Remove trailing commas before } or ]
    t = re.sub(r",\s*([}\]])", r"\1", t)
    # Balance braces/brackets if truncated
    opens = t.count("{") - t.count("}")
    opens_arr = t.count("[") - t.count("]")
    if opens_arr > 0:
        t += "]" * opens_arr
    if opens > 0:
        t += "}" * opens
    return t


def _coerce_nodes(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        if isinstance(raw.get("nodes"), list):
            return [n for n in raw["nodes"] if isinstance(n, dict)]
        # single node object
        if "title" in raw and "order" in raw:
            return [raw]
        # nested scenarios style
        for key in ("scenarios", "consequences", "items", "wheel"):
            if isinstance(raw.get(key), list):
                return [n for n in raw[key] if isinstance(n, dict)]
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, dict)]
    return []


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
            "required_domains": sorted(FUNDAMENTAL_DOMAINS),
        }
        if not self._client.api_key or self._client.api_key == "missing":
            return self._fallback(query, intel_snippets)

        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": WHEEL_SYSTEM},
                    {"role": "user", "content": json.dumps(user)},
                ],
                "temperature": 0.2,
            }
            # Prefer strict JSON when the provider supports it
            try:
                resp = await self._client.chat.completions.create(
                    **kwargs,
                    response_format={"type": "json_object"},
                )
            except Exception:
                resp = await self._client.chat.completions.create(**kwargs)

            content = resp.choices[0].message.content or "{}"
            try:
                data = _extract_json(content)
            except Exception as exc:
                logger.warning("wheel JSON parse failed (%s); using fallback. raw[:240]=%r", exc, content[:240])
                return self._fallback(query, intel_snippets)

            nodes_raw = _coerce_nodes(data)
            nodes: list[WheelNode] = []
            for n in nodes_raw:
                try:
                    # Normalize alternate field names from models
                    if "order" not in n and "level" in n:
                        n = {**n, "order": n.get("level")}
                    if "title" not in n and "text" in n:
                        n = {**n, "title": n.get("text")}
                    if "title" not in n and "name" in n:
                        n = {**n, "title": n.get("name")}
                    node = WheelNode.model_validate(n)
                    if self.market_filter.allow_wheel_domain(node.domain):
                        nodes.append(node)
                except Exception as exc:  # noqa: BLE001
                    logger.info("drop wheel node: %s | raw=%s", exc, n)

            if not nodes:
                logger.warning("wheel produced 0 valid nodes; using fallback")
                return self._fallback(query, intel_snippets)

            return FuturesWheelResult(query=query, nodes=nodes, raw_model=self.model)
        except Exception:
            logger.exception("Futures wheel LLM failed; using fallback")
            return self._fallback(query, intel_snippets)

    def _fallback(self, query: str, intel_snippets: list[str]) -> FuturesWheelResult:
        hint = intel_snippets[0][:120] if intel_snippets else query
        hint2 = intel_snippets[1][:120] if len(intel_snippets) > 1 else ""
        nodes = [
            WheelNode(
                order=1,
                title=f"Direct macro response to: {query[:80]}",
                domain="economics",
                direction="uncertain",
                confidence=0.45,
                kalshi_keywords=["fed", "rates", "inflation"],
                evidence_refs=[hint],
            ),
            WheelNode(
                order=1,
                title="Near-term geopolitical risk premium reprice",
                domain="geopolitics",
                direction="up",
                confidence=0.4,
                kalshi_keywords=["conflict", "sanctions", "war"],
                evidence_refs=[hint2] if hint2 else [hint],
            ),
            WheelNode(
                order=2,
                title="Secondary market volatility and risk premia shift",
                domain="economics",
                direction="up",
                confidence=0.38,
                kalshi_keywords=["recession", "volatility", "credit"],
                evidence_refs=[],
            ),
            WheelNode(
                order=2,
                title="Energy / commodity stress transmission into CPI prints",
                domain="energy",
                direction="up",
                confidence=0.36,
                kalshi_keywords=["oil", "energy", "cpi"],
                evidence_refs=[],
            ),
            WheelNode(
                order=3,
                title="Longer-horizon policy and trade regime adjustment",
                domain="politics",
                direction="uncertain",
                confidence=0.32,
                kalshi_keywords=["election", "tariffs", "legislation"],
                evidence_refs=[],
            ),
            WheelNode(
                order=3,
                title="Structural crypto-macro liquidity / ETF flow consequences",
                domain="crypto_macro",
                direction="uncertain",
                confidence=0.3,
                kalshi_keywords=["bitcoin", "etf", "crypto"],
                evidence_refs=[],
            ),
        ]
        return FuturesWheelResult(query=query, nodes=nodes, raw_model="fallback")
