"""Cross-category opportunity ranker — higher probability entries, no LLM required."""

from __future__ import annotations

from typing import Any


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def score_candidate(
    cand: dict[str, Any],
    *,
    min_net_edge: float = 0.06,
    intel_bias: str | None = None,
    intel_confidence: float = 0.0,
) -> dict[str, Any]:
    """Score a candidate and propose side + model edge after half-spread."""
    mid = _f(cand.get("market_prob"), 0.5)
    spread = _f(cand.get("spread"), 0.08)
    half = max(0.0, spread / 2.0)

    # Fair value prior: mild mean-reversion toward 0.5, stronger when intel agrees
    fair = 0.5
    if intel_bias == "yes":
        fair = min(0.72, 0.5 + 0.12 * max(0.2, intel_confidence))
    elif intel_bias == "no":
        fair = max(0.28, 0.5 - 0.12 * max(0.2, intel_confidence))

    edge_yes = fair - mid
    # Net edge after crossing half-spread
    if edge_yes >= 0:
        side = "yes"
        net = edge_yes - half
    else:
        side = "no"
        net = (-edge_yes) - half

    # Penalties / bonuses
    score = net * 100.0
    if mid < 0.15 or mid > 0.85:
        score -= 8.0  # lottery / locked
    if 0.47 <= mid <= 0.53:
        score -= 6.0  # coin flip
    if spread > 0.10:
        score -= (spread - 0.10) * 80.0
    if cand.get("micro_horizon"):
        score -= 1.5  # slight penalty vs slower markets unless edge huge
    if intel_bias and (
        (intel_bias == "yes" and side == "yes") or (intel_bias == "no" and side == "no")
    ):
        score += 3.0 * max(0.2, intel_confidence)

    ok = net >= min_net_edge and score > 0
    return {
        "ticker": cand.get("ticker"),
        "side": side,
        "market_prob": mid,
        "fair": round(fair, 4),
        "gross_edge": round(abs(edge_yes), 4),
        "net_edge": round(net, 4),
        "score": round(score, 3),
        "enter": ok,
        "spread": spread,
        "exchange_index": cand.get("exchange_index"),
        "category": cand.get("category"),
        "title": cand.get("title"),
        "micro_horizon": bool(cand.get("micro_horizon")),
        "event_ticker": cand.get("event_ticker"),
        "series_ticker": cand.get("series_ticker"),
        "close_time": cand.get("close_time"),
        "liquidity": cand.get("liquidity"),
    }


def rank_candidates(
    candidates: list[dict[str, Any]],
    *,
    min_net_edge: float = 0.06,
    intel_by_series: dict[str, dict[str, Any]] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    intel_by_series = intel_by_series or {}
    scored: list[dict[str, Any]] = []
    for c in candidates:
        series = str(c.get("series_ticker") or "")
        brief = intel_by_series.get(series) or {}
        row = score_candidate(
            c,
            min_net_edge=min_net_edge,
            intel_bias=brief.get("bias"),
            intel_confidence=_f(brief.get("confidence"), 0.0),
        )
        if row["enter"]:
            scored.append(row)
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]
