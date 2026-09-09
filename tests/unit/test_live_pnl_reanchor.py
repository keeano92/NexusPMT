from backend.app.config import Settings
from backend.app.state import build_app_state
from backend.app.store.pnl_series import PnLSeriesStore


def test_live_book_does_not_seed_fake_thousand():
    s = Settings(KALSHI_ENV="production", KALSHI_TRADING_MODE="live")
    state = build_app_state(s)
    book = state.book()
    assert book.key == "production:live"
    assert book.pnl.has_anchor() is False
    assert book.pnl.daily_pnl_cents() == 0


def test_reanchor_clears_phantom_loss():
    store = PnLSeriesStore()
    store.record(100_000)  # bad fake seed
    store.record(1_062)
    assert store.daily_pnl_cents() == -98_938
    store.reset_anchor(1_062, clear_history=True)
    assert store.daily_pnl_cents() == 0
    store.record(1_162)
    assert store.daily_pnl_cents() == 100


def test_switch_to_live_then_sync_math():
    state = build_app_state(Settings(KALSHI_ENV="demo", KALSHI_TRADING_MODE="paper"))
    state.switch_book("production", "live")
    assert state.pnl.has_anchor() is False
    state.pnl.reset_anchor(1062, clear_history=True)
    state.failsafes.reset_equity_anchors(1062)
    assert state.pnl.daily_pnl_cents() == 0
    # Should not trip max daily loss
    tripped = state.failsafes.update_equity(1062)
    assert tripped is None
    assert state.failsafes.can_place_orders()
