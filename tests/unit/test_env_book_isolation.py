from backend.app.config import Settings
from backend.app.state import build_app_state


def test_demo_and_prod_books_do_not_share_pnl():
    s = Settings(KALSHI_ENV="demo", KALSHI_TRADING_MODE="paper")
    state = build_app_state(s)
    demo = state.book()
    demo.paper_cash_cents = 50_000
    demo.pnl.record(50_000)
    demo.ledger.append(kind="fill", ticker="DEMO-A", status="filled", message="demo fill")

    prod = state.switch_book("production", "paper")
    assert prod.key == "production:paper"
    assert prod.paper_cash_cents == 100_000  # fresh book
    assert prod.ledger.list(limit=10) == []
    assert state.pnl.daily_pnl_cents() == 0  # new book anchor

    # Switch back — demo history preserved
    back = state.switch_book("demo", "paper")
    assert back.paper_cash_cents == 50_000
    assert any(r.get("ticker") == "DEMO-A" for r in back.ledger.list(limit=20))


def test_book_key_in_snapshot():
    state = build_app_state(Settings(KALSHI_ENV="demo", KALSHI_TRADING_MODE="live"))
    state.switch_book("demo", "live")
    snap = state.dashboard_snapshot()
    assert snap["book_key"] == "demo:live"
