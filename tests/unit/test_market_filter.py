from backend.app.kalshi.market_filter import MarketFilter, is_sports_market


def test_sports_category_blocked():
    assert is_sports_market(category="Sports", title="NBA Finals winner")


def test_economics_allowed():
    assert not is_sports_market(category="Economics", title="Fed cuts rates")


def test_filter_rejects_sports_series():
    mf = MarketFilter.from_settings(
        ["economics", "politics"],
        ["sports", "entertainment"],
        strict=True,
        mode="blocklist",
    )
    assert not mf.allow_series(
        {"ticker": "KXNBA", "category": "Sports", "title": "NBA Champ", "tags": ["nba"]}
    )


def test_filter_allows_non_allowlist_when_blocklist_mode():
    mf = MarketFilter.from_settings(
        ["economics"],  # narrow allowlist ignored in blocklist mode
        ["sports", "entertainment"],
        strict=True,
        mode="blocklist",
    )
    assert mf.allow_series(
        {"ticker": "KXRAIN", "category": "Climate and Weather", "title": "Rain in NYC", "tags": []}
    )
    assert mf.allow_series(
        {"ticker": "KXCOIN", "category": "Companies", "title": "Costco members", "tags": []}
    )


def test_filter_allows_fed_series():
    mf = MarketFilter.from_settings(
        ["economics", "politics"],
        ["sports", "entertainment"],
        strict=True,
        mode="blocklist",
    )
    assert mf.allow_series(
        {"ticker": "KXFED", "category": "Economics", "title": "Fed decision", "tags": ["rates"]}
    )


def test_wheel_domain_rejects_sports():
    mf = MarketFilter.from_settings(["economics"], ["sports"], strict=True, mode="blocklist")
    assert not mf.allow_wheel_domain("sports")
    assert mf.allow_wheel_domain("economics")
    assert mf.allow_wheel_domain("climate")
