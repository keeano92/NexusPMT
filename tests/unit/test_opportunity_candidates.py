from backend.app.kalshi.market_filter import MarketFilter
from backend.app.workers.opportunity_eval import collect_candidate_markets


def test_collect_candidates_skips_sports():
    mf = MarketFilter.from_settings(["economics", "politics"], ["sports"], strict=True)
    markets = [
        {
            "ticker": "SPORT-1",
            "title": "NBA finals",
            "category": "Sports",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.45",
            "volume_fp": "1000",
            "series_ticker": "SPORT",
        },
        {
            "ticker": "FED-1",
            "title": "Fed cuts rates",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.46",
            "volume_fp": "1000",
            "series_ticker": "FED",
            "event_ticker": "FED-EVT",
        },
    ]
    series = {
        "SPORT": {"ticker": "SPORT", "category": "Sports", "title": "Sports", "tags": ["nba"]},
        "FED": {"ticker": "FED", "category": "Economics", "title": "Fed", "tags": []},
    }
    out = collect_candidate_markets(markets, series, mf, max_spread=0.1, min_liquidity=0)
    assert len(out) == 1
    assert out[0]["ticker"] == "FED-1"
