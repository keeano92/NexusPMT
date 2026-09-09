"""TDD: multi-shard Kalshi balances + funded-shard market filter."""

from backend.app.kalshi.market_filter import MarketFilter
from backend.app.kalshi.portfolio import (
    funded_exchange_indexes,
    normalize_balance,
    shard_cash_cents,
)
from backend.app.workers.opportunity_eval import collect_candidate_markets


def test_normalize_balance_parses_breakdown_to_cents():
    out = normalize_balance(
        {
            "balance": 1061,
            "portfolio_value": 1,
            "balance_breakdown": [
                {"exchange_index": 0, "balance": "0.0000"},
                {"exchange_index": 1, "balance": "0.0000"},
                {"exchange_index": 2, "balance": "10.6182"},
                {"exchange_index": 3, "balance": "0.0000"},
            ],
        }
    )
    assert out["cash_cents"] == 1061
    assert out["equity_cents"] == 1062
    assert out["shard_balances_cents"] == {0: 0, 1: 0, 2: 1062, 3: 0}


def test_funded_exchange_indexes_only_positive():
    shards = {0: 0, 1: 0, 2: 1062, 3: 5}
    assert funded_exchange_indexes(shards, min_cents=1) == {2, 3}
    assert funded_exchange_indexes(shards, min_cents=100) == {2}


def test_shard_cash_cents_lookup():
    shards = {2: 1062}
    assert shard_cash_cents(shards, 2) == 1062
    assert shard_cash_cents(shards, 0) == 0
    assert shard_cash_cents(shards, None) == 0


def test_collect_candidates_filters_to_funded_shards():
    mf = MarketFilter.from_settings(["economics", "crypto"], ["sports"], strict=True)
    markets = [
        {
            "ticker": "FED-0",
            "title": "Fed cuts",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.46",
            "volume_fp": "1000",
            "series_ticker": "FED",
            "event_ticker": "FED-EVT",
            "exchange_index": 0,
        },
        {
            "ticker": "BTC-2",
            "title": "BTC above",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.46",
            "volume_fp": "1000",
            "series_ticker": "BTC",
            "event_ticker": "BTC-EVT",
            "exchange_index": 2,
        },
    ]
    series = {
        "FED": {"ticker": "FED", "category": "Economics", "title": "Fed", "tags": []},
        "BTC": {"ticker": "BTC", "category": "Crypto", "title": "BTC", "tags": []},
    }
    out = collect_candidate_markets(
        markets,
        series,
        mf,
        max_spread=0.1,
        min_liquidity=0,
        funded_shards={2},
    )
    assert len(out) == 1
    assert out[0]["ticker"] == "BTC-2"
    assert out[0]["exchange_index"] == 2


def test_collect_candidates_includes_exchange_index_without_filter():
    mf = MarketFilter.from_settings(["economics"], [], strict=True)
    markets = [
        {
            "ticker": "FED-0",
            "title": "Fed cuts",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.46",
            "volume_fp": "1000",
            "series_ticker": "FED",
            "event_ticker": "FED-EVT",
            "exchange_index": 0,
        },
    ]
    series = {"FED": {"ticker": "FED", "category": "Economics", "title": "Fed", "tags": []}}
    out = collect_candidate_markets(markets, series, mf, max_spread=0.1, min_liquidity=0)
    assert out[0]["exchange_index"] == 0
