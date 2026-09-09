"""Kalshi-first opportunity evaluation via xAI + Futures Wheel context."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from backend.app.kalshi.market_filter import MarketFilter
from backend.app.workers.edge_engine import _liquidity, _mid_prob, _spread

logger = logging.getLogger(__name__)

EVAL_SYSTEM = """You are NexusPMT opportunity desk evaluating Kalshi prediction markets.
Given a Kalshi market/event (often multiple outcome contracts), WorldMap intel, and Futures Wheel nodes:
1) Estimate true probability for the best actionable contract (YES or NO).
2) Compare to market mid price to find edge / value of interest.
3) Decide ENTER or SKIP.
Rules:
- Fundamentals only (economics, politics, geopolitics, energy, climate, tech, crypto_macro, trade). Never sports/entertainment.
- Be skeptical but not paralyzed. Sports/entertainment are out of scope; everything else is fair game.
- Do NOT skip solely because a market is weather or far-dated if edge/VOI is real.
- Reply JSON only, no markdown:
{"verdict":"ENTER"|"SKIP","side":"yes"|"no","ticker":"...","model_prob":0.0-1.0,"confidence":0.0-1.0,"value_of_interest":0.0-1.0,"strength":"strong"|"weak","reason":"...","wheel_contribution":"..."}
- value_of_interest is attractiveness (0-1). Prefer ENTER when strength=strong, VOI clears the desk threshold, and edge is meaningful.
"""


class OpportunityVerdict(BaseModel):
    verdict: Literal["ENTER", "SKIP"]
    side: Literal["yes", "no"] = "yes"
    ticker: str
    model_prob: float = Field(ge=0.0, le=1.0, default=0.5)
    market_prob: float = Field(ge=0.0, le=1.0, default=0.5)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    value_of_interest: float = Field(ge=0.0, le=1.0, default=0.0)
    strength: Literal["strong", "weak"] = "weak"
    reason: str = ""
    wheel_contribution: str = ""
    category: str | None = None
    title: str | None = None
    edge: float = 0.0

    @field_validator("verdict", mode="before")
    @classmethod
    def verdict_norm(cls, v: Any) -> str:
        s = str(v or "SKIP").strip().upper()
        return "ENTER" if s == "ENTER" else "SKIP"

    @field_validator("side", mode="before")
    @classmethod
    def side_norm(cls, v: Any) -> str:
        s = str(v or "yes").strip().lower()
        return "no" if s == "no" else "yes"

    @field_validator("strength", mode="before")
    @classmethod
    def strength_norm(cls, v: Any) -> str:
        s = str(v or "weak").strip().lower()
        return "strong" if s == "strong" else "weak"


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no json object")


def collect_candidate_markets(
    markets: list[dict[str, Any]],
    series_by: dict[str, dict[str, Any]],
    market_filter: MarketFilter,
    *,
    max_spread: float,
    min_liquidity: float,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Pick liquid fundamental open markets as evaluation candidates."""
    out: list[dict[str, Any]] = []
    for market in markets:
        ticker = str(market.get("ticker") or "")
        # Skip multivariate combo markets — noisy and usually empty books
        if "MVE" in ticker.upper() or market.get("mve_collection_ticker"):
            continue
        series = series_by.get(str(market.get("series_ticker") or "")) or series_by.get(
            str(market.get("event_ticker") or "").rsplit("-", 1)[0]
        )
        # Prefer markets we can attribute to an allowlisted series
        if series is None and series_by:
            continue
        if not market_filter.allow_market(market, series):
            continue
        mid = _mid_prob(market)
        if mid is None or mid <= 0.02 or mid >= 0.98:
            continue
        spread = _spread(market)
        # Empty book often reports spread 0 with 0/0 — already excluded by mid
        if spread is not None and spread > max_spread:
            continue
        liq = _liquidity(market)
        if min_liquidity > 0 and liq < min_liquidity:
            continue
        out.append(
            {
                "ticker": market.get("ticker"),
                "event_ticker": market.get("event_ticker"),
                "series_ticker": market.get("series_ticker") or (series or {}).get("ticker"),
                "title": market.get("title") or market.get("yes_sub_title"),
                "yes_sub_title": market.get("yes_sub_title"),
                "no_sub_title": market.get("no_sub_title"),
                "category": (series or {}).get("category") or market.get("category"),
                "market_prob": mid,
                "spread": spread,
                "liquidity": liq,
                "raw": market,
            }
        )
    # Prefer mid-range probs (more interesting)
    out.sort(key=lambda m: abs(float(m["market_prob"]) - 0.5))
    return out[:limit]


class OpportunityEvaluator:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        enter_voi_threshold: float = 0.65,
    ) -> None:
        self.model = model
        self.enter_voi_threshold = enter_voi_threshold
        self._client = AsyncOpenAI(api_key=api_key or "missing", base_url=base_url)

    async def evaluate(
        self,
        candidate: dict[str, Any],
        *,
        siblings: list[dict[str, Any]],
        intel_snippets: list[str],
        wheel_nodes: list[dict[str, Any]],
    ) -> OpportunityVerdict:
        market_prob = float(candidate.get("market_prob") or 0.5)
        ticker = str(candidate.get("ticker") or "")
        payload = {
            "market": {
                "ticker": ticker,
                "title": candidate.get("title"),
                "category": candidate.get("category"),
                "market_prob": market_prob,
                "yes": candidate.get("yes_sub_title"),
                "no": candidate.get("no_sub_title"),
            },
            "related_outcomes": [
                {
                    "ticker": s.get("ticker"),
                    "title": s.get("title"),
                    "market_prob": s.get("market_prob"),
                }
                for s in siblings[:6]
            ],
            "worldmap_intel": intel_snippets[:25],
            "futures_wheel": wheel_nodes[:12],
        }

        if not self._client.api_key or self._client.api_key == "missing":
            return self._heuristic(candidate, market_prob)

        try:
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": EVAL_SYSTEM},
                        {"role": "user", "content": json.dumps(payload)},
                    ],
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )
            except Exception:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": EVAL_SYSTEM},
                        {"role": "user", "content": json.dumps(payload)},
                    ],
                    temperature=0.2,
                )
            raw = resp.choices[0].message.content or "{}"
            data = _extract_json(raw)
            data.setdefault("ticker", ticker)
            data["market_prob"] = market_prob
            model_prob = float(data.get("model_prob") or market_prob)
            side = str(data.get("side") or "yes").lower()
            edge = (model_prob - market_prob) if side == "yes" else ((1.0 - model_prob) - (1.0 - market_prob))
            # If side=no, edge vs no price
            if side == "no":
                no_mkt = 1.0 - market_prob
                no_model = 1.0 - model_prob
                edge = no_model - no_mkt
            data["edge"] = round(edge, 4)
            data["category"] = candidate.get("category")
            data["title"] = candidate.get("title")
            verdict = OpportunityVerdict.model_validate(data)
            # Enforce gates
            if (
                verdict.verdict == "ENTER"
                and (
                    verdict.strength != "strong"
                    or verdict.value_of_interest < self.enter_voi_threshold
                    or abs(verdict.edge) < 0.03
                )
            ):
                verdict.verdict = "SKIP"
                verdict.strength = "weak"
                verdict.reason = (
                    (verdict.reason or "")
                    + " | downgraded: need strong + VOI>="
                    + str(self.enter_voi_threshold)
                    + " + edge"
                ).strip(" |")
            return verdict
        except Exception:
            logger.exception("opportunity eval failed for %s", ticker)
            return self._heuristic(candidate, market_prob)

    def _heuristic(self, candidate: dict[str, Any], market_prob: float) -> OpportunityVerdict:
        # Conservative offline fallback — almost always SKIP unless extreme mid
        voi = abs(market_prob - 0.5) * 0.4
        return OpportunityVerdict(
            verdict="SKIP",
            side="yes",
            ticker=str(candidate.get("ticker") or ""),
            model_prob=market_prob,
            market_prob=market_prob,
            confidence=0.2,
            value_of_interest=round(voi, 3),
            strength="weak",
            reason="Heuristic fallback only (no LLM) — skipping until xAI eval available",
            wheel_contribution="none",
            category=candidate.get("category"),
            title=candidate.get("title"),
            edge=0.0,
        )
