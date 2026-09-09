"""Cross-reference Futures Wheel nodes against filtered Kalshi markets."""

from __future__ import annotations

from typing import Any

from backend.app.kalshi.market_filter import MarketFilter
from backend.app.kalshi.models import EdgeCandidate
from backend.app.workers.futures_wheel import WheelNode


def _mid_prob(market: dict[str, Any]) -> float | None:
    # Prefer dollar fields; fall back to cent ints /100
    for bid_k, ask_k in (
        ("yes_bid_dollars", "yes_ask_dollars"),
        ("yes_bid", "yes_ask"),
    ):
        bid = market.get(bid_k)
        ask = market.get(ask_k)
        if bid is None or ask is None:
            continue
        try:
            b = float(bid)
            a = float(ask)
        except (TypeError, ValueError):
            continue
        if b > 1.0 or a > 1.0:
            b, a = b / 100.0, a / 100.0
        # Uncrossed / empty book → try last trade
        if a <= 0 and b <= 0:
            continue
        if a < b:
            continue
        if a == 0 and b > 0:
            return b
        if b == 0 and a > 0:
            return a
        return (b + a) / 2.0
    last = market.get("last_price") or market.get("last_price_dollars")
    if last is not None:
        try:
            v = float(last)
            if v <= 0:
                return None
            return v / 100.0 if v > 1.0 else v
        except (TypeError, ValueError):
            return None
    return None


def _spread(market: dict[str, Any]) -> float | None:
    bid = market.get("yes_bid_dollars", market.get("yes_bid"))
    ask = market.get("yes_ask_dollars", market.get("yes_ask"))
    if bid is None or ask is None:
        return None
    try:
        b = float(bid)
        a = float(ask)
    except (TypeError, ValueError):
        return None
    if b > 1.0 or a > 1.0:
        b, a = b / 100.0, a / 100.0
    return max(0.0, a - b)


def _liquidity(market: dict[str, Any]) -> float:
    for key in ("volume_fp", "volume", "open_interest", "yes_bid_size_fp"):
        val = market.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return 0.0


def _text_blob(market: dict[str, Any], series: dict[str, Any] | None) -> str:
    parts = [
        market.get("ticker"),
        market.get("event_ticker"),
        market.get("title"),
        market.get("yes_sub_title"),
        market.get("no_sub_title"),
        (series or {}).get("title"),
        (series or {}).get("category"),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def score_edges(
    nodes: list[WheelNode],
    markets: list[dict[str, Any]],
    series_by_ticker: dict[str, dict[str, Any]],
    market_filter: MarketFilter,
    *,
    min_edge: float,
    max_spread: float,
    min_liquidity: float,
) -> list[EdgeCandidate]:
    keywords: list[tuple[WheelNode, str]] = []
    for node in nodes:
        for kw in node.kalshi_keywords:
            keywords.append((node, kw.lower()))

    out: list[EdgeCandidate] = []
    for market in markets:
        series = series_by_ticker.get(str(market.get("series_ticker") or "")) or series_by_ticker.get(
            str(market.get("event_ticker") or "").rsplit("-", 1)[0]
        )
        if not market_filter.allow_market(market, series):
            continue

        mid = _mid_prob(market)
        if mid is None:
            continue
        spread = _spread(market)
        if spread is not None and spread > max_spread:
            continue
        liq = _liquidity(market)
        if liq < min_liquidity:
            continue

        blob = _text_blob(market, series)
        matched: WheelNode | None = None
        for node, kw in keywords:
            if kw and kw in blob:
                matched = node
                break
        if matched is None:
            # Soft category bridge: economics/politics nodes can match same-category markets
            cat = str((series or {}).get("category") or market.get("category") or "").lower()
            for node in nodes:
                if node.domain in {"economics", "politics", "trade"} and any(
                    x in cat for x in ("econom", "politic", "financ", "election")
                ):
                    matched = node
                    break
        if matched is None:
            continue

        model_prob = float(matched.confidence)
        # Directional nudge: "up" favors yes
        if matched.direction == "up":
            model_prob = min(0.95, model_prob + 0.1)
        elif matched.direction == "down":
            model_prob = max(0.05, 1.0 - model_prob)

        edge_yes = model_prob - mid
        if abs(edge_yes) < min_edge:
            continue

        if edge_yes >= min_edge:
            side = "yes"
            edge = edge_yes
            action = "buy_yes"
        else:
            side = "no"
            edge = -edge_yes
            action = "buy_no"
            model_prob = 1.0 - model_prob

        out.append(
            EdgeCandidate(
                ticker=str(market.get("ticker")),
                event_ticker=market.get("event_ticker"),
                category=(series or {}).get("category") or market.get("category"),
                title=market.get("title") or market.get("yes_sub_title"),
                yes_bid=float(market["yes_bid_dollars"]) if market.get("yes_bid_dollars") is not None else None,
                yes_ask=float(market["yes_ask_dollars"]) if market.get("yes_ask_dollars") is not None else None,
                model_prob=round(model_prob, 4),
                market_prob=round(mid, 4),
                edge=round(edge, 4),
                side=side,  # type: ignore[arg-type]
                liquidity=liq,
                action=action,  # type: ignore[arg-type]
            )
        )

    out.sort(key=lambda c: c.edge, reverse=True)
    return out
