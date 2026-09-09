"""Active desk: softened ENTER gates + odds heuristic that can trade without LLM."""

import pytest

from backend.app.workers.opportunity_eval import (
    OpportunityEvaluator,
    collect_candidate_markets,
    odds_heuristic_verdict,
)
from backend.app.kalshi.market_filter import MarketFilter


def test_odds_heuristic_fades_mild_favorite():
    """When YES mid ~57¢ with tight spread, fade → ENTER NO (like manual SOL)."""
    cand = {
        "ticker": "KXSOL15M-1",
        "title": "SOL up 15m",
        "category": "Crypto",
        "market_prob": 0.57,
        "spread": 0.04,
        "series_ticker": "KXSOL15M",
        "micro_horizon": True,
    }
    v = odds_heuristic_verdict(cand, enter_voi_threshold=0.28, min_edge=0.03)
    assert v.verdict == "ENTER"
    assert v.side == "no"
    assert v.value_of_interest >= 0.28 or abs(v.edge) >= 0.03


def test_odds_heuristic_follows_strong_favorite():
    cand = {
        "ticker": "KXBTC15M-1",
        "title": "BTC up",
        "category": "Crypto",
        "market_prob": 0.78,
        "spread": 0.03,
        "series_ticker": "KXBTC15M",
        "micro_horizon": True,
    }
    v = odds_heuristic_verdict(cand, enter_voi_threshold=0.28, min_edge=0.03)
    assert v.verdict == "ENTER"
    assert v.side == "yes"


def test_odds_heuristic_skips_wide_spread():
    cand = {
        "ticker": "WIDE-1",
        "market_prob": 0.55,
        "spread": 0.25,
        "series_ticker": "KXSOL15M",
        "micro_horizon": True,
        "category": "Crypto",
    }
    v = odds_heuristic_verdict(cand, enter_voi_threshold=0.28, min_edge=0.03)
    assert v.verdict == "SKIP"


@pytest.mark.asyncio
async def test_evaluate_accepts_weak_enter_when_strong_not_required():
    ev = OpportunityEvaluator(
        api_key="missing",
        base_url="https://api.x.ai/v1",
        model="grok-4.6",
        enter_voi_threshold=0.28,
        require_strong_enter=False,
        min_edge=0.03,
    )
    # Force heuristic path (no key)
    v = await ev.evaluate(
        {
            "ticker": "KXSOL15M-1",
            "title": "SOL",
            "category": "Crypto",
            "market_prob": 0.58,
            "spread": 0.03,
            "series_ticker": "KXSOL15M",
            "micro_horizon": True,
        },
        siblings=[],
        intel_snippets=[],
        wheel_nodes=[],
    )
    assert v.verdict == "ENTER"
    assert "Heuristic fallback only" not in (v.reason or "")


def test_collect_prefers_15m_when_flag_set():
    mf = MarketFilter.from_settings(["crypto"], ["sports"], strict=True)
    markets = [
        {
            "ticker": "BTCMAX-1",
            "title": "BTC max",
            "yes_bid_dollars": "0.44",
            "yes_ask_dollars": "0.48",
            "volume_fp": "100",
            "series_ticker": "KXBTCMAXMON",
            "event_ticker": "E1",
            "exchange_index": 2,
        },
        {
            "ticker": "SOL15M-1",
            "title": "SOL 15m",
            "yes_bid_dollars": "0.48",
            "yes_ask_dollars": "0.52",
            "volume_fp": "50",
            "series_ticker": "KXSOL15M",
            "event_ticker": "E2",
            "exchange_index": 2,
        },
    ]
    series = {
        "KXBTCMAXMON": {"ticker": "KXBTCMAXMON", "category": "Crypto", "title": "m", "tags": []},
        "KXSOL15M": {"ticker": "KXSOL15M", "category": "Crypto", "title": "15", "tags": []},
    }
    out = collect_candidate_markets(
        markets,
        series,
        mf,
        max_spread=0.1,
        min_liquidity=0,
        funded_shards={2},
        prefer_micro=True,
    )
    assert out[0]["ticker"] == "SOL15M-1"
    assert out[0]["micro_horizon"] is True
