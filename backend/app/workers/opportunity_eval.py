"""Kalshi-first opportunity evaluation via xAI + odds heuristics + Futures Wheel."""

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

EVAL_SYSTEM = """You are NexusPMT — an active prediction-market trading desk.
Goal: earn by being correct more often than wrong. Win some, lose some; do NOT freeze.

Given a Kalshi market, WorldMap intel, and Futures Wheel nodes:
1) Estimate true probability for YES.
2) Compare to market mid → pick YES or NO with the better odds.
3) Prefer ENTER over SKIP when there is a usable edge or clear favorite/fade.
4) 15-minute crypto up/down IS in scope for live desk testing — trade it.
5) Missing intel is not a reason to SKIP; use the book + wheel + siblings.
6) Reply JSON only:
{"verdict":"ENTER"|"SKIP","side":"yes"|"no","ticker":"...","model_prob":0.0-1.0,"confidence":0.0-1.0,"value_of_interest":0.0-1.0,"strength":"strong"|"weak","reason":"...","wheel_contribution":"..."}
- value_of_interest = how actionable (0-1). ENTER when VOI or edge is real.
- strength=weak is OK for live testing when side odds are clear.
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


def odds_heuristic_verdict(
    candidate: dict[str, Any],
    *,
    enter_voi_threshold: float = 0.28,
    min_edge: float = 0.03,
    max_spread_for_enter: float = 0.10,
) -> OpportunityVerdict:
    """Tradeable offline / no-LLM path — never always-SKIP.

    Mild favorites (mid 0.55–0.72): fade toward 0.5 (manual SOL-style).
    Strong favorites (mid ≥ 0.72 or ≤ 0.28): follow momentum.
    """
    mid = float(candidate.get("market_prob") or 0.5)
    spread = candidate.get("spread")
    try:
        spread_f = float(spread) if spread is not None else 0.05
    except (TypeError, ValueError):
        spread_f = 0.05
    ticker = str(candidate.get("ticker") or "")

    if mid <= 0.12 or mid >= 0.88:
        return OpportunityVerdict(
            verdict="SKIP",
            side="yes",
            ticker=ticker,
            model_prob=mid,
            market_prob=mid,
            confidence=0.2,
            value_of_interest=0.05,
            strength="weak",
            reason="odds heuristic: mid too extreme",
            category=candidate.get("category"),
            title=candidate.get("title"),
            edge=0.0,
        )
    if spread_f > max_spread_for_enter:
        return OpportunityVerdict(
            verdict="SKIP",
            side="yes",
            ticker=ticker,
            model_prob=mid,
            market_prob=mid,
            confidence=0.2,
            value_of_interest=0.05,
            strength="weak",
            reason=f"odds heuristic: spread {spread_f:.2f} too wide",
            category=candidate.get("category"),
            title=candidate.get("title"),
            edge=0.0,
        )

    dist = mid - 0.5
    if abs(dist) < 0.04:
        return OpportunityVerdict(
            verdict="SKIP",
            side="yes",
            ticker=ticker,
            model_prob=mid,
            market_prob=mid,
            confidence=0.25,
            value_of_interest=0.1,
            strength="weak",
            reason="odds heuristic: coin-flip mid",
            category=candidate.get("category"),
            title=candidate.get("title"),
            edge=0.0,
        )

    # Strong favorite → follow; mild favorite → fade
    if abs(dist) >= 0.22:
        side = "yes" if dist > 0 else "no"
        # slight confidence beyond market
        model = min(0.92, mid + 0.04) if side == "yes" else max(0.08, mid - 0.04)
        strength = "strong"
        style = "follow"
    else:
        side = "no" if dist > 0 else "yes"
        model = 0.50  # mean-reversion fair value
        strength = "weak"
        style = "fade"

    if side == "yes":
        edge = model - mid
    else:
        edge = (1.0 - model) - (1.0 - mid)
    voi = min(0.95, abs(edge) * 2.2 + (0.08 if candidate.get("micro_horizon") else 0.0))
    # Boost VOI for micro so live desk actually fires
    if candidate.get("micro_horizon"):
        voi = max(voi, enter_voi_threshold)

    enter = voi >= enter_voi_threshold or abs(edge) >= min_edge
    return OpportunityVerdict(
        verdict="ENTER" if enter else "SKIP",
        side=side,  # type: ignore[arg-type]
        ticker=ticker,
        model_prob=round(float(model), 4),
        market_prob=mid,
        confidence=0.55 if style == "follow" else 0.45,
        value_of_interest=round(voi, 3),
        strength=strength,  # type: ignore[arg-type]
        reason=f"odds heuristic {style}: mid={mid:.2f} edge={edge:+.3f} voi={voi:.2f}",
        wheel_contribution="none",
        category=candidate.get("category"),
        title=candidate.get("title"),
        edge=round(edge, 4),
    )


def collect_candidate_markets(
    markets: list[dict[str, Any]],
    series_by: dict[str, dict[str, Any]],
    market_filter: MarketFilter,
    *,
    max_spread: float,
    min_liquidity: float,
    limit: int = 12,
    funded_shards: set[int] | None = None,
    prefer_micro: bool = False,
) -> list[dict[str, Any]]:
    """Pick liquid open markets as evaluation candidates.

    When ``funded_shards`` is set, only markets on those exchange shards are kept.
    When ``prefer_micro`` is True, 15m/hourly series are ranked first (live desk).
    """
    out: list[dict[str, Any]] = []
    for market in markets:
        ticker = str(market.get("ticker") or "")
        if "MVE" in ticker.upper() or market.get("mve_collection_ticker"):
            continue
        series = series_by.get(str(market.get("series_ticker") or "")) or series_by.get(
            str(market.get("event_ticker") or "").rsplit("-", 1)[0]
        )
        if series is None and series_by:
            continue
        if not market_filter.allow_market(market, series):
            continue
        ex_raw = market.get("exchange_index")
        try:
            exchange_index = int(ex_raw) if ex_raw is not None else None
        except (TypeError, ValueError):
            exchange_index = None
        if funded_shards is not None:
            if exchange_index is None or exchange_index not in funded_shards:
                continue
        mid = _mid_prob(market)
        if mid is None or mid <= 0.02 or mid >= 0.98:
            continue
        spread = _spread(market)
        if spread is not None and spread > max_spread:
            continue
        liq = _liquidity(market)
        if min_liquidity > 0 and liq < min_liquidity:
            continue
        series_ticker = str(
            market.get("series_ticker") or (series or {}).get("ticker") or ""
        )
        micro = any(tag in series_ticker.upper() for tag in ("15M", "1H", "5M", "10M", "30M"))
        out.append(
            {
                "ticker": market.get("ticker"),
                "event_ticker": market.get("event_ticker"),
                "series_ticker": series_ticker or None,
                "title": market.get("title") or market.get("yes_sub_title"),
                "yes_sub_title": market.get("yes_sub_title"),
                "no_sub_title": market.get("no_sub_title"),
                "category": (series or {}).get("category") or market.get("category"),
                "market_prob": mid,
                "spread": spread,
                "liquidity": liq,
                "exchange_index": exchange_index,
                "micro_horizon": micro,
                "close_time": market.get("close_time") or market.get("expected_expiration_time"),
                "raw": market,
            }
        )
    if prefer_micro:
        # Micro first, then closer to actionable mids (not necessarily 0.5)
        out.sort(
            key=lambda m: (
                0 if m.get("micro_horizon") else 1,
                abs(float(m["market_prob"]) - 0.5),
            )
        )
    else:
        out.sort(
            key=lambda m: (
                1 if m.get("micro_horizon") else 0,
                abs(float(m["market_prob"]) - 0.5),
            )
        )
    return out[:limit]


class OpportunityEvaluator:
    # Process-wide breaker so one 403 stops the burn for the whole session.
    _llm_circuit_open: bool = False
    _llm_circuit_reason: str = ""
    _llm_calls_hour: list[float] = []

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        enter_voi_threshold: float = 0.28,
        require_strong_enter: bool = False,
        min_edge: float = 0.03,
        xai_enabled: bool = False,
        max_calls_per_hour: int = 10,
    ) -> None:
        self.model = model
        self.enter_voi_threshold = enter_voi_threshold
        self.require_strong_enter = require_strong_enter
        self.min_edge = min_edge
        self.xai_enabled = bool(xai_enabled)
        self.max_calls_per_hour = max(0, int(max_calls_per_hour))
        self._client = AsyncOpenAI(api_key=api_key or "missing", base_url=base_url)
        self._llm_disabled_reason: str | None = None

    def _llm_allowed(self) -> bool:
        if not self.xai_enabled:
            return False
        if OpportunityEvaluator._llm_circuit_open:
            return False
        if not self._client.api_key or self._client.api_key == "missing":
            return False
        import time

        now = time.time()
        OpportunityEvaluator._llm_calls_hour = [
            t for t in OpportunityEvaluator._llm_calls_hour if now - t < 3600
        ]
        return len(OpportunityEvaluator._llm_calls_hour) < self.max_calls_per_hour

    async def evaluate(
        self,
        candidate: dict[str, Any],
        *,
        siblings: list[dict[str, Any]],
        intel_snippets: list[str],
        wheel_nodes: list[dict[str, Any]],
        position: dict[str, Any] | None = None,
    ) -> OpportunityVerdict:
        market_prob = float(candidate.get("market_prob") or 0.5)
        ticker = str(candidate.get("ticker") or "")
        payload = {
            "market": {
                "ticker": ticker,
                "title": candidate.get("title"),
                "category": candidate.get("category"),
                "market_prob": market_prob,
                "spread": candidate.get("spread"),
                "yes": candidate.get("yes_sub_title"),
                "no": candidate.get("no_sub_title"),
                "micro_horizon": candidate.get("micro_horizon"),
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
            "open_position": position,
            "instruction": (
                "If open_position is set, recommend hold/stop/flip via side+verdict; "
                "if flat, ENTER the better odds side."
            ),
        }

        if not self._llm_allowed():
            reason = OpportunityEvaluator._llm_circuit_reason or "xai_disabled"
            v = odds_heuristic_verdict(
                candidate,
                enter_voi_threshold=self.enter_voi_threshold,
                min_edge=self.min_edge,
            )
            v.reason = f"{v.reason} ({reason})"
            return v

        try:
            import time

            OpportunityEvaluator._llm_calls_hour.append(time.time())
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": EVAL_SYSTEM},
                        {"role": "user", "content": json.dumps(payload)},
                    ],
                    temperature=0.3,
                    response_format={"type": "json_object"},
                )
            except Exception:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": EVAL_SYSTEM},
                        {"role": "user", "content": json.dumps(payload)},
                    ],
                    temperature=0.3,
                )
            raw = resp.choices[0].message.content or "{}"
            data = _extract_json(raw)
            data.setdefault("ticker", ticker)
            data["market_prob"] = market_prob
            model_prob = float(data.get("model_prob") or market_prob)
            side = str(data.get("side") or "yes").lower()
            if side == "no":
                no_mkt = 1.0 - market_prob
                no_model = 1.0 - model_prob
                edge = no_model - no_mkt
            else:
                edge = model_prob - market_prob
            data["edge"] = round(edge, 4)
            data["category"] = candidate.get("category")
            data["title"] = candidate.get("title")
            verdict = OpportunityVerdict.model_validate(data)
            if verdict.verdict == "ENTER":
                need_strong = self.require_strong_enter and verdict.strength != "strong"
                thin = (
                    verdict.value_of_interest < self.enter_voi_threshold
                    and abs(verdict.edge) < self.min_edge
                )
                if need_strong or thin:
                    verdict.verdict = "SKIP"
                    verdict.strength = "weak"
                    verdict.reason = (
                        (verdict.reason or "")
                        + f" | downgraded: VOI/edge gate (voi>={self.enter_voi_threshold} or edge>={self.min_edge}"
                        + ("; strong required" if self.require_strong_enter else "")
                        + ")"
                    ).strip(" |")
            self._llm_disabled_reason = None
            return verdict
        except Exception as exc:
            msg = str(exc)
            if "403" in msg or "429" in msg or "permission-denied" in msg.lower() or "spending limit" in msg.lower():
                OpportunityEvaluator._llm_circuit_open = True
                OpportunityEvaluator._llm_circuit_reason = f"circuit_open:{type(exc).__name__}"
                logger.error("xAI circuit OPEN — disabling further LLM calls (%s)", exc)
            self._llm_disabled_reason = f"{type(exc).__name__}: {exc}"
            logger.warning("opportunity eval LLM failed for %s: %s", ticker, exc)
            v = odds_heuristic_verdict(
                candidate,
                enter_voi_threshold=self.enter_voi_threshold,
                min_edge=self.min_edge,
            )
            v.reason = f"{v.reason} (LLM unavailable: {type(exc).__name__})"
            return v
