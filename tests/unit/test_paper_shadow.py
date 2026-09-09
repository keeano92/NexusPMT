from backend.app.store.paper_shadow import PaperShadowBook
from backend.app.workers.ranker import rank_candidates, score_candidate
from backend.app.workers.opportunity_eval import OpportunityEvaluator


def test_paper_open_close_at_live_book():
    book = PaperShadowBook.create(start_cents=1000, target_cents=10000)
    fill = book.open_position(
        ticker="FED-1",
        side="yes",
        qty=5,
        yes_bid=0.40,
        yes_ask=0.44,
        exchange_index=0,
    )
    assert fill is not None
    assert fill.price_cents == 44  # paid ask
    assert book.cash_cents == 1000 - 44 * 5
    book.update_mark("FED-1", 0.55)
    close = book.close_position(
        ticker="FED-1",
        yes_bid=0.54,
        yes_ask=0.56,
        status="take_profit",
        reason="test",
    )
    assert close is not None
    assert close.pnl_cents == 54 * 5 - 44 * 5
    assert book.wins == 1
    assert book.closed_count == 1


def test_paper_gate_requires_target_and_trades():
    book = PaperShadowBook.create(1000, 10000)
    assert book.unlocked_for_live() is False
    book.cash_cents = 10000
    book.peak_cents = 10000
    book.closed_count = 30
    book.realized_pnl_cents = 100
    assert book.unlocked_for_live() is True


def test_ranker_skips_coin_flip():
    row = score_candidate(
        {"ticker": "X", "market_prob": 0.50, "spread": 0.04, "micro_horizon": False},
        min_net_edge=0.06,
    )
    assert row["enter"] is False


def test_ranker_enters_with_net_edge():
    cands = [
        {
            "ticker": "A",
            "market_prob": 0.35,
            "spread": 0.04,
            "title": "Fed",
            "category": "Economics",
            "series_ticker": "FED",
            "micro_horizon": False,
        },
        {
            "ticker": "B",
            "market_prob": 0.50,
            "spread": 0.04,
            "title": "Coin",
            "category": "Crypto",
            "series_ticker": "COIN",
            "micro_horizon": True,
        },
    ]
    ranked = rank_candidates(cands, min_net_edge=0.06, limit=3)
    assert ranked
    assert ranked[0]["ticker"] == "A"
    assert ranked[0]["net_edge"] >= 0.06


def test_xai_disabled_skips_llm():
    OpportunityEvaluator._llm_circuit_open = False
    ev = OpportunityEvaluator(
        api_key="fake-key",
        base_url="https://api.x.ai/v1",
        model="grok-4.6",
        xai_enabled=False,
    )
    assert ev._llm_allowed() is False
